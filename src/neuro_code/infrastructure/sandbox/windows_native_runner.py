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
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, cast

from neuro_code.infrastructure.sandbox.windows_job import WindowsJobObject
from neuro_code.infrastructure.sandbox.windows_native_runtime_protocol import (
    PROTOCOL_VERSION,
    RuntimeFrame,
    RuntimeFrameDecoder,
    RuntimeFrameType,
    decode_json,
    encode_frame,
    encode_json,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_security_token import (
    WindowsRestrictedToken,
    WindowsRestrictedTokenRequest,
)
from neuro_code.shared.errors import SandboxError

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_CREATE_ALWAYS = 2
_PIPE_ACCESS_DUPLEX = 0x00000003
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
_HANDLE_FLAG_INHERIT = 0x00000001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF
# Do not ask CreateProcessWithLogonW to synchronously load the account
# profile.  W3 derives private HOME/TMP from the final token and creates those
# directories inside the runner; loading a fresh W2 profile here can block the
# controller before the named-pipe protocol starts.
_LOGON_FLAGS = 0
_TOKEN_USER = 1
_TOKEN_QUERY = 0x0008
_DACL_SECURITY_INFORMATION = 0x00000004


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


@dataclass(frozen=True, slots=True)
class RunnerLaunch:
    """Controller-side handle for the trusted runner process."""

    process_handle: int
    process_id: int
    thread_handle: int | None = None


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
            if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
                return b""
            raise OSError(error, f"ReadFile failed with Windows error {error}")
        return buffer.raw[: returned.value]

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


class WindowsNamedPipe:
    """Connected synchronous byte stream used by the framing layer."""

    def __init__(self, handle: int, *, api: _NativePipeApi | None = None) -> None:
        if handle <= 0:
            raise ValueError("pipe handle must be positive")
        self._api = _NativePipeApi() if api is None else api
        self._handle: int | None = handle
        self._write_lock = threading.Lock()

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise RuntimeError("pipe is closed")
        return self._handle

    def read(self) -> bytes:
        return self._api.read(self.handle)

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

    def write(self, payload: bytes) -> None:
        with self._write_lock:
            self._api.write(self.handle, payload)

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


def _security_descriptor(sids: Sequence[str]) -> tuple[_SecurityAttributes, int]:
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
    sddl = "D:P" + "".join(f"(A;;GA;;;{sid})" for sid in dict.fromkeys(sids))
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


class WindowsNamedPipeServer:
    """Controller-created, random local named pipe with an exact peer DACL."""

    def __init__(self, *, peer_sids: Sequence[str]) -> None:
        self.name = rf"\\.\pipe\neuro-code-{secrets.token_urlsafe(32)}"
        self._api = _NativePipeApi()
        attributes, descriptor = _security_descriptor(peer_sids)
        self._descriptor = descriptor
        try:
            handle = self._api.create_named_pipe(
                self.name,
                _PIPE_ACCESS_DUPLEX,
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
        self._pipe: WindowsNamedPipe | None = WindowsNamedPipe(
            int(cast(int, handle)), api=self._api
        )

    def accept(self) -> WindowsNamedPipe:
        pipe = self._pipe
        if pipe is None:
            raise RuntimeError("named-pipe server is closed")
        if not self._api.connect_named_pipe(pipe.handle, None):
            error = self._api.last_error()
            if error != _ERROR_PIPE_CONNECTED:
                self.close()
                raise OSError(error, "ConnectNamedPipe failed")
        self._pipe = None
        return pipe

    def accept_for_runner(
        self,
        runner_handle: int,
        *,
        timeout_seconds: float = 30.0,
    ) -> WindowsNamedPipe:
        """Accept while detecting a dead or non-connecting trusted runner.

        ``ConnectNamedPipe`` is synchronous. Running it behind a short
        monitor keeps a runner import/logon failure from hanging the
        controller forever while retaining the exact named-pipe DACL.
        """

        if timeout_seconds <= 0:
            raise ValueError("named-pipe connection timeout must be positive")
        result: list[WindowsNamedPipe] = []
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
                self.close()
                thread.join(timeout=1.0)
                raise SandboxError("trusted Windows runner exited before named-pipe connection")
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
    def connect(cls, name: str) -> WindowsNamedPipeClient:
        api = _NativePipeApi()
        handle = api.create_file(
            name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle is None or handle == 0 or handle == _INVALID_HANDLE_VALUE:
            raise OSError(api.last_error(), "CreateFileW(named pipe) failed")
        return cls(int(cast(int, handle)), api=api)


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
    pipe_name: str,
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
            "--pipe",
            pipe_name,
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


def _validated_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SandboxError(f"Windows runtime {label} is invalid")
    return value


class _RunnerChild:
    """Native restricted child and its relay threads, owned by one runner."""

    def __init__(self, pipe: WindowsNamedPipe, payload: Mapping[str, object]) -> None:
        self.pipe = pipe
        self._write_lock = threading.Lock()
        self._api = _NativeChildApi()
        self._job = WindowsJobObject.create()
        self._process_handle: int | None = None
        self._stdin_handle: int | None = None
        self._output_handles: list[int] = []
        self._threads: list[threading.Thread] = []
        try:
            self._create(payload)
        except BaseException:
            with contextlib.suppress(BaseException):
                self._job.terminate()
            self._job.close()
            raise

    def _create(self, payload: Mapping[str, object]) -> None:
        write_sid = SyntheticWindowsSid(_validated_text(payload.get("write_sid"), "write SID"))
        token_request = WindowsRestrictedTokenRequest((write_sid,))
        token = WindowsRestrictedToken.create_from_current_process(token_request)
        handles_to_close: list[int] = []
        created: RunnerLaunch | None = None
        try:
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
            private_home = self._api.get_user_profile(token.handle)
            private_tmp = self._api.get_private_temp_path(private_home)
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
            )
            if created.thread_handle is not None:
                self._api.close_handle(created.thread_handle)
                created = RunnerLaunch(created.process_handle, created.process_id)
            self._api.close_handle(stdin_child)
            handles_to_close.remove(stdin_child)
            self._api.close_handle(stdout_write)
            handles_to_close.remove(stdout_write)
            if not merge_output:
                self._api.close_handle(stderr_write)
                assert stderr_write is not None
                handles_to_close.remove(stderr_write)
            self._process_handle = created.process_handle
            self._stdin_handle = stdin_parent
            if stdin_parent is not None:
                handles_to_close.remove(stdin_parent)
            self._output_handles = [
                handle for handle in (stdout_read, stderr_read) if handle is not None
            ]
            for handle in self._output_handles:
                handles_to_close.remove(handle)
            self._send(
                RuntimeFrameType.SPAWN_READY,
                {"version": PROTOCOL_VERSION, "pid": created.process_id},
            )
            self._threads.append(
                threading.Thread(
                    target=self._relay, args=(stdout_read, RuntimeFrameType.STDOUT), daemon=True
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
            seen: set[int] = set()
            for handle in (*handles_to_close, *owned_handles):
                if handle in seen:
                    continue
                seen.add(handle)
                with contextlib.suppress(BaseException):
                    self._api.close_handle(handle)
            raise
        finally:
            token.close()

    def _send(self, kind: RuntimeFrameType, value: object = b"") -> None:
        payload = value if isinstance(value, bytes) else encode_json(value)
        with self._write_lock:
            self.pipe.write(encode_frame(kind, payload))

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
                    {"version": PROTOCOL_VERSION, "message": str(error)[:512]},
                )
        finally:
            with contextlib.suppress(BaseException):
                self._api.close_handle(handle)

    def _wait(self) -> None:
        assert self._process_handle is not None
        try:
            self._api.wait_process(self._process_handle)
            code = self._api.get_exit_code(self._process_handle)
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=2.0)
            self._send(RuntimeFrameType.EXIT, {"version": PROTOCOL_VERSION, "returncode": code})
        except BaseException as error:
            with contextlib.suppress(BaseException):
                self._send(
                    RuntimeFrameType.ERROR,
                    {"version": PROTOCOL_VERSION, "message": str(error)[:512]},
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

    def handle(self, frame: RuntimeFrame) -> bool:
        if frame.kind is RuntimeFrameType.STDIN:
            if self._stdin_handle is None:
                raise SandboxError("Windows runtime stdin is not available")
            self._api.write_all(self._stdin_handle, frame.payload)
            return True
        if frame.kind is RuntimeFrameType.CLOSE_STDIN:
            if self._stdin_handle is not None:
                self._api.close_handle(self._stdin_handle)
                self._stdin_handle = None
            return True
        if frame.kind is RuntimeFrameType.TERMINATE:
            self._job.terminate()
            return True
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
        userenv = cast(object, loader("userenv.dll", use_last_error=True))
        self._get_user_profile = _load_function(
            userenv,
            "GetUserProfileDirectoryW",
            [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int32,
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
    ) -> RunnerLaunch:
        required = ctypes.c_size_t()
        self._initialize_attribute_list(None, 2, 0, ctypes.byref(required))
        if not required.value:
            self._error("InitializeProcThreadAttributeList(size)")
        storage = ctypes.create_string_buffer(required.value)
        attributes = ctypes.cast(storage, ctypes.c_void_p)
        if not self._initialize_attribute_list(attributes, 2, 0, ctypes.byref(required)):
            self._error("InitializeProcThreadAttributeList")
        job_values = (ctypes.c_void_p * 1)(job_handle)
        handle_values = (ctypes.c_void_p * len(inherited_handles))(*inherited_handles)
        try:
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
            if not self._update_attribute(
                attributes,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_values, ctypes.c_void_p),
                ctypes.sizeof(handle_values),
                None,
                None,
            ):
                self._error("UpdateProcThreadAttribute(handles)")
            startup = _StartupInfoExW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = stdin_handle
            startup.StartupInfo.hStdOutput = stdout_handle
            startup.StartupInfo.hStdError = stderr_handle
            startup.lpAttributeList = attributes.value
            process = _ProcessInformation()
            mutable = ctypes.create_unicode_buffer(command_line)
            environment = _environment_block(env)
            created = self._create_process_as_user(
                token,
                application_name,
                mutable,
                None,
                None,
                True,
                _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
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
        if self._wait(handle, _INFINITE) != _WAIT_OBJECT_0:
            self._error("WaitForSingleObject")

    def get_exit_code(self, handle: int) -> int:
        value = ctypes.c_uint32()
        if not self._get_exit(handle, ctypes.byref(value)):
            self._error("GetExitCodeProcess")
        return int(value.value)

    def get_user_profile(self, token: int) -> str:
        required = ctypes.c_uint32()
        self._get_user_profile(token, None, ctypes.byref(required))
        if not required.value:
            self._error("GetUserProfileDirectoryW(size)")
        buffer = ctypes.create_unicode_buffer(required.value + 1)
        length = ctypes.c_uint32(len(buffer))
        if not self._get_user_profile(token, buffer, ctypes.byref(length)):
            self._error("GetUserProfileDirectoryW")
        return str(buffer.value)

    @staticmethod
    def get_private_temp_path(profile: str) -> str:
        """Return a temp directory owned by the selected sandbox profile.

        ``GetTempPathW`` would fall back to a machine-wide directory when the
        runner is started with its explicit minimal environment.  Deriving the
        profile-local path avoids accidentally inheriting a controller temp
        location and keeps HOME/TMP inside the selected W2 account boundary.
        """

        path = Path(profile) / "AppData" / "Local" / "Temp"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SandboxError(
                "Windows sandbox private temporary directory is unavailable"
            ) from error
        return str(path)

    def close_handle(self, handle: int) -> None:
        if handle and not self._close_handle(handle):
            self._error("CloseHandle")


def _runner_main(pipe_name: str) -> int:
    pipe = WindowsNamedPipeClient.connect(pipe_name)
    decoder = RuntimeFrameDecoder()
    child: _RunnerChild | None = None
    try:
        while True:
            data = pipe.read()
            if not data:
                decoder.finish()
                return 1
            for frame in decoder.feed(data):
                if child is None:
                    if frame.kind is not RuntimeFrameType.SPAWN_REQUEST:
                        raise SandboxError("Windows runtime first frame must be SpawnRequest")
                    payload = decode_json(frame.payload)
                    if not isinstance(payload, dict) or payload.get("version") != PROTOCOL_VERSION:
                        raise SandboxError("Windows runtime SpawnRequest version is invalid")
                    child = _RunnerChild(pipe, payload)
                elif not child.handle(frame):
                    if frame.kind is RuntimeFrameType.EXIT:
                        return 0
                    raise SandboxError("Windows runtime frame is not valid for the runner state")
    except BaseException as error:
        with contextlib.suppress(BaseException):
            pipe.write(
                encode_frame(
                    RuntimeFrameType.ERROR,
                    encode_json({"version": PROTOCOL_VERSION, "message": str(error)[:512]}),
                )
            )
        return 1
    finally:
        if child is not None:
            with contextlib.suppress(BaseException):
                child._job.close()
        pipe.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pipe", required=True)
    arguments = parser.parse_args(argv)
    return _runner_main(arguments.pipe)


if __name__ == "__main__":  # pragma: no cover - exercised by Windows acceptance
    raise SystemExit(main())


__all__ = [
    "RunnerLaunch",
    "WindowsNamedPipe",
    "WindowsNamedPipeClient",
    "WindowsNamedPipeServer",
    "close_runner_process",
    "current_user_sid",
    "launch_runner",
]
