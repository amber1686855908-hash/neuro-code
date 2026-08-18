"""Trusted Windows runner and controller-owned named-pipe transport.

This module is intentionally a small native boundary.  The controller starts
the module under a dedicated W2 account with ``CreateProcessWithLogonW``.  The
runner then creates the final child from its own token, applies the persisted
synthetic write SID with the W1 restricted-token primitive, and keeps the
final process in a kill-on-close Job Object.

The module is imported on all platforms for type discovery, but Win32 objects
are constructed lazily and fail closed away from Windows.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import enum
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, cast

from neuro_code.infrastructure.sandbox.windows_job import WindowsJobObject
from neuro_code.infrastructure.sandbox.windows_native_runtime_protocol import (
    PROTOCOL_VERSION,
    RuntimeChannel,
    RuntimeFrame,
    RuntimeFrameDecoder,
    RuntimeFrameType,
    decode_json,
    encode_frame,
    encode_json,
    validate_channel_frame,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_security_token import (
    WindowsRestrictedToken,
    WindowsRestrictedTokenRequest,
    WindowsTokenInspection,
    inspect_windows_process_token,
)
from neuro_code.shared.errors import SandboxError

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_CREATE_ALWAYS = 2
_PIPE_ACCESS_INBOUND = 0x00000001
_PIPE_ACCESS_OUTBOUND = 0x00000002
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_INSUFFICIENT_BUFFER = 122
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_HANDLE_FLAG_INHERIT = 0x00000001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_INFINITE = 0xFFFFFFFF
# Do not ask CreateProcessWithLogonW to synchronously load the account
# profile.  W3 derives private HOME/TMP from the final token and creates those
# directories inside the runner; loading a fresh W2 profile here can block the
# controller before the named-pipe protocol starts.
_LOGON_FLAGS = 0
_TOKEN_USER = 1
_TOKEN_LOGON_SID = 28
_TOKEN_QUERY = 0x0008
_DACL_SECURITY_INFORMATION = 0x00000004
_PROFILE_USERNAMES = frozenset({"NeuroSandboxOffline", "NeuroSandboxOnline"})
_WORLD_SID = "S-1-1-0"
_HRESULT_FILE_NOT_FOUND = -2_147_024_894  # 0x80070002
_HRESULT_PATH_NOT_FOUND = -2_147_024_893  # 0x80070003
_ERROR_ALREADY_EXISTS = 183
_DESKTOP_READOBJECTS = 0x0001
_DESKTOP_CREATEWINDOW = 0x0002
_DESKTOP_CREATEMENU = 0x0004
_DESKTOP_HOOKCONTROL = 0x0008
_DESKTOP_JOURNALRECORD = 0x0010
_DESKTOP_JOURNALPLAYBACK = 0x0020
_DESKTOP_ENUMERATE = 0x0040
_DESKTOP_WRITEOBJECTS = 0x0080
_DESKTOP_SWITCHDESKTOP = 0x0100
_DESKTOP_READ_CONTROL = 0x00020000
_DESKTOP_WRITE_DAC = 0x00040000
_DESKTOP_WRITE_OWNER = 0x00080000
_DESKTOP_ALL_ACCESS = (
    _DESKTOP_READOBJECTS
    | _DESKTOP_CREATEWINDOW
    | _DESKTOP_CREATEMENU
    | _DESKTOP_HOOKCONTROL
    | _DESKTOP_JOURNALRECORD
    | _DESKTOP_JOURNALPLAYBACK
    | _DESKTOP_ENUMERATE
    | _DESKTOP_WRITEOBJECTS
    | _DESKTOP_SWITCHDESKTOP
    | _DESKTOP_READ_CONTROL
    | _DESKTOP_WRITE_DAC
    | _DESKTOP_WRITE_OWNER
)
_FILE_READ_DATA = 0x00000001
_FILE_WRITE_DATA = 0x00000002
_FILE_APPEND_DATA = 0x00000004
_FILE_READ_EA = 0x00000008
_FILE_WRITE_EA = 0x00000010
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_WRITE_ATTRIBUTES = 0x00000100
_READ_CONTROL = 0x00020000
_SYNCHRONIZE = 0x00100000
_FILE_CREATE_PIPE_INSTANCE = _FILE_APPEND_DATA

# Named-pipe client rights are deliberately expressed as specific rights.
# ``FILE_GENERIC_WRITE`` includes FILE_APPEND_DATA, whose bit is also
# FILE_CREATE_PIPE_INSTANCE for named pipes.  The event writer therefore
# keeps the proven generic-write components except that authority, while the
# control reader mirrors the specific generic-read components.
_PIPE_CONTROL_READ_ACCESS = (
    _FILE_READ_DATA | _FILE_READ_EA | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE
)
_PIPE_EVENT_WRITE_ACCESS = (
    _FILE_WRITE_DATA
    | _FILE_WRITE_EA
    | _FILE_WRITE_ATTRIBUTES
    | _FILE_READ_ATTRIBUTES
    | _READ_CONTROL
    | _SYNCHRONIZE
)
_COORD_MAX = (1 << 15) - 1


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_int16), ("Y", ctypes.c_int16)]


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


def _last_error() -> int:
    getter = cast(object, getattr(ctypes, "get_last_error", lambda: 0))
    return cast(int, getter())  # type: ignore[operator]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int32),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.c_uint32),
    ]


class _TokenGroupsOne(ctypes.Structure):
    """Header plus the first entry returned for ``TokenLogonSid``.

    ``GetTokenInformation(TokenLogonSid)`` returns a ``TOKEN_GROUPS``
    structure, not a bare ``SID_AND_ATTRIBUTES`` value.  Keeping the header
    in the ctypes layout is important on 64-bit Windows because the first SID
    pointer is aligned after the DWORD group count.
    """

    _fields_ = [
        ("GroupCount", ctypes.c_uint32),
        ("Groups", _SidAndAttributes * 1),
    ]


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# FOLDERID_Profile.  Resolving the known folder from the final token works
# when CreateProcessWithLogonW was intentionally started without
# LOGON_WITH_PROFILE; GetUserProfileDirectoryW requires a loaded profile and
# would otherwise fail before the child boundary is even created.
_FOLDERID_PROFILE = _Guid(
    0x5E6C858F,
    0x0E22,
    0x4760,
    (ctypes.c_ubyte * 8)(0x9A, 0xFE, 0xEA, 0x33, 0x17, 0xB6, 0x71, 0x73),
)


@dataclass(frozen=True, slots=True)
class RunnerLaunch:
    """Controller-side handle for the trusted runner process."""

    process_handle: int
    process_id: int
    thread_handle: int | None = None


class _WindowsNativeDesktopMode(enum.StrEnum):
    """Trusted-runner-only desktop selection used by native diagnostics."""

    PRIVATE_DESKTOP = "private"
    INHERIT_DESKTOP = "inherit"


class _NativePipeApi:
    """Synchronous Win32 byte-pipe calls shared by controller and runner."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows native runtime is only available on Windows")
        loader = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if loader is None or get_last_error is None:  # pragma: no cover - Windows only
            raise OSError("this Python runtime does not expose the Win32 ctypes API")
        kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
        self._get_last_error = cast(_CFunction, get_last_error)
        self.create_named_pipe = _load_function(
            kernel32,
            "CreateNamedPipeW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(_SecurityAttributes),
            ],
            ctypes.c_void_p,
        )
        self.connect_named_pipe = _load_function(
            kernel32,
            "ConnectNamedPipe",
            [ctypes.c_void_p, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self.create_file = _load_function(
            kernel32,
            "CreateFileW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(_SecurityAttributes),
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ],
            ctypes.c_void_p,
        )
        self.wait_named_pipe = _load_function(
            kernel32,
            "WaitNamedPipeW",
            [ctypes.c_wchar_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.read_file = _load_function(
            kernel32,
            "ReadFile",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self.write_file = _load_function(
            kernel32,
            "WriteFile",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self.close_handle = _load_function(
            kernel32,
            "CloseHandle",
            [ctypes.c_void_p],
            ctypes.c_int32,
        )
        self.set_handle_information = _load_function(
            kernel32,
            "SetHandleInformation",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.wait_for_single_object = _load_function(
            kernel32,
            "WaitForSingleObject",
            [ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_uint32,
        )
        self.get_exit_code_process = _load_function(
            kernel32,
            "GetExitCodeProcess",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int32,
        )
        self._last_read_error: int | None = None

    def last_error(self) -> int:
        return cast(int, self._get_last_error())

    def close(self, handle: int) -> None:
        if handle and not self.close_handle(handle):
            raise OSError(self.last_error(), "CloseHandle failed")

    def read(self, handle: int, count: int = 65_536) -> bytes:
        buffer = ctypes.create_string_buffer(count)
        returned = ctypes.c_uint32()
        if not self.read_file(handle, buffer, count, ctypes.byref(returned), None):
            error = self.last_error()
            self._last_read_error = error
            if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
                return b""
            raise OSError(error, f"ReadFile failed with Windows error {error}")
        self._last_read_error = None
        return buffer.raw[: returned.value]

    @property
    def last_read_error(self) -> int | None:
        return self._last_read_error

    def observe_process(
        self,
        handle: int,
        *,
        active_state: str,
        exited_state: str,
    ) -> dict[str, object]:
        """Return bounded Win32 process state without exposing process data."""

        result = self.wait_for_single_object(handle, 0)
        if result == _WAIT_TIMEOUT:
            return {"state": active_state}
        if result != _WAIT_OBJECT_0:
            return {"state": "WAIT_FAILED", "wait_error": cast(int, result)}
        exit_code = ctypes.c_uint32()
        if not self.get_exit_code_process(handle, ctypes.byref(exit_code)):
            return {"state": "WAIT_FAILED", "wait_error": self.last_error()}
        return {"state": exited_state, "exit_code": int(exit_code.value)}

    def wait_process(
        self,
        handle: int,
        *,
        timeout_seconds: float,
        active_state: str,
        exited_state: str,
    ) -> dict[str, object]:
        """Wait for a trusted process and report its bounded final state."""

        timeout_ms = min(0xFFFFFFFF, max(0, int(timeout_seconds * 1000)))
        result = self.wait_for_single_object(handle, timeout_ms)
        if result == _WAIT_TIMEOUT:
            return {"state": active_state}
        if result != _WAIT_OBJECT_0:
            return {"state": "WAIT_FAILED", "wait_error": cast(int, result)}
        exit_code = ctypes.c_uint32()
        if not self.get_exit_code_process(handle, ctypes.byref(exit_code)):
            return {"state": "WAIT_FAILED", "wait_error": self.last_error()}
        return {"state": exited_state, "exit_code": int(exit_code.value)}

    def write(self, handle: int, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + 65_536]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = ctypes.c_uint32()
            if not self.write_file(handle, buffer, len(chunk), ctypes.byref(written), None):
                error = self.last_error()
                raise OSError(error, f"WriteFile failed with Windows error {error}")
            if written.value == 0:
                raise OSError("WriteFile returned zero bytes")
            offset += written.value


class _WindowsNamedPipeDirection(enum.StrEnum):
    """Access direction owned by one endpoint of the runtime transport."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class WindowsNamedPipe:
    """Common lifetime operations for one-direction synchronous pipe handle."""

    def __init__(self, handle: int, *, api: _NativePipeApi | None = None) -> None:
        if handle <= 0:
            raise ValueError("pipe handle must be positive")
        self._api = _NativePipeApi() if api is None else api
        self._handle: int | None = handle

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise RuntimeError("pipe is closed")
        return self._handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(BaseException):
                self._api.close(handle)

    def __enter__(self) -> WindowsNamedPipe:
        _ = self.handle
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class WindowsNamedPipeReader(WindowsNamedPipe):
    """Controller/runner endpoint that can only receive event/control bytes."""

    def read(self) -> bytes:
        return self._api.read(self.handle)

    @property
    def last_read_error(self) -> int | None:
        return self._api.last_read_error

    def observe_process(
        self,
        handle: int,
        *,
        active_state: str,
        exited_state: str,
    ) -> dict[str, object]:
        return self._api.observe_process(
            handle,
            active_state=active_state,
            exited_state=exited_state,
        )

    def wait_process(
        self,
        handle: int,
        *,
        timeout_seconds: float,
        active_state: str,
        exited_state: str,
    ) -> dict[str, object]:
        """Wait for the trusted runner after the controller closes control."""

        if timeout_seconds < 0:
            raise ValueError("process wait timeout must not be negative")
        return self._api.wait_process(
            handle,
            timeout_seconds=timeout_seconds,
            active_state=active_state,
            exited_state=exited_state,
        )

    def read_for_runner(
        self,
        runner_handle: int,
        *,
        timeout_seconds: float = 30.0,
    ) -> bytes:
        """Read one chunk while bounding a stalled trusted runner."""

        if timeout_seconds <= 0:
            raise ValueError("named-pipe read timeout must be positive")
        result: list[bytes] = []
        failure: list[BaseException] = []
        completed = threading.Event()

        def read() -> None:
            try:
                result.append(self.read())
            except BaseException as error:
                failure.append(error)
            finally:
                completed.set()

        thread = threading.Thread(target=read, name="neuro-code-windows-pipe-read", daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout_seconds
        while not completed.wait(0.05):
            if self._api.wait_for_single_object(runner_handle, 0) == _WAIT_OBJECT_0:
                self.close()
                thread.join(timeout=1.0)
                raise SandboxError("trusted Windows runner exited during named-pipe read")
            if time.monotonic() >= deadline:
                self.close()
                thread.join(timeout=1.0)
                raise SandboxError("trusted Windows runner produced no frame before timeout")
        if failure:
            raise failure[0]
        return result[0] if result else b""


class WindowsNamedPipeWriter(WindowsNamedPipe):
    """Controller/runner endpoint that can only send complete frames."""

    def __init__(self, handle: int, *, api: _NativePipeApi | None = None) -> None:
        super().__init__(handle, api=api)
        self._write_lock = threading.Lock()

    def write(self, payload: bytes) -> None:
        with self._write_lock:
            self._api.write(self.handle, payload)


def _security_descriptor(
    sids: Sequence[str],
    *,
    inherit_to_children: bool = False,
    access_masks: Mapping[str, int] | None = None,
) -> tuple[_SecurityAttributes, int]:
    """Build an exact, non-inheritable named-pipe DACL for the two peers."""

    if not sids or any(not sid or ";" in sid for sid in sids):
        raise ValueError("named-pipe peer SIDs are invalid")
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:  # pragma: no cover - Windows only
        raise OSError("Win32 API unavailable")
    advapi32 = cast(object, loader("advapi32.dll", use_last_error=True))
    convert = _load_function(
        advapi32,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ],
        ctypes.c_int32,
    )
    entries = []
    for sid in dict.fromkeys(sids):
        if access_masks is None:
            ace = "(A;OICI;GA;;;{sid})" if inherit_to_children else "(A;;GA;;;{sid})"
        else:
            mask = access_masks.get(sid)
            if mask is None:
                raise ValueError("named-pipe access mask is missing a peer SID")
            # Keep the ACE in the same specific-rights vocabulary as the
            # CreateFileW request.  In particular, never spell this as GW:
            # FILE_GENERIC_WRITE contains FILE_APPEND_DATA, which is the
            # FILE_CREATE_PIPE_INSTANCE bit for named pipes.
            rights = f"0x{mask:X}"
            ace = f"(A;;{rights};;;{sid})"
        entries.append(ace.format(sid=sid))
    sddl = "D:P" + "".join(entries)
    descriptor = ctypes.c_void_p()
    descriptor_size = ctypes.c_uint32()
    if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
        error = _last_error()
        raise OSError(error, "ConvertStringSecurityDescriptorToSecurityDescriptorW failed")
    attributes = _SecurityAttributes()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.lpSecurityDescriptor = descriptor.value
    attributes.bInheritHandle = False
    return attributes, int(descriptor.value or 0)


def _free_security_descriptor(pointer: int) -> None:
    if not pointer:
        return
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return
    kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
    local_free = _load_function(kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    local_free(pointer)


def current_user_sid() -> str:
    """Return the controller's token SID for the pipe DACL."""

    if os.name != "nt":
        raise OSError("current_user_sid is only available on Windows")
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:  # pragma: no cover - Windows only
        raise OSError("Win32 API unavailable")
    advapi32 = cast(object, loader("advapi32.dll", use_last_error=True))
    kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
    get_current_process = _load_function(kernel32, "GetCurrentProcess", [], ctypes.c_void_p)
    close_handle = _load_function(kernel32, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32)
    open_token = _load_function(
        advapi32,
        "OpenProcessToken",
        [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.c_int32,
    )
    get_token_information = _load_function(
        advapi32,
        "GetTokenInformation",
        [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ],
        ctypes.c_int32,
    )
    convert_sid = _load_function(
        advapi32,
        "ConvertSidToStringSidW",
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.c_int32,
    )
    token = ctypes.c_void_p()
    if not open_token(get_current_process(), _TOKEN_QUERY, ctypes.byref(token)) or not token.value:
        raise OSError(_last_error(), "OpenProcessToken failed")
    try:
        required = ctypes.c_uint32()
        get_token_information(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise OSError(_last_error(), "GetTokenInformation failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(token, _TOKEN_USER, buffer, required, ctypes.byref(required)):
            raise OSError(_last_error(), "GetTokenInformation failed")
        sid_pointer = ctypes.c_void_p.from_buffer(buffer)
        sid_string = ctypes.c_void_p()
        if not convert_sid(sid_pointer, ctypes.byref(sid_string)) or not sid_string.value:
            raise OSError(_last_error(), "ConvertSidToStringSidW failed")
        try:
            return ctypes.wstring_at(sid_string.value)
        finally:
            _free_security_descriptor(int(sid_string.value))
    finally:
        close_handle(token)


def current_logon_sid() -> str:
    """Return the per-logon SID used by the runner's window objects."""

    if os.name != "nt":
        raise OSError("current_logon_sid is only available on Windows")
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:  # pragma: no cover - Windows only
        raise OSError("Win32 API unavailable")
    advapi32 = cast(object, loader("advapi32.dll", use_last_error=True))
    kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
    get_current_process = _load_function(kernel32, "GetCurrentProcess", [], ctypes.c_void_p)
    close_handle = _load_function(kernel32, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32)
    open_token = _load_function(
        advapi32,
        "OpenProcessToken",
        [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.c_int32,
    )
    get_token_information = _load_function(
        advapi32,
        "GetTokenInformation",
        [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ],
        ctypes.c_int32,
    )
    convert_sid = _load_function(
        advapi32,
        "ConvertSidToStringSidW",
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.c_int32,
    )
    token = ctypes.c_void_p()
    if not open_token(get_current_process(), _TOKEN_QUERY, ctypes.byref(token)) or not token.value:
        raise OSError(_last_error(), "OpenProcessToken failed")
    try:
        required = ctypes.c_uint32()
        get_token_information(token, _TOKEN_LOGON_SID, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise OSError(_last_error(), "GetTokenInformation(TokenLogonSid) failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token, _TOKEN_LOGON_SID, buffer, required, ctypes.byref(required)
        ):
            raise OSError(_last_error(), "GetTokenInformation(TokenLogonSid) failed")
        groups = _TokenGroupsOne.from_buffer(buffer)
        if groups.GroupCount < 1 or not groups.Groups[0].Sid:
            raise OSError(_last_error(), "GetTokenInformation(TokenLogonSid) returned no SID")
        sid_pointer = groups.Groups[0].Sid
        sid_string = ctypes.c_void_p()
        if not convert_sid(sid_pointer, ctypes.byref(sid_string)) or not sid_string.value:
            raise OSError(_last_error(), "ConvertSidToStringSidW failed")
        try:
            return ctypes.wstring_at(sid_string.value)
        finally:
            _free_security_descriptor(int(sid_string.value))
    finally:
        close_handle(token)


class WindowsNamedPipeServer:
    """Controller-created, random local named pipe with an exact peer DACL."""

    def __init__(
        self,
        *,
        peer_sids: Sequence[str],
        direction: _WindowsNamedPipeDirection,
    ) -> None:
        if not isinstance(direction, _WindowsNamedPipeDirection):
            raise ValueError("named-pipe direction is invalid")
        peers = tuple(peer_sids)
        if len(peers) != 2:
            raise ValueError("named-pipe server requires controller and runner SIDs")
        controller_sid, runner_sid = peers
        self.name = rf"\\.\pipe\neuro-code-{secrets.token_urlsafe(32)}"
        self._api = _NativePipeApi()
        server_access = (
            _PIPE_ACCESS_OUTBOUND
            if direction is _WindowsNamedPipeDirection.OUTBOUND
            else _PIPE_ACCESS_INBOUND
        )
        controller_mask, runner_mask = (
            (_PIPE_EVENT_WRITE_ACCESS, _PIPE_CONTROL_READ_ACCESS)
            if direction is _WindowsNamedPipeDirection.OUTBOUND
            else (_PIPE_CONTROL_READ_ACCESS, _PIPE_EVENT_WRITE_ACCESS)
        )
        attributes, descriptor = _security_descriptor(
            (controller_sid, runner_sid),
            access_masks={controller_sid: controller_mask, runner_sid: runner_mask},
        )
        self._descriptor = descriptor
        try:
            handle = self._api.create_named_pipe(
                self.name,
                server_access,
                _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                1,
                65_536,
                65_536,
                0,
                ctypes.byref(attributes),
            )
        finally:
            _free_security_descriptor(descriptor)
        if handle is None or handle == 0 or handle == _INVALID_HANDLE_VALUE:
            raise OSError(self._api.last_error(), "CreateNamedPipeW failed")
        self._direction = direction
        self._pipe: WindowsNamedPipe | None = WindowsNamedPipe(
            int(cast(int, handle)), api=self._api
        )

    def accept(self) -> WindowsNamedPipeReader | WindowsNamedPipeWriter:
        pipe = self._pipe
        if pipe is None:
            raise RuntimeError("named-pipe server is closed")
        if not self._api.connect_named_pipe(pipe.handle, None):
            error = self._api.last_error()
            if error != _ERROR_PIPE_CONNECTED:
                self.close()
                raise OSError(error, "ConnectNamedPipe failed")
        handle, api = pipe.handle, pipe._api
        pipe._handle = None
        self._pipe = None
        if self._direction is _WindowsNamedPipeDirection.INBOUND:
            return WindowsNamedPipeReader(handle, api=api)
        return WindowsNamedPipeWriter(handle, api=api)

    def accept_for_runner(
        self,
        runner_handle: int,
        *,
        timeout_seconds: float = 30.0,
    ) -> WindowsNamedPipeReader | WindowsNamedPipeWriter:
        """Accept while detecting a dead or non-connecting trusted runner.

        ``ConnectNamedPipe`` is synchronous. Running it behind a short
        monitor keeps a runner import/logon failure from hanging the
        controller forever while retaining the exact named-pipe DACL.
        """

        if timeout_seconds <= 0:
            raise ValueError("named-pipe connection timeout must be positive")
        result: list[WindowsNamedPipeReader | WindowsNamedPipeWriter] = []
        failure: list[BaseException] = []
        completed = threading.Event()

        def connect() -> None:
            try:
                result.append(self.accept())
            except BaseException as error:
                failure.append(error)
            finally:
                completed.set()

        thread = threading.Thread(
            target=connect,
            name="neuro-code-windows-pipe-accept",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + timeout_seconds
        while not completed.wait(0.05):
            if self._api.wait_for_single_object(runner_handle, 0) == _WAIT_OBJECT_0:
                state = self._api.observe_process(
                    runner_handle,
                    active_state="RUNNER_STILL_ACTIVE",
                    exited_state="RUNNER_EXITED",
                )
                self.close()
                thread.join(timeout=1.0)
                raise SandboxError(
                    "trusted Windows runner exited before named-pipe connection " + repr(state)
                )
            if time.monotonic() >= deadline:
                self.close()
                thread.join(timeout=1.0)
                raise SandboxError("trusted Windows runner did not connect to its named pipe")
        if failure:
            raise failure[0]
        if not result:
            raise SandboxError("trusted Windows named-pipe accept returned no connection")
        return result[0]

    def close(self) -> None:
        pipe, self._pipe = getattr(self, "_pipe", None), None
        if pipe is not None:
            pipe.close()


class WindowsNamedPipeClient(WindowsNamedPipe):
    @classmethod
    def _connect(cls, name: str, *, direction: _WindowsNamedPipeDirection) -> WindowsNamedPipe:
        api = _NativePipeApi()
        if not api.wait_named_pipe(name, 5_000):
            error = api.last_error()
            raise OSError(error, f"WaitNamedPipeW failed with Windows error {error}")
        desired_access = (
            _PIPE_CONTROL_READ_ACCESS
            if direction is _WindowsNamedPipeDirection.INBOUND
            else _PIPE_EVENT_WRITE_ACCESS
        )
        handle = api.create_file(
            name,
            desired_access,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle is None or handle == 0 or handle == _INVALID_HANDLE_VALUE:
            raise OSError(api.last_error(), "CreateFileW(named pipe) failed")
        if direction is _WindowsNamedPipeDirection.INBOUND:
            return WindowsNamedPipeReader(int(cast(int, handle)), api=api)
        return WindowsNamedPipeWriter(int(cast(int, handle)), api=api)

    @classmethod
    def connect_reader(cls, name: str) -> WindowsNamedPipeReader:
        endpoint = cls._connect(name, direction=_WindowsNamedPipeDirection.INBOUND)
        assert isinstance(endpoint, WindowsNamedPipeReader)
        return endpoint

    @classmethod
    def connect_writer(cls, name: str) -> WindowsNamedPipeWriter:
        endpoint = cls._connect(name, direction=_WindowsNamedPipeDirection.OUTBOUND)
        assert isinstance(endpoint, WindowsNamedPipeWriter)
        return endpoint


def _environment_block(environment: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    values = dict(environment)
    if any("=" in name or "\x00" in name or "\x00" in value for name, value in values.items()):
        raise ValueError("Windows runner environment contains an invalid value")
    folded_names = [name.casefold() for name in values]
    if len(set(folded_names)) != len(folded_names):
        raise ValueError("Windows runner environment contains duplicate names")
    block = (
        "\0".join(
            f"{name}={value}"
            for name, value in sorted(values.items(), key=lambda item: item[0].casefold())
        )
        + "\0\0"
    )
    return ctypes.create_unicode_buffer(block)


def launch_runner(
    *,
    username: str,
    password: str,
    control_pipe_name: str,
    event_pipe_name: str,
    environment: Mapping[str, str],
) -> RunnerLaunch:
    """Launch this trusted module under a selected dedicated local account."""

    if os.name != "nt":
        raise OSError("CreateProcessWithLogonW is only available on Windows")
    if not username or not password or "\x00" in username or "\x00" in password:
        raise ValueError("runner credentials are invalid")
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:  # pragma: no cover - Windows only
        raise OSError("Win32 API unavailable")
    advapi32 = cast(object, loader("advapi32.dll", use_last_error=True))
    create = _load_function(
        advapi32,
        "CreateProcessWithLogonW",
        [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(_StartupInfoW),
            ctypes.POINTER(_ProcessInformation),
        ],
        ctypes.c_int32,
    )
    executable = str(Path(sys.executable).resolve())
    command = subprocess.list2cmdline(
        [
            executable,
            "-I",
            "-m",
            "neuro_code.infrastructure.sandbox.windows_native_runner",
            "--control-pipe",
            control_pipe_name,
            "--event-pipe",
            event_pipe_name,
        ]
    )
    mutable_command = ctypes.create_unicode_buffer(command)
    environment_block = _environment_block(environment)
    trusted_current_directory = str(Path(__file__).resolve().parent)
    startup = _StartupInfoW()
    startup.cb = ctypes.sizeof(startup)
    process = _ProcessInformation()
    created = create(
        username,
        ".",
        password,
        _LOGON_FLAGS,
        executable,
        mutable_command,
        _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
        ctypes.cast(environment_block, ctypes.c_void_p),
        trusted_current_directory,
        ctypes.byref(startup),
        ctypes.byref(process),
    )
    if not created or not process.hProcess or not process.hThread or not process.dwProcessId:
        error = _last_error()
        raise OSError(error, f"CreateProcessWithLogonW failed with Windows error {error}")
    api = _NativePipeApi()
    with contextlib.suppress(BaseException):
        api.close(int(process.hThread))
    return RunnerLaunch(int(process.hProcess), int(process.dwProcessId))


def close_runner_process(handle: int) -> None:
    """Close a controller-owned runner process handle after Exit/crash."""

    if os.name != "nt":
        return
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:  # pragma: no cover - Windows only
        return
    kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
    close = _load_function(kernel32, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32)
    if not close(handle):
        raise OSError(_last_error(), "CloseHandle(runner) failed")


def observe_process_id(pid: int) -> dict[str, object]:
    """Observe a child PID for diagnostics without opening a control handle."""

    if os.name != "nt":
        return {"state": "WAIT_FAILED", "wait_error": "NOT_WINDOWS"}
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:  # pragma: no cover - Windows only
        return {"state": "WAIT_FAILED", "wait_error": "WIN32_UNAVAILABLE"}
    kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
    open_process = _load_function(
        kernel32,
        "OpenProcess",
        [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32],
        ctypes.c_void_p,
    )
    wait = _load_function(
        kernel32,
        "WaitForSingleObject",
        [ctypes.c_void_p, ctypes.c_uint32],
        ctypes.c_uint32,
    )
    get_exit = _load_function(
        kernel32,
        "GetExitCodeProcess",
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
        ctypes.c_int32,
    )
    close = _load_function(kernel32, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32)
    handle = open_process(0x00100000 | 0x1000, False, pid)
    if not handle:
        error = _last_error()
        if error in {2, 87}:  # ERROR_FILE_NOT_FOUND / ERROR_INVALID_PARAMETER
            return {"state": "FINAL_CHILD_EXITED", "open_error": error}
        return {"state": "WAIT_FAILED", "wait_error": error}
    try:
        result = wait(handle, 0)
        if result == _WAIT_TIMEOUT:
            return {"state": "FINAL_CHILD_STILL_ACTIVE"}
        if result != _WAIT_OBJECT_0:
            return {"state": "WAIT_FAILED", "wait_error": cast(int, result)}
        exit_code = ctypes.c_uint32()
        if not get_exit(handle, ctypes.byref(exit_code)):
            return {"state": "WAIT_FAILED", "wait_error": _last_error()}
        return {"state": "FINAL_CHILD_EXITED", "exit_code": int(exit_code.value)}
    finally:
        with contextlib.suppress(BaseException):
            close(handle)


def _validated_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SandboxError(f"Windows runtime {label} is invalid")
    return value


def _valid_console_size(columns: object, rows: object) -> bool:
    return (
        isinstance(columns, int)
        and not isinstance(columns, bool)
        and isinstance(rows, int)
        and not isinstance(rows, bool)
        and 1 <= columns <= _COORD_MAX
        and 1 <= rows <= _COORD_MAX
    )


class _RunnerChild:
    """Native restricted child and its relay threads, owned by one runner."""

    def __init__(
        self,
        event_pipe: WindowsNamedPipeWriter,
        payload: Mapping[str, object],
        *,
        abort_control: Callable[[], None],
    ) -> None:
        self.event_pipe = event_pipe
        self._abort_control = abort_control
        self._write_lock = threading.Lock()
        self._event_failed = threading.Event()
        self._exit_sent = threading.Event()
        self._direct_child_exited = threading.Event()
        self._owned_scope_quiesced = threading.Event()
        self._api = _NativeChildApi()
        self._job = WindowsJobObject.create()
        self._process_handle: int | None = None
        self._process_id: int | None = None
        self._last_exit_code: int | None = None
        self._termination_observation: dict[str, object] | None = None
        self._stdin_handle: int | None = None
        self._desktop_handle: int | None = None
        self._desktop_name: str | None = None
        self._ephemeral_home: Path | None = None
        self._output_handles: list[int] = []
        self._threads: list[threading.Thread] = []
        self._token_attestation: WindowsTokenInspection | None = None
        self._pty_mode = False
        self._pseudo_console_handle: int | None = None
        try:
            self._create(payload)
        except BaseException:
            with contextlib.suppress(BaseException):
                self._job.terminate()
            self._job.close()
            raise

    def _create(self, payload: Mapping[str, object]) -> None:
        write_sid = SyntheticWindowsSid(_validated_text(payload.get("write_sid"), "write SID"))
        runner_sid = current_user_sid()
        runner_logon_sid = current_logon_sid()
        # Match the complete elevated Windows token model: capability SIDs
        # identify filesystem authorities, while the sandbox account, logon
        # session, and World SIDs keep the restricted-token second access check
        # compatible with ordinary Windows runtime objects.  Only the
        # capability SID is granted write authority by the filesystem ACL plan.
        token_request = WindowsRestrictedTokenRequest(
            (write_sid,),
            additional_restricting_sids=(runner_sid, runner_logon_sid, _WORLD_SID),
        )
        token = WindowsRestrictedToken.create_from_current_process(token_request)
        handles_to_close: list[int] = []
        created: RunnerLaunch | None = None
        try:
            expected_restricting_sids = (
                write_sid.value,
                runner_sid,
                runner_logon_sid,
                _WORLD_SID,
            )
            if token.inspection.restricted_sids != expected_restricting_sids:
                raise SandboxError(
                    "Windows restricted token does not contain the expected capability and identity SIDs"
                )
            if not token.inspection.change_notify_privilege_enabled:
                raise SandboxError(
                    "Windows restricted token did not preserve SeChangeNotifyPrivilege"
                )
            # These same runtime identities are object-DACL principals for
            # runner-created IPC and desktop objects.  The capability SID is
            # still the only managed write principal on workspace roots.
            token.set_default_dacl((runner_logon_sid, _WORLD_SID, write_sid.value))
            desktop_value = payload.get(
                "desktop_mode", _WindowsNativeDesktopMode.PRIVATE_DESKTOP.value
            )
            if not isinstance(desktop_value, str):
                raise SandboxError("Windows runtime desktop mode is invalid")
            try:
                desktop_mode = _WindowsNativeDesktopMode(desktop_value)
            except (TypeError, ValueError) as error:
                raise SandboxError("Windows runtime desktop mode is invalid") from error
            if desktop_mode is _WindowsNativeDesktopMode.PRIVATE_DESKTOP:
                self._desktop_handle, self._desktop_name = self._api.create_private_desktop(
                    (runner_sid, runner_logon_sid, write_sid.value, _WORLD_SID)
                )
            else:
                # The inherited/default desktop is a diagnostic-only launch
                # mode.  It changes no token, ACL, filesystem, or network
                # authority and is never selected by SandboxedProcessRequest.
                # Keep the comparison equivalent to the canonical Windows
                # launch contract: an omitted lpDesktop is not the same as
                # explicitly selecting the interactive default desktop.
                self._desktop_handle, self._desktop_name = None, r"Winsta0\Default"
            # Capture-backed launches use the inherited-console mode from the
            # canonical Windows contract.  The trusted adapter sends this
            # field explicitly; keep the fail-safe default aligned as well so
            # a legacy payload cannot reintroduce CREATE_NO_WINDOW for runtimes
            # whose CLR bootstrap requires normal console initialization.
            create_no_window = payload.get("create_no_window", False)
            if not isinstance(create_no_window, bool):
                raise SandboxError("Windows runtime console mode is invalid")
            stdio_mode = payload.get("stdio_mode", "")
            if stdio_mode not in {"capture", "merged-capture", "protocol", "pty"}:
                raise SandboxError("Windows runtime stdio mode is invalid")
            self._pty_mode = stdio_mode == "pty"
            if self._pty_mode and payload.get("terminal_mode") != "pty":
                raise SandboxError("Windows runtime PTY mode is invalid")
            if self._pty_mode and create_no_window:
                raise SandboxError("Windows runtime PTY cannot use CREATE_NO_WINDOW")
            initial_columns = payload.get("columns")
            initial_rows = payload.get("rows")
            if self._pty_mode and not _valid_console_size(initial_columns, initial_rows):
                raise SandboxError("Windows runtime PTY dimensions are invalid")
            executable = payload.get("executable")
            arguments = payload.get("arguments", [])
            shell_command = payload.get("shell_command")
            cwd = Path(_validated_text(payload.get("cwd"), "cwd"))
            environment = payload.get("environment")
            if not isinstance(environment, dict) or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in environment.items()
            ):
                raise SandboxError("Windows runtime child environment is invalid")
            environment = dict(environment)
            profile_username = _validated_text(payload.get("profile_username"), "profile username")
            private_home, ephemeral_home = self._api.get_user_profile(
                token.handle, profile_username
            )
            if ephemeral_home:
                self._ephemeral_home = Path(private_home)
            private_tmp = self._api.get_private_temp_path(private_home, runner_sid)
            # These values are derived from the selected sandbox account, never
            # inherited from the controller.  A model request cannot redirect
            # an enabled child into controller HOME/TMP.
            environment["HOME"] = private_home
            environment["USERPROFILE"] = private_home
            environment["TEMP"] = private_tmp
            environment["TMP"] = private_tmp
            if shell_command is not None:
                command = _validated_text(shell_command, "shell command")
                system_root = _validated_text(os.environ.get("SYSTEMROOT"), "SystemRoot")
                shell = str(Path(system_root) / "System32" / "cmd.exe")
                application_name = shell
                command_line = subprocess.list2cmdline([shell, "/d", "/s", "/c", command])
            else:
                application_name = _validated_text(executable, "executable")
                if not isinstance(arguments, list) or any(
                    not isinstance(argument, str) or "\x00" in argument for argument in arguments
                ):
                    raise SandboxError("Windows runtime child arguments are invalid")
                command_line = subprocess.list2cmdline([application_name, *arguments])
            merge_output = bool(payload.get("merge_output", False))
            pipe_stdin = bool(payload.get("pipe_stdin", False))
            if self._pty_mode:
                if shell_command is not None or merge_output or pipe_stdin:
                    raise SandboxError("Windows runtime PTY request has incompatible stdio")
                input_read, input_write = self._api.create_input_pipe()
                output_read, output_write = self._api.create_output_pipe()
                handles_to_close.extend((input_read, input_write, output_read, output_write))
                columns = cast(int, initial_columns)
                rows = cast(int, initial_rows)
                self._pseudo_console_handle = self._api.create_pseudo_console(
                    columns,
                    rows,
                    input_read,
                    output_write,
                )
                created = self._api.create_process_as_user(
                    token.handle,
                    application_name=application_name,
                    command_line=command_line,
                    cwd=cwd,
                    env=environment,
                    stdin_handle=0,
                    stdout_handle=0,
                    stderr_handle=0,
                    inherited_handles=(),
                    job_handle=self._job.process_creation_handle,
                    desktop_name=self._desktop_name,
                    create_no_window=False,
                    pseudo_console_handle=self._pseudo_console_handle,
                )
                # The host-side pipe endpoints remain owned by the runner;
                # the child-side endpoints were supplied to CreatePseudoConsole
                # and must be closed after the process is created.
                for handle in (input_read, output_write):
                    self._api.close_handle(handle)
                    handles_to_close.remove(handle)
                stdin_child, stdin_parent = input_read, input_write
                stdout_read, stdout_write = output_read, output_write
                stderr_read, stderr_write = None, None
            else:
                stdout_read, stdout_write = self._api.create_output_pipe()
                handles_to_close.extend((stdout_read, stdout_write))
                if merge_output:
                    stderr_read, stderr_write = None, stdout_write
                else:
                    stderr_read, stderr_write = self._api.create_output_pipe()
                    handles_to_close.extend((stderr_read, stderr_write))
                if pipe_stdin:
                    stdin_child, stdin_parent = self._api.create_input_pipe()
                else:
                    stdin_child, stdin_parent = self._api.open_null_input(), None
                handles_to_close.append(stdin_child)
                if stdin_parent is not None:
                    handles_to_close.append(stdin_parent)
                inherited = tuple(dict.fromkeys((stdin_child, stdout_write, stderr_write)))
                created = self._api.create_process_as_user(
                    token.handle,
                    application_name=application_name,
                    command_line=command_line,
                    cwd=cwd,
                    env=environment,
                    stdin_handle=stdin_child,
                    stdout_handle=stdout_write,
                    stderr_handle=stderr_write,
                    inherited_handles=inherited,
                    job_handle=self._job.process_creation_handle,
                    desktop_name=self._desktop_name,
                    create_no_window=create_no_window,
                )
                self._api.close_handle(stdin_child)
                handles_to_close.remove(stdin_child)
                self._api.close_handle(stdout_write)
                handles_to_close.remove(stdout_write)
                if not merge_output:
                    self._api.close_handle(stderr_write)
                    assert stderr_write is not None
                    handles_to_close.remove(stderr_write)
            self._process_handle = created.process_handle
            # The primary thread is owned by the process after creation; the
            # runner only needs the process handle for lifecycle ownership.
            if created.thread_handle is None:
                raise SandboxError("CreateProcessAsUserW returned no primary thread handle")
            self._api.close_handle(created.thread_handle)
            self._process_id = created.process_id
            self._stdin_handle = stdin_parent
            if stdin_parent is not None:
                handles_to_close.remove(stdin_parent)
            self._output_handles = [
                handle for handle in (stdout_read, stderr_read) if handle is not None
            ]
            for handle in self._output_handles:
                handles_to_close.remove(handle)
            # Attest the actual final child process, not the runner token or a
            # Python-side helper.  This is the last security gate before the
            # controller may observe SpawnReady; any failure tears down the
            # Job-owned child in the enclosing fail-closed path.
            child_attestation = inspect_windows_process_token(created.process_handle)
            if child_attestation.user_sid != runner_sid:
                raise SandboxError("Windows final child TokenUser does not match selected identity")
            if not child_attestation.is_restricted:
                raise SandboxError("Windows final child token is not restricted")
            if child_attestation.restricted_sids != expected_restricting_sids:
                raise SandboxError(
                    "Windows final child token does not contain the expected capability and identity SIDs"
                )
            if not child_attestation.change_notify_privilege_enabled:
                raise SandboxError(
                    "Windows final child token did not preserve SeChangeNotifyPrivilege"
                )
            if child_attestation.unexpected_enabled_privilege_count != 0:
                raise SandboxError("Windows final child token has unexpected enabled privileges")
            self._token_attestation = child_attestation
            self._send(
                RuntimeFrameType.SPAWN_READY,
                {
                    "version": PROTOCOL_VERSION,
                    "pid": created.process_id,
                    "security": {
                        "user_sid": child_attestation.user_sid,
                        "is_restricted": child_attestation.is_restricted,
                        "restricted_sids": list(child_attestation.restricted_sids),
                        "change_notify_privilege_enabled": (
                            child_attestation.change_notify_privilege_enabled
                        ),
                        "unexpected_enabled_privilege_count": (
                            child_attestation.unexpected_enabled_privilege_count
                        ),
                    },
                },
            )
            self._threads.append(
                threading.Thread(
                    target=self._relay,
                    args=(
                        stdout_read,
                        RuntimeFrameType.PTY_OUTPUT if self._pty_mode else RuntimeFrameType.STDOUT,
                    ),
                    daemon=True,
                )
            )
            if stderr_read is not None:
                self._threads.append(
                    threading.Thread(
                        target=self._relay, args=(stderr_read, RuntimeFrameType.STDERR), daemon=True
                    )
                )
            self._threads.append(threading.Thread(target=self._wait, daemon=True))
            for thread in self._threads:
                thread.start()
            handles_to_close.clear()
        except BaseException:
            if created is not None:
                with contextlib.suppress(BaseException):
                    self._api.close_handle(created.process_handle)
                if created.thread_handle is not None:
                    with contextlib.suppress(BaseException):
                        self._api.close_handle(created.thread_handle)
            owned_handles = list(self._output_handles)
            if self._stdin_handle is not None:
                owned_handles.append(self._stdin_handle)
            self._output_handles.clear()
            self._stdin_handle = None
            remaining_pseudo_console_handle = self._pseudo_console_handle
            self._pseudo_console_handle = None
            if remaining_pseudo_console_handle is not None:
                with contextlib.suppress(BaseException):
                    self._api.close_pseudo_console(remaining_pseudo_console_handle)
            seen: set[int] = set()
            for handle in (*handles_to_close, *owned_handles):
                if handle in seen:
                    continue
                seen.add(handle)
                with contextlib.suppress(BaseException):
                    self._api.close_handle(handle)
            self._remove_ephemeral_home()
            if self._desktop_handle is not None:
                with contextlib.suppress(BaseException):
                    self._api.close_desktop(self._desktop_handle)
                self._desktop_handle = None
                self._desktop_name = None
            raise
        finally:
            token.close()

    def _remove_ephemeral_home(self) -> None:
        home, self._ephemeral_home = self._ephemeral_home, None
        if home is not None:
            with contextlib.suppress(BaseException):
                shutil.rmtree(home)

    def _send(self, kind: RuntimeFrameType, value: object = b"") -> None:
        validate_channel_frame(RuntimeChannel.EVENT, kind)
        payload = value if isinstance(value, bytes) else encode_json(value)
        try:
            with self._write_lock:
                self.event_pipe.write(encode_frame(kind, payload))
        except BaseException:
            self._event_failed.set()
            with contextlib.suppress(BaseException):
                self._job.terminate()
            with contextlib.suppress(BaseException):
                self._abort_control()
            raise

    def observe(self) -> dict[str, object]:
        """Capture only final-child lifecycle facts safe for diagnostics."""

        handle = self._process_handle
        if handle is None:
            if self._last_exit_code is None:
                return {"state": "WAIT_FAILED", "wait_error": "HANDLE_CLOSED"}
            return {"state": "FINAL_CHILD_EXITED", "exit_code": self._last_exit_code}
        return self._api.observe_process(
            handle,
            active_state="FINAL_CHILD_STILL_ACTIVE",
            exited_state="FINAL_CHILD_EXITED",
        )

    def _error_payload(self, error: BaseException) -> dict[str, object]:
        return {
            "version": PROTOCOL_VERSION,
            "message": str(error)[:512],
            "child": self.observe(),
        }

    def _relay(self, handle: int, kind: RuntimeFrameType) -> None:
        try:
            while True:
                data = self._api.read_file(handle)
                if not data:
                    break
                self._send(kind, data)
        except BaseException as error:
            with contextlib.suppress(BaseException):
                self._send(
                    RuntimeFrameType.ERROR,
                    self._error_payload(error),
                )
        finally:
            with contextlib.suppress(BaseException):
                self._api.close_handle(handle)

    def _wait(self) -> None:
        assert self._process_handle is not None
        try:
            self._api.wait_process(self._process_handle)
            code = self._api.get_exit_code(self._process_handle)
            self._last_exit_code = code
            self._direct_child_exited.set()
            # A direct child exit is not the end of the Job-owned scope.  A
            # descendant may still be running without holding any relay
            # handle, so keep the independent wait thread alive until the Job
            # reports no active processes.  The control thread remains free to
            # receive TERMINATE/EOF while this bounded-frequency poll runs.
            while self._job.active_processes:
                time.sleep(0.02)
            # This is the only state transition that certifies the owned
            # scope is empty.  A direct-child exit alone is deliberately not
            # enough: a detached descendant may still be alive in the Job.
            self._owned_scope_quiesced.set()
            if self._pseudo_console_handle is not None:
                pseudo_console_handle = self._pseudo_console_handle
                self._pseudo_console_handle = None
                # ClosePseudoConsole may emit final output.  The PTY relay is
                # already draining the host output pipe and is joined only
                # after this call, so final bytes cannot race the Exit frame.
                self._api.close_pseudo_console(pseudo_console_handle)
            # Drain both output relays before publishing Exit.  The event pipe
            # is one-way, so the controller can keep reading while these
            # synchronous relay writes complete; Exit is therefore the final
            # event frame and the controller can close the event reader safely.
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=2.0)
            remaining = [
                thread
                for thread in self._threads
                if thread is not threading.current_thread() and thread.is_alive()
            ]
            if remaining:
                # A child exit should close every inherited stdout/stderr
                # writer.  If a relay is nevertheless still blocked, close
                # our read handles to unblock it rather than publishing Exit
                # while another thread could still write an event frame.
                for handle in self._output_handles:
                    with contextlib.suppress(BaseException):
                        self._api.close_handle(handle)
                for thread in remaining:
                    thread.join(timeout=1.0)
                if any(thread.is_alive() for thread in remaining):
                    raise SandboxError("Windows runtime output relay did not quiesce before Exit")
            if self._event_failed.is_set():
                raise SandboxError("Windows runtime event pipe failed before Exit")
            self._send(
                RuntimeFrameType.EXIT,
                {
                    "version": PROTOCOL_VERSION,
                    "returncode": code,
                    "child": self.observe(),
                    "termination_observation": self._termination_observation,
                },
            )
            self._exit_sent.set()
        except BaseException as error:
            with contextlib.suppress(BaseException):
                self._send(
                    RuntimeFrameType.ERROR,
                    self._error_payload(error),
                )
        finally:
            with contextlib.suppress(BaseException):
                self._api.close_handle(self._process_handle)
            self._process_handle = None
            stdin_handle, self._stdin_handle = self._stdin_handle, None
            if stdin_handle is not None:
                with contextlib.suppress(BaseException):
                    self._api.close_handle(stdin_handle)
            with contextlib.suppress(BaseException):
                self._job.close()
            remaining_pseudo_console_handle = self._pseudo_console_handle
            self._pseudo_console_handle = None
            if remaining_pseudo_console_handle is not None:
                with contextlib.suppress(BaseException):
                    self._api.close_pseudo_console(remaining_pseudo_console_handle)
            desktop_handle, self._desktop_handle = self._desktop_handle, None
            self._desktop_name = None
            if desktop_handle is not None:
                with contextlib.suppress(BaseException):
                    self._api.close_desktop(desktop_handle)
            self._remove_ephemeral_home()

    def handle(self, frame: RuntimeFrame) -> bool:
        if frame.kind is RuntimeFrameType.STDIN:
            if self._stdin_handle is None:
                raise SandboxError("Windows runtime stdin is not available")
            self._api.write_all(self._stdin_handle, frame.payload)
            return True
        if frame.kind is RuntimeFrameType.RESIZE:
            if not self._pty_mode or self._pseudo_console_handle is None:
                raise SandboxError("Windows runtime resize is not available")
            payload = decode_json(frame.payload)
            if (
                not isinstance(payload, dict)
                or payload.get("version") != PROTOCOL_VERSION
                or not _valid_console_size(payload.get("columns"), payload.get("rows"))
            ):
                raise SandboxError("Windows runtime resize dimensions are invalid")
            self._api.resize_pseudo_console(
                self._pseudo_console_handle,
                int(payload["columns"]),
                int(payload["rows"]),
            )
            return True
        if frame.kind is RuntimeFrameType.CLOSE_STDIN:
            if self._stdin_handle is not None:
                self._api.close_handle(self._stdin_handle)
                self._stdin_handle = None
            return True
        if frame.kind is RuntimeFrameType.TERMINATE:
            self._termination_observation = self.observe()
            self._job.terminate()
            return True
        if frame.kind in {
            RuntimeFrameType.SPAWN_READY,
            RuntimeFrameType.STDOUT,
            RuntimeFrameType.STDERR,
            RuntimeFrameType.EXIT,
            RuntimeFrameType.ERROR,
            RuntimeFrameType.PTY_OUTPUT,
        }:
            raise SandboxError("Windows runtime event frame arrived on control pipe")
        return False

    @property
    def exit_sent(self) -> bool:
        return self._exit_sent.is_set()

    @property
    def event_failed(self) -> bool:
        return self._event_failed.is_set()

    @property
    def owned_scope_quiesced(self) -> bool:
        """Return whether the direct child and its Job-owned descendants are done.

        The event is set by ``_wait`` only after the Job reports zero active
        processes.  When control EOF races that event, re-check the live Job
        count rather than sleeping for a protocol grace period.  A failed
        query remains non-quiesced so the caller fails closed.
        """

        if self._owned_scope_quiesced.is_set():
            return True
        if not self._direct_child_exited.is_set():
            return False
        try:
            if self._job.active_processes == 0:
                self._owned_scope_quiesced.set()
                return True
        except BaseException:
            return False
        return False

    def fail_closed(self) -> None:
        with contextlib.suppress(BaseException):
            self._job.terminate()


def _control_eof_is_harmless(child: _RunnerChild) -> bool:
    """Classify control EOF from actual runner state, never elapsed time."""

    try:
        if child.event_failed:
            return False
        return child.exit_sent or child.owned_scope_quiesced
    except BaseException:
        return False


def _handle_control_eof(child: _RunnerChild) -> bool:
    """Apply the state-based EOF contract and report clean runner shutdown."""

    if _control_eof_is_harmless(child):
        return True
    child.fail_closed()
    return False


class _NativeChildApi:
    """Win32 process/pipe facade used only inside the trusted runner."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows native child API is only available on Windows")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:  # pragma: no cover - Windows only
            raise OSError("Win32 API unavailable")
        kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
        self._get_last_error = cast(_CFunction, getattr(ctypes, "get_last_error", lambda: 0))
        self._create_pipe = _load_function(
            kernel32,
            "CreatePipe",
            [
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(_SecurityAttributes),
                ctypes.c_uint32,
            ],
            ctypes.c_int32,
        )
        self._create_pseudo_console = _load_function(
            kernel32,
            "CreatePseudoConsole",
            [_Coord, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self._resize_pseudo_console = _load_function(
            kernel32,
            "ResizePseudoConsole",
            [ctypes.c_void_p, _Coord],
            ctypes.c_int32,
        )
        self._close_pseudo_console = _load_function(
            kernel32,
            "ClosePseudoConsole",
            [ctypes.c_void_p],
            None,
        )
        self._set_handle_information = _load_function(
            kernel32,
            "SetHandleInformation",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self._create_file = _load_function(
            kernel32,
            "CreateFileW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(_SecurityAttributes),
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ],
            ctypes.c_void_p,
        )
        self._create_directory = _load_function(
            kernel32,
            "CreateDirectoryW",
            [ctypes.c_wchar_p, ctypes.POINTER(_SecurityAttributes)],
            ctypes.c_int32,
        )
        self._initialize_attribute_list = _load_function(
            kernel32,
            "InitializeProcThreadAttributeList",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self._update_attribute = _load_function(
            kernel32,
            "UpdateProcThreadAttribute",
            [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self._delete_attribute_list = _load_function(
            kernel32, "DeleteProcThreadAttributeList", [ctypes.c_void_p], None
        )
        self._close_handle = _load_function(
            kernel32, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32
        )
        self._read_file = _load_function(
            kernel32,
            "ReadFile",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self._write_file = _load_function(
            kernel32,
            "WriteFile",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self._wait = _load_function(
            kernel32, "WaitForSingleObject", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_uint32
        )
        self._get_exit = _load_function(
            kernel32,
            "GetExitCodeProcess",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int32,
        )
        self._get_temp_path = _load_function(
            kernel32,
            "GetTempPathW",
            [ctypes.c_uint32, ctypes.c_wchar_p],
            ctypes.c_uint32,
        )
        advapi32 = cast(object, loader("advapi32.dll", use_last_error=True))
        shell32 = cast(object, loader("shell32.dll", use_last_error=True))
        ole32 = cast(object, loader("ole32.dll", use_last_error=True))
        self._get_known_folder_path = _load_function(
            shell32,
            "SHGetKnownFolderPath",
            [
                ctypes.POINTER(_Guid),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p),
            ],
            ctypes.c_int32,
        )
        self._co_task_mem_free = _load_function(
            ole32,
            "CoTaskMemFree",
            [ctypes.c_void_p],
            None,
        )
        self._create_process_as_user = _load_function(
            advapi32,
            "CreateProcessAsUserW",
            [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        user32 = cast(object, loader("user32.dll", use_last_error=True))
        self._create_desktop = _load_function(
            user32,
            "CreateDesktopW",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(_SecurityAttributes),
            ],
            ctypes.c_void_p,
        )
        self._close_desktop = _load_function(
            user32,
            "CloseDesktop",
            [ctypes.c_void_p],
            ctypes.c_int32,
        )

    def _error(self, operation: str) -> NoReturn:
        error = cast(int, self._get_last_error())
        raise OSError(error, f"{operation} failed with Windows error {error}")

    @staticmethod
    def _attrs() -> _SecurityAttributes:
        value = _SecurityAttributes()
        value.nLength = ctypes.sizeof(value)
        value.bInheritHandle = True
        return value

    def create_output_pipe(self) -> tuple[int, int]:
        return self._pipe(parent_is_read=True)

    def create_input_pipe(self) -> tuple[int, int]:
        return self._pipe(parent_is_read=False)

    def create_pseudo_console(
        self,
        columns: int,
        rows: int,
        input_read_handle: int,
        output_write_handle: int,
    ) -> int:
        if not _valid_console_size(columns, rows):
            raise ValueError("Windows PTY dimensions are invalid")
        handle = ctypes.c_void_p()
        result = cast(
            int,
            self._create_pseudo_console(
                _Coord(columns, rows),
                input_read_handle,
                output_write_handle,
                0,
                ctypes.byref(handle),
            ),
        )
        if result != 0:
            raise OSError(result, f"CreatePseudoConsole failed with HRESULT {result}")
        if handle.value is None:
            raise OSError("CreatePseudoConsole returned an invalid handle")
        return int(handle.value)

    def resize_pseudo_console(self, handle: int, columns: int, rows: int) -> None:
        if not _valid_console_size(columns, rows):
            raise ValueError("Windows PTY dimensions are invalid")
        result = cast(int, self._resize_pseudo_console(handle, _Coord(columns, rows)))
        if result != 0:
            raise OSError(result, f"ResizePseudoConsole failed with HRESULT {result}")

    def close_pseudo_console(self, handle: int) -> None:
        self._close_pseudo_console(handle)

    def _pipe(self, *, parent_is_read: bool) -> tuple[int, int]:
        attrs = self._attrs()
        read, write = ctypes.c_void_p(), ctypes.c_void_p()
        if not self._create_pipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(attrs), 0):
            self._error("CreatePipe")
        first, second = int(read.value or 0), int(write.value or 0)
        parent = first if parent_is_read else second
        if not self._set_handle_information(parent, _HANDLE_FLAG_INHERIT, 0):
            self.close_handle(first)
            self.close_handle(second)
            self._error("SetHandleInformation")
        return first, second

    def open_null_input(self) -> int:
        attrs = self._attrs()
        value = self._create_file(
            "NUL",
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            ctypes.byref(attrs),
            _OPEN_EXISTING,
            0x80,
            None,
        )
        if value is None or value == 0 or value == _INVALID_HANDLE_VALUE:
            self._error("CreateFileW(NUL)")
        return int(cast(int, value))

    def create_process_as_user(
        self,
        token: int,
        *,
        application_name: str,
        command_line: str,
        cwd: Path,
        env: Mapping[str, str],
        stdin_handle: int,
        stdout_handle: int,
        stderr_handle: int,
        inherited_handles: tuple[int, ...],
        job_handle: int,
        desktop_name: str | None,
        create_no_window: bool,
        pseudo_console_handle: int | None = None,
    ) -> RunnerLaunch:
        pty_mode = pseudo_console_handle is not None
        if pty_mode and inherited_handles:
            raise ValueError("PTY process creation cannot inherit arbitrary handles")
        attribute_count = 2
        required = ctypes.c_size_t()
        self._initialize_attribute_list(None, attribute_count, 0, ctypes.byref(required))
        if not required.value:
            self._error("InitializeProcThreadAttributeList(size)")
        storage = ctypes.create_string_buffer(required.value)
        attributes = ctypes.cast(storage, ctypes.c_void_p)
        if not self._initialize_attribute_list(
            attributes, attribute_count, 0, ctypes.byref(required)
        ):
            self._error("InitializeProcThreadAttributeList")
        job_values = (ctypes.c_void_p * 1)(job_handle)
        handle_values = (ctypes.c_void_p * len(inherited_handles))(*inherited_handles)
        try:
            if pty_mode and not self._update_attribute(
                attributes,
                0,
                _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                pseudo_console_handle,
                ctypes.sizeof(ctypes.c_void_p),
                None,
                None,
            ):
                self._error("UpdateProcThreadAttribute(pseudoconsole)")
            if not self._update_attribute(
                attributes,
                0,
                _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                ctypes.cast(job_values, ctypes.c_void_p),
                ctypes.sizeof(job_values),
                None,
                None,
            ):
                self._error("UpdateProcThreadAttribute(job)")
            if not pty_mode and not self._update_attribute(
                attributes,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_values, ctypes.c_void_p),
                ctypes.sizeof(handle_values),
                None,
                None,
            ):
                self._error("UpdateProcThreadAttribute(handles)")
            # CreatePipe marks both ends inheritable, but make the contract
            # explicit immediately before CreateProcessAsUserW.  The handle
            # list attribute is only honored for inheritable handles; relying
            # on the CreatePipe security-attributes default is fragile across
            # Windows runners and leaves the child with detached stdio.
            if not pty_mode:
                for handle in dict.fromkeys(inherited_handles):
                    if not self._set_handle_information(
                        handle, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT
                    ):
                        self._error("SetHandleInformation(stdio inherit)")
            startup = _StartupInfoExW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # The desktop is selected by trusted runner composition.  The
            # inherited/default value is used only by the native diagnostic
            # probes; model-controlled requests cannot select it.
            startup.StartupInfo.lpDesktop = desktop_name
            if pty_mode:
                startup.StartupInfo.dwFlags = 0
                startup.StartupInfo.hStdInput = None
                startup.StartupInfo.hStdOutput = None
                startup.StartupInfo.hStdError = None
            else:
                startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput = stdin_handle
                startup.StartupInfo.hStdOutput = stdout_handle
                startup.StartupInfo.hStdError = stderr_handle
            startup.lpAttributeList = attributes.value
            process = _ProcessInformation()
            mutable = ctypes.create_unicode_buffer(command_line)
            environment = _environment_block(env)
            creation_flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT
            if create_no_window:
                creation_flags |= _CREATE_NO_WINDOW
            created = self._create_process_as_user(
                token,
                None,
                mutable,
                None,
                None,
                not pty_mode,
                creation_flags,
                ctypes.cast(environment, ctypes.c_void_p),
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process),
            )
            if not created:
                self._error("CreateProcessAsUserW")
        finally:
            self._delete_attribute_list(attributes)
        if not process.hProcess or not process.hThread or not process.dwProcessId:
            raise OSError("CreateProcessAsUserW returned invalid process information")
        return RunnerLaunch(
            int(process.hProcess),
            int(process.dwProcessId),
            int(process.hThread),
        )

    def observe_process(
        self,
        handle: int,
        *,
        active_state: str,
        exited_state: str,
    ) -> dict[str, object]:
        """Capture a process wait/exit result without reading child data."""

        result = self._wait(handle, 0)
        if result == _WAIT_TIMEOUT:
            return {"state": active_state}
        if result != _WAIT_OBJECT_0:
            return {"state": "WAIT_FAILED", "wait_error": cast(int, result)}
        exit_code = ctypes.c_uint32()
        if not self._get_exit(handle, ctypes.byref(exit_code)):
            return {"state": "WAIT_FAILED", "wait_error": cast(int, self._get_last_error())}
        return {"state": exited_state, "exit_code": int(exit_code.value)}

    def create_private_desktop(self, sids: tuple[str, ...]) -> tuple[int, str]:
        """Create a private desktop accessible only to the selected account."""

        desktop_name = f"NeuroCodeW3-{secrets.token_hex(16)}"
        attributes, descriptor = _security_descriptor((*sids, "S-1-5-18", "S-1-5-32-544"))
        try:
            handle = self._create_desktop(
                desktop_name,
                None,
                None,
                0,
                _DESKTOP_ALL_ACCESS,
                ctypes.byref(attributes),
            )
        finally:
            _free_security_descriptor(descriptor)
        if handle is None or handle == 0 or handle == _INVALID_HANDLE_VALUE:
            self._error("CreateDesktopW")
        # STARTUPINFO.lpDesktop takes a window-station/desktop pair.  A bare
        # desktop name is resolved against the caller's context and can leave
        # a restricted child waiting during user32 initialization.  The
        # runner is launched on the interactive WinSta0 station, matching the
        # native Windows launch contract used by the existing Job/ConPTY
        # implementation.
        return int(cast(int, handle)), rf"Winsta0\{desktop_name}"

    def close_desktop(self, handle: int) -> None:
        if handle and not self._close_desktop(handle):
            self._error("CloseDesktop")

    def read_file(self, handle: int, count: int = 65_536) -> bytes:
        buffer = ctypes.create_string_buffer(count)
        returned = ctypes.c_uint32()
        if not self._read_file(handle, buffer, count, ctypes.byref(returned), None):
            error = cast(int, self._get_last_error())
            if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
                return b""
            raise OSError(error, "ReadFile failed")
        return buffer.raw[: returned.value]

    def write_all(self, handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 65_536]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = ctypes.c_uint32()
            if not self._write_file(handle, buffer, len(chunk), ctypes.byref(written), None):
                self._error("WriteFile")
            if written.value == 0:
                raise OSError("WriteFile returned zero bytes")
            offset += written.value

    def wait_process(self, handle: int) -> None:
        result = cast(int, self._wait(handle, _INFINITE))
        if result != _WAIT_OBJECT_0:
            self._error("WaitForSingleObject")

    def get_exit_code(self, handle: int) -> int:
        value = ctypes.c_uint32()
        if not self._get_exit(handle, ctypes.byref(value)):
            self._error("GetExitCodeProcess")
        return int(value.value)

    def create_private_directory(self, path: Path, user_sid: str) -> None:
        attributes, descriptor = _security_descriptor(
            (user_sid, "S-1-5-18", "S-1-5-32-544"),
            inherit_to_children=True,
        )
        try:
            created = self._create_directory(str(path), ctypes.byref(attributes))
            if not created:
                error = cast(int, self._get_last_error())
                if error != _ERROR_ALREADY_EXISTS:
                    raise OSError(error, f"CreateDirectoryW failed with Windows error {error}")
        finally:
            _free_security_descriptor(descriptor)

    def get_user_profile(self, token: int, profile_username: str) -> tuple[str, bool]:
        path_pointer = ctypes.c_wchar_p()
        result = self._get_known_folder_path(
            ctypes.byref(_FOLDERID_PROFILE),
            0,
            token,
            ctypes.byref(path_pointer),
        )
        result_code = cast(int, result)
        if result_code == 0 and path_pointer.value:
            try:
                return str(path_pointer.value), False
            finally:
                self._co_task_mem_free(path_pointer)
        if path_pointer.value:
            self._co_task_mem_free(path_pointer)
        # A first-run local account has no loaded profile hive yet.  W3 does
        # not use LOGON_WITH_PROFILE because that can block the controller;
        # for the two fixed W2 identities the standard profile root is a
        # deterministic, account-private fallback.  Reject every other name
        # and any malformed SystemRoot rather than accepting a caller path.
        if (
            result_code not in {2, 3, _HRESULT_FILE_NOT_FOUND, _HRESULT_PATH_NOT_FOUND}
            or profile_username not in _PROFILE_USERNAMES
        ):
            raise OSError(
                result_code,
                f"SHGetKnownFolderPath(Profile) failed with Windows error {result_code}",
            )
        system_root = Path(os.environ.get("SYSTEMROOT", ""))
        if not system_root.anchor:
            raise OSError(result_code, "Windows SystemRoot has no drive anchor")
        fallback = (
            Path(system_root.anchor)
            / "Windows"
            / "Temp"
            / f"neuro-code-home-{profile_username}-{secrets.token_hex(16)}"
        )
        try:
            self.create_private_directory(fallback, current_user_sid())
        except OSError as error:
            raise SandboxError("Windows sandbox private HOME is unavailable") from error
        return str(fallback), True

    def get_private_temp_path(self, profile: str, user_sid: str) -> str:
        """Return a temp directory owned by the selected sandbox profile.

        ``GetTempPathW`` would fall back to a machine-wide directory when the
        runner is started with its explicit minimal environment.  Deriving the
        profile-local path avoids accidentally inheriting a controller temp
        location and keeps HOME/TMP inside the selected W2 account boundary.
        """

        path = Path(profile)
        try:
            for component in ("AppData", "Local", "Temp"):
                path = path / component
                self.create_private_directory(path, user_sid)
        except OSError as error:
            raise SandboxError(
                "Windows sandbox private temporary directory is unavailable"
            ) from error
        return str(path)

    def close_handle(self, handle: int) -> None:
        if handle and not self._close_handle(handle):
            self._error("CloseHandle")


def _runner_main(control_pipe_name: str, event_pipe_name: str) -> int:
    control_pipe: WindowsNamedPipeReader | None = None
    event_pipe: WindowsNamedPipeWriter | None = None
    child: _RunnerChild | None = None
    try:
        control_pipe = WindowsNamedPipeClient.connect_reader(control_pipe_name)
        event_pipe = WindowsNamedPipeClient.connect_writer(event_pipe_name)
        decoder = RuntimeFrameDecoder()
        while True:
            data = control_pipe.read()
            if not data:
                decoder.finish()
                if child is not None and _handle_control_eof(child):
                    # EOF is harmless only after the final event is known to
                    # be sent or the complete Job-owned scope is quiesced.
                    # An active Job fails closed immediately; there is no
                    # time-based protocol grace that could hide controller
                    # loss.
                    return 0
                return 1
            for frame in decoder.feed(data):
                validate_channel_frame(RuntimeChannel.CONTROL, frame.kind)
                if child is None:
                    if frame.kind is not RuntimeFrameType.SPAWN_REQUEST:
                        raise SandboxError("Windows runtime first frame must be SpawnRequest")
                    payload = decode_json(frame.payload)
                    if not isinstance(payload, dict) or payload.get("version") != PROTOCOL_VERSION:
                        raise SandboxError("Windows runtime SpawnRequest version is invalid")
                    child = _RunnerChild(event_pipe, payload, abort_control=control_pipe.close)
                elif not child.handle(frame):
                    raise SandboxError("Windows runtime frame is not valid for the runner state")
    except BaseException as error:
        with contextlib.suppress(BaseException):
            if child is not None:
                child.fail_closed()
            if event_pipe is not None:
                diagnostic: dict[str, object] = {
                    "version": PROTOCOL_VERSION,
                    "message": str(error)[:512],
                }
                if child is not None:
                    diagnostic["child"] = child.observe()
                event_pipe.write(
                    encode_frame(
                        RuntimeFrameType.ERROR,
                        encode_json(diagnostic),
                    )
                )
        # Preserve a bounded, non-zero diagnostic in the trusted runner's
        # process exit code.  The controller can inspect this when a pipe
        # connection fails before the event channel exists; no paths,
        # credentials, or request data are encoded here.
        win_error = getattr(error, "winerror", None)
        if not isinstance(win_error, int) or win_error <= 0:
            win_error = getattr(error, "errno", None)
        if not isinstance(win_error, int) or win_error <= 0:
            win_error = 1
        return 0xE0000000 | (win_error & 0xFFFF)
    finally:
        if child is not None:
            with contextlib.suppress(BaseException):
                child._job.close()
        if control_pipe is not None:
            control_pipe.close()
        if event_pipe is not None:
            event_pipe.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--control-pipe", required=True)
    parser.add_argument("--event-pipe", required=True)
    arguments = parser.parse_args(argv)
    return _runner_main(arguments.control_pipe, arguments.event_pipe)


if __name__ == "__main__":  # pragma: no cover - exercised by Windows acceptance
    raise SystemExit(main())


__all__ = [
    "RunnerLaunch",
    "WindowsNamedPipe",
    "WindowsNamedPipeClient",
    "WindowsNamedPipeReader",
    "WindowsNamedPipeServer",
    "WindowsNamedPipeWriter",
    "close_runner_process",
    "current_user_sid",
    "launch_runner",
    "observe_process_id",
]
