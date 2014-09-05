"""This module provides a way to prepare and launch Sibyl tests on a binary"""


import time
import signal
import logging
from miasm2.analysis.binary import Container


class TimeoutException(Exception):
    """Exception to be called on timeouts"""
    pass


class TestLauncher(object):
    "Launch tests for a function and report matching candidates"

    def __init__(self, filename, machine, abicls, tests_cls, jitter_engine,
                 map_addr=0):

        # Logging facilities
        self.init_logger()

        # Prepare JiT engine
        self.machine = machine
        self.init_jit(jitter_engine)

        # Init and snapshot VM
        self.load_vm(filename, map_addr)
        self.save_vm()

        # Init tests
        self.init_abi(abicls)
        self.initialize_tests(tests_cls)

    def init_logger(self):
        self.logger = logging.getLogger("testlauncher")

        console_handler = logging.StreamHandler()
        log_format = "%(levelname)-5s: %(message)s"
        console_handler.setFormatter(logging.Formatter(log_format))
        self.logger.addHandler(console_handler)

        self.logger.setLevel(logging.ERROR)

    def initialize_tests(self, tests_cls):
        tests = []
        for testcls in tests_cls:
            tests.append(testcls(self.jitter, self.abi))
        self.tests = tests

    def load_vm(self, filename, map_addr):
        self.ctr = Container(filename, self.jitter.vm, map_addr)
        self.jitter.cpu.vm_init_regs()
        self.jitter.init_stack()

    def save_vm(self):
        self.vm_mem = self.jitter.vm.vm_get_all_memory()
        self.vm_regs = self.jitter.cpu.vm_get_gpreg()

    def restore_vm(self, reset_mem=True):
        # Restore memory
        if reset_mem:
            self.jitter.vm.vm_reset_memory_page_pool()
            for addr, metadata in self.vm_mem.items():
                self.jitter.vm.vm_add_memory_page(addr,
                                                  metadata["access"],
                                                  metadata["data"])

        # Restore registers
        self.jitter.cpu.vm_init_regs()
        self.jitter.cpu.vm_set_gpreg(self.vm_regs)

    @staticmethod
    def _code_sentinelle(jitter):
        jitter.run = False
        jitter.pc = 0
        return True

    @staticmethod
    def _timeout(signum, frame):
        raise TimeoutException()

    def init_jit(self, jit_engine):
        jitter = self.machine.jitter(jit_engine)
        jitter.set_breakpoint(0x1337beef, TestLauncher._code_sentinelle)
        self.jitter = jitter

        # Signal handling
        #
        # Due to Python signal handling implementation, signals aren't handled
        # nor passed to Jitted code in case of registration with signal API
        if jit_engine == "python":
            signal.signal(signal.SIGALRM, TestLauncher._timeout)
        elif jit_engine in ["llvm", "tcc"]:
            self.jitter.vm.set_alarm()

    def init_abi(self, abicls):
        ira = self.machine.ira()
        self.abi = abicls(self.jitter, ira)

    def reset_state(self, reset_mem=True):
        self.restore_vm(reset_mem)
        self.jitter.vm.vm_set_exception(0)
        self.abi.reset()

    def launch_tests(self, test, address, timeout_seconds=0):
        # Reset between functions
        good = True
        reset_mem = True
        test.reset_full()

        # Launch subtests
        for (init, check) in test.tests:
            # Reset VM
            self.reset_state(reset_mem=reset_mem)
            test.reset()

            # Prepare VM
            init(test)
            self.abi.prepare_call(ret_addr=0x1337beef)
            self.jitter.init_run(address)

            # Run code
            try:
                signal.alarm(timeout_seconds)
                self.jitter.continue_run()
            except (AssertionError, RuntimeError, ValueError,
                    KeyError, IndexError, TimeoutException) as _:
                good = False
            except Exception as error:
                self.logger.error("ERROR: %x: %s" % (address, error))
                good = False
            finally:
                signal.alarm(0)

            if not good:
                break

            if check(test) is not True:
                good = False
                break

            # Update flags
            reset_mem = test.reset_mem

        if good:
            self._possible_funcs.append(test.func)

    def run(self, address, *args, **kwargs):
        self._possible_funcs = []

        nb_tests = len(self.tests)
        self.logger.info("Launch tests (%d available functions)" % (nb_tests))
        starttime = time.time()

        for test in self.tests:
            self.launch_tests(test, address, *args, **kwargs)

        self.logger.info("Total time: %.4f seconds" % (time.time() - starttime))
        return self._possible_funcs

    def get_possible_funcs(self):
        return self._possible_funcs
    possible_funcs = property(get_possible_funcs)
