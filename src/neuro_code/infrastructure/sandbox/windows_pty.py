from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from neuro_code.application.ports.terminal import (
    TerminalEofHandler,
    TerminalErrorHandler,
    TerminalOutputHandler,
    TerminalPlatformSession,
)
from neuro_code.domain.terminal.models import TerminalSignal, TerminalSize
from neuro_code.infrastructure.sandbox.windows_conpty import WindowsPseudoConsoleSession


class WindowsConPtySession:
    """Project the native ConPTY owner onto the shared terminal platform port.

    将原生 ConPTY 所有者投影到共享终端平台端口."""

    def __init__(self, session: WindowsPseudoConsoleSession, size: TerminalSize) -> None:
        self._session = session
        self._size = size

    @property
    def process_id(self) -> int:
        return self._session.process_id

    def write(self, data: bytes) -> None:
        self._session.write(data)

    def resize(self, size: TerminalSize) -> None:
        if not isinstance(size, TerminalSize):
            raise TypeError("size must be a TerminalSize")
        self._session.resize(size.columns, size.rows)
        self._size = size

    def send_signal(self, terminal_signal: TerminalSignal) -> None:
        if not isinstance(terminal_signal, TerminalSignal):
            raise TypeError("signal must be a TerminalSignal")
        if terminal_signal is TerminalSignal.INTERRUPT:
            self._session.write(b"\x03")
        else:
            self._session.terminate()

    def poll_exit(self) -> int | None:
        return self._session.wait(0)

    def close(self) -> None:
        self._session.close()


class WindowsConPtyPlatform:
    def __init__(self, *, diagnostic_capture_bytes: int = 65_536) -> None:
        if diagnostic_capture_bytes <= 0:
            raise ValueError("diagnostic_capture_bytes must be positive")
        self._diagnostic_capture_bytes = diagnostic_capture_bytes

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
        native = WindowsPseudoConsoleSession.spawn(
            (executable, *arguments),
            cwd=cwd,
            env=env,
            columns=size.columns,
            rows=size.rows,
            max_output_bytes=self._diagnostic_capture_bytes,
            on_output=on_output,
            on_eof=on_eof,
            on_error=on_error,
        )
        return WindowsConPtySession(native, size)


__all__ = ["WindowsConPtyPlatform", "WindowsConPtySession"]
