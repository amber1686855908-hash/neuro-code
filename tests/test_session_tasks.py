from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from neuro_code.domain.plans import PlanStep, PlanStepStatus, SessionPlan
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus


class SessionTaskTests(unittest.TestCase):
    def test_running_task_transitions_once_to_a_terminal_state(self) -> None:
        started_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
        plan = SessionPlan(
            (
                PlanStep("Inspect the current implementation", PlanStepStatus.COMPLETED),
                PlanStep("Apply the reviewed change", PlanStepStatus.IN_PROGRESS),
            ),
            "Preserve the execution revision for audit",
        )
        task = SessionTask(
            "task-plan-execution",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            started_at,
            plan_snapshot=plan,
        )

        completed = task.finish(
            SessionTaskStatus.COMPLETED,
            finished_at=started_at + timedelta(seconds=1),
        )

        self.assertTrue(completed.status.terminal)
        self.assertEqual(completed.finished_at, started_at + timedelta(seconds=1))
        self.assertEqual(completed.plan_snapshot, plan)
        self.assertEqual(completed.to_dict()["plan"], plan.to_dict())
        with self.assertRaisesRegex(ValueError, "already terminal"):
            completed.finish(
                SessionTaskStatus.FAILED,
                finished_at=started_at + timedelta(seconds=2),
            )

    def test_task_rejects_invalid_lifecycle_shapes(self) -> None:
        started_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "id is invalid"):
            SessionTask(
                "task\x00bad",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.RUNNING,
                started_at,
            )
        with self.assertRaisesRegex(ValueError, "terminal state"):
            SessionTask(
                "task-finished-without-time",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.COMPLETED,
                started_at,
            )
        task = SessionTask(
            "task-running",
            SessionTaskKind.PLAN_EXECUTION,
            SessionTaskStatus.RUNNING,
            started_at,
        )
        with self.assertRaisesRegex(ValueError, "must be terminal"):
            task.finish(SessionTaskStatus.RUNNING, finished_at=started_at + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "only a plan execution"):
            SessionTask(
                "subagent-with-plan",
                SessionTaskKind.SUBAGENT,
                SessionTaskStatus.RUNNING,
                started_at,
                plan_snapshot=SessionPlan((PlanStep("Should be rejected"),)),
            )

    def test_task_rejects_noncanonical_types_and_invalid_timestamps(self) -> None:
        started_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "kind must be canonical"):
            SessionTask("task-kind", "plan_execution", SessionTaskStatus.RUNNING, started_at)
        with self.assertRaisesRegex(ValueError, "status must be canonical"):
            SessionTask("task-status", SessionTaskKind.PLAN_EXECUTION, "running", started_at)
        with self.assertRaisesRegex(ValueError, "start time must be timezone-aware"):
            SessionTask(
                "task-naive-start",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.RUNNING,
                datetime.fromisoformat("2026-07-29T12:00:00"),
            )
        with self.assertRaisesRegex(ValueError, "finish time must be timezone-aware"):
            SessionTask(
                "task-naive-finish",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.COMPLETED,
                started_at,
                datetime.fromisoformat("2026-07-29T12:00:00"),
            )
        with self.assertRaisesRegex(ValueError, "must not precede"):
            SessionTask(
                "task-backdated-finish",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.COMPLETED,
                started_at,
                started_at - timedelta(seconds=1),
            )
        with self.assertRaisesRegex(ValueError, "snapshot must be canonical"):
            SessionTask(
                "task-invalid-plan",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.RUNNING,
                started_at,
                plan_snapshot="not-a-plan",
            )
