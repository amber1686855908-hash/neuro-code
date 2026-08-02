from __future__ import annotations

import asyncio
import contextlib
import math
import os
from collections.abc import Mapping
from typing import Any

from neuro_code.adapters.process_tree import ProcessTree
from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.sandbox import ShellLaunch
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import BackgroundTaskCapacityError, ToolError


class BashTool:
    side_effecting = True

    def __init__(self, *, background_enabled: bool = False) -> None:
        self._background_enabled = background_enabled
        properties: dict[str, object] = {
            "command": {"type": "string"},
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
        }
        description = "Run a shell command in the current workspace."
        if background_enabled:
            properties["is_background"] = {
                "type": "boolean",
                "description": "Return a task ID immediately and keep the command managed.",
            }
            description += (
                " Set is_background=true for a managed task that can be inspected with "
                "task_output or wait_tasks and stopped with kill_task. A foreground command "
                "that exceeds its wait budget continues as the same managed task."
            )
        self.definition = ToolDefinition(
            name="bash",
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader, byte_limit: int) -> tuple[bytes, bool]:
        captured = bytearray()
        truncated = False
        while chunk := await stream.read(65_536):
            remaining = max(0, byte_limit - len(captured))
            captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(captured), truncated

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        # Bash does not call instruction_tracker.check_path() because:
        # 1. Bash runs in context.cwd (workspace root), so calling
        #    check_path(context.cwd) would reset the tracker target to the
        #    root, losing any deep directory context gained from prior
        #    read_file/list_dir/grep calls.
        # 2. Bash can access files in any directory (via cd, paths, pipes),
        #    and parsing the command to extract target paths is fragile and
        #    out of scope for the instruction tracker's design.
        # As a result, bash is a coarse-grained tool: deep AGENTS.md files
        # in directories that bash writes to are NOT guaranteed to be in the
        # model's instruction context before the write happens.  Models that
        # need instruction-aware writes should use search_replace (which has
        # a pre-flight check) or read the target directory first.
        command = arguments.get("command")
        has_explicit_timeout = "timeout_seconds" in arguments
        explicit_timeout = arguments.get("timeout_seconds")
        timeout = explicit_timeout if has_explicit_timeout else context.command_timeout_seconds
        is_background = arguments.get("is_background", False)
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ToolError("command must be a non-empty string")
        if not isinstance(is_background, bool):
            raise ToolError("is_background must be a boolean")
        if is_background and not self._background_enabled:
            raise ToolError("background command execution is not enabled")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ToolError("timeout_seconds must be positive")
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        if (
            not math.isfinite(context.termination_grace_seconds)
            or context.termination_grace_seconds <= 0
        ):
            raise ToolError("termination_grace_seconds must be positive")

        protected = {name.casefold() for name in context.protected_environment_variables}
        env = {
            name: value for name, value in os.environ.items() if name.casefold() not in protected
        }
        env["PAGER"] = "cat"
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        background_timeout = float(timeout) if has_explicit_timeout else None
        auto_promote = (
            self._background_enabled and not is_background and context.background_tasks is not None
        )
        if context.sandbox_profile.enabled:
            if context.shell_sandbox is None:
                raise ToolError(
                    f"sandbox profile {context.sandbox_profile.value!r} is not enforced"
                )
            if context.shell_sandbox.profile is not context.sandbox_profile:
                raise ToolError("tool context sandbox profile does not match its shell adapter")
            launch = context.shell_sandbox.shell_launch(command)
            if is_background:
                if context.background_tasks is None:
                    raise ToolError("background command execution is unavailable")
                snapshot = await context.background_tasks.start_exec(
                    launch.executable,
                    launch.arguments,
                    display_command=command,
                    cwd=context.cwd,
                    env=env,
                    output_byte_limit=context.output_byte_limit,
                    termination_grace_seconds=context.termination_grace_seconds,
                    timeout_seconds=background_timeout,
                )
                return self._background_result(snapshot.task_id)
            if auto_promote:
                managed_result = await self._managed_foreground_result(
                    command=command,
                    wait_budget=float(timeout),
                    context=context,
                    env=env,
                    launch=launch,
                )
                if managed_result is not None:
                    return managed_result
            tree = await ProcessTree.spawn_exec(
                launch.executable,
                launch.arguments,
                cwd=context.cwd,
                env=env,
            )
        else:
            if is_background:
                if context.background_tasks is None:
                    raise ToolError("background command execution is unavailable")
                snapshot = await context.background_tasks.start_shell(
                    command,
                    cwd=context.cwd,
                    env=env,
                    output_byte_limit=context.output_byte_limit,
                    termination_grace_seconds=context.termination_grace_seconds,
                    timeout_seconds=background_timeout,
                )
                return self._background_result(snapshot.task_id)
            if auto_promote:
                managed_result = await self._managed_foreground_result(
                    command=command,
                    wait_budget=float(timeout),
                    context=context,
                    env=env,
                    launch=None,
                )
                if managed_result is not None:
                    return managed_result
            tree = await ProcessTree.spawn_shell(
                command,
                cwd=context.cwd,
                env=env,
            )
        process = tree.process
        assert process.stdout is not None
        assert process.stderr is not None
        execution = asyncio.gather(
            self._read_limited(process.stdout, context.output_byte_limit),
            self._read_limited(process.stderr, context.output_byte_limit),
            tree.wait(),
        )
        try:
            (stdout, stdout_truncated), (stderr, stderr_truncated), _ = await asyncio.wait_for(
                asyncio.shield(execution), float(timeout)
            )
        except TimeoutError as error:
            await tree.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise ToolError(f"command timed out after {timeout:g} seconds") from error
        except asyncio.CancelledError:
            await tree.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise
        except Exception:
            await tree.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise

        combined = stdout + (b"\n" if stdout and stderr else b"") + stderr
        combined_truncated = len(combined) > context.output_byte_limit
        truncated = stdout_truncated or stderr_truncated or combined_truncated
        if combined_truncated:
            combined = combined[: context.output_byte_limit]
        content = combined.decode("utf-8", "replace")
        if truncated:
            content += "\n[output truncated]"
        return ToolResult(
            content,
            is_error=process.returncode != 0,
            metadata={"exit_code": process.returncode, "truncated": truncated},
        )

    async def _managed_foreground_result(
        self,
        *,
        command: str,
        wait_budget: float,
        context: ToolContext,
        env: Mapping[str, str],
        launch: ShellLaunch | None,
    ) -> ToolResult | None:
        """Wait on one manager-owned launch, promoting it without a respawn."""

        manager = context.background_tasks
        assert manager is not None
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        try:
            if launch is None:
                started = await manager.start_shell(
                    command,
                    cwd=context.cwd,
                    env=env,
                    output_byte_limit=context.output_byte_limit,
                    termination_grace_seconds=context.termination_grace_seconds,
                    timeout_seconds=None,
                )
            else:
                started = await manager.start_exec(
                    launch.executable,
                    launch.arguments,
                    display_command=command,
                    cwd=context.cwd,
                    env=env,
                    output_byte_limit=context.output_byte_limit,
                    termination_grace_seconds=context.termination_grace_seconds,
                    timeout_seconds=None,
                )
        except BackgroundTaskCapacityError:
            # A full managed registry must not turn an ordinary short command
            # into a new failure. Fall back to the established foreground
            # behavior, which remains bounded and kills on timeout.
            return None

        task_id = started.task_id
        discarded = False
        try:
            remaining = max(0.0, wait_budget - (loop.time() - start_time))
            observed = await manager.get(task_id, wait_seconds=remaining)
            if observed is None:
                raise ToolError("managed foreground task disappeared while waiting")
            if observed.status is BackgroundTaskStatus.RUNNING:
                return self._promoted_result(observed)

            result = self._foreground_result(observed)
            if not await manager.discard_completed(task_id):
                raise ToolError("completed foreground task could not be discarded")
            discarded = True
            return result
        except asyncio.CancelledError as cancellation:
            try:
                await self._cleanup_managed_task(manager, task_id)
            except BaseException as cleanup_error:
                raise cleanup_error from cancellation
            raise
        except Exception as error:
            if not discarded:
                try:
                    await self._cleanup_managed_task(manager, task_id)
                except BaseException as cleanup_error:
                    raise cleanup_error from error
            raise

    @staticmethod
    async def _cleanup_managed_task(manager: BackgroundTaskManager, task_id: str) -> None:
        killed = await manager.kill(task_id)
        if killed is None:
            raise ToolError("managed foreground task could not be cleaned up")
        if not await manager.discard_completed(task_id):
            raise ToolError("managed foreground task record could not be discarded")

    @staticmethod
    def _foreground_result(snapshot: BackgroundTaskSnapshot) -> ToolResult:
        content = snapshot.output
        if snapshot.truncated:
            content += "\n[output truncated]"
        return ToolResult(
            content,
            is_error=(
                snapshot.status is not BackgroundTaskStatus.COMPLETED or snapshot.exit_code != 0
            ),
            metadata={
                "exit_code": snapshot.exit_code,
                "truncated": snapshot.truncated,
            },
        )

    @staticmethod
    def _promoted_result(snapshot: BackgroundTaskSnapshot) -> ToolResult:
        task_id = snapshot.task_id
        return ToolResult(
            (
                "Foreground wait budget expired; the command continues as a managed "
                f"background task: {task_id}\n"
                f"Use task_output, wait_tasks, or kill_task with task_id={task_id!r}."
            ),
            metadata={
                "task_id": task_id,
                "status": "running",
                "is_background": True,
                "promoted_from_foreground": True,
            },
        )

    @staticmethod
    def _background_result(task_id: str) -> ToolResult:
        return ToolResult(
            (
                f"Background task started: {task_id}\n"
                f"Use task_output with task_id={task_id!r} to inspect or wait for it."
            ),
            metadata={
                "task_id": task_id,
                "status": "running",
                "is_background": True,
            },
        )
