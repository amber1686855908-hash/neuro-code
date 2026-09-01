"""Conservative cross-platform process-owner liveness probes.

进程 owner 的保守跨平台存活探针.

The probe is intentionally observation-only.  A process is considered dead
only when the platform proves that its PID is missing or its process object is
signalled.  Access failures and unexpected wait results remain alive so a
caller never treats an unproven owner as reclaimable.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from typing import cast

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_ERROR_FILE_NOT_FOUND = 2
_ERROR_INVALID_PARAMETER = 87


def owner_is_alive(pid: int | None) -> bool:
    """Return whether a persisted process owner is conservatively alive."""

    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_owner_is_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False
    return True


def _windows_owner_is_alive(pid: int) -> bool:
    """Observe a Windows PID through its signalled process object.

    ``ERROR_FILE_NOT_FOUND`` and ``ERROR_INVALID_PARAMETER`` prove that the
    PID cannot be opened as a process.  ``ERROR_ACCESS_DENIED`` and all
    unexpected API failures stay conservatively alive.
    """

    loader = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if loader is None or get_last_error is None:  # pragma: no cover - Windows-only runtime boundary
        return True
    read_last_error = cast(Callable[[], int], get_last_error)
    try:
        kernel32 = loader("kernel32.dll", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait_for_single_object.restype = ctypes.c_uint32
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int32
        handle = open_process(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
            False,
            pid,
        )
    except (AttributeError, OSError):  # pragma: no cover - Windows-only fallback
        return True
    if not handle:
        error = read_last_error()
        return error not in {_ERROR_FILE_NOT_FOUND, _ERROR_INVALID_PARAMETER}
    try:
        result = cast(int, wait_for_single_object(handle, 0))
        return result != _WAIT_OBJECT_0
    finally:
        close_handle(handle)


__all__ = ["owner_is_alive"]
