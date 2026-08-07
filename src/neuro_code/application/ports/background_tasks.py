"""Canonical background-task ports.

定义规范的后台任务端口."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from neuro_code.application.ports.tools import ToolOutputArtifactStore

from neuro_code.domain.background_tasks.models import (
    BackgroundTaskKillResult,
    BackgroundTaskSnapshot,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)


class BackgroundTaskManager(Protocol):
    """Own background process trees visible to one conversation binding.

    管理一个会话绑定可见的后台进程树."""

    async def start_shell(
        self,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        output_byte_limit: int,
        termination_grace_seconds: float,
        timeout_seconds: float | None = None,
        output_artifact_store: ToolOutputArtifactStore | None = None,
    ) -> BackgroundTaskSnapshot: ...

    async def start_exec(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        display_command: str,
        cwd: Path,
        env: Mapping[str, str],
        output_byte_limit: int,
        termination_grace_seconds: float,
        timeout_seconds: float | None = None,
        output_artifact_store: ToolOutputArtifactStore | None = None,
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

    async def list(self) -> tuple[BackgroundTaskSnapshot, ...]: ...

    async def pending_completions(self) -> tuple[BackgroundTaskSnapshot, ...]: ...

    async def discard_completed(self, task_id: str) -> bool: ...

    async def mark_completions_reported(self, task_ids: tuple[str, ...]) -> None: ...

    async def shutdown(self) -> None: ...


class BackgroundTaskSupervisor(Protocol):
    """Create isolated task scopes and clean up every scope at application exit.

    创建隔离的任务范围,并在应用退出时清理每个范围."""

    def open_scope(self) -> BackgroundTaskManager: ...

    async def shutdown(self) -> None: ...
