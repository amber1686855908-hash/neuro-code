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
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
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
from neuro_code.application.permissions.scopes import PermissionScopeContext
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.result_adoption import (
    WorkspaceMutationRequest,
    WorkspaceMutationResult,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tool_pipeline import ToolPipelineHook
from neuro_code.application.ports.tools import (
    FilesystemTargetProvider,
    Tool,
    ToolCollection,
    ToolContext,
)
from neuro_code.application.ports.web_search import HostedWebSearchEvent
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeObserver,
    WorkspaceChangeReport,
    WorkspaceMutationJournalProjection,
    WorkspaceMutationTargetProvider,
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
from neuro_code.domain.tools import ToolExecutionResult, ToolResult
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_arguments, redact_sensitive_text

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
        "_binding_scope_identity",
        "_context_builder",
        "_hooks",
        "_observation_builder",
        "_permissions",
        "_session_store",
        "_tool_context",
        "_tools",
        "_workspace_change_observer",
        "_workspace_mutation_tool",
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
        hooks: Sequence[ToolPipelineHook] = (),
        workspace_mutation_tool: Tool | None = None,
    ) -> None:
        self._tools = tools
        self._permissions = permissions
        self._approver = approver
        self._binding_scope_identity = uuid.uuid4().hex
        self._tool_context = tool_context
        self._session_store = session_store
        self._hooks = tuple(hooks)
        self._workspace_change_observer = workspace_change_observer
        self._workspace_mutation_tool = workspace_mutation_tool
        self._context_builder = context_builder
        self._observation_builder = ToolObservationBuilder(tool_context.redaction_values)

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult:
        """Apply one internal exact mutation through the normal write boundary.

        This is an application port, not a model tool call: it deliberately
        emits no conversation events and never exposes the hidden request to a
        provider.  Canonical path resolution, target permission evaluation,
        interactive approval, instruction checks, sandbox checks, and the
        injected filesystem executor remain the same effective layers used by
        ordinary workspace edits.
        """

        if not isinstance(request, WorkspaceMutationRequest):
            raise TypeError("workspace mutation request must be canonical")
        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            raise ValueError("workspace mutation session identity is invalid")
        tool = self._workspace_mutation_tool
        if tool is None:
            raise ToolError("internal workspace mutation is unavailable")
        arguments: dict[str, Any] = {
            "path": request.path,
            "operation": request.operation.value,
            "_workspace_mutation_request": request,
        }
        if not isinstance(tool, FilesystemTargetProvider):
            raise ToolError("internal workspace mutation has no canonical target provider")
        try:
            filesystem_access_plan = tool.prepare_filesystem_targets(
                arguments,
                self._tool_context,
            )
        except Exception as error:
            raise ToolError("internal workspace mutation target preflight failed") from error
        if (
            filesystem_access_plan is None
            or filesystem_access_plan.tool_name != tool.definition.name
        ):
            raise ToolError("internal workspace mutation target plan is invalid")
        decision = self._permissions.decide_targets(
            tool.definition.name,
            filesystem_access_plan.targets,
            side_effecting=tool.side_effecting,
        )
        if decision.effect is PermissionEffect.ASK:
            scope_context = self._permission_scope_context(session_id)
            scope_candidates = self._permissions.scope_candidates(
                tool.definition.name,
                arguments,
                decision=decision,
                targets=filesystem_access_plan.targets,
                workspace_root=Path(scope_context.workspace_root),
            )
            public_arguments = {
                "path": request.path,
                "operation": request.operation.value,
            }
            approval_request = build_permission_request(
                f"result-adoption-{uuid.uuid4().hex}",
                tool.definition.name,
                public_arguments,
                decision.reason,
                scope_candidates=scope_candidates,
                scope_context=scope_context,
            )
            approval = (
                await self._approver.request(approval_request)
                if self._approver is not None
                else PermissionApproval.deny("interactive approval interface is unavailable")
            )
            if not approval.allowed:
                raise ToolError(f"permission denied: {approval.reason}")
        elif not decision.allowed:
            raise ToolError(f"permission denied: {decision.reason}")
        result = await tool.execute(
            arguments,
            replace(self._tool_context, filesystem_access_plan=filesystem_access_plan),
        )
        if result.is_error:
            raise ToolError(result.content)
        return WorkspaceMutationResult(request.path, request.operation)

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
        recovery_started_sink: Callable[[str, str, bool], Awaitable[None]] | None = None,
    ) -> ToolExecutionObservation | None:
        resolved = False
        tool_requested_at = monotonic()
        workspace_before: WorkspaceChangeCheckpoint | None = None
        change_report: WorkspaceChangeReport | None = None
        tool: Tool | None = None
        result: ToolResult | None = None
        journal_started = False
        journal_recorded = False
        targeted_mutation = False
        target_paths: tuple[str, ...] = ()
        plan_fingerprint_before = (
            self._context_builder.plan.fingerprint
            if self._context_builder.plan is not None
            else None
        )

        def terminal_event_data(result: ToolResult, **extra: object) -> dict[str, object]:
            duration_seconds = monotonic() - tool_requested_at
            canonical = ToolExecutionResult.from_tool_result(
                call.id,
                call.name,
                result,
                duration_seconds=duration_seconds,
                not_started=extra.get("not_started") is True,
                cancelled=extra.get("cancelled") is True,
            )
            return {
                "id": call.id,
                "name": call.name,
                **result.to_dict(),
                "duration_seconds": duration_seconds,
                "execution_result": canonical.to_dict(),
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

        async def record_journal() -> None:
            nonlocal journal_recorded
            if journal_recorded or not journal_started:
                return
            journal_recorded = True
            journal = self._tool_context.workspace_change_journal
            if journal is None:
                return
            try:
                await run_blocking(
                    journal.after_mutation,
                    self._workspace_roots(),
                    tool_name=call.name,
                    mutation_metadata=(result.metadata if result is not None else None),
                    explicit_redactions=self._journal_redaction_values(),
                    target_paths=target_paths,
                )
            except Exception as error:
                LOGGER.debug(
                    "workspace mutation journal after-capture unavailable error_type=%s",
                    type(error).__name__,
                )

        try:
            safe_arguments = redact_sensitive_arguments(
                call.arguments,
                explicit_values=self._tool_context.redaction_values,
            )

            async def web_search_event_sink(event: HostedWebSearchEvent) -> AgentEvent | None:
                await emit(
                    AgentEventKind.BACKEND_TOOL_COMPLETED
                    if event.completed
                    else AgentEventKind.BACKEND_TOOL_STARTED,
                    {
                        "id": event.call_id,
                        "name": event.name,
                        "provider": event.provider_profile,
                        "model": event.model,
                    },
                )
                return None

            await emit(
                AgentEventKind.TOOL_REQUESTED,
                {"id": call.id, "name": call.name, "arguments": safe_arguments},
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

            filesystem_access_plan = None
            try:
                if isinstance(tool, FilesystemTargetProvider):
                    filesystem_access_plan = tool.prepare_filesystem_targets(
                        call.arguments,
                        self._tool_context,
                    )
                    if (
                        self._tool_context.client_file_system is None
                        and filesystem_access_plan is None
                    ):
                        raise ToolError(
                            "local structured filesystem tool did not prepare a canonical target plan"
                        )
                    if (
                        filesystem_access_plan is not None
                        and filesystem_access_plan.tool_name != call.name
                    ):
                        raise ToolError(
                            "canonical filesystem target plan does not match the requested tool"
                        )
            except Exception as error:
                decision = PermissionDecision(
                    PermissionEffect.DENY,
                    f"canonical filesystem target preflight failed: {type(error).__name__}: {error}",
                )
                await emit(
                    AgentEventKind.TOOL_PERMISSION,
                    {
                        "id": call.id,
                        "name": call.name,
                        "effect": decision.effect.value,
                        "reason": decision.reason,
                        "policy_source": decision.source.value,
                    },
                )
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

            if filesystem_access_plan is not None:
                decision = self._permissions.decide_targets(
                    call.name,
                    filesystem_access_plan.targets,
                    side_effecting=tool.side_effecting,
                )
            else:
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
                    "policy_source": decision.source.value,
                },
            )
            if decision.effect is PermissionEffect.ASK:
                scope_context = self._permission_scope_context(session_id)
                scope_candidates = self._permissions.scope_candidates(
                    call.name,
                    call.arguments,
                    decision=decision,
                    targets=(
                        filesystem_access_plan.targets if filesystem_access_plan is not None else ()
                    ),
                    workspace_root=(
                        Path(scope_context.workspace_root) if scope_context is not None else None
                    ),
                )
                request = build_permission_request(
                    call.id,
                    call.name,
                    call.arguments,
                    decision.reason,
                    scope_candidates=scope_candidates,
                    scope_context=scope_context,
                )
                await emit(
                    AgentEventKind.TOOL_APPROVAL_REQUESTED,
                    {
                        "id": call.id,
                        "name": call.name,
                        "reason": request.reason,
                        "summary": request.summary,
                        "scope_candidates": [
                            candidate.audit_metadata() for candidate in request.scope_candidates
                        ],
                        "scope_workspace_root": (
                            request.scope_context.workspace_root
                            if request.scope_context is not None and request.scope_candidates
                            else None
                        ),
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
                        "cache_hit": approval.cache_hit,
                        "scope": (
                            approval.scope_candidate.audit_metadata()
                            if approval.scope_candidate is not None
                            else None
                        ),
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

            for hook in self._hooks:
                await hook.before_tool(
                    call.id,
                    call.name,
                    safe_arguments,
                    side_effecting=tool.side_effecting,
                )
            if recovery_started_sink is None:
                await emit(AgentEventKind.TOOL_STARTED, {"id": call.id, "name": call.name})
            else:
                await recovery_started_sink(call.id, call.name, tool.side_effecting)
            if tool.side_effecting:
                journal = self._tool_context.workspace_change_journal
                target_paths = (
                    tuple(str(target.canonical_path) for target in filesystem_access_plan.targets)
                    if filesystem_access_plan is not None
                    else _workspace_target_paths(tool, call.arguments)
                )
                targeted_mutation = bool(target_paths and journal is not None)
                if not targeted_mutation:
                    workspace_before = await self.capture_workspace_snapshot()
                if targeted_mutation and journal is not None:
                    try:
                        await run_blocking(
                            journal.before_mutation,
                            self._workspace_roots(),
                            tool_name=call.name,
                            explicit_redactions=self._journal_redaction_values(),
                            target_paths=target_paths,
                        )
                        journal_started = True
                    except Exception as error:
                        LOGGER.debug(
                            "workspace mutation journal before-capture unavailable error_type=%s",
                            type(error).__name__,
                        )

            async def interaction_event_sink(
                kind: AgentEventKind,
                data: Mapping[str, object],
            ) -> AgentEvent:
                return await emit(kind, dict(data))

            try:
                result = await tool.execute(
                    call.arguments,
                    replace(
                        self._tool_context,
                        filesystem_access_plan=filesystem_access_plan,
                        interaction_event_sink=interaction_event_sink,
                        web_search_event_sink=web_search_event_sink,
                    ),
                )
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
            hook_result = ToolExecutionResult.from_tool_result(
                call.id,
                call.name,
                result,
                duration_seconds=monotonic() - tool_requested_at,
            )
            for hook in self._hooks:
                try:
                    await hook.after_tool(hook_result)
                except Exception as error:
                    LOGGER.warning(
                        "tool post-hook failed hook=%s error_type=%s",
                        type(hook).__name__,
                        type(error).__name__,
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
            await record_journal()
            if targeted_mutation:
                change_report = _journal_change_report(
                    self._tool_context.workspace_change_journal,
                    self._journal_redaction_values(),
                )
            else:
                change_report = await self.workspace_change_report(workspace_before)
                _record_external_observation(
                    self._tool_context.workspace_change_journal,
                    change_report,
                )
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
                if not targeted_mutation:
                    change_report = await self.workspace_change_report(workspace_before)
                    _record_external_observation(
                        self._tool_context.workspace_change_journal,
                        change_report,
                    )
                if change_report is not None:
                    terminal_data["workspace_changes"] = change_report.to_event_payload()
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_data,
                )
            await record_journal()
            if targeted_mutation:
                change_report = _journal_change_report(
                    self._tool_context.workspace_change_journal,
                    self._journal_redaction_values(),
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

    def _workspace_roots(self) -> tuple[Path, ...]:
        return (self._tool_context.cwd, *self._tool_context.additional_workspace_roots)

    def _permission_scope_context(self, session_id: str | None) -> PermissionScopeContext:
        """Build trusted in-memory grant identity from runtime-owned values."""

        identity = (
            session_id
            if isinstance(session_id, str)
            and bool(session_id)
            and "\x00" not in session_id
            and len(session_id.encode("utf-8")) <= 512
            else f"executor:{self._binding_scope_identity}"
        )
        try:
            root = self._tool_context.cwd.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            root = Path(os.path.abspath(os.fspath(self._tool_context.cwd)))
        return PermissionScopeContext(identity, os.fspath(root))

    def _journal_redaction_values(self) -> tuple[str, ...]:
        protected_names = {
            name.casefold() for name in self._tool_context.protected_environment_variables
        }
        return tuple(
            dict.fromkeys(
                (
                    *self._tool_context.redaction_values,
                    *(
                        value
                        for name, value in os.environ.items()
                        if name.casefold() in protected_names and value
                    ),
                )
            )
        )

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
            canonical = ToolExecutionResult.from_tool_result(
                call.id,
                call.name,
                result,
                not_started=True,
                cancelled=cancelled,
            )
            await emit(
                AgentEventKind.TOOL_FAILED,
                {
                    "id": call.id,
                    "name": call.name,
                    **result.to_dict(),
                    "cancelled": cancelled,
                    "not_started": True,
                    "execution_result": canonical.to_dict(),
                },
            )

    @staticmethod
    async def record_rejected_tool_calls(
        calls: Sequence[ToolCall],
        messages: list[Message],
        context_items: list[SessionItem],
        emit: Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]],
        *,
        reason: str,
    ) -> None:
        """Pair a control-tool batch rejection without executing any call."""

        result = ToolResult(reason, is_error=True)
        for call in calls:
            message = Message(Role.TOOL, reason, name=call.name, tool_call_id=call.id)
            messages.append(message)
            context_items.append(message)
        for call in calls:
            canonical = ToolExecutionResult.from_tool_result(
                call.id,
                call.name,
                result,
                not_started=True,
            )
            await emit(
                AgentEventKind.TOOL_FAILED,
                {
                    "id": call.id,
                    "name": call.name,
                    **result.to_dict(),
                    "not_started": True,
                    "control_batch_rejected": True,
                    "execution_result": canonical.to_dict(),
                },
            )


def _workspace_target_paths(tool: Tool, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    if not isinstance(tool, WorkspaceMutationTargetProvider):
        return ()
    try:
        paths = tool.workspace_target_paths(arguments)
    except Exception as error:
        LOGGER.debug(
            "workspace target discovery unavailable error_type=%s",
            type(error).__name__,
        )
        return ()
    if not isinstance(paths, tuple) or not all(isinstance(path, str) for path in paths):
        return ()
    return tuple(dict.fromkeys(path for path in paths if path))


def _journal_change_report(
    journal: object | None,
    redactions: tuple[str, ...],
) -> WorkspaceChangeReport | None:
    if not isinstance(journal, WorkspaceMutationJournalProjection):
        return None
    try:
        return journal.last_change_report(explicit_redactions=redactions)
    except Exception as error:
        LOGGER.debug(
            "workspace journal report unavailable error_type=%s",
            type(error).__name__,
        )
        return None


def _record_external_observation(
    journal: object | None,
    report: WorkspaceChangeReport | None,
) -> None:
    if report is None or not isinstance(journal, WorkspaceMutationJournalProjection):
        return
    try:
        journal.record_external_observation(report)
    except Exception as error:
        LOGGER.debug(
            "workspace journal external observation unavailable error_type=%s",
            type(error).__name__,
        )


__all__ = ["ToolExecutor", "ToolObservationBuilder"]
