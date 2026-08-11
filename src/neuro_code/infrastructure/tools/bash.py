"""Canonical shell command tool infrastructure adapter.

定义规范的 Shell 命令工具基础设施适配器."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessNetworkPolicy,
    LocalProcessOutput,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
    ToolContext,
    ToolOutputArtifact,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox
from neuro_code.shared.errors import BackgroundTaskCapacityError, ToolError

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CapturedOutput:
    preview: bytes
    artifact: bytes
    truncated: bool
    artifact_truncated: bool


class BashTool:
    side_effecting = True

    def __init__(
        self,
        *,
        background_enabled: bool = False,
        local_process_sandbox: LocalProcessSandbox | None = None,
    ) -> None:
        self._background_enabled = background_enabled
        self._requires_context_process_sandbox = local_process_sandbox is None
        self._local_process_sandbox = (
            local_process_sandbox
            if local_process_sandbox is not None
            else ProcessTreeLocalProcessSandbox()
        )
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
    async def _read_limited(
        stream: LocalProcessOutput,
        byte_limit: int,
        artifact_limit: int = 0,
    ) -> _CapturedOutput:
        captured = bytearray()
        artifact = bytearray()
        total_bytes = 0
        truncated = False
        artifact_truncated = False
        while chunk := await stream.read(65_536):
            remaining = max(0, byte_limit - len(captured))
            captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
            if artifact_limit:
                artifact_remaining = max(0, artifact_limit - len(artifact))
                artifact.extend(chunk[:artifact_remaining])
                if len(chunk) > artifact_remaining:
                    artifact_truncated = True
            total_bytes += len(chunk)
        if total_bytes > byte_limit:
            truncated = True
        return _CapturedOutput(
            preview=bytes(captured),
            artifact=bytes(artifact),
            truncated=truncated,
            artifact_truncated=artifact_truncated,
        )

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

        env = self._child_environment(context)
        background_timeout = float(timeout) if has_explicit_timeout else None
        auto_promote = (
            self._background_enabled and not is_background and context.background_tasks is not None
        )
        if is_background:
            if context.background_tasks is None:
                raise ToolError("background command execution is unavailable")
            snapshot = await context.background_tasks.start_process(
                self._process_request(
                    command=command,
                    context=context,
                    environment=env,
                    purpose=LocalProcessPurpose.BACKGROUND_BASH,
                    stdio_mode=LocalProcessStdioMode.MERGED_CAPTURE,
                ),
                display_command=command,
                output_byte_limit=context.output_byte_limit,
                timeout_seconds=background_timeout,
                output_artifact_store=context.output_artifact_store,
            )
            return self._background_result(snapshot.task_id)
        if auto_promote:
            managed_result = await self._managed_foreground_result(
                command=command,
                wait_budget=float(timeout),
                context=context,
                request=self._process_request(
                    command=command,
                    context=context,
                    environment=env,
                    purpose=LocalProcessPurpose.BACKGROUND_BASH,
                    stdio_mode=LocalProcessStdioMode.MERGED_CAPTURE,
                ),
            )
            if managed_result is not None:
                return managed_result
        process = await self._process_sandbox(context).spawn(
            self._process_request(
                command=command,
                context=context,
                environment=env,
                purpose=LocalProcessPurpose.BASH,
                stdio_mode=LocalProcessStdioMode.CAPTURE,
            )
        )
        assert process.stdout is not None
        assert process.stderr is not None
        artifact_limit = (
            MAX_TOOL_OUTPUT_ARTIFACT_BYTES if context.output_artifact_store is not None else 0
        )
        execution = asyncio.gather(
            self._read_limited(process.stdout, context.output_byte_limit, artifact_limit),
            self._read_limited(process.stderr, context.output_byte_limit, artifact_limit),
            process.wait(),
        )
        try:
            stdout_capture, stderr_capture, _ = await asyncio.wait_for(
                asyncio.shield(execution), float(timeout)
            )
        except TimeoutError as error:
            await process.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise ToolError(f"command timed out after {timeout:g} seconds") from error
        except asyncio.CancelledError:
            await process.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise
        except Exception:
            await process.terminate(grace_seconds=context.termination_grace_seconds)
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise

        stdout = stdout_capture.preview
        stderr = stderr_capture.preview
        combined = stdout + (b"\n" if stdout and stderr else b"") + stderr
        combined_truncated = len(combined) > context.output_byte_limit
        truncated = stdout_capture.truncated or stderr_capture.truncated or combined_truncated
        if combined_truncated:
            combined = combined[: context.output_byte_limit]
        content = combined.decode("utf-8", "replace")
        if truncated:
            content += "\n[output truncated]"
        artifact = None
        if truncated and context.output_artifact_store is not None:
            raw_artifact = stdout_capture.artifact
            if stdout_capture.artifact or stderr_capture.artifact:
                raw_artifact += (
                    b"\n" if stdout_capture.artifact and stderr_capture.artifact else b""
                ) + stderr_capture.artifact
            artifact = await self._save_artifact(
                context,
                raw_artifact,
                content_truncated=(
                    stdout_capture.artifact_truncated or stderr_capture.artifact_truncated
                ),
            )
        metadata: dict[str, object] = {
            "exit_code": process.returncode,
            "truncated": truncated,
        }
        self._add_artifact_metadata(metadata, artifact)
        return ToolResult(
            content,
            is_error=process.returncode != 0,
            metadata=metadata,
        )

    @staticmethod
    async def _save_artifact(
        context: ToolContext,
        content: bytes,
        *,
        content_truncated: bool = False,
    ) -> ToolOutputArtifact | None:
        store = context.output_artifact_store
        if store is None:
            return None
        try:
            return await store.save(
                tool_name="bash",
                content=content,
                content_truncated=content_truncated,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The artifact is diagnostic and must not turn a successful command
            # into a tool failure when the state directory is unavailable.
            LOGGER.debug("tool output artifact was not saved: %s", type(error).__name__)
            return None

    @staticmethod
    def _add_artifact_metadata(
        metadata: dict[str, object],
        artifact: ToolOutputArtifact | None,
    ) -> None:
        if artifact is None:
            return
        metadata.update(
            {
                "output_artifact_id": artifact.artifact_id,
                "output_artifact_path": artifact.relative_path,
                "output_artifact_bytes": artifact.byte_count,
                "output_artifact_truncated": artifact.truncated,
            }
        )

    async def _managed_foreground_result(
        self,
        *,
        command: str,
        wait_budget: float,
        context: ToolContext,
        request: SandboxedProcessRequest,
    ) -> ToolResult | None:
        """Wait on one manager-owned launch, promoting it without a respawn.

        等待一个由管理器拥有的启动任务,在不重新生成进程的情况下将其提升到前台."""

        manager = context.background_tasks
        assert manager is not None
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        try:
            started = await manager.start_process(
                request,
                display_command=command,
                output_byte_limit=context.output_byte_limit,
                timeout_seconds=None,
                output_artifact_store=context.output_artifact_store,
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

    def _process_sandbox(self, context: ToolContext) -> LocalProcessSandbox:
        """Resolve the composition-owned launcher without exposing ProcessTree.

        解析由 composition 拥有的启动器,而不暴露 ProcessTree.
        """

        if context.local_process_sandbox is not None:
            return context.local_process_sandbox
        if context.sandbox_profile.enabled and self._requires_context_process_sandbox:
            raise ToolError(
                f"sandbox profile {context.sandbox_profile.value!r} requires a child process sandbox"
            )
        return self._local_process_sandbox

    @staticmethod
    def _child_environment(context: ToolContext) -> dict[str, str]:
        """Build the environment request without leaking protected controller state.

        为环境请求构建变量,而不泄露受保护的 controller 状态.

        Enabled profiles intentionally use a small allowlist.  The Linux child
        adapter repeats that filtering before adding ``--clearenv`` so neither
        layer can accidentally turn an approval decision into credential
        exposure.  ``off`` retains the historic filtered host environment.

        启用的 profile 刻意使用小型白名单.Linux 子适配器会在添加 ``--clearenv``
        前再次过滤,因此两个层级都不会意外把审批决定变成凭据暴露.``off`` 保留
        历史上的已过滤宿主环境.
        """

        protected = {name.casefold() for name in context.protected_environment_variables}
        if context.sandbox_profile.enabled:
            allowed = {
                "COLORTERM",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "NO_COLOR",
                "PATH",
                "TERM",
            }
            environment = {
                name: value
                for name, value in os.environ.items()
                if name in allowed and name.casefold() not in protected
            }
        else:
            environment = {
                name: value
                for name, value in os.environ.items()
                if name.casefold() not in protected
            }
        environment["PAGER"] = "cat"
        environment["GIT_PAGER"] = "cat"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        return environment

    @staticmethod
    def _process_request(
        *,
        command: str,
        context: ToolContext,
        environment: Mapping[str, str],
        purpose: LocalProcessPurpose,
        stdio_mode: LocalProcessStdioMode,
    ) -> SandboxedProcessRequest:
        """Project a validated Bash execution into the canonical process request.

        将经过验证的 Bash 执行投影为规范进程请求.
        """

        workspace_mode = (
            LocalWorkspaceAccessMode.READ_ONLY
            if context.sandbox_profile is SandboxProfile.READ_ONLY
            else LocalWorkspaceAccessMode.READ_WRITE
        )
        roots = BashTool._workspace_access_roots(context, workspace_mode)
        filesystem_policy = LocalProcessFilesystemPolicy(
            roots,
            private_home=context.sandbox_profile.enabled,
            private_temporary_directory=context.sandbox_profile.enabled,
        )
        lifecycle = LocalProcessLifecycle(
            termination_grace_seconds=context.termination_grace_seconds,
        )
        cwd = context.cwd.expanduser().resolve()
        network_policy = (
            LocalProcessNetworkPolicy.ISOLATED
            if context.sandbox_profile.restricts_child_network
            else LocalProcessNetworkPolicy.INHERIT
        )
        environment_policy = LocalProcessEnvironmentPolicy(environment)
        return SandboxedProcessRequest.shell(
            command,
            purpose=purpose,
            cwd=cwd,
            sandbox_profile=context.sandbox_profile,
            filesystem_policy=filesystem_policy,
            network_policy=network_policy,
            environment_policy=environment_policy,
            stdio_mode=stdio_mode,
            lifecycle=lifecycle,
        )

    @staticmethod
    def _workspace_access_roots(
        context: ToolContext,
        mode: LocalWorkspaceAccessMode,
    ) -> tuple[LocalWorkspaceAccess, ...]:
        """Return non-overlapping roots visible to one Bash child.

        返回对一个 Bash 子进程可见且互不重叠的根目录.

        A nested additional root adds no capability beyond its already exposed
        parent.  Removing it avoids ambiguous Bubblewrap mount ordering while
        preserving all authorized paths.

        嵌套的 additional root 不会超出已公开父目录的能力.移除它可避免含糊的
        Bubblewrap 挂载顺序,同时保留所有已授权路径.
        """

        candidates = [context.cwd.expanduser().resolve()]
        candidates.extend(
            root.expanduser().resolve() for root in context.additional_workspace_roots
        )
        roots: list[Path] = []
        for candidate in candidates:
            if any(candidate == root or candidate.is_relative_to(root) for root in roots):
                continue
            if any(root.is_relative_to(candidate) for root in roots):
                raise ToolError(
                    "additional workspace roots must not contain the configured workspace"
                )
            roots.append(candidate)
        return tuple(LocalWorkspaceAccess(root, mode) for root in roots)

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
                **(
                    {
                        "output_artifact_id": snapshot.output_artifact_id,
                        "output_artifact_path": snapshot.output_artifact_path,
                        "output_artifact_bytes": snapshot.output_artifact_bytes,
                        "output_artifact_truncated": snapshot.output_artifact_truncated,
                    }
                    if snapshot.output_artifact_id is not None
                    else {}
                ),
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
