from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionTimeoutError,
    ContextCompactionTurnProjection,
    project_context_compaction_failure,
)
from neuro_code.application.runtime.event_recorder import TurnEventRecorder
from neuro_code.domain.conversation.compaction import DurableCompactionItem
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SupervisorReasonCode,
    TurnSource,
)
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError, ProviderError


def _compaction_item() -> DurableCompactionItem:
    return DurableCompactionItem(
        compaction_id="recorder-compaction",
        provider_name="provider",
        model_name="model",
        capacity_tokens=1_000,
        context_affinity="profile-a",
        source_item_count=3,
        protected_item_count=0,
        recent_item_count=1,
        candidate_range=(0, 2),
        target_tokens=800,
        summary_tokens=12,
        source_fingerprint="d" * 64,
        summary="safe summary",
        summary_redacted=True,
        summary_truncated=False,
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


class TurnEventRecorderTests(unittest.IsolatedAsyncioTestCase):
    def _recorder(
        self,
        *,
        store: object | None = None,
        session_id: str | None = None,
        session_task: SessionTask | None = None,
        turn_id: str | None = None,
        sink: object | None = None,
    ) -> TurnEventRecorder:
        return TurnEventRecorder(
            sink=sink,  # type: ignore[arg-type]
            session_store=store,  # type: ignore[arg-type]
            session_id=session_id,
            turn_source=TurnSource.USER,
            turn_started_at=0.0,
            persist_turn_context=True,
            turn_context_prefix=(),
            context_items=[],
            events=[],
            sequence=0,
            session_task=session_task,
            pristine_cancel_eligible=False,
            turn_id=turn_id,
        )

    async def test_recovery_markers_are_noops_without_a_persisted_turn(self) -> None:
        recorder = self._recorder()
        await recorder.emit(
            AgentEventKind.MODEL_REQUEST_STARTED,
            {},
            deliver_event=False,
        )
        await recorder.record_model_request_started(
            request_id="request-1",
            step=1,
            provider="provider",
            model="model",
        )
        await recorder.record_model_output_started(
            request_id="request-1",
            step=1,
            output_kind="text",
        )
        await recorder.record_tool_started(
            tool_id="tool-1",
            tool_name="read_file",
            side_effecting=False,
        )

    async def test_marker_persistence_completes_before_cancellation_propagates(self) -> None:
        class BlockingStore:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def append_turn_recovery_fact(self, *_args: object) -> None:
                self.started.set()
                await self.release.wait()

        store = BlockingStore()
        delivered: list[object] = []

        async def sink(event: object) -> None:
            delivered.append(event)

        recorder = self._recorder(
            store=store,
            session_id="session-1",
            turn_id="turn-1",
            sink=sink,
        )
        marker = asyncio.create_task(
            recorder.record_model_request_started(
                request_id="request-1",
                step=1,
                provider="provider",
                model="model",
            )
        )
        await store.started.wait()
        marker.cancel()
        store.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await marker
        self.assertEqual(len(delivered), 1)

        store.started = asyncio.Event()
        store.release = asyncio.Event()
        tool_marker = asyncio.create_task(
            recorder.record_tool_started(
                tool_id="tool-1",
                tool_name="apply_patch",
                side_effecting=True,
            )
        )
        await store.started.wait()
        tool_marker.cancel()
        store.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await tool_marker

    async def test_legacy_failure_and_completion_fallbacks_remain_supported(self) -> None:
        class LegacyStore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def update_session_task(self, _session_id: str, _task: SessionTask) -> None:
                self.calls.append("update_task")

            async def append_event(self, _session_id: str, event: AgentEvent) -> None:
                self.calls.append(event.kind.value)

            async def save_session_items(self, _session_id: str, _items: object) -> None:
                self.calls.append("save_items")

            async def finalize_turn(self, *_args: object) -> None:
                self.calls.append("finalize_turn")

            async def finalize_turn_with_compaction(self, *_args: object) -> None:
                self.calls.append("finalize_compaction")

        store = LegacyStore()
        running_task = SessionTask(
            "task-1",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            datetime.now(UTC),
        )
        failure_recorder = self._recorder(
            store=store,
            session_id="session-1",
            session_task=running_task,
        )
        await failure_recorder.record_turn_failure(ProviderError("provider unavailable"))
        self.assertIn("update_task", store.calls)
        self.assertIn("turn_failed", store.calls)
        self.assertIn("save_items", store.calls)

        completion_recorder = self._recorder(store=store, session_id="session-1")
        outcome = AgentExecutionOutcome(
            AgentExecutionStatus.COMPLETED,
            None,
            finalized=False,
            recoverable=False,
        )
        await completion_recorder.finalize_turn_completion(outcome, {}, ())
        await completion_recorder.finalize_turn_completion(outcome, {}, (), _compaction_item())
        self.assertIn("finalize_turn", store.calls)
        self.assertIn("finalize_compaction", store.calls)

    async def test_recorder_rejects_invalid_projection_and_compaction_inputs(self) -> None:
        recorder = self._recorder()
        outcome = AgentExecutionOutcome(
            AgentExecutionStatus.COMPLETED,
            None,
            finalized=False,
            recoverable=False,
        )
        with self.assertRaises(TypeError):
            await recorder.finalize_turn_completion(outcome, {}, (), object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            await recorder.finalize_turn_from_compaction_projection(object(), {}, ())  # type: ignore[arg-type]
        terminal_projection = project_context_compaction_failure(
            ContextCompactionTimeoutError("timeout")
        )
        assert terminal_projection is not None
        with self.assertRaises(ConfigurationError):
            await recorder.finalize_turn_from_compaction_projection(
                terminal_projection,
                {},
                (),
                completed_outcome=outcome,
            )

    async def test_legacy_task_finisher_maps_each_terminal_status(self) -> None:
        class TaskStore:
            async def update_session_task(self, _session_id: str, _task: SessionTask) -> None:
                return None

            async def append_event(self, _session_id: str, _event: object) -> None:
                return None

        await self._recorder().finish_session_task(SessionTaskStatus.COMPLETED)
        for status in (
            SessionTaskStatus.COMPLETED,
            SessionTaskStatus.FAILED,
            SessionTaskStatus.CANCELLED,
        ):
            with self.subTest(status=status):
                recorder = self._recorder(
                    store=TaskStore(),
                    session_id="session-1",
                    session_task=SessionTask(
                        f"task-{status.value}",
                        SessionTaskKind.PLAN_EXECUTION,
                        SessionTaskStatus.RUNNING,
                        datetime.now(UTC),
                    ),
                )
                await recorder.finish_session_task(status)

    async def test_explicit_compaction_item_uses_atomic_turn_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "provider", "model")
            initial_items = [Message(Role.USER, "existing")]
            await store.save_session_items(session_id, initial_items)
            context_items = list(initial_items)
            events = []
            recorder = TurnEventRecorder(
                sink=None,
                session_store=store,
                session_id=session_id,
                turn_source=TurnSource.USER,
                turn_started_at=0.0,
                persist_turn_context=True,
                turn_context_prefix=tuple(initial_items),
                context_items=context_items,
                events=events,
                sequence=0,
                session_task=None,
                pristine_cancel_eligible=False,
            )
            final_items = [*initial_items, Message(Role.ASSISTANT, "done")]

            await recorder.finalize_turn_completion(
                AgentExecutionOutcome(
                    AgentExecutionStatus.STUCK,
                    SupervisorReasonCode.NO_PROGRESS,
                    finalized=True,
                    recoverable=True,
                ),
                {"execution_status": "stuck"},
                final_items,
                _compaction_item(),
            )

            self.assertEqual(await store.load_session_items(session_id), final_items)
            self.assertEqual(await store.load_compaction_items(session_id), [_compaction_item()])
            self.assertEqual([event.sequence for event in events], [1])
            self.assertEqual(
                [event["sequence"] for event in await store.load_events(session_id)],
                [1],
            )

    async def test_compaction_finalization_requires_a_persisted_session(self) -> None:
        events = []
        recorder = TurnEventRecorder(
            sink=None,
            session_store=None,
            session_id=None,
            turn_source=TurnSource.USER,
            turn_started_at=0.0,
            persist_turn_context=True,
            turn_context_prefix=(),
            context_items=[],
            events=events,
            sequence=0,
            session_task=None,
            pristine_cancel_eligible=False,
        )

        with self.assertRaisesRegex(ConfigurationError, "persisted session"):
            await recorder.finalize_turn_completion(
                AgentExecutionOutcome(
                    AgentExecutionStatus.COMPLETED,
                    None,
                    finalized=False,
                    recoverable=False,
                ),
                {},
                (),
                _compaction_item(),
            )
        self.assertEqual(events, [])

    async def test_projection_owner_commits_successful_item_with_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "provider", "model")
            initial_items = [Message(Role.USER, "existing")]
            await store.save_session_items(session_id, initial_items)
            context_items = list(initial_items)
            events = []
            recorder = TurnEventRecorder(
                sink=None,
                session_store=store,
                session_id=session_id,
                turn_source=TurnSource.USER,
                turn_started_at=0.0,
                persist_turn_context=True,
                turn_context_prefix=tuple(initial_items),
                context_items=context_items,
                events=events,
                sequence=0,
                session_task=None,
                pristine_cancel_eligible=False,
            )

            await recorder.finalize_turn_from_compaction_projection(
                ContextCompactionTurnProjection(True, _compaction_item()),
                {"execution_status": "completed"},
                [*initial_items, Message(Role.ASSISTANT, "done")],
                completed_outcome=AgentExecutionOutcome(
                    AgentExecutionStatus.COMPLETED,
                    None,
                    finalized=False,
                    recoverable=False,
                ),
            )

            self.assertEqual(await store.load_compaction_items(session_id), [_compaction_item()])
            self.assertEqual([event.sequence for event in events], [1])

    async def test_projection_owner_consumes_timeout_outcome_without_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "provider", "model")
            events = []
            recorder = TurnEventRecorder(
                sink=None,
                session_store=store,
                session_id=session_id,
                turn_source=TurnSource.USER,
                turn_started_at=0.0,
                persist_turn_context=True,
                turn_context_prefix=(),
                context_items=[],
                events=events,
                sequence=0,
                session_task=None,
                pristine_cancel_eligible=False,
            )
            projection = project_context_compaction_failure(
                ContextCompactionTimeoutError("secret timeout")
            )
            assert projection is not None

            await recorder.finalize_turn_from_compaction_projection(
                projection,
                {"execution_status": "budget_limited"},
                (),
            )

            record = await store.load_execution_record(session_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.outcome.status, AgentExecutionStatus.BUDGET_LIMITED)
            self.assertEqual(record.outcome.reason_code, SupervisorReasonCode.WALL_TIME_BUDGET)
            self.assertEqual(await store.load_compaction_items(session_id), [])

    async def test_projection_owner_rejects_noop_and_propagation_before_events(self) -> None:
        events = []
        recorder = TurnEventRecorder(
            sink=None,
            session_store=None,
            session_id=None,
            turn_source=TurnSource.USER,
            turn_started_at=0.0,
            persist_turn_context=True,
            turn_context_prefix=(),
            context_items=[],
            events=events,
            sequence=0,
            session_task=None,
            pristine_cancel_eligible=False,
        )

        for projection in (
            ContextCompactionTurnProjection(False),
            project_context_compaction_failure(ProviderError("provider failure")),
        ):
            assert projection is not None
            with self.assertRaisesRegex(ConfigurationError, "cannot finalize|not ready"):
                await recorder.finalize_turn_from_compaction_projection(
                    projection,
                    {},
                    (),
                )
        self.assertEqual(events, [])

    async def test_projection_owner_requires_outcome_for_success_item(self) -> None:
        events = []
        recorder = TurnEventRecorder(
            sink=None,
            session_store=None,
            session_id=None,
            turn_source=TurnSource.USER,
            turn_started_at=0.0,
            persist_turn_context=True,
            turn_context_prefix=(),
            context_items=[],
            events=events,
            sequence=0,
            session_task=None,
            pristine_cancel_eligible=False,
        )

        with self.assertRaisesRegex(ConfigurationError, "requires a turn outcome"):
            await recorder.finalize_turn_from_compaction_projection(
                ContextCompactionTurnProjection(True, _compaction_item()),
                {},
                (),
            )
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
