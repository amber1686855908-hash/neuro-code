from __future__ import annotations

import contextlib
import ctypes
import os
from typing import NoReturn, Protocol, Self, cast

_DWORD_MAX = (1 << 32) - 1
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _WindowsJobApi(Protocol):
    def create_job_object(self) -> int | None: ...

    def set_information_job_object(
        self,
        job_handle: int,
        information_class: int,
        information: _ExtendedLimitInformation,
    ) -> bool: ...

    def query_information_job_object(
        self,
        job_handle: int,
        information_class: int,
        information: _BasicAccountingInformation,
    ) -> bool: ...

    def terminate_job_object(self, job_handle: int, exit_code: int) -> bool: ...

    def close_handle(self, handle: int) -> bool: ...

    def get_last_error(self) -> int: ...


class _CFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> object: ...


def _load_function(
    library: object,
    name: str,
    argtypes: list[object],
    restype: object,
) -> _CFunction:
    function = cast(_CFunction, getattr(library, name))
    function.argtypes = argtypes
    function.restype = restype
    return function


class _NativeWindowsJobApi:
    """Small, typed facade over the kernel32 calls used by ``WindowsJobObject``."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are only available on Windows")

        loader = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if loader is None or get_last_error is None:  # pragma: no cover - defensive on Windows
            raise OSError("this Python runtime does not expose the Win32 ctypes API")

        kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
        self._get_last_error = cast(_CFunction, get_last_error)
        self._create_job_object = _load_function(
            kernel32,
            "CreateJobObjectW",
            [ctypes.c_void_p, ctypes.c_wchar_p],
            ctypes.c_void_p,
        )
        self._set_information_job_object = _load_function(
            kernel32,
            "SetInformationJobObject",
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self._query_information_job_object = _load_function(
            kernel32,
            "QueryInformationJobObject",
            [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
        )
        self._terminate_job_object = _load_function(
            kernel32,
            "TerminateJobObject",
            [ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self._close_handle = _load_function(
            kernel32,
            "CloseHandle",
            [ctypes.c_void_p],
            ctypes.c_int32,
        )

    def create_job_object(self) -> int | None:
        return cast(int | None, self._create_job_object(None, None))

    def set_information_job_object(
        self,
        job_handle: int,
        information_class: int,
        information: _ExtendedLimitInformation,
    ) -> bool:
        return bool(
            self._set_information_job_object(
                job_handle,
                information_class,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
        )

    def query_information_job_object(
        self,
        job_handle: int,
        information_class: int,
        information: _BasicAccountingInformation,
    ) -> bool:
        return bool(
            self._query_information_job_object(
                job_handle,
                information_class,
                ctypes.byref(information),
                ctypes.sizeof(information),
                None,
            )
        )

    def terminate_job_object(self, job_handle: int, exit_code: int) -> bool:
        return bool(self._terminate_job_object(job_handle, exit_code))

    def close_handle(self, handle: int) -> bool:
        return bool(self._close_handle(handle))

    def get_last_error(self) -> int:
        return cast(int, self._get_last_error())


def _raise_api_error(api: _WindowsJobApi, operation: str) -> NoReturn:
    error_code = api.get_last_error()
    raise OSError(error_code, f"{operation} failed with Windows error {error_code}")


class WindowsJobObject:
    """Own a configured Win32 Job Object and the processes assigned to it."""

    def __init__(self, handle: int, api: _WindowsJobApi) -> None:
        self._handle: int | None = handle
        self._api = api

    @classmethod
    def create(cls, *, api: _WindowsJobApi | None = None) -> Self:
        """Create a Job Object whose remaining processes die when it is closed."""

        job_api = _NativeWindowsJobApi() if api is None else api
        handle = job_api.create_job_object()
        if handle is None or handle == 0:
            _raise_api_error(job_api, "CreateJobObjectW")

        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            configured = job_api.set_information_job_object(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                limits,
            )
            if not configured:
                _raise_api_error(job_api, "SetInformationJobObject")
        except BaseException:
            with contextlib.suppress(BaseException):
                job_api.close_handle(handle)
            raise
        return cls(handle, job_api)

    @property
    def process_creation_handle(self) -> int:
        """Borrow the Job handle for a process-creation attribute list.

        The caller must keep this object alive for the complete ``CreateProcessW``
        call and must never close the borrowed numeric handle.
        """

        return self._open_handle()

    @property
    def active_processes(self) -> int:
        """Return the number of processes that are currently active in the job."""

        accounting = _BasicAccountingInformation()
        if not self._api.query_information_job_object(
            self._open_handle(),
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            accounting,
        ):
            _raise_api_error(self._api, "QueryInformationJobObject")
        return int(accounting.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> None:
        """Terminate every process assigned to the job."""

        if exit_code < 0 or exit_code > _DWORD_MAX:
            raise ValueError("exit_code must be an unsigned 32-bit integer")
        if not self._api.terminate_job_object(self._open_handle(), exit_code):
            _raise_api_error(self._api, "TerminateJobObject")

    def close(self) -> None:
        """Close the owned Job Object handle exactly once."""

        handle = self._handle
        if handle is None:
            return
        # Clear ownership before calling Win32 so a failing CloseHandle cannot
        # lead to a second close of the same numeric handle.
        self._handle = None
        if not self._api.close_handle(handle):
            _raise_api_error(self._api, "CloseHandle(job)")

    def _open_handle(self) -> int:
        if self._handle is None:
            raise RuntimeError("Windows Job Object is closed")
        return self._handle

    def __enter__(self) -> Self:
        self._open_handle()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Destructors cannot safely report errors, but they should still make a
        # best effort to honor kill-on-close when a caller abandons the object.
        with contextlib.suppress(BaseException):
            self.close()
