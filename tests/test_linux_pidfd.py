from __future__ import annotations

import errno
import os
import signal
import sys
import unittest
from typing import cast
from unittest import mock

from neuro_code.infrastructure.sandbox import linux_pidfd
from neuro_code.infrastructure.sandbox.linux_pidfd import LinuxPidfdOps
from neuro_code.shared.errors import SandboxError


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux pidfd operations")
class LinuxPidfdOpsTests(unittest.TestCase):
    def test_default_operations_probe_the_running_kernel(self) -> None:
        operations = linux_pidfd.default_linux_pidfd_ops()
        operations.probe()

        pidfd = operations.open(os.getpid())
        try:
            operations.send_signal(pidfd, 0)
            self.assertFalse(os.get_inheritable(pidfd))
        finally:
            os.close(pidfd)

    def test_missing_stdlib_open_uses_libc_fallback(self) -> None:
        with mock.patch.object(linux_pidfd.os, "pidfd_open", None):
            operations = linux_pidfd.default_linux_pidfd_ops()

        self.assertIsInstance(operations, linux_pidfd._LibcLinuxPidfdOps)
        operations.probe()

    def test_missing_libc_symbols_fail_closed(self) -> None:
        fake_libc = object()
        with (
            mock.patch.object(linux_pidfd.os, "pidfd_open", None),
            mock.patch.object(linux_pidfd.ctypes.util, "find_library", return_value="c"),
            mock.patch.object(linux_pidfd.ctypes, "CDLL", return_value=fake_libc),
            self.assertRaisesRegex(OSError, "pidfd wrappers are unavailable"),
        ):
            linux_pidfd.default_linux_pidfd_ops()

    def test_pidfd_send_signal_errno_is_preserved(self) -> None:
        class FailingFunction:
            argtypes: object
            restype: object

            def __init__(self, result: int) -> None:
                self._result = result

            def __call__(self, *args: object) -> int:
                del args
                return self._result

        class FailingLibc:
            pidfd_open = FailingFunction(-1)
            pidfd_send_signal = FailingFunction(-1)

        with mock.patch.object(linux_pidfd.ctypes, "get_errno", return_value=signal.SIGPIPE):
            operations = linux_pidfd._LibcLinuxPidfdOps(
                cast(linux_pidfd.ctypes.CDLL, FailingLibc())
            )
            with self.assertRaises(OSError) as context:
                operations.open(os.getpid())
        self.assertEqual(context.exception.errno, signal.SIGPIPE)

    def test_kernel_or_native_capability_failure_is_fail_closed(self) -> None:
        class Unsupported:
            def probe(self) -> None:
                raise OSError(errno.ENOSYS, "pidfd is not supported")

        with self.assertRaisesRegex(SandboxError, "requires pidfd lifecycle ownership"):
            from neuro_code.infrastructure.sandbox.linux_local_process import (
                LinuxBubblewrapLocalProcessSandbox,
            )

            LinuxBubblewrapLocalProcessSandbox._validate_pidfd_support(
                cast(LinuxPidfdOps, Unsupported())
            )


if __name__ == "__main__":
    unittest.main()
