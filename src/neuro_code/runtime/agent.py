from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from time import monotonic

from neuro_code.async_utils import run_blocking
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.interaction_mode import InteractionMode, interaction_mode_guidance
from neuro_code.domain.messages import (
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    ToolCall,
)
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.reasoning import ReasoningEffort, reasoning_guidance
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.tools import ToolResult
from neuro_code.errors import ProviderError, ToolError
from neuro_code.permissions import (
    PermissionApproval,
    PermissionDecision,
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    build_permission_request,
)
from neuro_code.ports.approval import PermissionApprover
from neuro_code.ports.model import ModelProvider
from neuro_code.ports.storage import SessionStore
from neuro_code.ports.tools import ToolContext
from neuro_code.runtime.background_task_reminders import (
    BACKGROUND_TASK_COMPLETION_BATCH_LIMIT,
    format_background_task_completion_reminder,
)
from neuro_code.tools.registry import ToolRegistry
from neuro_code.workspace_changes import (
    WorkspaceSnapshot,
    capture_workspace_snapshot,
    compare_workspace_snapshots,
)

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


DEFAULT_SYSTEM_PROMPT = """You are Neuro Code, a terminal coding agent.
Use tools when repository evidence is needed. Read before editing. Never claim a
tool action succeeded unless its result confirms success. Keep the final answer
concise and state which files or checks changed. Prefer workspace edit tools over
shell redirection when changing files so the resulting changes remain auditable."""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    session_id: str | None
    response: str
    messages: tuple[Message, ...]
    items: tuple[SessionItem, ...]
    events: tuple[AgentEvent, ...]
    steps: int


