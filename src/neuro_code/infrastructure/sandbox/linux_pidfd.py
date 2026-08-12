"""Small, fail-closed Linux pidfd operations used by sandbox boundaries.

The Python runtime shipped by some distributions does not expose every pidfd
wrapper even when the running kernel and libc do.  This module keeps the
compatibility path inside trusted sandbox infrastructure: stdlib wrappers are
preferred, then libc's exported Linux interfaces are used, and no PID-based
fallback is offered.

用于沙箱边界的小型、失败关闭的 Linux pidfd 操作实现.

某些 Python 发行版即使运行中的内核和 libc 支持 pidfd,也可能没有暴露完整的
Python 包装器.本模块将兼容路径限制在受信任的 sandbox infrastructure 中:优先
使用标准库,其次使用 libc 导出的 Linux 接口,绝不回退到基于 PID 的信号.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import signal
from collections.abc import Callable
from typing import Protocol, cast


class LinuxPidfdOps(Protocol):
    """Kernel-owned process identity operations required by a sandbox."""

    def open(self, pid: int) -> int:
        """Open a close-on-exec pidfd for ``pid``."""
        ...

    def send_signal(self, pidfd: int, native_signal: int | signal.Signals) -> None:
        """Send a signal through an already-owned pidfd."""
        ...

    def probe(self) -> None:
        """Raise ``OSError`` when this runtime/kernel cannot use pidfds."""
        ...


class _CFunction(Protocol):
    argtypes: object
    restype: object

    def __call__(self, *args: object) -> int: ...


class _StdlibLinuxPidfdOps:
    def __init__(
        self,
        open_fn: Callable[[int], int],
        send_signal_fn: Callable[[int, int | signal.Signals], None],
    ) -> None:
        self._open_fn = open_fn
        self._send_signal_fn = send_signal_fn

    def open(self, pid: int) -> int:
        fd = self._open_fn(pid)
        _make_close_on_exec(fd)
        return fd

    def send_signal(self, pidfd: int, native_signal: int | signal.Signals) -> None:
        self._send_signal_fn(pidfd, native_signal)

    def probe(self) -> None:
        fd = self.open(os.getpid())
        try:
            self.send_signal(fd, 0)
        finally:
            os.close(fd)


class _LibcLinuxPidfdOps:
    def __init__(self, libc: object) -> None:
        self._libc = libc
        self._pidfd_open = cast(_CFunction | None, getattr(libc, "pidfd_open", None))
        self._pidfd_send_signal = cast(
            _CFunction | None,
            getattr(libc, "pidfd_send_signal", None),
        )
        if self._pidfd_open is None or self._pidfd_send_signal is None:
            raise OSError(errno.ENOSYS, "libc does not export the required pidfd interfaces")

        pidfd_open = self._pidfd_open
        pidfd_send_signal = self._pidfd_send_signal
        assert pidfd_open is not None
        assert pidfd_send_signal is not None
        pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
        pidfd_open.restype = ctypes.c_int
        pidfd_send_signal.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        pidfd_send_signal.restype = ctypes.c_int

    def open(self, pid: int) -> int:
        pidfd_open = self._pidfd_open
        assert pidfd_open is not None
        fd = pidfd_open(pid, 0)
        if fd < 0:
            _raise_errno("pidfd_open")
        _make_close_on_exec(fd)
        return fd

    def send_signal(self, pidfd: int, native_signal: int | signal.Signals) -> None:
        pidfd_send_signal = self._pidfd_send_signal
        assert pidfd_send_signal is not None
        result = pidfd_send_signal(pidfd, int(native_signal), None, 0)
        if result != 0:
            _raise_errno("pidfd_send_signal")

    def probe(self) -> None:
        fd = self.open(os.getpid())
        try:
            self.send_signal(fd, 0)
        finally:
            os.close(fd)


def default_linux_pidfd_ops() -> LinuxPidfdOps:
    """Return the strongest available Linux pidfd implementation.

    The returned object is still probed by the caller before an enabled
    sandbox is accepted.  Missing wrappers, libc symbols, or kernel support
    therefore fail closed at the sandbox boundary.
    """

    stdlib_open = getattr(os, "pidfd_open", None)
    stdlib_send = getattr(signal, "pidfd_send_signal", None)
    if stdlib_open is not None and stdlib_send is not None:
        return _StdlibLinuxPidfdOps(
            cast(Callable[[int], int], stdlib_open),
            cast(Callable[[int, int | signal.Signals], None], stdlib_send),
        )

    library_name = ctypes.util.find_library("c")
    if not library_name:
        raise OSError(errno.ENOSYS, "libc is unavailable for Linux pidfd fallback")
    try:
        libc = ctypes.CDLL(library_name, use_errno=True)
        return _LibcLinuxPidfdOps(libc)
    except (OSError, AttributeError) as error:
        raise OSError(errno.ENOSYS, "Linux pidfd wrappers are unavailable") from error


def _make_close_on_exec(fd: int) -> None:
    if fd < 0:
        raise OSError(errno.EBADF, "pidfd_open returned an invalid descriptor")
    try:
        os.set_inheritable(fd, False)
        inheritable = os.get_inheritable(fd)
    except BaseException:
        os.close(fd)
        raise
    if inheritable:
        os.close(fd)
        raise OSError(errno.EPERM, "pidfd could not be made close-on-exec")


def _raise_errno(operation: str) -> None:
    error_number = ctypes.get_errno() or errno.EIO
    raise OSError(error_number, f"{operation} failed: {os.strerror(error_number)}")


__all__ = ["LinuxPidfdOps", "default_linux_pidfd_ops"]
