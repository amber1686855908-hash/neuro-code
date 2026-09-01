"""ACP client-owned filesystem and terminal adapters.

ACP 客户端拥有的文件系统和终端适配器.

This module is the canonical interface-layer owner for the bounded ACP client
I/O ports. It adapts the ACP SDK client to the existing application ports while
keeping capability negotiation, session publication, tool registration, and
session cleanup in ``neuro_code.acp``.
本模块是有界 ACP 客户端 I/O port 的规范 interface-layer 所有者. 它把 ACP SDK
client 适配到既有 application port,同时将能力协商、session 发布、工具注册和
session 清理保留在 ``neuro_code.acp``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from acp.interfaces import Client

from neuro_code.application.ports.client_terminal import (
    MAX_CLIENT_TERMINAL_OUTPUT_BYTES,
    ClientTerminalResult,
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
from neuro_code.shared.errors import ToolError

MAX_CLIENT_FILE_BYTES = 1024 * 1024
MAX_CLIENT_TERMINAL_COMMAND_BYTES = 4 * 1024
MAX_CLIENT_TERMINAL_ARGUMENTS = 64
MAX_CLIENT_TERMINAL_ARGUMENT_BYTES = 4 * 1024
MAX_CLIENT_TERMINAL_ARGUMENT_TOTAL_BYTES = 32 * 1024
MAX_CLIENT_TERMINAL_ID_BYTES = 512
MAX_CLIENT_TERMINAL_SIGNAL_BYTES = 128
MAX_CLIENT_TERMINAL_TASKS = 8
MAX_CLIENT_TERMINAL_RETAINED_TASKS = 32


@dataclass(slots=True)
class _AcpClientTerminalTask:
    task_id: str
    terminal_id: str
    command: str
    cwd: str
    output_byte_limit: int
    timeout_seconds: float | None
    started_at: datetime
    status: BackgroundTaskStatus = BackgroundTaskStatus.RUNNING
    output: str = ""
    total_output_bytes: int = 0
    truncated: bool = False
    exit_code: int | None = None
    finished_at: datetime | None = None
    kill_requested: bool = False
    timed_out: bool = False
    failed: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watcher: asyncio.Task[None] | None = None


class _AcpClientTerminal:
    """Bounded standard-ACP terminal adapter for one active ACP session.

    为一个活动 ACP 会话提供的有界标准 ACP 终端适配器.
    """

    def __init__(self, client: Client, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._tasks: dict[str, _AcpClientTerminalTask] = {}
        self._tasks_lock = asyncio.Lock()
        self._pending_starts = 0
        self._closed = False

    async def run(
        self,
        command: str,
        arguments: Sequence[str],
        /,
        *,
        cwd: Path,
        output_byte_limit: int,
        timeout_seconds: float,
    ) -> ClientTerminalResult:
        self._ensure_open()
        validated_command, validated_arguments = _client_terminal_command(command, arguments)
        validated_cwd = _client_terminal_cwd(cwd)
        _client_terminal_limits(output_byte_limit, timeout_seconds)
        terminal_id = await self._create_terminal(
            validated_command,
            validated_arguments,
            cwd=validated_cwd,
            output_byte_limit=output_byte_limit,
        )
        needs_kill = True
        try:
            try:
                exit_status = await asyncio.wait_for(
                    self._client.wait_for_terminal_exit(self._session_id, terminal_id),
                    timeout_seconds,
                )
            except TimeoutError as error:
                await self._best_effort_kill(terminal_id)
                needs_kill = False
                raise ToolError(f"command timed out after {timeout_seconds:g} seconds") from error
            except asyncio.CancelledError:
                await self._best_effort_kill(terminal_id)
                needs_kill = False
                raise
            except Exception:
                await self._best_effort_kill(terminal_id)
                needs_kill = False
                raise ToolError("ACP client terminal wait failed") from None

            exit_code, signal = _client_terminal_exit_status(
                exit_status.exit_code,
                exit_status.signal,
            )
            needs_kill = False
            try:
                output = await self._client.terminal_output(self._session_id, terminal_id)
                content = output.output
                truncated = output.truncated
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ToolError("ACP client terminal output failed") from None
            if not isinstance(content, str) or not isinstance(truncated, bool):
                raise ToolError("ACP client terminal returned an invalid response")
            if len(content.encode("utf-8")) > output_byte_limit:
                raise ToolError("ACP client terminal response exceeds the output limit")
            return ClientTerminalResult(
                output=content,
                exit_code=exit_code,
                signal=signal,
                truncated=truncated,
            )
        finally:
            if needs_kill:
                await self._best_effort_kill(terminal_id)
            await self._best_effort_release(terminal_id)

    async def start_exec(
        self,
        command: str,
        arguments: Sequence[str],
        /,
        *,
        cwd: Path,
        output_byte_limit: int,
        timeout_seconds: float | None = None,
    ) -> BackgroundTaskSnapshot:
        """Start one direct executable and retain its standard ACP lifecycle.

        启动一个直接可执行文件,并保留其标准 ACP 生命周期.
        """

        self._ensure_open()
        validated_command, validated_arguments = _client_terminal_command(command, arguments)
        validated_cwd = _client_terminal_cwd(cwd)
        _client_terminal_background_limits(output_byte_limit, timeout_seconds)
        async with self._tasks_lock:
            self._ensure_open()
            self._prune_tasks()
            running = sum(
                task.status is BackgroundTaskStatus.RUNNING for task in self._tasks.values()
            )
            if running + self._pending_starts >= MAX_CLIENT_TERMINAL_TASKS:
                raise ToolError(
                    f"ACP client terminal task limit reached ({MAX_CLIENT_TERMINAL_TASKS} running tasks)"
                )
            self._pending_starts += 1
        try:
            terminal_id = await self._create_terminal(
                validated_command,
                validated_arguments,
                cwd=validated_cwd,
                output_byte_limit=output_byte_limit,
            )
        finally:
            async with self._tasks_lock:
                self._pending_starts -= 1
        task = _AcpClientTerminalTask(
            task_id=f"terminal-task-{uuid.uuid4().hex[:12]}",
            terminal_id=terminal_id,
            command=validated_command,
            cwd=validated_cwd,
            output_byte_limit=output_byte_limit,
            timeout_seconds=timeout_seconds,
            started_at=datetime.now(UTC),
        )
        async with self._tasks_lock:
            if self._closed:
                await self._best_effort_kill(terminal_id)
                await self._best_effort_release(terminal_id)
                raise ToolError("ACP client terminal is closed")
            self._tasks[task.task_id] = task
        task.watcher = asyncio.create_task(
            self._watch_task(task),
            name=f"neuro-code-acp-terminal-{task.task_id}",
        )
        return self._snapshot(task)

    async def get(
        self,
        task_id: str,
        *,
        wait_seconds: float = 0.0,
    ) -> BackgroundTaskSnapshot | None:
        task = await self._task(task_id)
        if task is None:
            return None
        _client_terminal_wait_seconds(wait_seconds)
        if wait_seconds > 0 and not task.done.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(task.done.wait(), timeout=wait_seconds)
        if not task.done.is_set():
            await self._refresh_output(task)
        return self._snapshot(task)

    async def wait(
        self,
        task_ids: tuple[str, ...],
        *,
        mode: BackgroundTaskWaitMode,
        timeout_seconds: float,
    ) -> BackgroundTaskWaitResult:
        if not task_ids:
            raise ToolError("ACP client terminal wait requires at least one task ID")
        if len(task_ids) > MAX_BACKGROUND_TASK_WAIT_IDS:
            raise ToolError(
                f"ACP client terminal wait accepts at most {MAX_BACKGROUND_TASK_WAIT_IDS} task IDs"
            )
        if len(set(task_ids)) != len(task_ids):
            raise ToolError("ACP client terminal wait IDs must be unique")
        if not isinstance(mode, BackgroundTaskWaitMode):
            raise ToolError("ACP client terminal wait mode is invalid")
        _client_terminal_wait_seconds(timeout_seconds)

        tasks: list[_AcpClientTerminalTask] = []
        missing: list[str] = []
        for task_id in task_ids:
            task = await self._task(task_id)
            if task is None:
                missing.append(task_id)
            else:
                tasks.append(task)

        def condition_met() -> bool:
            if not tasks:
                return True
            if mode is BackgroundTaskWaitMode.WAIT_ANY:
                return any(task.done.is_set() for task in tasks)
            return all(task.done.is_set() for task in tasks)

        if not condition_met() and timeout_seconds > 0:
            waiters = [asyncio.create_task(task.done.wait()) for task in tasks]
            try:
                await asyncio.wait(
                    waiters,
                    timeout=timeout_seconds,
                    return_when=(
                        asyncio.FIRST_COMPLETED
                        if mode is BackgroundTaskWaitMode.WAIT_ANY
                        else asyncio.ALL_COMPLETED
                    ),
                )
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

        snapshots: list[BackgroundTaskSnapshot] = []
        for task in tasks:
            if not task.done.is_set():
                with contextlib.suppress(ToolError):
                    await self._refresh_output(task)
            snapshots.append(self._snapshot(task))
        return BackgroundTaskWaitResult(
            mode=mode,
            snapshots=tuple(snapshots),
            missing_task_ids=tuple(missing),
            timed_out=not condition_met(),
        )

    async def kill(self, task_id: str) -> BackgroundTaskKillResult | None:
        task = await self._task(task_id)
        if task is None:
            return None
        if task.done.is_set():
            return BackgroundTaskKillResult(
                BackgroundTaskKillOutcome.ALREADY_EXITED, self._snapshot(task)
            )
        async with task.termination_lock:
            if task.done.is_set():
                return BackgroundTaskKillResult(
                    BackgroundTaskKillOutcome.ALREADY_EXITED,
                    self._snapshot(task),
                )
            task.kill_requested = True
            await self._best_effort_kill(task.terminal_id)
            watcher = task.watcher
            if watcher is not None and watcher is not asyncio.current_task() and not watcher.done():
                watcher.cancel()
        await task.done.wait()
        return BackgroundTaskKillResult(BackgroundTaskKillOutcome.KILLED, self._snapshot(task))

    async def shutdown(self) -> None:
        async with self._tasks_lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._tasks.values())
        await asyncio.gather(*(self.kill(task.task_id) for task in tasks), return_exceptions=True)
        async with self._tasks_lock:
            self._tasks.clear()

    async def _watch_task(self, task: _AcpClientTerminalTask) -> None:
        try:
            wait = self._client.wait_for_terminal_exit(self._session_id, task.terminal_id)
            if task.timeout_seconds is None:
                response = await wait
            else:
                try:
                    response = await asyncio.wait_for(wait, timeout=task.timeout_seconds)
                except TimeoutError:
                    task.timed_out = True
                    await self._best_effort_kill(task.terminal_id)
                    response = None
            if response is not None:
                task.exit_code, signal = _client_terminal_exit_status(
                    response.exit_code,
                    response.signal,
                )
                task.failed = task.failed or signal is not None
        except asyncio.CancelledError:
            task.kill_requested = True
            await self._best_effort_kill(task.terminal_id)
        except Exception:
            task.failed = True
            await self._best_effort_kill(task.terminal_id)
        finally:
            try:
                await self._refresh_output(task)
            except ToolError:
                task.failed = True
            if task.kill_requested:
                task.status = BackgroundTaskStatus.CANCELLED
            elif task.timed_out:
                task.status = BackgroundTaskStatus.TIMED_OUT
            elif task.failed or task.exit_code != 0:
                task.status = BackgroundTaskStatus.FAILED
            else:
                task.status = BackgroundTaskStatus.COMPLETED
            task.finished_at = datetime.now(UTC)
            task.done.set()
            await self._best_effort_release(task.terminal_id)

    async def _refresh_output(self, task: _AcpClientTerminalTask) -> None:
        async with task.output_lock:
            if task.done.is_set():
                return
            try:
                response = await self._client.terminal_output(self._session_id, task.terminal_id)
                output = response.output
                truncated = response.truncated
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ToolError("ACP client terminal output failed") from None
            if not isinstance(output, str) or not isinstance(truncated, bool):
                raise ToolError("ACP client terminal returned an invalid response")
            output_bytes = len(output.encode("utf-8"))
            if output_bytes > task.output_byte_limit:
                raise ToolError("ACP client terminal response exceeds the output limit")
            task.output = output
            task.total_output_bytes = max(task.total_output_bytes, output_bytes)
            task.truncated = task.truncated or truncated

    async def _task(self, task_id: object) -> _AcpClientTerminalTask | None:
        validated_task_id = _client_terminal_task_id(task_id)
        async with self._tasks_lock:
            return self._tasks.get(validated_task_id)

    def _prune_tasks(self) -> None:
        overflow = len(self._tasks) - MAX_CLIENT_TERMINAL_RETAINED_TASKS + 1
        if overflow <= 0:
            return
        completed = [task_id for task_id, task in self._tasks.items() if task.done.is_set()]
        for task_id in completed[:overflow]:
            del self._tasks[task_id]
        if len(self._tasks) >= MAX_CLIENT_TERMINAL_RETAINED_TASKS:
            raise ToolError(
                "ACP client terminal task retention limit reached "
                f"({MAX_CLIENT_TERMINAL_RETAINED_TASKS} tasks)"
            )

    @staticmethod
    def _snapshot(task: _AcpClientTerminalTask) -> BackgroundTaskSnapshot:
        return BackgroundTaskSnapshot(
            task_id=task.task_id,
            command=task.command,
            cwd=task.cwd,
            status=task.status,
            output=task.output,
            total_output_bytes=task.total_output_bytes,
            truncated=task.truncated,
            exit_code=task.exit_code,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise ToolError("ACP client terminal is closed")

    async def _create_terminal(
        self,
        command: str,
        arguments: tuple[str, ...],
        *,
        cwd: str,
        output_byte_limit: int,
    ) -> str:
        creation = asyncio.create_task(
            self._client.create_terminal(
                self._session_id,
                command,
                args=list(arguments),
                cwd=cwd,
                output_byte_limit=output_byte_limit,
            )
        )
        try:
            response = await asyncio.shield(creation)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                response = await creation
                terminal_id = _client_terminal_id(response.terminal_id)
                await self._best_effort_kill(terminal_id)
                await self._best_effort_release(terminal_id)
            raise
        except Exception:
            raise ToolError("ACP client terminal creation failed") from None
        return _client_terminal_id(response.terminal_id)

    async def _best_effort_kill(self, terminal_id: str) -> None:
        await self._best_effort_terminal_request(
            self._client.kill_terminal(self._session_id, terminal_id)
        )

    async def _best_effort_release(self, terminal_id: str) -> None:
        await self._best_effort_terminal_request(
            self._client.release_terminal(self._session_id, terminal_id)
        )

    @staticmethod
    async def _best_effort_terminal_request(request: Awaitable[object]) -> None:
        operation = asyncio.ensure_future(request)
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await operation
            raise
        except Exception:
            return


def _client_terminal_command(command: object, arguments: object) -> tuple[str, tuple[str, ...]]:
    if (
        not isinstance(command, str)
        or not command
        or "\x00" in command
        or len(command.encode("utf-8")) > MAX_CLIENT_TERMINAL_COMMAND_BYTES
    ):
        raise ToolError("ACP client terminal command is invalid")
    if isinstance(arguments, str | bytes) or not isinstance(arguments, Sequence):
        raise ToolError("ACP client terminal arguments are invalid")
    if len(arguments) > MAX_CLIENT_TERMINAL_ARGUMENTS:
        raise ToolError("ACP client terminal has too many arguments")
    total_bytes = 0
    validated: list[str] = []
    for argument in arguments:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ToolError("ACP client terminal arguments are invalid")
        size = len(argument.encode("utf-8"))
        if size > MAX_CLIENT_TERMINAL_ARGUMENT_BYTES:
            raise ToolError("ACP client terminal argument exceeds the size limit")
        total_bytes += size
        if total_bytes > MAX_CLIENT_TERMINAL_ARGUMENT_TOTAL_BYTES:
            raise ToolError("ACP client terminal arguments exceed the size limit")
        validated.append(argument)
    return command, tuple(validated)


def _client_terminal_cwd(cwd: object) -> str:
    if not isinstance(cwd, Path):
        raise ToolError("ACP client terminal working directory is invalid")
    rendered = str(cwd)
    if not cwd.is_absolute() or "\x00" in rendered:
        raise ToolError("ACP client terminal working directory is invalid")
    return rendered


def _client_terminal_limits(output_byte_limit: object, timeout_seconds: object) -> None:
    if (
        isinstance(output_byte_limit, bool)
        or not isinstance(output_byte_limit, int)
        or not 1 <= output_byte_limit <= MAX_CLIENT_TERMINAL_OUTPUT_BYTES
    ):
        raise ToolError("ACP client terminal output limit is invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ToolError("ACP client terminal timeout is invalid")


def _client_terminal_background_limits(
    output_byte_limit: object,
    timeout_seconds: object,
) -> None:
    if (
        isinstance(output_byte_limit, bool)
        or not isinstance(output_byte_limit, int)
        or not 1 <= output_byte_limit <= MAX_CLIENT_TERMINAL_OUTPUT_BYTES
    ):
        raise ToolError("ACP client terminal output limit is invalid")
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ToolError("ACP client terminal timeout is invalid")


def _client_terminal_wait_seconds(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ToolError("ACP client terminal wait timeout is invalid")
    return float(value)


def _client_terminal_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8")) > MAX_CLIENT_TERMINAL_ID_BYTES
    ):
        raise ToolError("ACP client terminal returned an invalid identifier")
    return value


def _client_terminal_task_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8")) > 128
    ):
        raise ToolError("ACP client terminal task identifier is invalid")
    return value


def _client_terminal_exit_status(
    exit_code: object,
    signal: object,
) -> tuple[int | None, str | None]:
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0
    ):
        raise ToolError("ACP client terminal returned an invalid exit status")
    if signal is not None and (
        not isinstance(signal, str)
        or not signal
        or "\x00" in signal
        or any(ord(character) < 32 or ord(character) == 127 for character in signal)
        or len(signal.encode("utf-8")) > MAX_CLIENT_TERMINAL_SIGNAL_BYTES
    ):
        raise ToolError("ACP client terminal returned an invalid exit status")
    if exit_code is None and signal is None:
        raise ToolError("ACP client terminal returned no exit status")
    return exit_code, signal


class _AcpClientFileSystem:
    """Bounded ACP client filesystem adapter for one active session.

    为一个活动会话提供有界的 ACP 客户端文件系统适配器.
    """

    def __init__(
        self,
        client: Client,
        session_id: str,
        *,
        supports_read: bool,
        supports_write: bool,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._supports_read = supports_read
        self._supports_write = supports_write

    @property
    def supports_read(self) -> bool:
        return self._supports_read

    @property
    def supports_write(self) -> bool:
        return self._supports_write

    async def read_text_file(
        self,
        path: Path,
        /,
        *,
        line: int | None = None,
        limit: int | None = None,
    ) -> str:
        if not self._supports_read:
            raise ToolError("ACP client does not support text-file reads")
        try:
            response = await self._client.read_text_file(
                self._session_id,
                str(path),
                line=line,
                limit=limit,
            )
            content = response.content
            byte_count = len(content.encode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ToolError("ACP client text-file read failed") from None
        if byte_count > MAX_CLIENT_FILE_BYTES:
            raise ToolError("ACP client text-file response exceeds the size limit")
        return content

    async def write_text_file(self, path: Path, content: str, /) -> None:
        if not self._supports_write:
            raise ToolError("ACP client does not support text-file writes")
        try:
            if len(content.encode("utf-8")) > MAX_CLIENT_FILE_BYTES:
                raise ToolError("ACP client text-file write exceeds the size limit")
            await self._client.write_text_file(self._session_id, str(path), content)
        except asyncio.CancelledError:
            raise
        except ToolError:
            raise
        except Exception:
            raise ToolError("ACP client text-file write failed") from None


__all__ = [
    "MAX_CLIENT_FILE_BYTES",
    "MAX_CLIENT_TERMINAL_ARGUMENTS",
    "MAX_CLIENT_TERMINAL_ARGUMENT_BYTES",
    "MAX_CLIENT_TERMINAL_ARGUMENT_TOTAL_BYTES",
    "MAX_CLIENT_TERMINAL_COMMAND_BYTES",
    "MAX_CLIENT_TERMINAL_ID_BYTES",
    "MAX_CLIENT_TERMINAL_OUTPUT_BYTES",
    "MAX_CLIENT_TERMINAL_RETAINED_TASKS",
    "MAX_CLIENT_TERMINAL_SIGNAL_BYTES",
    "MAX_CLIENT_TERMINAL_TASKS",
]
