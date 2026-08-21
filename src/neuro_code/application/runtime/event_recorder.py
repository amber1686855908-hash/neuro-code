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

from neuro_code.application.memory.compaction_runtime import ContextCompactionTurnProjection
from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.compaction import DurableCompactionItem
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import Message, SessionItem
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    SessionExecutionRecord,
    TurnRecoveryFact,
    TurnRecoveryFactKind,
    TurnSource,
)
from neuro_code.domain.session_tasks import SessionTask, SessionTaskStatus
from neuro_code.shared.errors import ConfigurationError

EventSink = Callable[[AgentEvent], Awaitable[None] | None]


def _durable_session_items(items: Sequence[SessionItem]) -> tuple[SessionItem, ...]:
    """Drop all in-memory synthetic notices before a session-store write.

    Context shaping and append-only runtime notices are request-only control
    data.  They must not become historical user messages or alter resume
    replay after a turn is persisted.

    在写入会话存储前移除全部仅内存合成通知。上下文整形和仅追加的运行时通知是请求
    范围的控制数据,不得成为历史用户消息或改变恢复后的重放。
    """

    return tuple(
        item
        for item in items
        if not (isinstance(item, Message) and item.synthetic_reason is not None)
    )


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
        "_turn_id",
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
        turn_id: str | None = None,
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
        self._turn_id = turn_id
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
        event = self._create_event(kind, data)
        self._events.append(event)
        if persist and self._session_store is not None and self._session_id is not None:
            await self._session_store.append_event(self._session_id, event)
        if deliver_event:
            await self._deliver(event)
        return event

    def _create_event(self, kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
        self._sequence += 1
        return AgentEvent.create(self._sequence, kind, data)

    async def _persist_recovery_fact(
        self,
        event: AgentEvent,
        fact: TurnRecoveryFact,
        *,
        deliver_after_persistence: bool = True,
    ) -> None:
        """Finish a durable marker before propagating cancellation.

        A cancellation can arrive while SQLite is committing a tool marker.
        The marker must not be abandoned halfway through its write-ahead
        boundary, and the already-committed lifecycle event should still reach
        the interface before the cancellation is re-raised.

        在 SQLite 提交工具标记期间可能收到取消。标记不能在 write-ahead 边界中途被放弃,
        已提交的生命周期事件也必须在重新抛出取消前到达接口。
        """

        assert self._session_store is not None
        assert self._session_id is not None
        assert self._turn_id is not None
        persistence = asyncio.create_task(
            self._session_store.append_turn_recovery_fact(
                self._session_id,
                self._turn_id,
                event,
                fact,
            )
        )
        try:
            await asyncio.shield(persistence)
        except asyncio.CancelledError:
            await persistence
            if deliver_after_persistence:
                await self._deliver(event)
            raise
        if deliver_after_persistence:
            await self._deliver(event)

    async def record_model_request_started(
        self,
        *,
        request_id: str,
        step: int,
        provider: str,
        model: str,
    ) -> None:
        if self._session_store is None or self._session_id is None or self._turn_id is None:
            return
        fact = TurnRecoveryFact(
            TurnRecoveryFactKind.MODEL_REQUEST_STARTED,
            request_id=request_id,
            step=step,
            provider=provider,
            model=model,
        )
        event = self._create_event(
            AgentEventKind.MODEL_REQUEST_STARTED,
            fact.to_event_data(self._turn_id),
        )
        self._events.append(event)
        await self._persist_recovery_fact(event, fact)

    async def record_model_output_started(
        self,
        *,
        request_id: str,
        step: int,
        output_kind: str,
    ) -> None:
        if self._session_store is None or self._session_id is None or self._turn_id is None:
            return
        fact = TurnRecoveryFact(
            TurnRecoveryFactKind.MODEL_OUTPUT_STARTED,
            request_id=request_id,
            step=step,
            output_kind=output_kind,
        )
        event = self._create_event(
            AgentEventKind.MODEL_OUTPUT_STARTED,
            fact.to_event_data(self._turn_id),
        )
        self._events.append(event)
        await self._persist_recovery_fact(event, fact)

    async def record_tool_started(
        self,
        *,
        tool_id: str,
        tool_name: str,
        side_effecting: bool,
    ) -> None:
        if self._session_store is None or self._session_id is None or self._turn_id is None:
            return
        fact = TurnRecoveryFact(
            TurnRecoveryFactKind.TOOL_STARTED,
            tool_id=tool_id,
            tool_name=tool_name,
            side_effecting=side_effecting,
        )
        tool_started_data = fact.to_event_data(self._turn_id)
        tool_started_data.update({"id": tool_id, "name": tool_name})
        event = self._create_event(AgentEventKind.TOOL_STARTED, tool_started_data)
        self._events.append(event)
        # Keep the established interface timing: the in-progress projection is
        # delivered as soon as the tool crosses its local start boundary. The
        # durable marker still completes before ToolExecutor enters the body.
        await self._deliver(event)
        await self._persist_recovery_fact(
            event,
            fact,
            deliver_after_persistence=False,
        )

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
        task_status = SessionTaskStatus.CANCELLED if cancelled else SessionTaskStatus.FAILED
        task, task_event = self._prepare_task_terminal(task_status)
        failure_data: dict[str, object] = {
            "error_type": type(error).__name__,
            "message": "turn cancelled" if cancelled else str(error)[:1024],
            "cancelled": cancelled,
            "pristine_rewound": pristine_rewound,
            "duration_seconds": monotonic() - self._turn_started_at,
        }
        if self._turn_id is not None:
            failure_data["turn_id"] = self._turn_id
        failure_event = self._create_event(
            AgentEventKind.TURN_FAILED,
            failure_data,
        )
        self._events.append(failure_event)
        durable_items = (
            self._turn_context_prefix
            if pristine_rewound or not self._persist_turn_context
            else _durable_session_items(self._context_items)
        )
        if self._session_store is not None and self._session_id is not None:
            atomic_failure = getattr(self._session_store, "finalize_turn_failure", None)
            if callable(atomic_failure):
                await atomic_failure(
                    self._session_id,
                    self._turn_id,
                    failure_event,
                    durable_items,
                    resolution="cancelled" if cancelled else "failed",
                    task=task,
                    task_event=task_event,
                )
            else:
                if task is not None:
                    await self._session_store.update_session_task(self._session_id, task)
                if task_event is not None:
                    await self._session_store.append_event(self._session_id, task_event)
                await self._session_store.append_event(self._session_id, failure_event)
                await self._session_store.save_session_items(self._session_id, durable_items)
        if task_event is not None:
            await self._deliver(task_event)
        await self._deliver(failure_event)

    def _prepare_task_terminal(
        self,
        status: SessionTaskStatus,
    ) -> tuple[SessionTask | None, AgentEvent | None]:
        task = self.session_task
        if task is None or task.status is not SessionTaskStatus.RUNNING:
            return None, None
        task = task.finish(status, finished_at=datetime.now(UTC))
        self.session_task = task
        event_kind = {
            SessionTaskStatus.COMPLETED: AgentEventKind.SESSION_TASK_COMPLETED,
            SessionTaskStatus.FAILED: AgentEventKind.SESSION_TASK_FAILED,
            SessionTaskStatus.CANCELLED: AgentEventKind.SESSION_TASK_CANCELLED,
        }[status]
        event = self._create_event(event_kind, {"task": task.to_dict()})
        self._events.append(event)
        return task, event

    async def finalize_turn_completion(
        self,
        outcome: AgentExecutionOutcome,
        data: dict[str, object],
        result_items: Sequence[SessionItem],
        compaction_item: DurableCompactionItem | None = None,
    ) -> None:
        """Persist one completed turn, optionally with its durable compaction.

        The optional compaction item is already generated, redacted, and
        validated by its caller.  Supplying it transfers only storage
        finalization ownership to this recorder; Provider generation remains
        outside the SQLite transaction.  Ordinary calls keep the existing
        ``finalize_turn`` path.

        持久化一个已完成回合,并可选地同时保存持久化压缩条目.

        可选压缩条目必须已经由调用方生成、脱敏并校验. 传入它只把存储最终化所有权
        交给本记录器; Provider 生成仍然在 SQLite 事务之外. 普通调用继续使用原有的
        ``finalize_turn`` 路径.
        """

        if compaction_item is not None and not isinstance(
            compaction_item,
            DurableCompactionItem,
        ):
            raise TypeError("compaction_item must be a DurableCompactionItem or None")
        if compaction_item is not None and (
            self._session_store is None or self._session_id is None
        ):
            raise ConfigurationError("compaction finalization requires a persisted session")
        completion_data = dict(data)
        if self._turn_id is not None:
            completion_data.setdefault("turn_id", self._turn_id)
        task, task_event = self._prepare_task_terminal(SessionTaskStatus.COMPLETED)
        completed_event = self._create_event(AgentEventKind.TURN_COMPLETED, completion_data)
        self._events.append(completed_event)
        record = (
            None
            if self._turn_source is TurnSource.BACKGROUND_TASK_AUTO_WAKE
            else SessionExecutionRecord(
                outcome,
                completed_event.sequence,
                completed_event.created_at,
            )
        )
        durable_result_items = _durable_session_items(result_items)
        if self._session_store is not None and self._session_id is not None:
            finalizer: Callable[..., Awaitable[None]]
            if compaction_item is None:
                finalizer = self._session_store.finalize_turn
                if callable(getattr(self._session_store, "start_turn_attempt", None)):
                    await finalizer(
                        self._session_id,
                        completed_event,
                        durable_result_items,
                        record,
                        self._turn_id,
                        task,
                        task_event,
                    )
                else:
                    await finalizer(
                        self._session_id,
                        completed_event,
                        durable_result_items,
                        record,
                    )
            else:
                finalizer = self._session_store.finalize_turn_with_compaction
                if callable(getattr(self._session_store, "start_turn_attempt", None)):
                    await finalizer(
                        self._session_id,
                        completed_event,
                        durable_result_items,
                        record,
                        compaction_item,
                        self._turn_id,
                        task,
                        task_event,
                    )
                else:
                    await finalizer(
                        self._session_id,
                        completed_event,
                        durable_result_items,
                        record,
                        compaction_item,
                    )
        await self._deliver(completed_event)

    async def finalize_turn_from_compaction_projection(
        self,
        projection: ContextCompactionTurnProjection,
        data: dict[str, object],
        result_items: Sequence[SessionItem],
        *,
        completed_outcome: AgentExecutionOutcome | None = None,
    ) -> None:
        """Consume one explicit compaction projection at turn finalization.

        A successful compaction projection still needs the caller's ordinary
        turn outcome; a timeout projection supplies its own bounded outcome.
        Propagation-only and no-op projections fail closed before any event is
        appended. This method is an opt-in owner seam and is never called by
        the normal Agent loop.

        在回合最终化时消费一次显式的压缩投影。

        成功压缩投影仍需要调用方提供普通回合 outcome;超时投影提供自己的有界 outcome。
        只能传播的投影和无操作投影会在追加任何事件前失败关闭。本方法是可选的所有者接缝,普通 Agent loop 不会调用。
        """

        if not isinstance(projection, ContextCompactionTurnProjection):
            raise TypeError("projection must be a ContextCompactionTurnProjection")
        if projection.must_propagate:
            raise ConfigurationError(
                "propagation-only compaction projection cannot finalize a turn"
            )
        if projection.triggered:
            if completed_outcome is None:
                raise ConfigurationError("successful compaction projection requires a turn outcome")
            await self.finalize_turn_completion(
                completed_outcome,
                data,
                result_items,
                projection.compaction_item,
            )
            return
        if projection.outcome is None:
            raise ConfigurationError("compaction projection is not ready for finalization")
        if completed_outcome is not None:
            raise ConfigurationError(
                "terminal compaction projection must not receive another turn outcome"
            )
        await self.finalize_turn_completion(
            projection.outcome,
            data,
            result_items,
        )

    async def _deliver(self, event: AgentEvent) -> None:
        if self._sink is not None:
            outcome = self._sink(event)
            if inspect.isawaitable(outcome):
                await outcome


__all__ = ["TurnEventRecorder"]
