"""Tool pipeline observation collaborator.

Stages 3A/3D of the Runtime Kernel split: this module owns the typed, redacted
supervision observation built after a tool terminal path and the full tool
execution pipeline (permission/approval, execution, workspace capture,
plan handoff, and unstarted-call recording).  ``AgentRuntime`` delegates tool
execution here; no event ordering, tool result pairing, cancellation, or
persistence behavior changes.

The module intentionally does not import :mod:`agent`; it depends only on
ports, domain values, runtime collaborators, and supervision primitives.

提供工具流水线观察协作者,负责权限审批、执行、工作区捕获、计划交接和未启动调用记录.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from time import monotonic
from typing import Any

from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    build_permission_request,
)
from neuro_code.application.permissions.policy import (
    PermissionDecision,
    PermissionEffect,
    PermissionManager,
)
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import Tool, ToolCollection, ToolContext
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeObserver,
    WorkspaceChangeReport,
)
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.supervision import (
    StableMetadataFact,
    ToolExecutionObservation,
    stable_metadata_fact,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import Message, Role, SessionItem, ToolCall
from neuro_code.domain.execution import ProgressKind
from neuro_code.domain.plans import SessionPlan
from neuro_code.domain.tools import ToolResult
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text

LOGGER = logging.getLogger(__name__)

_SUPERVISION_METADATA_KEYS = frozenset(
    {
        "client_delegated",
        "count",
        "exit_code",
        "is_background",
        "requested_count",
        "status",
        "terminal_count",
        "timed_out",
        "total_lines",
        "total_output_bytes",
        "truncated",
        "output_artifact_bytes",
        "output_artifact_truncated",
    }
)
_BACKGROUND_STATE_TOOL_NAMES = frozenset({"kill_task", "task_output"})


class ToolObservationBuilder:
    """Build redacted supervision observations after a tool terminal path.

    The builder is stateless apart from the redaction values bound to one
    tool context.  ``build`` may raise for unexpected inputs; the runtime
    caller keeps the fail-open ``None`` fallback and its logging.

    在工具终态路径之后构建脱敏的监督观察结果. 除脱敏值外构建器无状态,运行时保留失败开放的空值回退.
    """

    __slots__ = ("_redaction_values",)

    def __init__(self, redaction_values: tuple[str, ...]) -> None:
        self._redaction_values = redaction_values

    def metadata_facts(
        self,
        metadata: Mapping[str, object] | None,
    ) -> tuple[StableMetadataFact, ...]:
        if metadata is None:
            return ()
        facts: list[StableMetadataFact] = []
        for name in sorted(_SUPERVISION_METADATA_KEYS.intersection(metadata)):
            value = metadata[name]
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, str):
                rendered = value
            else:
                continue
            facts.append(
                stable_metadata_fact(
                    name,
                    rendered,
                    redaction_values=self._redaction_values,
                )
            )
        return tuple(facts)

    @staticmethod
    def workspace_progress_token(
        change_report: WorkspaceChangeReport | None,
    ) -> str | None:
        if change_report is None or not change_report.files:
            return None
        payload = {
            "files": [
                {
                    "additions": change.additions,
                    "deletions": change.deletions,
                    "path": change.path,
                    "status": change.status,
                }
                for change in change_report.files
            ],
            "omitted_files": change_report.omitted_files,
            "scan_limited": change_report.scan_limited,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def background_state_token(
        tool_name: str,
        metadata: Mapping[str, object] | None,
    ) -> str | None:
        if tool_name not in _BACKGROUND_STATE_TOOL_NAMES or metadata is None:
            return None
        values: list[str] = []
        for name in ("status", "total_output_bytes", "exit_code"):
            value = metadata.get(name)
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, str):
                rendered = value
            else:
                continue
            values.append(f"{name}={rendered}")
        return "|".join(values) if values else None

    @staticmethod
    def plan_from_tool_result(name: str, result: ToolResult) -> SessionPlan | None:
        if name != "update_plan" or result.is_error or result.metadata is None:
            return None
        raw_plan = result.metadata.get("plan")
        try:
            return SessionPlan.from_dict(raw_plan)
        except ValueError as error:
            raise ToolError("update_plan returned an invalid plan") from error

    def build(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: ToolResult,
        tool: Tool | None,
        change_report: WorkspaceChangeReport | None,
        plan_fingerprint_before: str | None,
        current_plan_fingerprint: str | None,
        tool_call_id: str,
    ) -> ToolExecutionObservation:
        """Build a fail-open, redacted supervision record after a tool terminal path.

        在工具终态路径之后构建失败开放且脱敏的监督记录."""

        workspace_changed = change_report is not None and bool(change_report.files)
        workspace_progress_token = self.workspace_progress_token(change_report)
        plan_fingerprint = (
            current_plan_fingerprint
            if current_plan_fingerprint != plan_fingerprint_before
            else None
        )
        external_state_token = self.background_state_token(tool_name, result.metadata)
        if workspace_changed:
            progress_kind = ProgressKind.WORKSPACE
        elif plan_fingerprint is not None:
            progress_kind = ProgressKind.PLAN
        elif external_state_token is not None:
            progress_kind = ProgressKind.EXTERNAL_STATE
        elif not result.is_error and tool is not None and not tool.side_effecting:
            progress_kind = ProgressKind.EVIDENCE
        else:
            progress_kind = ProgressKind.NONE
        return ToolExecutionObservation.from_result(
            tool_name=tool_name,
            arguments=arguments,
            result_content=result.content,
            is_error=result.is_error,
            metadata_facts=self.metadata_facts(result.metadata),
            workspace_changed=workspace_changed,
            workspace_progress_token=workspace_progress_token,
            plan_fingerprint=plan_fingerprint,
            external_state_token=external_state_token,
            progress_kind=progress_kind,
            path_context=None,
            redaction_values=self._redaction_values,
            tool_call_id=tool_call_id,
        )


class ToolExecutor:
    """Execute one tool call through the permission/workspace/supervision pipeline.

    通过权限、工作区和监督流水线执行一次工具调用."""

    __slots__ = (
        "_approver",
        "_context_builder",
        "_observation_builder",
        "_permissions",
        "_session_store",
        "_tool_context",
        "_tools",
        "_workspace_change_observer",
    )

    def __init__(
        self,
        *,
        tools: ToolCollection,
        permissions: PermissionManager,
        approver: PermissionApprover | None,
        tool_context: ToolContext,
        session_store: SessionStore | None,
        workspace_change_observer: WorkspaceChangeObserver,
        context_builder: ContextBuilder,
    ) -> None:
        self._tools = tools
        self._permissions = permissions
        self._approver = approver
        self._tool_context = tool_context
        self._session_store = session_store
        self._workspace_change_observer = workspace_change_observer
        self._context_builder = context_builder
        self._observation_builder = ToolObservationBuilder(tool_context.redaction_values)

    async def execute(
        self,
        call: ToolCall,
        messages: list[Message],
        context_items: list[SessionItem],
        emit: Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]],
        session_id: str | None,
        *,
        interrupted_observation_sink: Callable[[ToolExecutionObservation], None] | None = None,
        workspace_change_sink: Callable[[WorkspaceChangeReport], None] | None = None,
    ) -> ToolExecutionObservation | None:
        resolved = False
        tool_requested_at = monotonic()
        workspace_before: WorkspaceChangeCheckpoint | None = None
        change_report: WorkspaceChangeReport | None = None
        tool: Tool | None = None
        result: ToolResult | None = None
        plan_fingerprint_before = (
            self._context_builder.plan.fingerprint
            if self._context_builder.plan is not None
            else None
        )

        def terminal_event_data(result: ToolResult, **extra: object) -> dict[str, object]:
            return {
                "id": call.id,
                "name": call.name,
                **result.to_dict(),
                "duration_seconds": monotonic() - tool_requested_at,
                **extra,
            }

        def record_result(result: ToolResult) -> None:
            nonlocal resolved
            if resolved:
                return
            message = Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
            messages.append(message)
            context_items.append(message)
            resolved = True

        try:
            await emit(
                AgentEventKind.TOOL_REQUESTED,
                {"id": call.id, "name": call.name, "arguments": dict(call.arguments)},
            )
            tool = self._tools.get(call.name)
            if tool is None:
                result = ToolResult(f"unknown tool: {call.name}", is_error=True)
                record_result(result)
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_event_data(result),
                )
                return self._tool_execution_observation(
                    call,
                    result,
                    tool=None,
                    change_report=None,
                    plan_fingerprint_before=plan_fingerprint_before,
                )

            decision = self._permissions.decide(
                call.name,
                call.arguments,
                side_effecting=tool.side_effecting,
            )
            await emit(
                AgentEventKind.TOOL_PERMISSION,
                {
                    "id": call.id,
                    "name": call.name,
                    "effect": decision.effect.value,
                    "reason": decision.reason,
                },
            )
            if decision.effect is PermissionEffect.ASK:
                request = build_permission_request(
                    call.id,
                    call.name,
                    call.arguments,
                    decision.reason,
                )
                await emit(
                    AgentEventKind.TOOL_APPROVAL_REQUESTED,
                    {
                        "id": call.id,
                        "name": call.name,
                        "reason": request.reason,
                        "summary": request.summary,
                    },
                )
                approval = (
                    await self._approver.request(request)
                    if self._approver is not None
                    else PermissionApproval.deny("interactive approval interface is unavailable")
                )
                effect = PermissionEffect.ALLOW if approval.allowed else PermissionEffect.DENY
                decision = PermissionDecision(effect, approval.reason)
                await emit(
                    AgentEventKind.TOOL_APPROVAL_RESOLVED,
                    {
                        "id": call.id,
                        "name": call.name,
                        "effect": effect.value,
                        "outcome": approval.kind.value,
                        "reason": approval.reason,
                    },
                )
            if not decision.allowed:
                result = ToolResult(f"permission denied: {decision.reason}", is_error=True)
                record_result(result)
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_event_data(result),
                )
                return self._tool_execution_observation(
                    call,
                    result,
                    tool=tool,
                    change_report=None,
                    plan_fingerprint_before=plan_fingerprint_before,
                )

            await emit(AgentEventKind.TOOL_STARTED, {"id": call.id, "name": call.name})
            if tool.side_effecting:
                workspace_before = await self.capture_workspace_snapshot()
            try:
                result = await tool.execute(call.arguments, self._tool_context)
            except (ToolError, OSError, UnicodeError) as error:
                result = ToolResult(f"{type(error).__name__}: {error}", is_error=True)
            safe_content = redact_sensitive_text(
                result.content,
                explicit_values=self._tool_context.redaction_values,
            )
            if safe_content != result.content:
                result = ToolResult(
                    safe_content,
                    is_error=result.is_error,
                    metadata=result.metadata,
                )
            kind = AgentEventKind.TOOL_FAILED if result.is_error else AgentEventKind.TOOL_COMPLETED
            plan = self._observation_builder.plan_from_tool_result(call.name, result)
            if plan is not None:
                if self._session_store is None or session_id is None:
                    raise ToolError("session-backed plan storage is unavailable")
                await self._session_store.save_session_plan(session_id, plan)
                self._context_builder.set_plan(plan)
            record_result(result)
            terminal_data = terminal_event_data(result)
            change_report = await self.workspace_change_report(workspace_before)
            if change_report is not None:
                terminal_data["workspace_changes"] = change_report.to_event_payload()
            await emit(kind, terminal_data)
            if plan is not None:
                await emit(AgentEventKind.PLAN_UPDATED, plan.to_dict())
            if change_report is not None and workspace_change_sink is not None:
                workspace_change_sink(change_report)
            return self._tool_execution_observation(
                call,
                result,
                tool=tool,
                change_report=change_report,
                plan_fingerprint_before=plan_fingerprint_before,
            )
        except BaseException as error:
            if not resolved:
                cancelled = isinstance(error, asyncio.CancelledError)
                result = ToolResult(
                    (
                        "tool call cancelled before completion"
                        if cancelled
                        else "tool call interrupted before completion"
                    ),
                    is_error=True,
                )
                record_result(result)
                terminal_data = terminal_event_data(result, cancelled=cancelled)
                change_report = await self.workspace_change_report(workspace_before)
                if change_report is not None:
                    terminal_data["workspace_changes"] = change_report.to_event_payload()
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_data,
                )
            if result is not None and interrupted_observation_sink is not None:
                observation = self._tool_execution_observation(
                    call,
                    result,
                    tool=tool,
                    change_report=change_report,
                    plan_fingerprint_before=plan_fingerprint_before,
                )
                if observation is not None:
                    interrupted_observation_sink(observation)
            raise

    def _tool_execution_observation(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        tool: Tool | None,
        change_report: WorkspaceChangeReport | None,
        plan_fingerprint_before: str | None,
    ) -> ToolExecutionObservation | None:
        """Build a fail-open, redacted supervision record after a tool terminal path.

        在工具终态路径之后构建失败开放且脱敏的监督记录."""

        try:
            current_plan_fingerprint = (
                self._context_builder.plan.fingerprint
                if self._context_builder.plan is not None
                else None
            )
            return self._observation_builder.build(
                tool_name=call.name,
                arguments=call.arguments,
                result=result,
                tool=tool,
                change_report=change_report,
                plan_fingerprint_before=plan_fingerprint_before,
                current_plan_fingerprint=current_plan_fingerprint,
                tool_call_id=call.id,
            )
        except Exception as error:
            LOGGER.debug(
                "supervision tool observation unavailable error_type=%s",
                type(error).__name__,
            )
            return None

    async def capture_workspace_snapshot(self) -> WorkspaceChangeCheckpoint | None:
        try:
            return await run_blocking(
                self._workspace_change_observer.capture, self._tool_context.cwd
            )
        except (OSError, RuntimeError):
            return None

    async def workspace_change_report(
        self,
        before: WorkspaceChangeCheckpoint | None,
    ) -> WorkspaceChangeReport | None:
        if before is None:
            return None
        after = await self.capture_workspace_snapshot()
        if after is None:
            return None
        protected_names = {
            name.casefold() for name in self._tool_context.protected_environment_variables
        }
        redactions = tuple(
            dict.fromkeys(
                value
                for name, value in os.environ.items()
                if name.casefold() in protected_names and value
            )
        )
        report = await run_blocking(
            self._workspace_change_observer.compare,
            before,
            after,
            explicit_redactions=redactions,
        )
        return report if report.should_emit else None

    @staticmethod
    async def record_unstarted_tool_calls(
        calls: Sequence[ToolCall],
        messages: list[Message],
        context_items: list[SessionItem],
        emit: Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]],
        *,
        cancelled: bool,
    ) -> None:
        if not calls:
            return
        result = ToolResult(
            (
                "tool call cancelled before execution"
                if cancelled
                else "tool call skipped because the turn stopped"
            ),
            is_error=True,
        )
        for call in calls:
            message = Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
            messages.append(message)
            context_items.append(message)
        for call in calls:
            await emit(
                AgentEventKind.TOOL_FAILED,
                {
                    "id": call.id,
                    "name": call.name,
                    **result.to_dict(),
                    "cancelled": cancelled,
                    "not_started": True,
                },
            )


__all__ = ["ToolExecutor", "ToolObservationBuilder"]
