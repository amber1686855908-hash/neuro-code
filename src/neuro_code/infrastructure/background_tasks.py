"""Canonical local background-task manager.

This module owns process-backed background task scopes and their bounded
observation lifecycle. It depends on the application background-task port,
domain task projections, and the canonical process-tree adapter; it does not
own SQLite persistence or tool definitions. The retired adapter facade has
been removed; callers use this canonical infrastructure module.

规范的本地后台任务管理器.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
    ToolOutputArtifact,
    ToolOutputArtifactStore,
)
from neuro_code.domain.background_tasks.models import (
    MAX_BACKGROUND_TASK_WAIT_IDS,
    BackgroundTaskKillOutcome,
    BackgroundTaskKillResult,
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)
from neuro_code.infrastructure.sandbox.process_tree import ProcessTree
from neuro_code.shared.errors import BackgroundTaskCapacityError, ToolError


class _BoundedOutput:
    """Keep a fixed-size head/tail preview while counting every output byte.

    在统计每个输出字节的同时保留固定大小的头尾预览."""

    def __init__(self, byte_limit: int, artifact_limit: int = 0) -> None:
        if byte_limit <= 0:
            raise ValueError("output byte limit must be positive")
        self._head_limit = max(1, byte_limit // 2)
        self._tail_limit = byte_limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0
        self._byte_limit = byte_limit
        if artifact_limit < 0:
            raise ValueError("artifact byte limit must not be negative")
        self._artifact_limit = artifact_limit
        self._artifact = bytearray()
        self._artifact_truncated = False

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self._byte_limit

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        if self._artifact_limit:
            remaining_artifact = self._artifact_limit - len(self._artifact)
            if remaining_artifact > 0:
                self._artifact.extend(chunk[:remaining_artifact])
            if len(chunk) > max(0, remaining_artifact):
                self._artifact_truncated = True
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

    def render(self) -> str:
        if not self.truncated:
            payload = bytes(self._head + self._tail)
        else:
            payload = bytes(self._head) + b"\n[older output truncated]\n" + bytes(self._tail)
        return payload.decode("utf-8", "replace")

    @property
    def artifact_truncated(self) -> bool:
        return self._artifact_truncated

    def artifact_bytes(self) -> bytes:
        return bytes(self._artifact)


@dataclass(slots=True)
class _TaskRecord:
    scope_id: str
    task_id: str
    command: str
    cwd: Path
    tree: ProcessTree
    output: _BoundedOutput
    output_artifact_store: ToolOutputArtifactStore | None
    termination_grace_seconds: float
    timeout_seconds: float | None
    started_at: datetime
    status: BackgroundTaskStatus = BackgroundTaskStatus.RUNNING
    exit_code: int | None = None
    finished_at: datetime | None = None
    kill_requested: bool = False
    timed_out: bool = False
    internal_failure: bool = False
    completion_reported: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watcher: asyncio.Task[None] | None = None
    output_artifact: ToolOutputArtifact | None = None


class LocalBackgroundTaskManager:
    """Application supervisor with isolated, process-owned conversation scopes.

    为每个进程拥有且相互隔离的会话作用域提供应用监督器."""

    def __init__(
        self,
        *,
        max_running_tasks: int = 16,
        max_retained_tasks: int = 64,
    ) -> None:
        if max_running_tasks <= 0:
            raise ValueError("max_running_tasks must be positive")
        if max_retained_tasks < max_running_tasks:
            raise ValueError("max_retained_tasks must cover max_running_tasks")
        self._max_running_tasks = max_running_tasks
        self._max_retained_tasks = max_retained_tasks
        self._records: dict[str, _TaskRecord] = {}
        self._registry_lock = asyncio.Lock()
        self._closed = False
        self._root_scope_id = uuid.uuid4().hex
        self._open_scopes = {self._root_scope_id}

    def open_scope(self) -> BackgroundTaskManager:
        """Return a manager whose task IDs and lifecycle are isolated from peers.

        返回一个与其他范围隔离任务 ID 和生命周期的管理器."""
        if self._closed:
            raise ToolError("background task manager is closed")
        scope_id = uuid.uuid4().hex
        self._open_scopes.add(scope_id)
        return _LocalBackgroundTaskScope(self, scope_id)

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
    ) -> BackgroundTaskSnapshot:
        return await self._start(
            self._root_scope_id,
            lambda: ProcessTree.spawn_shell(
                command,
                cwd=cwd,
                env=env,
                merge_output=True,
            ),
            command=command,
            cwd=cwd,
            output_byte_limit=output_byte_limit,
            termination_grace_seconds=termination_grace_seconds,
            timeout_seconds=timeout_seconds,
            output_artifact_store=output_artifact_store,
        )

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
    ) -> BackgroundTaskSnapshot:
        return await self._start(
            self._root_scope_id,
            lambda: ProcessTree.spawn_exec(
                executable,
                arguments,
                cwd=cwd,
                env=env,
                merge_output=True,
            ),
            command=display_command,
            cwd=cwd,
            output_byte_limit=output_byte_limit,
            termination_grace_seconds=termination_grace_seconds,
            timeout_seconds=timeout_seconds,
            output_artifact_store=output_artifact_store,
        )

    async def _start(
        self,
        scope_id: str,
        spawn: Callable[[], Awaitable[ProcessTree]],
        *,
        command: str,
        cwd: Path,
        output_byte_limit: int,
        termination_grace_seconds: float,
        timeout_seconds: float | None,
        output_artifact_store: ToolOutputArtifactStore | None,
    ) -> BackgroundTaskSnapshot:
        if not command.strip() or "\x00" in command:
            raise ToolError("background command must be a non-empty string")
        if output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        if not math.isfinite(termination_grace_seconds) or termination_grace_seconds <= 0:
            raise ToolError("termination_grace_seconds must be positive")
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ToolError("background timeout_seconds must be positive")

        async with self._registry_lock:
            if self._closed or scope_id not in self._open_scopes:
                raise ToolError("background task manager is closed")
            running = sum(
                record.status is BackgroundTaskStatus.RUNNING for record in self._records.values()
            )
            if running >= self._max_running_tasks:
                raise BackgroundTaskCapacityError(
                    f"background task limit reached ({self._max_running_tasks} running tasks)"
                )
            self._prune_completed(scope_id)

            spawn_task: asyncio.Future[ProcessTree] = asyncio.ensure_future(spawn())
            try:
                tree = await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    tree = await spawn_task
                    await tree.terminate(grace_seconds=termination_grace_seconds)
                raise

            task_id = f"task-{uuid.uuid4().hex[:12]}"
            record = _TaskRecord(
                scope_id=scope_id,
                task_id=task_id,
                command=command,
                cwd=cwd,
                tree=tree,
                output=_BoundedOutput(
                    output_byte_limit,
                    MAX_TOOL_OUTPUT_ARTIFACT_BYTES if output_artifact_store is not None else 0,
                ),
                output_artifact_store=output_artifact_store,
                termination_grace_seconds=termination_grace_seconds,
                timeout_seconds=timeout_seconds,
                started_at=datetime.now(UTC),
            )
            self._records[task_id] = record
            record.watcher = asyncio.create_task(
                self._watch(record),
                name=f"neuro-code-background-{task_id}",
            )
            return self._snapshot(record)

    def _prune_completed(self, scope_id: str) -> None:
        scope_records = tuple(
            record for record in self._records.values() if record.scope_id == scope_id
        )
        overflow = len(scope_records) - self._max_retained_tasks + 1
        if overflow <= 0:
            return
        completed = [record.task_id for record in scope_records if record.status.terminal]
        for task_id in completed[:overflow]:
            del self._records[task_id]
        retained = sum(record.scope_id == scope_id for record in self._records.values())
        if retained >= self._max_retained_tasks:
            raise BackgroundTaskCapacityError(
                f"background task retention limit reached ({self._max_retained_tasks} tasks)"
            )

    async def _watch(self, record: _TaskRecord) -> None:
        process = record.tree.process
        assert process.stdout is not None
        capture = asyncio.create_task(self._capture(process.stdout, record.output))
        try:
            wait = record.tree.wait()
            if record.timeout_seconds is None:
                await wait
            else:
                try:
                    await asyncio.wait_for(wait, timeout=record.timeout_seconds)
                except TimeoutError:
                    record.timed_out = True
                    await self._terminate(record)
            try:
                await asyncio.wait_for(capture, timeout=record.termination_grace_seconds)
            except TimeoutError:
                # A child that escaped the owned process group can keep its
                # inherited pipe open after the direct command has exited.
                # Never let that prevent kill(), shutdown, or cancellation
                # from reaching a terminal record.
                record.internal_failure = True
                capture.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await capture
        except asyncio.CancelledError:
            record.kill_requested = True
            with contextlib.suppress(Exception):
                await self._terminate(record)
            capture.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await capture
        except Exception:
            record.internal_failure = True
            with contextlib.suppress(Exception):
                await self._terminate(record)
            capture.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await capture
        finally:
            record.exit_code = process.returncode
            if record.kill_requested:
                record.status = BackgroundTaskStatus.CANCELLED
            elif record.timed_out:
                record.status = BackgroundTaskStatus.TIMED_OUT
            elif record.internal_failure:
                record.status = BackgroundTaskStatus.FAILED
            elif record.exit_code == 0:
                record.status = BackgroundTaskStatus.COMPLETED
            else:
                record.status = BackgroundTaskStatus.FAILED
            if record.output_artifact_store is not None and record.output.truncated:
                try:
                    record.output_artifact = await record.output_artifact_store.save(
                        tool_name="bash",
                        content=record.output.artifact_bytes(),
                        content_truncated=record.output.artifact_truncated,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Artifact persistence is diagnostic and must not change
                    # the command's terminal status or cancellation semantics.
                    record.output_artifact = None
            record.finished_at = datetime.now(UTC)
            record.done.set()

    @staticmethod
    async def _capture(stream: asyncio.StreamReader, output: _BoundedOutput) -> None:
        while chunk := await stream.read(65_536):
            output.append(chunk)

    async def _terminate(self, record: _TaskRecord) -> None:
        async with record.termination_lock:
            await record.tree.terminate(
                grace_seconds=record.termination_grace_seconds,
            )

    async def get(
        self,
        task_id: str,
        *,
        wait_seconds: float = 0.0,
    ) -> BackgroundTaskSnapshot | None:
        return await self._get(self._root_scope_id, task_id, wait_seconds=wait_seconds)

    async def _get(
        self,
        scope_id: str,
        task_id: str,
        *,
        wait_seconds: float = 0.0,
    ) -> BackgroundTaskSnapshot | None:
        if not math.isfinite(wait_seconds) or wait_seconds < 0:
            raise ToolError("wait_seconds must be finite and not negative")
        record = self._records.get(task_id)
        if record is not None and record.scope_id != scope_id:
            record = None
        if record is None:
            return None
        if wait_seconds > 0 and not record.done.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(record.done.wait(), timeout=wait_seconds)
        return self._snapshot(record)

    async def kill(self, task_id: str) -> BackgroundTaskKillResult | None:
        return await self._kill(self._root_scope_id, task_id)

    async def wait(
        self,
        task_ids: tuple[str, ...],
        *,
        mode: BackgroundTaskWaitMode,
        timeout_seconds: float,
    ) -> BackgroundTaskWaitResult:
        return await self._wait(
            self._root_scope_id,
            task_ids,
            mode=mode,
            timeout_seconds=timeout_seconds,
        )

    async def _wait(
        self,
        scope_id: str,
        task_ids: tuple[str, ...],
        *,
        mode: BackgroundTaskWaitMode,
        timeout_seconds: float,
    ) -> BackgroundTaskWaitResult:
        if not task_ids:
            raise ToolError("background task wait requires at least one task ID")
        if len(task_ids) > MAX_BACKGROUND_TASK_WAIT_IDS:
            raise ToolError(
                f"background task wait accepts at most {MAX_BACKGROUND_TASK_WAIT_IDS} task IDs"
            )
        if len(set(task_ids)) != len(task_ids):
            raise ToolError("background task wait IDs must be unique")
        if any(not task_id or "\x00" in task_id for task_id in task_ids):
            raise ToolError("background task wait IDs must be non-empty strings")
        if not isinstance(mode, BackgroundTaskWaitMode):
            raise ToolError("invalid background task wait mode")
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ToolError("background task wait timeout must be finite and not negative")

        records: list[_TaskRecord] = []
        missing_task_ids: list[str] = []
        for task_id in task_ids:
            record = self._records.get(task_id)
            if record is None or record.scope_id != scope_id:
                missing_task_ids.append(task_id)
            else:
                records.append(record)

        def condition_met() -> bool:
            if not records:
                return True
            if mode is BackgroundTaskWaitMode.WAIT_ANY:
                return any(record.done.is_set() for record in records)
            return all(record.done.is_set() for record in records)

        if not condition_met() and timeout_seconds > 0:
            waiters = [asyncio.create_task(record.done.wait()) for record in records]
            return_when = (
                asyncio.FIRST_COMPLETED
                if mode is BackgroundTaskWaitMode.WAIT_ANY
                else asyncio.ALL_COMPLETED
            )
            try:
                await asyncio.wait(
                    waiters,
                    timeout=timeout_seconds,
                    return_when=return_when,
                )
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

        return BackgroundTaskWaitResult(
            mode=mode,
            snapshots=tuple(self._snapshot(record) for record in records),
            missing_task_ids=tuple(missing_task_ids),
            timed_out=not condition_met(),
        )

    async def _kill(
        self,
        scope_id: str,
        task_id: str,
    ) -> BackgroundTaskKillResult | None:
        record = self._records.get(task_id)
        if record is not None and record.scope_id != scope_id:
            record = None
        if record is None:
            return None
        if record.done.is_set():
            return BackgroundTaskKillResult(
                BackgroundTaskKillOutcome.ALREADY_EXITED,
                self._snapshot(record),
            )
        record.kill_requested = True
        await self._terminate(record)
        await record.done.wait()
        return BackgroundTaskKillResult(
            BackgroundTaskKillOutcome.KILLED,
            self._snapshot(record),
        )

    async def list(self) -> tuple[BackgroundTaskSnapshot, ...]:
        return self._list(self._root_scope_id)

    def _list(self, scope_id: str) -> tuple[BackgroundTaskSnapshot, ...]:
        return tuple(
            self._snapshot(record)
            for record in self._records.values()
            if record.scope_id == scope_id
        )

    async def pending_completions(self) -> tuple[BackgroundTaskSnapshot, ...]:
        return self._pending_completions(self._root_scope_id)

    def _pending_completions(
        self,
        scope_id: str,
    ) -> tuple[BackgroundTaskSnapshot, ...]:
        return tuple(
            self._snapshot(record)
            for record in self._records.values()
            if record.scope_id == scope_id
            and record.status.terminal
            and not record.completion_reported
        )

    async def discard_completed(self, task_id: str) -> bool:
        return await self._discard_completed(self._root_scope_id, task_id)

    async def _discard_completed(self, scope_id: str, task_id: str) -> bool:
        """Remove one terminal record owned by ``scope_id``.

        This is intentionally narrower than ``kill`` or a general delete: a
        running record, an unknown ID, and an ID belonging to another scope are
        all left untouched and report ``False``. The registry lock makes the
        terminal check and removal one operation for concurrent task polling.

        移除由 ``scope_id`` 所有的一条终端记录. 运行中、未知或属于其他范围的记录保持不变.
        """

        async with self._registry_lock:
            record = self._records.get(task_id)
            if record is None or record.scope_id != scope_id or not record.status.terminal:
                return False
            del self._records[task_id]
            return True

    async def mark_completions_reported(self, task_ids: tuple[str, ...]) -> None:
        self._mark_completions_reported(self._root_scope_id, task_ids)

    def _mark_completions_reported(
        self,
        scope_id: str,
        task_ids: tuple[str, ...],
    ) -> None:
        for task_id in task_ids:
            record = self._records.get(task_id)
            if record is not None and record.scope_id == scope_id and record.status.terminal:
                record.completion_reported = True

    async def _shutdown_scope(self, scope_id: str) -> None:
        async with self._registry_lock:
            if scope_id not in self._open_scopes:
                return
            self._open_scopes.remove(scope_id)
            records = tuple(
                record for record in self._records.values() if record.scope_id == scope_id
            )
        await asyncio.gather(
            *(
                self._kill(scope_id, record.task_id)
                for record in records
                if not record.done.is_set()
            )
        )
        watchers = tuple(record.watcher for record in records if record.watcher is not None)
        if watchers:
            await asyncio.gather(*watchers)
        async with self._registry_lock:
            for record in records:
                if self._records.get(record.task_id) is record:
                    del self._records[record.task_id]

    async def shutdown(self) -> None:
        async with self._registry_lock:
            if self._closed:
                return
            self._closed = True
            self._open_scopes.clear()
            records = tuple(self._records.values())
        await asyncio.gather(
            *(self._kill_record(record) for record in records if not record.done.is_set())
        )
        watchers = tuple(record.watcher for record in records if record.watcher is not None)
        if watchers:
            await asyncio.gather(*watchers)

    async def _kill_record(self, record: _TaskRecord) -> None:
        if record.done.is_set():
            return
        record.kill_requested = True
        await self._terminate(record)
        await record.done.wait()

    @staticmethod
    def _snapshot(record: _TaskRecord) -> BackgroundTaskSnapshot:
        return BackgroundTaskSnapshot(
            task_id=record.task_id,
            command=record.command,
            cwd=str(record.cwd),
            status=record.status,
            output=record.output.render(),
            total_output_bytes=record.output.total_bytes,
            truncated=record.output.truncated,
            exit_code=record.exit_code,
            started_at=record.started_at,
            finished_at=record.finished_at,
            completion_reported=record.completion_reported,
            output_artifact_id=(
                record.output_artifact.artifact_id if record.output_artifact is not None else None
            ),
            output_artifact_path=(
                record.output_artifact.relative_path if record.output_artifact is not None else None
            ),
            output_artifact_bytes=(
                record.output_artifact.byte_count if record.output_artifact is not None else None
            ),
            output_artifact_truncated=(
                record.output_artifact.truncated if record.output_artifact is not None else False
            ),
        )


class _LocalBackgroundTaskScope:
    """Conversation-local view over an application-owned task supervisor.

    提供会话本地的应用层任务监督器视图."""

    def __init__(self, supervisor: LocalBackgroundTaskManager, scope_id: str) -> None:
        self._supervisor = supervisor
        self._scope_id = scope_id

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
    ) -> BackgroundTaskSnapshot:
        return await self._supervisor._start(
            self._scope_id,
            lambda: ProcessTree.spawn_shell(
                command,
                cwd=cwd,
                env=env,
                merge_output=True,
            ),
            command=command,
            cwd=cwd,
            output_byte_limit=output_byte_limit,
            termination_grace_seconds=termination_grace_seconds,
            timeout_seconds=timeout_seconds,
            output_artifact_store=output_artifact_store,
        )

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
    ) -> BackgroundTaskSnapshot:
        return await self._supervisor._start(
            self._scope_id,
            lambda: ProcessTree.spawn_exec(
                executable,
                arguments,
                cwd=cwd,
                env=env,
                merge_output=True,
            ),
            command=display_command,
            cwd=cwd,
            output_byte_limit=output_byte_limit,
            termination_grace_seconds=termination_grace_seconds,
            timeout_seconds=timeout_seconds,
            output_artifact_store=output_artifact_store,
        )

    async def get(
        self,
        task_id: str,
        *,
        wait_seconds: float = 0.0,
    ) -> BackgroundTaskSnapshot | None:
        return await self._supervisor._get(
            self._scope_id,
            task_id,
            wait_seconds=wait_seconds,
        )

    async def kill(self, task_id: str) -> BackgroundTaskKillResult | None:
        return await self._supervisor._kill(self._scope_id, task_id)

    async def wait(
        self,
        task_ids: tuple[str, ...],
        *,
        mode: BackgroundTaskWaitMode,
        timeout_seconds: float,
    ) -> BackgroundTaskWaitResult:
        return await self._supervisor._wait(
            self._scope_id,
            task_ids,
            mode=mode,
            timeout_seconds=timeout_seconds,
        )

    async def list(self) -> tuple[BackgroundTaskSnapshot, ...]:
        return self._supervisor._list(self._scope_id)

    async def pending_completions(self) -> tuple[BackgroundTaskSnapshot, ...]:
        return self._supervisor._pending_completions(self._scope_id)

    async def discard_completed(self, task_id: str) -> bool:
        return await self._supervisor._discard_completed(self._scope_id, task_id)

    async def mark_completions_reported(self, task_ids: tuple[str, ...]) -> None:
        self._supervisor._mark_completions_reported(self._scope_id, task_ids)

    async def shutdown(self) -> None:
        await self._supervisor._shutdown_scope(self._scope_id)


__all__ = ["LocalBackgroundTaskManager"]
