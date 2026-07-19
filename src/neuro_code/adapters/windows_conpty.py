from __future__ import annotations

import contextlib
import ctypes
import math
import os
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, Self, cast

from neuro_code.adapters.windows_job import WindowsJobObject
from neuro_code.adapters.windows_process import (
    windows_environment_block as _environment_block,
)

_DWORD_MAX = (1 << 32) - 1
_COORD_MAX = (1 << 15) - 1
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_int16), ("Y", ctypes.c_int16)]


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


class _WindowsConPtyApi(Protocol):
    def create_pipe(self) -> tuple[int, int]: ...

    def create_pseudo_console(
        self,
        columns: int,
        rows: int,
        input_read_handle: int,
        output_write_handle: int,
    ) -> int: ...

    def create_process(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        pseudo_console_handle: int,
        job_handle: int | None = None,
    ) -> _CreatedProcess: ...

    def resize_pseudo_console(self, handle: int, columns: int, rows: int) -> None: ...

    def close_pseudo_console(self, handle: int) -> None: ...

    def read_file(self, handle: int, byte_count: int) -> bytes: ...

    def write_file(self, handle: int, data: bytes) -> int: ...

    def wait_process(self, handle: int, timeout_milliseconds: int) -> bool: ...

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


class _NativeWindowsConPtyApi:
    """Typed, synchronous kernel32 facade used by the ConPTY owner."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows pseudoconsoles are only available on Windows")

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
                    ctypes.c_void_p,
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
        except AttributeError as error:  # pragma: no cover - old Windows only
            raise OSError(
                "this Windows version does not provide the required ConPTY APIs"
            ) from error

    def create_pipe(self) -> tuple[int, int]:
        read_handle = ctypes.c_void_p()
        write_handle = ctypes.c_void_p()
        if not self._create_pipe(
            ctypes.byref(read_handle),
            ctypes.byref(write_handle),
            None,
            0,
        ):
            self._raise_last_error("CreatePipe")
        if read_handle.value is None or write_handle.value is None:  # pragma: no cover
            raise OSError("CreatePipe returned an invalid handle")
        return int(read_handle.value), int(write_handle.value)

    def create_pseudo_console(
        self,
        columns: int,
        rows: int,
        input_read_handle: int,
        output_write_handle: int,
    ) -> int:
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
            self._raise_hresult("CreatePseudoConsole", result)
        if handle.value is None:  # pragma: no cover - defensive on Windows
            raise OSError("CreatePseudoConsole returned an invalid handle")
        return int(handle.value)

    def create_process(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        pseudo_console_handle: int,
        job_handle: int | None = None,
    ) -> _CreatedProcess:
        attribute_count = 2 if job_handle is not None else 1
        attribute_bytes = ctypes.c_size_t()
        self._initialize_attribute_list(
            None,
            attribute_count,
            0,
            ctypes.byref(attribute_bytes),
        )
        if attribute_bytes.value == 0:
            self._raise_last_error("InitializeProcThreadAttributeList(size)")

        attribute_storage = ctypes.create_string_buffer(attribute_bytes.value)
        attribute_list = ctypes.cast(attribute_storage, ctypes.c_void_p)
        if not self._initialize_attribute_list(
            attribute_list,
            attribute_count,
            0,
            ctypes.byref(attribute_bytes),
        ):
            self._raise_last_error("InitializeProcThreadAttributeList")

        try:
            if not self._update_attribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                pseudo_console_handle,
                ctypes.sizeof(ctypes.c_void_p),
                None,
                None,
            ):
                self._raise_last_error("UpdateProcThreadAttribute(pseudoconsole)")
            if job_handle is not None:
                job_handles = (ctypes.c_void_p * 1)(job_handle)
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

            startup = _StartupInfoExW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # Do not let a console-hosting parent leak its standard handles
            # into the child. ConPTY replaces these null placeholders while
            # attaching the new process to the pseudoconsole.
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = None
            startup.StartupInfo.hStdOutput = None
            startup.StartupInfo.hStdError = None
            startup.lpAttributeList = attribute_list.value
            process = _ProcessInformation()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
            environment = ctypes.create_unicode_buffer(_environment_block(env))
            created = self._create_process(
                arguments[0],
                command_line,
                None,
                None,
                False,
                _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
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

    def resize_pseudo_console(self, handle: int, columns: int, rows: int) -> None:
        result = cast(int, self._resize_pseudo_console(handle, _Coord(columns, rows)))
        if result != 0:
            self._raise_hresult("ResizePseudoConsole", result)

    def close_pseudo_console(self, handle: int) -> None:
        self._close_pseudo_console(handle)

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
        buffer = ctypes.create_string_buffer(data)
        bytes_written = ctypes.c_uint32()
        if not self._write_file(
            handle,
            buffer,
            len(data),
            ctypes.byref(bytes_written),
            None,
        ):
            self._raise_last_error("WriteFile")
        return int(bytes_written.value)

    def wait_process(self, handle: int, timeout_milliseconds: int) -> bool:
        result = cast(int, self._wait_for_single_object(handle, timeout_milliseconds))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
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

    @staticmethod
    def _raise_hresult(operation: str, result: int) -> NoReturn:
        unsigned = result & _DWORD_MAX
        raise OSError(unsigned, f"{operation} failed with HRESULT 0x{unsigned:08x}")


class _BoundedCapture:
    def __init__(self, byte_limit: int) -> None:
        if byte_limit <= 0:
            raise ValueError("max_output_bytes must be positive")
        self._head_limit = max(1, byte_limit // 2)
        self._tail_limit = byte_limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0
        self.byte_limit = byte_limit

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.byte_limit

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        head_remaining = self._head_limit - len(self._head)
        if head_remaining > 0:
            self._head.extend(chunk[:head_remaining])
            chunk = chunk[head_remaining:]
        if not chunk or self._tail_limit == 0:
            return
        self._tail.extend(chunk)
        overflow = len(self._tail) - self._tail_limit
        if overflow > 0:
            del self._tail[:overflow]

    def render(self) -> bytes:
        return bytes(self._head + self._tail)


class WindowsPseudoConsoleSession:
    """Own a ConPTY, its hosted process, pipes, and continuously drained output."""

    def __init__(
        self,
        *,
        api: _WindowsConPtyApi,
        pseudo_console_handle: int,
        input_write_handle: int,
        output_read_handle: int,
        process_handle: int,
        process_id: int,
        job: WindowsJobObject | None,
        max_output_bytes: int,
        on_output: Callable[[bytes], None] | None,
        on_eof: Callable[[], None] | None,
        on_error: Callable[[BaseException], None] | None,
    ) -> None:
        self._api = api
        self._pseudo_console_handle: int | None = pseudo_console_handle
        self._input_write_handle: int | None = input_write_handle
        self._output_read_handle: int | None = output_read_handle
        self._process_handle: int | None = process_handle
        self._process_id = process_id
        self._job: WindowsJobObject | None = job
        self._exit_code: int | None = None
        self._closed = False
        self._lifecycle_lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._capture = _BoundedCapture(max_output_bytes)
        self._reader_error: BaseException | None = None
        self._on_output = on_output
        self._on_eof = on_eof
        self._on_error = on_error
        self._reader_stopping = threading.Event()
        self._reader = threading.Thread(
            target=self._drain_output,
            args=(output_read_handle,),
            name=f"neuro-code-conpty-{process_id}",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def spawn(
        cls,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        columns: int = 100,
        rows: int = 30,
        max_output_bytes: int = 8 * 1024 * 1024,
        api: _WindowsConPtyApi | None = None,
        job: WindowsJobObject | None = None,
        on_output: Callable[[bytes], None] | None = None,
        on_eof: Callable[[], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> Self:
        """Create a pseudoconsole and start one hosted process inside it."""

        argv = _validated_arguments(arguments)
        _validate_dimensions(columns, rows)
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        if not isinstance(cwd, Path):
            raise TypeError("cwd must be a pathlib.Path")
        if "\x00" in str(cwd):
            raise ValueError("cwd must not contain a null byte")
        _environment_block(env)

        native_api = api is None
        conpty_api: _WindowsConPtyApi = _NativeWindowsConPtyApi() if api is None else api
        owned_job = WindowsJobObject.create() if native_api and job is None else job
        input_read: int | None = None
        input_write: int | None = None
        output_read: int | None = None
        output_write: int | None = None
        pseudo_console: int | None = None
        created_process: _CreatedProcess | None = None
        try:
            input_read, input_write = conpty_api.create_pipe()
            output_read, output_write = conpty_api.create_pipe()
            pseudo_console = conpty_api.create_pseudo_console(
                columns,
                rows,
                input_read,
                output_write,
            )
            created_process = conpty_api.create_process(
                argv,
                cwd=cwd,
                env=env,
                pseudo_console_handle=pseudo_console,
                job_handle=(owned_job.process_creation_handle if owned_job is not None else None),
            )

            thread_handle = created_process.thread_handle
            created_process = _CreatedProcess(
                created_process.process_handle,
                0,
                created_process.process_id,
            )
            conpty_api.close_handle(thread_handle)

            pseudo_input = input_read
            input_read = None
            conpty_api.close_handle(pseudo_input)
            pseudo_output = output_write
            output_write = None
            conpty_api.close_handle(pseudo_output)

            session = cls(
                api=conpty_api,
                pseudo_console_handle=pseudo_console,
                input_write_handle=input_write,
                output_read_handle=output_read,
                process_handle=created_process.process_handle,
                process_id=created_process.process_id,
                job=owned_job,
                max_output_bytes=max_output_bytes,
                on_output=on_output,
                on_eof=on_eof,
                on_error=on_error,
            )
            pseudo_console = None
            input_write = None
            output_read = None
            created_process = None
            owned_job = None
            return session
        except BaseException:
            if created_process is not None:
                if created_process.thread_handle:
                    with contextlib.suppress(BaseException):
                        conpty_api.close_handle(created_process.thread_handle)
                if owned_job is not None:
                    with contextlib.suppress(BaseException):
                        owned_job.terminate(1)
                else:
                    with contextlib.suppress(BaseException):
                        conpty_api.terminate_process(created_process.process_handle, 1)
                with contextlib.suppress(BaseException):
                    conpty_api.wait_process(created_process.process_handle, 5_000)
                with contextlib.suppress(BaseException):
                    conpty_api.close_handle(created_process.process_handle)
            # Closing the host read end first prevents an older Windows build
            # from blocking ClosePseudoConsole while no reader thread exists.
            if output_read is not None:
                with contextlib.suppress(BaseException):
                    conpty_api.close_handle(output_read)
            if pseudo_console is not None:
                with contextlib.suppress(BaseException):
                    conpty_api.close_pseudo_console(pseudo_console)
            for handle in (input_write, input_read, output_write):
                if handle is not None:
                    with contextlib.suppress(BaseException):
                        conpty_api.close_handle(handle)
            if owned_job is not None:
                with contextlib.suppress(BaseException):
                    owned_job.close()
            raise

    @property
    def process_id(self) -> int:
        return self._process_id

    @property
    def output(self) -> bytes:
        with self._capture_lock:
            return self._capture.render()

    @property
    def output_truncated(self) -> bool:
        with self._capture_lock:
            return self._capture.truncated

    def write(self, data: bytes) -> None:
        """Write raw UTF-8/virtual-terminal input to the hosted console."""

        if not isinstance(data, bytes):
            raise TypeError("ConPTY input must be bytes")
        if not data:
            return
        with self._lifecycle_lock:
            handle = self._open_input_handle()
            offset = 0
            while offset < len(data):
                written = self._api.write_file(handle, data[offset:])
                if written <= 0 or written > len(data) - offset:
                    raise OSError("WriteFile returned an invalid byte count")
                offset += written

    def resize(self, columns: int, rows: int) -> None:
        """Resize the ConPTY buffer reported to hosted console applications."""

        _validate_dimensions(columns, rows)
        with self._lifecycle_lock:
            self._api.resize_pseudo_console(
                self._open_pseudo_console_handle(),
                columns,
                rows,
            )

    def wait(self, timeout_seconds: float) -> int | None:
        """Return the hosted process exit code, or ``None`` at the deadline."""

        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be finite and not negative")
        with self._lifecycle_lock:
            if self._exit_code is not None:
                return self._exit_code
            process_handle = self._open_process_handle()
            milliseconds = min(math.ceil(timeout_seconds * 1_000), _DWORD_MAX - 1)
            if not self._api.wait_process(process_handle, milliseconds):
                return None
            self._exit_code = self._api.get_exit_code(process_handle)
            return self._exit_code

    @property
    def running(self) -> bool:
        return self.wait(0) is None

    def terminate(self, exit_code: int = 1) -> None:
        """Terminate the complete hosted Job, or the direct test-seam process."""

        if exit_code < 0 or exit_code > _DWORD_MAX:
            raise ValueError("exit_code must be an unsigned 32-bit integer")
        with self._lifecycle_lock:
            if self._exit_code is not None:
                return
            process_handle = self._open_process_handle()
            if self._api.wait_process(process_handle, 0):
                self._exit_code = self._api.get_exit_code(process_handle)
                return
            if self._job is not None:
                self._job.terminate(exit_code)
            else:
                self._api.terminate_process(process_handle, exit_code)

    def close(self) -> None:
        """Close every owned resource while the output reader remains live."""

        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            process_handle = self._process_handle
            self._process_handle = None
            job = self._job
            self._job = None
            input_handle = self._input_write_handle
            self._input_write_handle = None
            pseudo_console = self._pseudo_console_handle
            self._pseudo_console_handle = None
            output_handle = self._output_read_handle
            self._output_read_handle = None

            errors: list[BaseException] = []

            def attempt(action: Callable[[], object]) -> None:
                try:
                    action()
                except BaseException as error:
                    errors.append(error)

            if process_handle is not None:
                try:
                    process_done = self._api.wait_process(process_handle, 0)
                except BaseException as error:
                    errors.append(error)
                    process_done = False
                if not process_done:
                    if job is not None:
                        attempt(lambda: job.terminate(1))
                    else:
                        attempt(lambda: self._api.terminate_process(process_handle, 1))
                    attempt(lambda: self._api.wait_process(process_handle, 5_000))
                if self._exit_code is None:
                    try:
                        if self._api.wait_process(process_handle, 0):
                            self._exit_code = self._api.get_exit_code(process_handle)
                    except BaseException as error:
                        errors.append(error)

            if input_handle is not None:
                attempt(lambda: self._api.close_handle(input_handle))
            # ClosePseudoConsole may emit a final frame. The reader thread stays
            # active until this call returns, avoiding the documented deadlock.
            if pseudo_console is not None:
                attempt(lambda: self._api.close_pseudo_console(pseudo_console))
            self._reader_stopping.set()
            self._reader.join(timeout=1)
            if output_handle is not None:
                attempt(lambda: self._api.close_handle(output_handle))
            self._reader.join(timeout=4)
            if self._reader.is_alive():
                errors.append(TimeoutError("ConPTY output reader did not stop during close"))
            elif self._reader_error is not None:
                errors.append(self._reader_error)
            if process_handle is not None:
                attempt(lambda: self._api.close_handle(process_handle))
            if job is not None:
                attempt(job.close)
            if errors:
                raise errors[0]

    def _drain_output(self, handle: int) -> None:
        while True:
            try:
                chunk = self._api.read_file(handle, 65_536)
            except OSError as error:
                if not self._reader_stopping.is_set():
                    self._reader_error = error
                    self._notify_error(error)
                else:
                    self._notify_eof()
                return
            if not chunk:
                self._notify_eof()
                return
            with self._capture_lock:
                self._capture.append(chunk)
            if self._on_output is not None:
                try:
                    self._on_output(chunk)
                except BaseException as error:
                    self._reader_error = error
                    self._notify_error(error)
                    # Keep draining even when an observer fails. Otherwise
                    # ClosePseudoConsole can block on older Windows builds.
                    self._on_output = None

    def _notify_eof(self) -> None:
        if self._on_eof is not None:
            try:
                self._on_eof()
            except BaseException as error:
                self._reader_error = error
                self._notify_error(error)

    def _notify_error(self, error: BaseException) -> None:
        if self._on_error is not None:
            with contextlib.suppress(BaseException):
                self._on_error(error)

    def _open_input_handle(self) -> int:
        if self._closed or self._input_write_handle is None:
            raise RuntimeError("Windows pseudoconsole input is closed")
        return self._input_write_handle

    def _open_pseudo_console_handle(self) -> int:
        if self._closed or self._pseudo_console_handle is None:
            raise RuntimeError("Windows pseudoconsole is closed")
        return self._pseudo_console_handle

    def _open_process_handle(self) -> int:
        if self._closed or self._process_handle is None:
            raise RuntimeError("Windows pseudoconsole process is closed")
        return self._process_handle

    def __enter__(self) -> Self:
        self._open_pseudo_console_handle()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(BaseException):
            self.close()


def _validated_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    if isinstance(arguments, str | bytes):
        raise TypeError("arguments must be a sequence of strings")
    argv = tuple(arguments)
    if not argv:
        raise ValueError("arguments must not be empty")
    if any(not isinstance(argument, str) for argument in argv):
        raise TypeError("arguments must contain only strings")
    if any("\x00" in argument for argument in argv):
        raise ValueError("arguments must not contain null bytes")
    if not argv[0]:
        raise ValueError("the executable must not be empty")
    return argv


def _validate_dimensions(columns: int, rows: int) -> None:
    if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= _COORD_MAX:
        raise ValueError(f"columns must be an integer from 1 to {_COORD_MAX}")
    if isinstance(rows, bool) or not isinstance(rows, int) or not 1 <= rows <= _COORD_MAX:
        raise ValueError(f"rows must be an integer from 1 to {_COORD_MAX}")


__all__ = ["WindowsPseudoConsoleSession"]
