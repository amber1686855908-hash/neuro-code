"""Turn event recorder collaborator.

Stage 3B of the Runtime Kernel split: this module owns the per-turn event
sequence, persistence, session-task finishing, turn-failure recording, and
terminal completion recording previously embedded in ``AgentRuntime.run()``
closures.  ``AgentRuntime`` binds the recorder's methods as local names so the
call sites and event ordering remain unchanged.

The module intentionally does not import :mod:`agent`; it depends only on
ports, domain values, and standard library primitives.

提供回合事件记录协作者,负责事件序列、持久化、任务收尾、失败记录和完成记录.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from time import monotonic

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import SessionItem
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    SessionExecutionRecord,
    TurnSource,
)
from neuro_code.domain.session_tasks import SessionTask, SessionTaskStatus

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


class TurnEventRecorder:
    """Own per-turn event and session-task bookkeeping.

    The recorder shares mutable state with the runtime loop: it appends to the
    same ``events`` list and mutates the same ``context_items`` list passed at
    construction.  ``session_task`` and ``pristine_cancel_eligible`` are
    public mutable attributes so the loop can seed and update them.

    管理每回合的事件和会话任务记账,并共享运行时的事件与上下文状态.
    """

    __slots__ = (
        "_context_items",
        "_events",
        "_persist_turn_context",
        "_sequence",
        "_session_id",
        "_session_store",
        "_sink",
        "_turn_context_prefix",
        "_turn_source",
        "_turn_started_at",
        "pristine_cancel_eligible",
        "session_task",
    )

    def __init__(
        self,
        *,
        sink: EventSink | None,
        session_store: SessionStore | None,
        session_id: str | None,
        turn_source: TurnSource,
        turn_started_at: float,
        persist_turn_context: bool,
        turn_context_prefix: tuple[SessionItem, ...],
        context_items: list[SessionItem],
        events: list[AgentEvent],
        sequence: int,
        session_task: SessionTask | None,
        pristine_cancel_eligible: bool,
    ) -> None:
        self._sink = sink
        self._session_store = session_store
        self._session_id = session_id
        self._turn_source = turn_source
        self._turn_started_at = turn_started_at
        self._persist_turn_context = persist_turn_context
        self._turn_context_prefix = turn_context_prefix
        self._context_items = context_items
        self._events = events
        self._sequence = sequence
        self.session_task = session_task
        self.pristine_cancel_eligible = pristine_cancel_eligible

    async def emit(
        self,
        kind: AgentEventKind,
        data: dict[str, object],
        *,
        persist: bool = True,
        deliver_event: bool = True,
    ) -> AgentEvent:
        self._sequence += 1
        event = AgentEvent.create(self._sequence, kind, data)
        self._events.append(event)
        if persist and self._session_store is not None and self._session_id is not None:
            await self._session_store.append_event(self._session_id, event)
        if deliver_event:
            await self._deliver(event)
        return event

    async def finish_session_task(self, status: SessionTaskStatus) -> None:
        if self.session_task is None:
            return
        assert self._session_store is not None
        assert self._session_id is not None
        task = self.session_task.finish(status, finished_at=datetime.now(UTC))
        await self._session_store.update_session_task(self._session_id, task)
        self.session_task = task
        if status is SessionTaskStatus.COMPLETED:
            event_kind = AgentEventKind.SESSION_TASK_COMPLETED
        elif status is SessionTaskStatus.FAILED:
            event_kind = AgentEventKind.SESSION_TASK_FAILED
        elif status is SessionTaskStatus.CANCELLED:
            event_kind = AgentEventKind.SESSION_TASK_CANCELLED
        else:
            raise AssertionError("a session task must finish in a terminal state")
        await self.emit(event_kind, {"task": task.to_dict()})

    async def record_turn_failure(self, error: BaseException) -> None:
        cancelled = isinstance(error, asyncio.CancelledError)
        pristine_rewound = cancelled and self.pristine_cancel_eligible
        await self.finish_session_task(
            SessionTaskStatus.CANCELLED if cancelled else SessionTaskStatus.FAILED
        )
        await self.emit(
            AgentEventKind.TURN_FAILED,
            {
                "error_type": type(error).__name__,
                "message": "turn cancelled" if cancelled else str(error),
                "cancelled": cancelled,
                "pristine_rewound": pristine_rewound,
                "duration_seconds": monotonic() - self._turn_started_at,
            },
        )
        if self._session_store is not None and self._session_id is not None:
            await self._session_store.save_session_items(
                self._session_id,
                (
                    self._turn_context_prefix
                    if pristine_rewound or not self._persist_turn_context
                    else self._context_items
                ),
            )

    async def finalize_turn_completion(
        self,
        outcome: AgentExecutionOutcome,
        data: dict[str, object],
        result_items: Sequence[SessionItem],
    ) -> None:
        completed_event = await self.emit(
            AgentEventKind.TURN_COMPLETED,
            data,
            persist=False,
            deliver_event=False,
        )
        record = (
            None
            if self._turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE
            else SessionExecutionRecord(
                outcome,
                completed_event.sequence,
                completed_event.created_at,
            )
        )
        if self._session_store is not None and self._session_id is not None:
            await self._session_store.finalize_turn(
                self._session_id,
                completed_event,
                result_items,
                record,
            )
        await self._deliver(completed_event)

    async def _deliver(self, event: AgentEvent) -> None:
        if self._sink is not None:
            outcome = self._sink(event)
            if inspect.isawaitable(outcome):
                await outcome


__all__ = ["TurnEventRecorder"]
