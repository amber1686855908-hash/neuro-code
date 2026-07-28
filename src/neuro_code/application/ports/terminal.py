"""Canonical interactive-terminal ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from neuro_code.domain.terminal import TerminalOutputChunk, TerminalSignal, TerminalSize


class InteractiveTerminalSession(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def process_id(self) -> int: ...

    @property
    def size(self) -> TerminalSize: ...

    async def read(
        self,
        *,
        after_offset: int = 0,
        max_bytes: int = 65_536,
        wait_seconds: float = 0.0,
    ) -> TerminalOutputChunk: ...

    async def write(self, data: bytes) -> None: ...

    async def resize(self, size: TerminalSize) -> None: ...

    async def send_signal(self, signal: TerminalSignal) -> None: ...

    async def wait(self, *, timeout_seconds: float | None = None) -> int | None: ...

    async def close(self) -> None: ...


class InteractiveTerminalManager(Protocol):
    async def create_exec(
        self,
        call_id: str,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        size: TerminalSize,
        output_capacity: int,
    ) -> InteractiveTerminalSession: ...

    async def shutdown(self) -> None: ...


class TerminalPlatformSession(Protocol):
    @property
    def process_id(self) -> int: ...

    def write(self, data: bytes) -> None: ...

    def resize(self, size: TerminalSize) -> None: ...

    def send_signal(self, signal: TerminalSignal) -> None: ...

    def poll_exit(self) -> int | None: ...

    def close(self) -> None: ...


TerminalOutputHandler = Callable[[bytes], None]
TerminalEofHandler = Callable[[], None]
TerminalErrorHandler = Callable[[BaseException], None]


class TerminalPlatform(Protocol):
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
    ) -> TerminalPlatformSession: ...


__all__ = [
    "InteractiveTerminalManager",
    "InteractiveTerminalSession",
    "TerminalEofHandler",
    "TerminalErrorHandler",
    "TerminalOutputHandler",
    "TerminalPlatform",
    "TerminalPlatformSession",
]
