from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ntpath
import os
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, Self, cast

from neuro_code.infrastructure.sandbox.windows_process import (
    windows_environment_block as _environment_block,
)
from neuro_code.shared.async_utils import run_blocking

_DWORD_MAX = (1 << 32) - 1
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_HANDLE_FLAG_INHERIT = 0x00000001
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232


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
    _fields_ = [
        ("StartupInfo", _StartupInfoW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


@dataclass(frozen=True, slots=True)
class _CreatedProcess:
    process_handle: int
    thread_handle: int
    process_id: int


class _WindowsJobProcessApi(Protocol):
    def create_output_pipe(self) -> tuple[int, int]: ...

    def create_input_pipe(self) -> tuple[int, int]: ...

    def open_null_input(self) -> int: ...

    def create_process(
        self,
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
    ) -> _CreatedProcess: ...

    def read_file(self, handle: int, byte_count: int) -> bytes: ...

    def write_file(self, handle: int, data: bytes) -> int: ...

    def wait_process(self, handle: int) -> None: ...

    def get_exit_code(self, handle: int) -> int: ...

    def terminate_process(self, handle: int, exit_code: int) -> None: ...

    def close_handle(self, handle: int) -> None: ...


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


class _NativeWindowsJobProcessApi:
    """Typed synchronous Win32 facade for atomic Job-bound process creation.

    提供原子 Job 绑定进程创建的类型化同步 Win32 门面."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("atomic Windows Job process creation is only available on Windows")

        loader = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if loader is None or get_last_error is None:  # pragma: no cover - defensive on Windows
            raise OSError("this Python runtime does not expose the Win32 ctypes API")

        kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
        self._get_last_error = cast(_CFunction, get_last_error)
        try:
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
                kernel32,
                "DeleteProcThreadAttributeList",
                [ctypes.c_void_p],
                None,
            )
            self._create_process = _load_function(
                kernel32,
                "CreateProcessW",
                [
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
            self._wait_for_single_object = _load_function(
                kernel32,
                "WaitForSingleObject",
                [ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_uint32,
            )
            self._get_exit_code_process = _load_function(
                kernel32,
                "GetExitCodeProcess",
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
                ctypes.c_int32,
            )
            self._terminate_process = _load_function(
                kernel32,
                "TerminateProcess",
                [ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int32,
            )
            self._close_handle = _load_function(
                kernel32,
                "CloseHandle",
                [ctypes.c_void_p],
                ctypes.c_int32,
            )
        except AttributeError as error:  # pragma: no cover - unsupported Windows only
            raise OSError(
                "this Windows version does not provide extended process attributes"
            ) from error

    def create_output_pipe(self) -> tuple[int, int]:
        return self._create_directional_pipe(parent_is_read=True)

    def create_input_pipe(self) -> tuple[int, int]:
        return self._create_directional_pipe(parent_is_read=False)

    def _create_directional_pipe(self, *, parent_is_read: bool) -> tuple[int, int]:
        security = _inheritable_security_attributes()
        read_handle = ctypes.c_void_p()
        write_handle = ctypes.c_void_p()
        if not self._create_pipe(
            ctypes.byref(read_handle),
            ctypes.byref(write_handle),
            ctypes.byref(security),
            0,
        ):
            self._raise_last_error("CreatePipe")
        if read_handle.value is None or write_handle.value is None:  # pragma: no cover
            raise OSError("CreatePipe returned an invalid handle")
        read_value = int(read_handle.value)
        write_value = int(write_handle.value)
        parent_handle = read_value if parent_is_read else write_value
        try:
            if not self._set_handle_information(parent_handle, _HANDLE_FLAG_INHERIT, 0):
                self._raise_last_error("SetHandleInformation")
        except BaseException:
            with contextlib.suppress(BaseException):
                self.close_handle(read_value)
            with contextlib.suppress(BaseException):
                self.close_handle(write_value)
            raise
        return read_value, write_value

    def open_null_input(self) -> int:
        security = _inheritable_security_attributes()
        handle = cast(
            int | None,
            self._create_file(
                "NUL",
                _GENERIC_READ,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                ctypes.byref(security),
                _OPEN_EXISTING,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            ),
        )
        invalid = ctypes.c_void_p(-1).value
        if handle is None or handle == 0 or handle == invalid:
            self._raise_last_error("CreateFileW(NUL)")
        return handle

    def create_process(
        self,
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
    ) -> _CreatedProcess:
        if not inherited_handles:
            raise ValueError("inherited_handles must not be empty")
        attribute_bytes = ctypes.c_size_t()
        self._initialize_attribute_list(None, 2, 0, ctypes.byref(attribute_bytes))
        if attribute_bytes.value == 0:
            self._raise_last_error("InitializeProcThreadAttributeList(size)")

        attribute_storage = ctypes.create_string_buffer(attribute_bytes.value)
        attribute_list = ctypes.cast(attribute_storage, ctypes.c_void_p)
        if not self._initialize_attribute_list(
            attribute_list,
            2,
            0,
            ctypes.byref(attribute_bytes),
        ):
            self._raise_last_error("InitializeProcThreadAttributeList")

        job_handles = (ctypes.c_void_p * 1)(job_handle)
        handle_values = (ctypes.c_void_p * len(inherited_handles))(*inherited_handles)
        try:
            if not self._update_attribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                ctypes.cast(job_handles, ctypes.c_void_p),
                ctypes.sizeof(job_handles),
                None,
                None,
            ):
                self._raise_last_error("UpdateProcThreadAttribute(job list)")
            if not self._update_attribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_values, ctypes.c_void_p),
                ctypes.sizeof(handle_values),
                None,
                None,
            ):
                self._raise_last_error("UpdateProcThreadAttribute(handle list)")

            startup = _StartupInfoExW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = stdin_handle
            startup.StartupInfo.hStdOutput = stdout_handle
            startup.StartupInfo.hStdError = stderr_handle
            startup.lpAttributeList = attribute_list.value
            process = _ProcessInformation()
            mutable_command = ctypes.create_unicode_buffer(command_line)
            environment = ctypes.create_unicode_buffer(_environment_block(env))
            created = self._create_process(
                application_name,
                mutable_command,
                None,
                None,
                True,
                _EXTENDED_STARTUPINFO_PRESENT
                | _CREATE_UNICODE_ENVIRONMENT
                | _CREATE_NEW_PROCESS_GROUP
                | _CREATE_NO_WINDOW,
                ctypes.cast(environment, ctypes.c_void_p),
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process),
            )
            if not created:
                self._raise_last_error("CreateProcessW")
        finally:
            self._delete_attribute_list(attribute_list)

        if (
            process.hProcess is None or process.hThread is None or process.dwProcessId == 0
        ):  # pragma: no cover - defensive on Windows
            raise OSError("CreateProcessW returned invalid process information")
        return _CreatedProcess(
            int(process.hProcess),
            int(process.hThread),
            int(process.dwProcessId),
        )

    def read_file(self, handle: int, byte_count: int) -> bytes:
        buffer = ctypes.create_string_buffer(byte_count)
        bytes_read = ctypes.c_uint32()
        if not self._read_file(
            handle,
            buffer,
            byte_count,
            ctypes.byref(bytes_read),
            None,
        ):
            error_code = self._last_error()
            if error_code in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
                return b""
            raise OSError(error_code, f"ReadFile failed with Windows error {error_code}")
        return buffer.raw[: bytes_read.value]

    def write_file(self, handle: int, data: bytes) -> int:
        if not data:
            return 0
        byte_count = min(len(data), _DWORD_MAX)
        buffer = ctypes.create_string_buffer(data[:byte_count], byte_count)
        bytes_written = ctypes.c_uint32()
        if not self._write_file(
            handle,
            buffer,
            byte_count,
            ctypes.byref(bytes_written),
            None,
        ):
            error_code = self._last_error()
            if error_code in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
                raise BrokenPipeError(error_code, "process stdin pipe is closed")
            raise OSError(error_code, f"WriteFile failed with Windows error {error_code}")
        return int(bytes_written.value)

    def wait_process(self, handle: int) -> None:
        result = cast(int, self._wait_for_single_object(handle, _INFINITE))
        if result != _WAIT_OBJECT_0:
            self._raise_last_error("WaitForSingleObject")

    def get_exit_code(self, handle: int) -> int:
        exit_code = ctypes.c_uint32()
        if not self._get_exit_code_process(handle, ctypes.byref(exit_code)):
            self._raise_last_error("GetExitCodeProcess")
        if exit_code.value == _STILL_ACTIVE:
            raise OSError("GetExitCodeProcess reported a process that is still active")
        return int(exit_code.value)

    def terminate_process(self, handle: int, exit_code: int) -> None:
        if not self._terminate_process(handle, exit_code):
            self._raise_last_error("TerminateProcess")

    def close_handle(self, handle: int) -> None:
        if not self._close_handle(handle):
            self._raise_last_error("CloseHandle")

    def _last_error(self) -> int:
        return cast(int, self._get_last_error())

    def _raise_last_error(self, operation: str) -> NoReturn:
        error_code = self._last_error()
        raise OSError(error_code, f"{operation} failed with Windows error {error_code}")


class WindowsJobProcess:
    """Async stream projection of a process atomically created inside a Job.

    提供在 Job 内原子创建的进程的异步流投影."""

    def __init__(
        self,
        *,
        api: _WindowsJobProcessApi,
        created: _CreatedProcess,
        stdin_handle: int | None,
        stdout_handle: int,
        stderr_handle: int | None,
    ) -> None:
        self.pid = created.process_id
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader() if stderr_handle is not None else None
        self.stdin = None
        self._api = api
        self._process_handle: int | None = created.process_handle
        self._returncode: int | None = None
        self._wait_error: BaseException | None = None
        self._handle_lock = threading.Lock()
        self._stdin_handle: int | None = stdin_handle
        self._stdin_lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        self._reader_threads = [
            self._reader_thread(stdout_handle, self.stdout, "stdout"),
        ]
        if stderr_handle is not None and self.stderr is not None:
            self._reader_threads.append(self._reader_thread(stderr_handle, self.stderr, "stderr"))
        for thread in self._reader_threads:
            thread.start()
        self._waiter_thread = threading.Thread(
            target=self._wait_in_thread,
            name=f"neuro-code-windows-wait-{self.pid}",
            daemon=True,
        )
        self._waiter_thread.start()

    @classmethod
    def spawn_exec(
        cls,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        job_handle: int,
        merge_output: bool = False,
        pipe_stdin: bool = False,
        api: _WindowsJobProcessApi | None = None,
    ) -> Self:
        if isinstance(arguments, str | bytes):
            raise TypeError("arguments must be a sequence of strings")
        argv = _validated_arguments((executable, *arguments))
        return cls._spawn(
            application_name=argv[0],
            command_line=subprocess.list2cmdline(argv),
            cwd=cwd,
            env=env,
            job_handle=job_handle,
            merge_output=merge_output,
            pipe_stdin=pipe_stdin,
            api=api,
        )

    @classmethod
    def spawn_shell(
        cls,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        job_handle: int,
        merge_output: bool = False,
        pipe_stdin: bool = False,
        api: _WindowsJobProcessApi | None = None,
    ) -> Self:
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        if not command.strip() or "\x00" in command:
            raise ValueError("command must be non-empty and contain no null bytes")
        _environment_block(env)
        # ``env`` is the deliberately minimal child environment.  Shell
        # resolution is a trusted launcher concern and must not depend on
        # values (or PATH lookup) supplied to the untrusted child.
        # ``env`` 是刻意精简的子进程环境. Shell 定位属于受信任启动器职责,
        # 不得依赖不可信子进程提供的值(或 PATH 查找).
        comspec = _windows_shell()
        return cls._spawn(
            application_name=comspec,
            command_line=f'{comspec} /c "{command}"',
            cwd=cwd,
            env=env,
            job_handle=job_handle,
            merge_output=merge_output,
            pipe_stdin=pipe_stdin,
            api=api,
        )

    @classmethod
    def _spawn(
        cls,
        *,
        application_name: str,
        command_line: str,
        cwd: Path,
        env: Mapping[str, str],
        job_handle: int,
        merge_output: bool,
        pipe_stdin: bool,
        api: _WindowsJobProcessApi | None,
    ) -> Self:
        if not isinstance(cwd, Path):
            raise TypeError("cwd must be a pathlib.Path")
        if "\x00" in str(cwd):
            raise ValueError("cwd must not contain a null byte")
        if isinstance(job_handle, bool) or not isinstance(job_handle, int) or job_handle <= 0:
            raise ValueError("job_handle must be positive")
        _environment_block(env)

        process_api = _NativeWindowsJobProcessApi() if api is None else api
        stdout_read: int | None = None
        stdout_write: int | None = None
        stderr_read: int | None = None
        stderr_write: int | None = None
        stdin_child: int | None = None
        stdin_parent: int | None = None
        created: _CreatedProcess | None = None
        try:
            stdout_read, stdout_write = process_api.create_output_pipe()
            if merge_output:
                stderr_write = stdout_write
            else:
                stderr_read, stderr_write = process_api.create_output_pipe()
            if pipe_stdin:
                stdin_child, stdin_parent = process_api.create_input_pipe()
            else:
                stdin_child = process_api.open_null_input()
            inherited = tuple(dict.fromkeys((stdin_child, stdout_write, stderr_write)))
            created = process_api.create_process(
                application_name=application_name,
                command_line=command_line,
                cwd=cwd,
                env=env,
                stdin_handle=stdin_child,
                stdout_handle=stdout_write,
                stderr_handle=stderr_write,
                inherited_handles=inherited,
                job_handle=job_handle,
            )
            process_api.close_handle(created.thread_handle)
            created = _CreatedProcess(created.process_handle, 0, created.process_id)
            process_api.close_handle(stdin_child)
            stdin_child = None
            process_api.close_handle(stdout_write)
            stdout_write = None
            if merge_output:
                stderr_write = None
            else:
                process_api.close_handle(stderr_write)
                stderr_write = None

            process = cls(
                api=process_api,
                created=created,
                stdin_handle=stdin_parent,
                stdout_handle=stdout_read,
                stderr_handle=stderr_read,
            )
            stdin_parent = None
            stdout_read = None
            stderr_read = None
            created = None
            return process
        except BaseException:
            if created is not None:
                if created.thread_handle:
                    with contextlib.suppress(BaseException):
                        process_api.close_handle(created.thread_handle)
                with contextlib.suppress(BaseException):
                    process_api.terminate_process(created.process_handle, 1)
                with contextlib.suppress(BaseException):
                    process_api.wait_process(created.process_handle)
                with contextlib.suppress(BaseException):
                    process_api.close_handle(created.process_handle)
            for handle in _unique_handles(
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
                stdin_child,
                stdin_parent,
            ):
                with contextlib.suppress(BaseException):
                    process_api.close_handle(handle)
            raise

    async def write_stdin(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("process stdin data must be bytes")
        if not data:
            return
        async with self._stdin_lock:
            handle = self._stdin_handle
            if handle is None:
                raise RuntimeError("process stdin is not piped or has been closed")
            await run_blocking(self._write_all, handle, data)

    async def close_stdin(self) -> None:
        async with self._stdin_lock:
            handle = self._stdin_handle
            if handle is None:
                return
            self._stdin_handle = None
            # CloseHandle is a short, non-blocking kernel operation. Keeping it
            # on the event-loop thread also avoids racing executor shutdown
            # while the process's native waiter completes.
            self._api.close_handle(handle)

    def _write_all(self, handle: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self._api.write_file(handle, data[offset:])
            if written <= 0 or written > len(data) - offset:
                raise OSError("WriteFile returned an invalid byte count")
            offset += written

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        while self._returncode is None and self._wait_error is None:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        if self._wait_error is not None:
            raise self._wait_error
        if self._returncode is None:  # pragma: no cover - loop invariant
            raise RuntimeError("Windows process wait completed without an exit code")
        return self._returncode

    def terminate(self) -> None:
        self._terminate(exit_code=1)

    def kill(self) -> None:
        self._terminate(exit_code=1)

    def _terminate(self, *, exit_code: int) -> None:
        with self._handle_lock:
            handle = self._process_handle
            if handle is None or self._returncode is not None:
                return
            self._api.terminate_process(handle, exit_code)

    def _wait_in_thread(self) -> None:
        try:
            exit_code = self._wait_and_close_process()
        except BaseException as error:
            self._wait_error = error
        else:
            self._returncode = exit_code

    def _wait_and_close_process(self) -> int:
        with self._handle_lock:
            handle = self._process_handle
        if handle is None:
            if self._returncode is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("Windows process handle closed before exit was observed")
            return self._returncode

        try:
            self._api.wait_process(handle)
            return self._api.get_exit_code(handle)
        finally:
            with self._handle_lock:
                if self._process_handle == handle:
                    self._process_handle = None
                    self._api.close_handle(handle)

    def _reader_thread(
        self,
        handle: int,
        stream: asyncio.StreamReader,
        stream_name: str,
    ) -> threading.Thread:
        return threading.Thread(
            target=self._drain_stream,
            args=(handle, stream),
            name=f"neuro-code-windows-{stream_name}-{self.pid}",
            daemon=True,
        )

    def _drain_stream(self, handle: int, stream: asyncio.StreamReader) -> None:
        error: BaseException | None = None
        try:
            while chunk := self._api.read_file(handle, 65_536):
                self._call_stream(stream.feed_data, chunk)
        except BaseException as caught:
            error = caught
        finally:
            try:
                self._api.close_handle(handle)
            except BaseException as caught:
                if error is None:
                    error = caught
            if error is None:
                self._call_stream(stream.feed_eof)
            else:
                self._call_stream(stream.set_exception, error)

    def _call_stream(self, callback: Callable[..., object], *arguments: object) -> None:
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(callback, *arguments)


def _inheritable_security_attributes() -> _SecurityAttributes:
    security = _SecurityAttributes()
    security.nLength = ctypes.sizeof(security)
    security.bInheritHandle = True
    return security


def _validated_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    if isinstance(arguments, str | bytes):
        raise TypeError("arguments must be a sequence of strings")
    argv = tuple(arguments)
    if any(not isinstance(argument, str) for argument in argv):
        raise TypeError("arguments must contain only strings")
    if not argv or not argv[0]:
        raise ValueError("the executable must not be empty")
    if any("\x00" in argument for argument in argv):
        raise ValueError("arguments must not contain null bytes")
    return argv


def _windows_shell(host_environment: Mapping[str, str] | None = None) -> str:
    """Return an absolute trusted Windows shell path without using child env.

    The optional mapping is only a testable projection of the trusted host
    environment; it is never the environment passed to the child process.

    可选映射只是受信任宿主环境的可测试投影,绝不会作为环境传给子进程.
    """

    source = os.environ if host_environment is None else host_environment
    folded = {name.casefold(): value for name, value in source.items()}
    comspec = folded.get("comspec")
    system_root = folded.get("systemroot", "")
    system_shell = ntpath.join(system_root, "System32", "cmd.exe") if system_root else ""
    if comspec and system_shell:
        if ntpath.normcase(ntpath.normpath(comspec)) != ntpath.normcase(
            ntpath.normpath(system_shell)
        ):
            raise FileNotFoundError("shell not found: %ComSpec% is not the trusted system shell")
    elif not comspec:
        comspec = system_shell
    if not comspec or not ntpath.isabs(comspec) or "\x00" in comspec:
        raise FileNotFoundError(
            "shell not found: neither an absolute %ComSpec% nor %SystemRoot% is set"
        )
    if ntpath.basename(comspec).casefold() != "cmd.exe":
        raise FileNotFoundError("shell not found: trusted shell must be cmd.exe")
    return comspec


def _unique_handles(*handles: int | None) -> tuple[int, ...]:
    return tuple(dict.fromkeys(handle for handle in handles if handle is not None))


__all__ = ["WindowsJobProcess"]
