"""Canonical interactive-terminal ports.

定义规范的交互式终端端口."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neuro_code.application.ports.sandbox import LocalProcessLifecycleCapability
from neuro_code.domain.terminal.models import TerminalOutputChunk, TerminalSignal, TerminalSize


@dataclass(frozen=True, slots=True)
class TerminalCreationAuthorization:
    """One runtime-owned authorization hand-off for model terminal creation.

    The token is created only after ``ToolExecutor`` has completed the normal
    model-tool permission flow.  It is intentionally bound to one tool-call
    identity so a terminal tool cannot reuse approval for another call.

    模型创建终端的一次运行时授权交接.令牌只在 ToolExecutor 完成正常模型工具权限流程后
    创建,并绑定到单个工具调用身份,避免不同调用复用审批结果.
    """

    call_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id or "\x00" in self.call_id:
            raise ValueError("terminal creation authorization call ID is invalid")


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
        authorization: TerminalCreationAuthorization | None = None,
    ) -> InteractiveTerminalSession: ...

    async def get_session(self, session_id: str) -> InteractiveTerminalSession | None: ...

    async def list_sessions(self) -> tuple[InteractiveTerminalSession, ...]: ...

    async def shutdown(self) -> None: ...


class TerminalPlatformSession(Protocol):
    @property
    def process_id(self) -> int: ...

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability: ...

    def write(self, data: bytes) -> None: ...

    def resize(self, size: TerminalSize) -> None: ...

    def send_signal(self, signal: TerminalSignal) -> None: ...

    def poll_exit(self) -> int | None: ...

    def close(self) -> None: ...


TerminalOutputHandler = Callable[[bytes], None]
TerminalEofHandler = Callable[[], None]
TerminalErrorHandler = Callable[[BaseException], None]


class TerminalPlatform(Protocol):
    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability: ...

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
    "TerminalCreationAuthorization",
    "TerminalEofHandler",
    "TerminalErrorHandler",
    "TerminalOutputHandler",
    "TerminalPlatform",
    "TerminalPlatformSession",
]
