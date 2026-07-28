"""Canonical background-task ports."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from neuro_code.domain.background_tasks import (
    BackgroundTaskKillResult,
    BackgroundTaskSnapshot,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)


class BackgroundTaskManager(Protocol):
    """Own background process trees visible to one conversation binding."""

    async def start_shell(
        self,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        output_byte_limit: int,
        termination_grace_seconds: float,
        timeout_seconds: float | None = None,
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

    async def mark_completions_reported(self, task_ids: tuple[str, ...]) -> None: ...

    async def shutdown(self) -> None: ...


class BackgroundTaskSupervisor(Protocol):
    """Create isolated task scopes and clean up every scope at application exit."""

    def open_scope(self) -> BackgroundTaskManager: ...

    async def shutdown(self) -> None: ...
