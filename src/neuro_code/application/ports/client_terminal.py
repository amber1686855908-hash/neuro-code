"""Session-scoped foreground command execution delegated to an ACP client.

定义委托给 ACP 客户端的会话范围前台命令执行."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neuro_code.domain.background_tasks.models import (
    BackgroundTaskKillResult,
    BackgroundTaskSnapshot,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)

MAX_CLIENT_TERMINAL_OUTPUT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ClientTerminalResult:
    """A bounded terminal result returned by a capability-negotiated client.

    表示能力协商客户端返回的有界终端结果."""

    output: str
    exit_code: int | None
    signal: str | None
    truncated: bool


class ClientTerminal(Protocol):
    """Run direct foreground and managed background executables through ACP.

    The caller supplies an executable and argument vector, never a shell command.
    Implementations keep the request bound to one ACP session, must not forward
    configured Neuro Code credentials, and fail closed when terminal capability
    was not advertised.  The output limit cannot exceed
    ``MAX_CLIENT_TERMINAL_OUTPUT_BYTES``.

    通过 ACP 运行直接前台可执行文件和受管理的后台可执行文件.
    """

    async def run(
        self,
        command: str,
        arguments: Sequence[str],
        /,
        *,
        cwd: Path,
        output_byte_limit: int,
        timeout_seconds: float,
    ) -> ClientTerminalResult: ...

    async def start_exec(
        self,
        command: str,
        arguments: Sequence[str],
        /,
        *,
        cwd: Path,
        output_byte_limit: int,
        timeout_seconds: float | None = None,
    ) -> BackgroundTaskSnapshot: ...

    async def get(
        self,
        task_id: str,
        *,
        wait_seconds: float = 0.0,
    ) -> BackgroundTaskSnapshot | None: ...

    async def wait(
        self,
        task_ids: tuple[str, ...],
        *,
        mode: BackgroundTaskWaitMode,
        timeout_seconds: float,
    ) -> BackgroundTaskWaitResult: ...

    async def kill(self, task_id: str) -> BackgroundTaskKillResult | None: ...

    async def shutdown(self) -> None: ...


__all__ = [
    "MAX_CLIENT_TERMINAL_OUTPUT_BYTES",
    "ClientTerminal",
    "ClientTerminalResult",
]
