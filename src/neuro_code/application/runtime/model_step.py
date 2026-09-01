"""Per-step model stream processor.

Stage 3E of the Runtime Kernel split: this module owns the normalization of
one provider stream into step text, reasoning, tool calls, and completion
state.  It also owns provider-origin adoption bookkeeping, thinking-completion
timing, and pristine cancel-eligibility updates for the current step.

The module intentionally does not import :mod:`agent`; it depends only on
ports, domain values, and callbacks supplied by the loop.

提供逐模型步骤的流处理器,负责规范化文本、推理、工具调用和完成状态.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import ToolCall

EventSink = Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]]


@dataclass(frozen=True, slots=True)
class ModelStepResult:
    """Normalized state produced by consuming one provider stream.

    表示消费一次 Provider 流后生成的规范化状态."""

    text: tuple[str, ...]
    reasoning: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    completion: ModelCompleted | None
    selected_provider: ModelProviderSelected | None = None


class ModelStepProcessor:
    """Consume one provider stream and normalize events into step state.

    消费一次 Provider 流,并将事件规范化为模型步骤状态."""

    __slots__ = ("_session_store",)

    def __init__(self, *, session_store: SessionStore | None) -> None:
        self._session_store = session_store

    async def consume(
        self,
        stream: AsyncIterator[ModelEvent],
        *,
        emit: EventSink,
        step: int,
        step_started_at: float,
        session_id: str | None,
        can_adopt_provider_origin: bool,
        on_imperfect: Callable[[], None],
        on_output_started: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelStepResult:
        """Consume one model stream, emitting normalized events as it goes.

        消费一次模型流,同时持续发出规范化事件."""

        step_text: list[str] = []
        step_reasoning: list[str] = []
        tool_calls: list[ToolCall] = []
        completion: ModelCompleted | None = None
        selected_provider: ModelProviderSelected | None = None
        backend_tool_started_at: dict[str, float] = {}
        thinking_completed = False
        output_started = False

        async def mark_output_started(output_kind: str) -> None:
            nonlocal output_started
            if output_started or on_output_started is None:
                return
            await on_output_started(output_kind)
            output_started = True

        async def complete_thinking() -> None:
            nonlocal thinking_completed
            if thinking_completed:
                return
            thinking_completed = True
            await emit(
                AgentEventKind.MODEL_THINKING_COMPLETED,
                {
                    "step": step,
                    "duration_seconds": monotonic() - step_started_at,
                },
            )

        async for model_event in stream:
            if isinstance(model_event, ModelProviderAttemptFailed):
                await emit(
                    AgentEventKind.PROVIDER_ATTEMPT_FAILED,
                    {
                        "provider": model_event.provider,
                        "model": model_event.model,
                        "error_type": model_event.error_type,
                        "message": model_event.message,
                        "failure_kind": model_event.failure_kind,
                        "status_code": model_event.status_code,
                    },
                )
            elif isinstance(model_event, ModelProviderSelected):
                selected_provider = model_event
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
                if model_event.text:
                    await mark_output_started("text")
                    on_imperfect()
                step_text.append(model_event.text)
                await emit(AgentEventKind.TEXT_DELTA, {"text": model_event.text})
            elif isinstance(model_event, ModelReasoningDelta):
                if model_event.text:
                    await mark_output_started("reasoning")
                    on_imperfect()
                step_reasoning.append(model_event.text)
                await emit(
                    AgentEventKind.REASONING_DELTA,
                    {"text": model_event.text},
                )
            elif isinstance(model_event, ModelBackendToolStarted):
                await mark_output_started("backend_tool_started")
                await complete_thinking()
                on_imperfect()
                backend_tool_started_at[model_event.call_id] = monotonic()
                await emit(
                    AgentEventKind.BACKEND_TOOL_STARTED,
                    {"id": model_event.call_id, "name": model_event.name},
                )
            elif isinstance(model_event, ModelBackendToolCompleted):
                await mark_output_started("backend_tool_completed")
                await complete_thinking()
                on_imperfect()
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
                await mark_output_started("tool_call")
                await complete_thinking()
                on_imperfect()
                tool_calls.append(model_event.call)
            elif isinstance(model_event, ModelCompleted):
                await mark_output_started("completed")
                await complete_thinking()
                on_imperfect()
                completion = model_event

        return ModelStepResult(
            text=tuple(step_text),
            reasoning=tuple(step_reasoning),
            tool_calls=tuple(tool_calls),
            completion=completion,
            selected_provider=selected_provider,
        )


__all__ = ["ModelStepProcessor", "ModelStepResult"]
