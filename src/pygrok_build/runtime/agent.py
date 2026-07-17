from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pygrok_build.domain.events import AgentEvent, AgentEventKind
from pygrok_build.domain.messages import Message, Role, ToolCall
from pygrok_build.domain.model_events import (
    ModelCompleted,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from pygrok_build.domain.tools import ToolResult
from pygrok_build.errors import ProviderError, ToolError
from pygrok_build.permissions import PermissionManager
from pygrok_build.ports.model import ModelProvider
from pygrok_build.ports.storage import SessionStore
from pygrok_build.ports.tools import ToolContext
from pygrok_build.tools.registry import ToolRegistry

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


DEFAULT_SYSTEM_PROMPT = """You are PyGrokBuild, a terminal coding agent.
Use tools when repository evidence is needed. Read before editing. Never claim a
tool action succeeded unless its result confirms success. Keep the final answer
concise and state which files or checks changed."""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    session_id: str | None
    response: str
    messages: tuple[Message, ...]
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
        initial_messages: Sequence[Message] = (),
        session_id: str | None = None,
    ) -> AgentRunResult:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        messages = list(initial_messages)
        if not messages:
            messages.append(Message(Role.SYSTEM, self._system_prompt))
        events: list[AgentEvent] = []
        sequence = 0

        if self._session_store is not None and session_id is None:
            session_id = await self._session_store.create_session(
                str(self._tool_context.cwd),
                self._provider.provider_name,
                self._provider.model_name,
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
        messages.append(Message(Role.USER, prompt))
        await emit(AgentEventKind.USER_MESSAGE, {"content": prompt})

        response_parts: list[str] = []
        try:
            for step in range(1, self._max_steps + 1):
                await emit(AgentEventKind.MODEL_STEP_STARTED, {"step": step})
                step_text: list[str] = []
                tool_calls: list[ToolCall] = []
                completion: ModelCompleted | None = None

                async for model_event in self._provider.stream(
                    tuple(messages), self._tools.definitions()
                ):
                    if isinstance(model_event, ModelTextDelta):
                        step_text.append(model_event.text)
                        response_parts.append(model_event.text)
                        await emit(AgentEventKind.TEXT_DELTA, {"text": model_event.text})
                    elif isinstance(model_event, ModelReasoningDelta):
                        await emit(
                            AgentEventKind.REASONING_DELTA,
                            {"text": model_event.text},
                        )
                    elif isinstance(model_event, ModelToolCall):
                        tool_calls.append(model_event.call)
                    elif isinstance(model_event, ModelCompleted):
                        completion = model_event

                if completion is None:
                    raise ProviderError("provider stream ended without a completion event")
                assistant_message = Message(
                    Role.ASSISTANT,
                    "".join(step_text),
                    tool_calls=tuple(tool_calls),
                )
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
                        await self._session_store.save_messages(session_id, messages)
                    return AgentRunResult(
                        session_id,
                        "".join(response_parts),
                        tuple(messages),
                        tuple(events),
                        step,
                    )

                for call in tool_calls:
                    await self._execute_tool(call, messages, emit)
                if self._session_store is not None and session_id is not None:
                    await self._session_store.save_messages(session_id, messages)

            raise ProviderError(f"agent exceeded the maximum of {self._max_steps} model steps")
        except BaseException as error:
            # Preserve cancellation semantics while still making the session auditable.
            await emit(
                AgentEventKind.TURN_FAILED,
                {"error_type": type(error).__name__, "message": str(error)},
            )
            if self._session_store is not None and session_id is not None:
                await self._session_store.save_messages(session_id, messages)
            raise

    async def _execute_tool(
        self,
        call: ToolCall,
        messages: list[Message],
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
            messages.append(
                Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
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
        if not decision.allowed:
            result = ToolResult(f"permission denied: {decision.reason}", is_error=True)
            await emit(
                AgentEventKind.TOOL_FAILED,
                {"id": call.id, "name": call.name, **result.to_dict()},
            )
            messages.append(
                Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id)
            )
            return

        await emit(AgentEventKind.TOOL_STARTED, {"id": call.id, "name": call.name})
        try:
            result = await tool.execute(call.arguments, self._tool_context)
        except (ToolError, OSError, UnicodeError) as error:
            result = ToolResult(f"{type(error).__name__}: {error}", is_error=True)
        kind = AgentEventKind.TOOL_FAILED if result.is_error else AgentEventKind.TOOL_COMPLETED
        await emit(kind, {"id": call.id, "name": call.name, **result.to_dict()})
        messages.append(Message(Role.TOOL, result.content, name=call.name, tool_call_id=call.id))