class AgentRuntime:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        permissions: PermissionManager,
        tool_context: ToolContext,
        approver: PermissionApprover | None = None,
        session_store: SessionStore | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 24,
        reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH,
        interaction_mode: InteractionMode | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._provider = provider
        self._tools = tools
        self._permissions = permissions
        self._tool_context = tool_context
        self._approver = approver
        self._session_store = session_store
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._reasoning_effort = reasoning_effort
        self._auto_permission_mode = (
            PermissionMode.BYPASS
            if permissions.mode is PermissionMode.BYPASS
            else PermissionMode.ACCEPT_EDITS
        )
        inferred_mode = {
            PermissionMode.DEFAULT: InteractionMode.NORMAL,
            PermissionMode.ACCEPT_EDITS: InteractionMode.ACCEPT_EDITS,
            PermissionMode.DONT_ASK: InteractionMode.PLAN,
            PermissionMode.BYPASS: InteractionMode.AUTO,
        }[permissions.mode]
        self._interaction_mode = interaction_mode or inferred_mode
        self._apply_interaction_mode_permissions()

    @property
    def sandbox_profile(self) -> SandboxProfile:
        return self._tool_context.sandbox_profile

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        if not isinstance(effort, ReasoningEffort):
            raise TypeError("reasoning effort must be a ReasoningEffort")
        self._reasoning_effort = effort

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return self._auto_permission_mode is PermissionMode.BYPASS

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        if not isinstance(mode, InteractionMode):
            raise TypeError("interaction mode must be an InteractionMode")
        self._interaction_mode = mode
        self._apply_interaction_mode_permissions()

    def _apply_interaction_mode_permissions(self) -> None:
        permission_mode = {
            InteractionMode.NORMAL: PermissionMode.DEFAULT,
            InteractionMode.ACCEPT_EDITS: PermissionMode.ACCEPT_EDITS,
            InteractionMode.PLAN: PermissionMode.DONT_ASK,
            InteractionMode.AUTO: self._auto_permission_mode,
        }[self._interaction_mode]
        self._permissions.set_mode(permission_mode)

    def _model_items_with_reasoning_guidance(
        self,
        items: Sequence[SessionItem],
    ) -> tuple[SessionItem, ...]:
        """Apply the selected policy to a request without persisting control text."""

        instruction = "\n\n".join(
            (
                reasoning_guidance(self._reasoning_effort),
                interaction_mode_guidance(self._interaction_mode),
            )
        )
        rendered = tuple(items)
        for index, item in enumerate(rendered):
            if not isinstance(item, Message) or item.role is not Role.SYSTEM:
                continue
            guided = Message(Role.SYSTEM, f"{item.model_content()}\n\n{instruction}")
            return (*rendered[:index], guided, *rendered[index + 1 :])
        return (Message(Role.SYSTEM, instruction), *rendered)

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        initial_items: Sequence[SessionItem] = (),
        source_provider: str | None = None,
        source_model: str | None = None,
        source_context_affinity: str | None = None,
        session_id: str | None = None,
    ) -> AgentRunResult:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        turn_started_at = monotonic()
        context_items = list(initial_items)
        messages = [item for item in context_items if isinstance(item, Message)]
        context_source_provider = source_provider
        context_source_model = source_model
        context_source_affinity = source_context_affinity
        can_adopt_provider_origin = not any(
            isinstance(item, PreservedContextItem) for item in context_items
        )
        if not messages:
            system_message = Message(Role.SYSTEM, self._system_prompt)
            context_items.append(system_message)
            messages.append(system_message)
        events: list[AgentEvent] = []
        sequence = 0

        if self._session_store is not None and session_id is None:
            session_id = await self._session_store.create_session(
                str(self._tool_context.cwd),
                self._provider.provider_name,
                self._provider.model_name,
                getattr(self._provider, "context_affinity", None),
                self._tool_context.sandbox_profile,
            )
        elif self._session_store is not None and session_id is not None:
            sequence = await self._session_store.next_event_sequence(session_id) - 1

        async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
            nonlocal sequence
            sequence += 1
            event = AgentEvent.create(sequence, kind, data)
            events.append(event)
            if self._session_store is not None and session_id is not None:
                await self._session_store.append_event(session_id, event)
            if sink is not None:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
            return event

        async def record_turn_failure(error: BaseException) -> None:
            cancelled = isinstance(error, asyncio.CancelledError)
            await emit(
                AgentEventKind.TURN_FAILED,
                {
                    "error_type": type(error).__name__,
                    "message": "turn cancelled" if cancelled else str(error),
                    "cancelled": cancelled,
                    "duration_seconds": monotonic() - turn_started_at,
                },
            )
            if self._session_store is not None and session_id is not None:
                await self._session_store.save_session_items(session_id, context_items)

        response_parts: list[str] = []
        completion_reminders: list[Message] = []
        try:
            await emit(
                AgentEventKind.SESSION_STARTED,
                {
                    "session_id": session_id or "",
                    "provider": self._provider.provider_name,
                    "model": self._provider.model_name,
                },
            )
            user_message = Message(Role.USER, prompt)
            context_items.append(user_message)
            messages.append(user_message)
            await emit(AgentEventKind.USER_MESSAGE, {"content": prompt})

            for step in range(1, self._max_steps + 1):
                step_started_at = monotonic()
                thinking_completed = False

                async def complete_thinking(
                    step_number: int = step,
                    started_at: float = step_started_at,
                ) -> None:
                    nonlocal thinking_completed
                    if thinking_completed:
                        return
                    thinking_completed = True
                    await emit(
                        AgentEventKind.MODEL_THINKING_COMPLETED,
                        {
                            "step": step_number,
                            "duration_seconds": monotonic() - started_at,
                        },
                    )

                await emit(AgentEventKind.MODEL_STEP_STARTED, {"step": step})
                completion_batch: tuple[BackgroundTaskSnapshot, ...] = ()
                background_tasks = self._tool_context.background_tasks
                if background_tasks is not None:
                    pending_completions = await background_tasks.pending_completions()
                    completion_batch = pending_completions[:BACKGROUND_TASK_COMPLETION_BATCH_LIMIT]
                    if completion_batch:
                        remaining_count = len(pending_completions) - len(completion_batch)
                        completion_reminders.append(
                            Message(
                                Role.USER,
                                format_background_task_completion_reminder(
                                    completion_batch,
                                    remaining_count=remaining_count,
                                    task_output_tool=(
                                        "task_output"
                                        if self._tools.get("task_output") is not None
                                        else None
                                    ),
                                ),
                            )
                        )
                        await emit(
                            AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER,
                            {
                                "task_ids": [snapshot.task_id for snapshot in completion_batch],
                                "statuses": [
                                    snapshot.status.value for snapshot in completion_batch
                                ],
                                "count": len(completion_batch),
                                "remaining_count": remaining_count,
                                "model_context_only": True,
                            },
                        )
                step_text: list[str] = []
                step_reasoning: list[str] = []
                tool_calls: list[ToolCall] = []
                completion: ModelCompleted | None = None
                backend_tool_started_at: dict[str, float] = {}

                context = ModelContext(
                    self._model_items_with_reasoning_guidance(
                        (*context_items, *completion_reminders)
                    ),
                    context_source_provider,
                    context_source_model,
                    context_source_affinity,
                    self._reasoning_effort,
                )
                async for model_event in self._provider.stream(context, self._tools.definitions()):
                    if isinstance(model_event, ModelProviderAttemptFailed):
                        await emit(
                            AgentEventKind.PROVIDER_ATTEMPT_FAILED,
                            {
                                "provider": model_event.provider,
                                "model": model_event.model,
                                "error_type": model_event.error_type,
                                "message": model_event.message,
                            },
                        )
                    elif isinstance(model_event, ModelProviderSelected):
                        origin_updated = False
                        if (
                            can_adopt_provider_origin
                            and self._session_store is not None
                            and session_id is not None
                        ):
                            await self._session_store.update_session_provider(
                                session_id,
                                model_event.provider,
                                model_event.model,
                                model_event.context_affinity,
                            )
                            origin_updated = True
                        await emit(
                            AgentEventKind.PROVIDER_SELECTED,
                            {
                                "provider": model_event.provider,
                                "model": model_event.model,
                                "context_window_tokens": model_event.context_window_tokens,
                                "failover": model_event.failover,
                                "session_origin_updated": origin_updated,
                            },
                        )
                    elif isinstance(model_event, ModelTextDelta):
                        await complete_thinking()
                        step_text.append(model_event.text)
                        await emit(AgentEventKind.TEXT_DELTA, {"text": model_event.text})
                    elif isinstance(model_event, ModelReasoningDelta):
                        step_reasoning.append(model_event.text)
                        await emit(
                            AgentEventKind.REASONING_DELTA,
                            {"text": model_event.text},
                        )
                    elif isinstance(model_event, ModelBackendToolStarted):
                        await complete_thinking()
                        backend_tool_started_at[model_event.call_id] = monotonic()
                        await emit(
                            AgentEventKind.BACKEND_TOOL_STARTED,
                            {"id": model_event.call_id, "name": model_event.name},
                        )
                    elif isinstance(model_event, ModelBackendToolCompleted):
                        await complete_thinking()
                        started_at = backend_tool_started_at.pop(
                            model_event.call_id,
                            step_started_at,
                        )
                        await emit(
                            AgentEventKind.BACKEND_TOOL_COMPLETED,
                            {
                                "id": model_event.call_id,
                                "name": model_event.name,
                                "duration_seconds": monotonic() - started_at,
                            },
                        )
                    elif isinstance(model_event, ModelToolCall):
                        await complete_thinking()
                        tool_calls.append(model_event.call)
                    elif isinstance(model_event, ModelCompleted):
                        await complete_thinking()
                        completion = model_event

                if completion is None:
                    raise ProviderError("provider stream ended without a completion event")
                if completion.input_tokens is not None:
                    output_tokens = completion.output_tokens or 0
                    await emit(
                        AgentEventKind.CONTEXT_USAGE_UPDATED,
                        {
                            "input_tokens": completion.input_tokens,
                            "output_tokens": completion.output_tokens,
                            "used_tokens": completion.input_tokens + output_tokens,
                            "estimated": completion.output_tokens is None,
                        },
                    )
                if completion_batch and background_tasks is not None:
                    await background_tasks.mark_completions_reported(
                        tuple(snapshot.task_id for snapshot in completion_batch)
                    )
                if completion.context_items:
                    context_items.extend(completion.context_items)
                    if can_adopt_provider_origin:
                        context_source_provider = self._provider.provider_name
                        context_source_model = self._provider.model_name
                        context_source_affinity = getattr(self._provider, "context_affinity", None)
                        can_adopt_provider_origin = False
                assistant_content = (
                    completion.response_text
                    if completion.response_text is not None
                    else "".join(step_text)
                )
                response_parts.append(assistant_content)
                assistant_message = Message(
                    Role.ASSISTANT,
                    assistant_content,
                    tool_calls=tuple(tool_calls),
                    reasoning_content="".join(step_reasoning) or None,
                )
                context_items.append(assistant_message)
                messages.append(assistant_message)

                if not tool_calls:
                    await emit(
                        AgentEventKind.TURN_COMPLETED,
                        {
                            "step": step,
                            "stop_reason": completion.stop_reason,
                            "input_tokens": completion.input_tokens,
                            "output_tokens": completion.output_tokens,
                            "duration_seconds": monotonic() - turn_started_at,
                        },
                    )
                    if self._session_store is not None and session_id is not None:
                        await self._session_store.save_session_items(session_id, context_items)
                    return AgentRunResult(
                        session_id,
                        "".join(response_parts),
                        tuple(messages),
                        tuple(context_items),
                        tuple(events),
                        step,
                    )

                for index, call in enumerate(tool_calls):
                    try:
                        await self._execute_tool(call, messages, context_items, emit)
                    except BaseException as error:
                        await self._record_unstarted_tool_calls(
                            tool_calls[index + 1 :],
                            messages,
                            context_items,
                            emit,
                            cancelled=isinstance(error, asyncio.CancelledError),
                        )
                        raise
                if self._session_store is not None and session_id is not None:
                    await self._session_store.save_session_items(session_id, context_items)

            raise ProviderError(f"agent exceeded the maximum of {self._max_steps} model steps")
        except BaseException as error:
            # Preserve cancellation semantics while still making the session auditable.
            await record_turn_failure(error)
            raise

    async def _execute_tool(
        self,
        call: ToolCall,
        messages: list[Message],
        context_items: list[SessionItem],
        emit: Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]],
    ) -> None:
        resolved = False
        tool_requested_at = monotonic()
        workspace_before: WorkspaceSnapshot | None = None

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
                return

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
                return

            await emit(AgentEventKind.TOOL_STARTED, {"id": call.id, "name": call.name})
            if tool.side_effecting:
                workspace_before = await self._capture_workspace_snapshot()
            try:
                result = await tool.execute(call.arguments, self._tool_context)
            except (ToolError, OSError, UnicodeError) as error:
                result = ToolResult(f"{type(error).__name__}: {error}", is_error=True)
            kind = AgentEventKind.TOOL_FAILED if result.is_error else AgentEventKind.TOOL_COMPLETED
            record_result(result)
            terminal_data = terminal_event_data(result)
            change_report = await self._workspace_change_report(workspace_before)
            if change_report is not None:
                terminal_data["workspace_changes"] = change_report
            await emit(kind, terminal_data)
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
                change_report = await self._workspace_change_report(workspace_before)
                if change_report is not None:
                    terminal_data["workspace_changes"] = change_report
                await emit(
                    AgentEventKind.TOOL_FAILED,
                    terminal_data,
                )
            raise

    async def _capture_workspace_snapshot(self) -> WorkspaceSnapshot | None:
        try:
            return await run_blocking(capture_workspace_snapshot, self._tool_context.cwd)
        except (OSError, RuntimeError):
            return None

    async def _workspace_change_report(
        self,
        before: WorkspaceSnapshot | None,
    ) -> dict[str, object] | None:
        if before is None:
            return None
        after = await self._capture_workspace_snapshot()
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
            compare_workspace_snapshots,
            before,
            after,
            explicit_redactions=redactions,
        )
        files = report.get("files")
        if files or report.get("scan_limited"):
            return report
        return None

    @staticmethod
    async def _record_unstarted_tool_calls(
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
