from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from neuro_code.domain.events import AgentEvent, AgentEventKind
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
from neuro_code.domain.tools import ToolResult
from neuro_code.errors import ProviderError, ToolError
from neuro_code.permissions import PermissionManager
from neuro_code.ports.model import ModelProvider
from neuro_code.ports.storage import SessionStore
from neuro_code.ports.tools import ToolContext
from neuro_code.tools.registry import ToolRegistry

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


DEFAULT_SYSTEM_PROMPT = """You are Neuro Code, a terminal coding agent.
Use tools when repository evidence is needed. Read before editing. Never claim a
tool action succeeded unless its result confirms success. Keep the final answer
concise and state which files or checks changed."""


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
        session_store: SessionStore | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 24,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._provider = provider
        self._tools = tools
        self._permissions = permissions
        self._tool_context = tool_context
        self._session_store = session_store
        self._system_prompt = system_prompt
        self._max_steps = max_steps

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

        response_parts: list[str] = []
        try:
            for step in range(1, self._max_steps + 1):
                await emit(AgentEventKind.MODEL_STEP_STARTED, {"step": step})
                step_text: list[str] = []
                step_reasoning: list[str] = []
                tool_calls: list[ToolCall] = []
                completion: ModelCompleted | None = None

                context = ModelContext(
                    tuple(context_items),
                    context_source_provider,
                    context_source_model,
                    context_source_affinity,
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
                                "failover": model_event.failover,
                                "session_origin_updated": origin_updated,
                            },
                        )
                    elif isinstance(model_event, ModelTextDelta):
                        step_text.append(model_event.text)
                        await emit(AgentEventKind.TEXT_DELTA, {"text": model_event.text})
                    elif isinstance(model_event, ModelReasoningDelta):
                        step_reasoning.append(model_event.text)
                        await emit(
                            AgentEventKind.REASONING_DELTA,
                            {"text": model_event.text},
                        )
                    elif isinstance(model_event, ModelBackendToolStarted):
                        await emit(
                            AgentEventKind.BACKEND_TOOL_STARTED,
                            {"id": model_event.call_id, "name": model_event.name},
                        )
                    elif isinstance(model_event, ModelBackendToolCompleted):
                        await emit(
                            AgentEventKind.BACKEND_TOOL_COMPLETED,
                            {"id": model_event.call_id, "name": model_event.name},
                        )
                    elif isinstance(model_event, ModelToolCall):
                        tool_calls.append(model_event.call)
                    elif isinstance(model_event, ModelCompleted):
                        completion = model_event

                if completion is None:
                    raise ProviderError("provider stream ended without a completion event")
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

                for call in tool_calls:
                    await self._execute_tool(call, messages, context_items, emit)
                if self._session_store is not None and session_id is not None:
                    await self._session_store.save_session_items(session_id, context_items)

            raise ProviderError(f"agent exceeded the maximum of {self._max_steps} model steps")
        except BaseException as error:
            # Preserve cancellation semantics while still making the session auditable.
            await emit(
                AgentEventKind.TURN_FAILED,
                {"error_type": type(error).__name__, "message": str(error)},
            )
            if self._session_store is not None and session_id is not None:
                await self._session_store.save_session_items(session_id, context_items)
            raise

    async def _execute_tool(
        self,
        call: ToolCall,
        messages: list[Message],
        context_items: list[SessionItem],
        emit: Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]],
    ) -> None:
        await emit(
            AgentEventKind.TOOL_REQUESTED,
            {"id": call.id, "name": call.name, "arguments": dict(call.arguments)},
        )
        tool = self._tools.get(call.name)
        if tool is None:
            result = ToolResult(f"unknown tool: {call.name}", is_error=True)
            await emit(
                AgentEventKind.TOOL_FAILED,
                {"id": call.id, "name": call.name, **result.to_dict()},
            )
            message = Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
            messages.append(message)
            context_items.append(message)
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
        if not decision.allowed:
            result = ToolResult(f"permission denied: {decision.reason}", is_error=True)
            await emit(
                AgentEventKind.TOOL_FAILED,
                {"id": call.id, "name": call.name, **result.to_dict()},
            )
            message = Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
            messages.append(message)
            context_items.append(message)
            return

        await emit(AgentEventKind.TOOL_STARTED, {"id": call.id, "name": call.name})
        try:
            result = await tool.execute(call.arguments, self._tool_context)
        except (ToolError, OSError, UnicodeError) as error:
            result = ToolResult(f"{type(error).__name__}: {error}", is_error=True)
        kind = AgentEventKind.TOOL_FAILED if result.is_error else AgentEventKind.TOOL_COMPLETED
        await emit(kind, {"id": call.id, "name": call.name, **result.to_dict()})
        message = Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
        messages.append(message)
        context_items.append(message)
