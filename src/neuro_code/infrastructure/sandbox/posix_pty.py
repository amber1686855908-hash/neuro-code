from __future__ import annotations

import contextlib
import errno
import os
import signal
import struct
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

from neuro_code.application.ports.terminal import (
    TerminalEofHandler,
    TerminalErrorHandler,
    TerminalOutputHandler,
    TerminalPlatformSession,
)
from neuro_code.domain.terminal.models import TerminalSignal, TerminalSize


class PosixPtySession:
    """Own a POSIX PTY master, session process group, and drain threads.

    管理 POSIX PTY 主端、会话进程组和排空线程."""

    def __init__(
        self,
        *,
        master_fd: int,
        process: subprocess.Popen[bytes],
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> None:
        self._master_fd: int | None = master_fd
        self._process = process
        self._size = size
        self._on_output = on_output
        self._on_eof = on_eof
        self._on_error = on_error
        self._closed = False
        self._closing = threading.Event()
        self._lock = threading.RLock()
        self._reader = threading.Thread(
            target=self._drain_output,
            name=f"neuro-code-posix-pty-read-{process.pid}",
            daemon=True,
        )
        self._waiter = threading.Thread(
            target=self._wait_for_exit,
            name=f"neuro-code-posix-pty-wait-{process.pid}",
            daemon=True,
        )
        self._reader.start()
        self._waiter.start()

    @classmethod
    def spawn(
        cls,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> Self:
        if os.name != "posix":
            raise OSError("POSIX pseudoterminals are only available on POSIX")
        argv = _validated_arguments(executable, arguments)
        _validated_environment(env)
        if not isinstance(cwd, Path):
            raise TypeError("cwd must be a pathlib.Path")

        import fcntl
        import pty
        import termios

        master_fd: int | None = None
        slave_fd: int | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", size.rows, size.columns, 0, 0),
            )
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
            if process.pid <= 1 or process.pid == os.getpgrp():
                raise OSError(f"unsafe PTY process-group id: {process.pid}")
            os.close(slave_fd)
            slave_fd = None
            session = cls(
                master_fd=master_fd,
                process=process,
                size=size,
                on_output=on_output,
                on_eof=on_eof,
                on_error=on_error,
            )
            master_fd = None
            process = None
            return session
        except BaseException:
            if process is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(BaseException):
                    process.wait(timeout=5)
            for fd in (slave_fd, master_fd):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise

    @property
    def process_id(self) -> int:
        return self._process.pid

    def write(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("terminal input must be bytes")
        if not data:
            return
        with self._lock:
            fd = self._open_master()
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0 or written > len(data) - offset:
                    raise OSError("PTY write returned an invalid byte count")
                offset += written

    def resize(self, size: TerminalSize) -> None:
        if not isinstance(size, TerminalSize):
            raise TypeError("size must be a TerminalSize")
        import fcntl
        import termios

        with self._lock:
            fcntl.ioctl(
                self._open_master(),
                termios.TIOCSWINSZ,
                struct.pack("HHHH", size.rows, size.columns, 0, 0),
            )
            self._size = size

    def send_signal(self, terminal_signal: TerminalSignal) -> None:
        if not isinstance(terminal_signal, TerminalSignal):
            raise TypeError("signal must be a TerminalSignal")
        native = {
            TerminalSignal.INTERRUPT: signal.SIGINT,
            TerminalSignal.TERMINATE: signal.SIGTERM,
            TerminalSignal.KILL: signal.SIGKILL,
        }[terminal_signal]
        with self._lock:
            if self._closed:
                raise RuntimeError("POSIX pseudoterminal is closed")
            self._signal_group(native)

    def poll_exit(self) -> int | None:
        return self._process.poll()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._closing.set()
            master_fd = self._master_fd
            self._master_fd = None

            errors: list[BaseException] = []
            try:
                self._signal_group(signal.SIGTERM)
                deadline = time.monotonic() + 1.0
                while self._process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self._signal_group(signal.SIGKILL)
            except BaseException as error:
                errors.append(error)
            try:
                self._process.wait(timeout=5)
            except BaseException as error:
                errors.append(error)
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError as error:
                    errors.append(error)

        self._reader.join(timeout=5)
        self._waiter.join(timeout=5)
        if self._reader.is_alive() or self._waiter.is_alive():
            errors.append(TimeoutError("POSIX pseudoterminal threads did not stop"))
        if errors:
            raise errors[0]

    def _open_master(self) -> int:
        if self._closed or self._master_fd is None:
            raise RuntimeError("POSIX pseudoterminal is closed")
        return self._master_fd

    def _signal_group(self, native_signal: signal.Signals) -> None:
        try:
            os.killpg(self._process.pid, native_signal)
        except ProcessLookupError:
            return

    def _drain_output(self) -> None:
        while True:
            with self._lock:
                master_fd = self._master_fd
            if master_fd is None:
                self._notify_eof()
                return
            try:
                chunk = os.read(master_fd, 65_536)
            except OSError as error:
                if error.errno == errno.EIO or self._closing.is_set():
                    self._notify_eof()
                else:
                    self._notify_error(error)
                return
            if not chunk:
                self._notify_eof()
                return
            try:
                self._on_output(chunk)
            except BaseException as error:
                self._notify_error(error)
                return

    def _wait_for_exit(self) -> None:
        try:
            self._process.wait()
        except BaseException as error:
            self._notify_error(error)

    def _notify_eof(self) -> None:
        try:
            self._on_eof()
        except BaseException as error:
            self._notify_error(error)

    def _notify_error(self, error: BaseException) -> None:
        with contextlib.suppress(BaseException):
            self._on_error(error)

    def __del__(self) -> None:
        with contextlib.suppress(BaseException):
            self.close()


class PosixPtyPlatform:
    def spawn_exec(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession:
        return PosixPtySession.spawn(
            executable,
            arguments,
            cwd=cwd,
            env=env,
            size=size,
            on_output=on_output,
            on_eof=on_eof,
            on_error=on_error,
        )


def _validated_arguments(executable: str, arguments: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(executable, str):
        raise TypeError("executable must be a string")
    if isinstance(arguments, str | bytes):
        raise TypeError("arguments must be a sequence of strings")
    argv = (executable, *arguments)
    if not executable:
        raise ValueError("executable must not be empty")
    if any(not isinstance(argument, str) for argument in argv):
        raise TypeError("arguments must contain only strings")
    if any("\x00" in argument for argument in argv):
        raise ValueError("arguments must not contain null bytes")
    return argv


def _validated_environment(environment: Mapping[str, str]) -> None:
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not name
            or "=" in name
            or "\x00" in name
            or "\x00" in value
        ):
            raise ValueError("environment contains an invalid name or value")


__all__ = ["PosixPtyPlatform", "PosixPtySession"]
