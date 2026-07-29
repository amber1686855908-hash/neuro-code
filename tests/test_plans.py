from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import cast

from neuro_code.domain.plans import (
    MAX_PLAN_COMMENT_BYTES,
    MAX_PLAN_EXPLANATION_BYTES,
    MAX_PLAN_STEP_BYTES,
    MAX_PLAN_STEPS,
    PlanComment,
    PlanStep,
    PlanStepStatus,
    SessionPlan,
    plan_from_update_arguments,
)


class SessionPlanTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_changes_with_the_plan_revision(self) -> None:
        first = SessionPlan(
            (PlanStep("Inspect the implementation", PlanStepStatus.IN_PROGRESS),),
            "Review the current behavior",
        )
        equivalent = SessionPlan(
            (PlanStep("Inspect the implementation", PlanStepStatus.IN_PROGRESS),),
            "Review the current behavior",
        )
        revised = SessionPlan((PlanStep("Implement the change"),))

        self.assertEqual(first.fingerprint, equivalent.fingerprint)
        self.assertNotEqual(first.fingerprint, revised.fingerprint)

    def test_comment_guidance_is_bounded_to_current_plan_steps(self) -> None:
        plan = SessionPlan((PlanStep("Verify the result"),))
        comment = PlanComment(
            "plan-comment-guidance",
            1,
            "Include the exact verification command.",
            datetime(2026, 7, 29, 14, tzinfo=UTC),
        )

        guidance = plan.comment_guidance((comment,))

        self.assertIn("User comments on the current structured plan", guidance)
        self.assertIn("Step 1: Include the exact verification command.", guidance)
        with self.assertRaisesRegex(ValueError, "unknown step"):
            plan.comment_guidance(
                (
                    PlanComment(
                        "plan-comment-missing-step",
                        2,
                        "This step does not exist.",
                        datetime(2026, 7, 29, 14, tzinfo=UTC),
                    ),
                )
            )

    def test_serialized_plan_round_trips_and_renders_model_guidance(self) -> None:
        payload = {
            "explanation": "  Keep   the  durable  revision  clear.  ",
            "plan": [
                {"step": "  Inspect\tthe saved state ", "status": "completed"},
                {"step": "Apply the reviewed follow-up", "status": "in_progress"},
            ],
        }

        plan = SessionPlan.from_dict(payload)

        self.assertEqual(
            plan.to_dict(),
            {
                "explanation": "Keep the durable revision clear.",
                "plan": [
                    {"step": "Inspect the saved state", "status": "completed"},
                    {"step": "Apply the reviewed follow-up", "status": "in_progress"},
                ],
            },
        )
        self.assertEqual(plan_from_update_arguments(payload), plan)
        guidance = plan.model_guidance()
        self.assertIn("Purpose: Keep the durable revision clear.", guidance)
        self.assertIn("1. [completed] Inspect the saved state", guidance)
        self.assertIn("2. [in_progress] Apply the reviewed follow-up", guidance)

    def test_plan_rejects_malformed_public_payloads(self) -> None:
        invalid_payloads = (
            (None, "payload must be an object"),
            ({"plan": []}, "unsupported fields"),
            ({"explanation": 3, "plan": []}, "string or null"),
            ({"explanation": None, "plan": "not-an-array"}, "steps must be an array"),
            ({"explanation": None, "plan": ["not-a-step"]}, "unsupported fields"),
            (
                {"explanation": None, "plan": [{"step": "ok", "status": 1}]},
                "status is invalid",
            ),
            (
                {"explanation": None, "plan": [{"step": "ok", "status": "unknown"}]},
                "status is invalid",
            ),
        )

        for payload, message in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                SessionPlan.from_dict(payload)

    def test_plan_and_comment_values_reject_invalid_bounded_fields(self) -> None:
        timestamp = datetime(2026, 7, 29, 14, tzinfo=UTC)
        invalid_steps = (
            (None, "must be a string"),
            ("   ", "must not be empty"),
            ("contains\x01control", "control characters"),
            ("x" * (MAX_PLAN_STEP_BYTES + 1), "too large"),
        )
        for step, message in invalid_steps:
            with self.subTest(step=step), self.assertRaisesRegex(ValueError, message):
                PlanStep(step)

        with self.assertRaisesRegex(ValueError, "comment id is invalid"):
            PlanComment("", 1, "valid", timestamp)
        self.assertEqual(
            PlanComment("comment-dict", 1, "valid", timestamp).to_dict(),
            {
                "comment_id": "comment-dict",
                "step_index": 1,
                "content": "valid",
                "created_at": timestamp.isoformat(),
            },
        )
        with self.assertRaisesRegex(ValueError, "step index must be an integer"):
            PlanComment("comment-bool", True, "valid", timestamp)
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            PlanComment("comment-step", MAX_PLAN_STEPS + 1, "valid", timestamp)
        with self.assertRaisesRegex(ValueError, "comment is too large"):
            PlanComment("comment-large", 1, "x" * (MAX_PLAN_COMMENT_BYTES + 1), timestamp)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            PlanComment("comment-naive", 1, "valid", datetime.fromisoformat("2026-07-29T14:00:00"))

        with self.assertRaisesRegex(ValueError, "at least one step"):
            SessionPlan(())
        with self.assertRaisesRegex(ValueError, "at most"):
            SessionPlan(tuple(PlanStep(f"Step {index}") for index in range(MAX_PLAN_STEPS + 1)))
        with self.assertRaisesRegex(ValueError, "steps must be canonical"):
            SessionPlan(cast(tuple[PlanStep, ...], ("not-a-plan-step",)))
        with self.assertRaisesRegex(ValueError, "explanation is too large"):
            SessionPlan((PlanStep("valid"),), "x" * (MAX_PLAN_EXPLANATION_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "status must be canonical"):
            PlanStep("valid", cast(PlanStepStatus, "pending"))

    def test_comment_guidance_rejects_non_comments_and_empty_comments_are_omitted(self) -> None:
        plan = SessionPlan((PlanStep("Review the stored revision"),))

        self.assertEqual(plan.comment_guidance(()), "")
        with self.assertRaisesRegex(ValueError, "comments must be canonical"):
            plan.comment_guidance(("not-a-comment",))
