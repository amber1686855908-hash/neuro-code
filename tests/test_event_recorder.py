from __future__ import annotations

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
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SupervisorReasonCode,
    TurnSource,
)
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
