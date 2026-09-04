from __future__ import annotations

import unittest

from neuro_code.application.runtime.supervision import ToolExecutionObservation
from neuro_code.application.runtime.tool_pipeline import ToolObservationBuilder
from neuro_code.application.runtime.verification import (
    MAX_VERIFICATION_EVIDENCE_ITEMS,
    MAX_VERIFICATION_SCOPE_BYTES,
    MAX_VERIFICATION_SUMMARY_BYTES,
    VerificationEvidence,
    VerificationFreshness,
    VerificationOutcome,
    VerificationState,
    VerificationTracker,
)
from neuro_code.domain.execution import ProgressKind
from neuro_code.domain.tools import ToolResult


def observation(
    *,
    tool_name: str = "bash",
    content: str = "verification output",
    is_error: bool = False,
    progress_kind: ProgressKind = ProgressKind.NONE,
    workspace_changed: bool = False,
    verification_scope: tuple[str, ...] = (),
) -> ToolExecutionObservation:
    return ToolExecutionObservation.from_result(
        tool_name=tool_name,
        arguments={"command": "pytest -q"},
        result_content=content,
        is_error=is_error,
        progress_kind=progress_kind,
        workspace_changed=workspace_changed,
        verification_scope=verification_scope,
    )


class VerificationTrackerTests(unittest.TestCase):
    def test_mutation_then_success_is_pass(self) -> None:
        tracker = VerificationTracker()

        tracker.observe(observation(workspace_changed=True, progress_kind=ProgressKind.WORKSPACE))
        tracker.observe(
            observation(
                content="1 passed",
                progress_kind=ProgressKind.VERIFICATION,
                verification_scope=("bash:test",),
            )
        )

        report = tracker.report()
        self.assertIs(report.state, VerificationState.PASS)
        self.assertIs(report.latest_freshness, VerificationFreshness.CURRENT)
        self.assertEqual(report.latest.outcome, VerificationOutcome.SUCCESS)  # type: ignore[union-attr]
        self.assertTrue(report.confirmed_items)
        self.assertEqual(report.unverified_items, ())

    def test_mutation_then_failure_is_fail(self) -> None:
        tracker = VerificationTracker()

        tracker.observe(observation(workspace_changed=True, progress_kind=ProgressKind.WORKSPACE))
        tracker.observe(
            observation(
                content="1 failed",
                is_error=True,
                progress_kind=ProgressKind.VERIFICATION,
            )
        )

        report = tracker.report()
        self.assertIs(report.state, VerificationState.FAIL)
        self.assertIs(report.latest_freshness, VerificationFreshness.CURRENT)
        self.assertEqual(report.confirmed_items, ())
        self.assertIn("failed", report.unverified_items[0])

    def test_success_then_mutation_is_incomplete_and_stale(self) -> None:
        tracker = VerificationTracker()

        tracker.observe(
            observation(
                content="1 passed",
                progress_kind=ProgressKind.VERIFICATION,
            )
        )
        tracker.observe(observation(workspace_changed=True, progress_kind=ProgressKind.WORKSPACE))

        report = tracker.report()
        self.assertIs(report.state, VerificationState.INCOMPLETE)
        self.assertIs(report.latest_freshness, VerificationFreshness.STALE)
        self.assertEqual(report.confirmed_items, ())
        self.assertIn("stale", report.unverified_items[0])

    def test_mutation_without_verification_is_incomplete(self) -> None:
        tracker = VerificationTracker()
        tracker.observe(observation(workspace_changed=True, progress_kind=ProgressKind.WORKSPACE))

        self.assertIs(tracker.report().state, VerificationState.INCOMPLETE)

    def test_without_mutation_or_requirement_is_not_applicable(self) -> None:
        self.assertIs(VerificationTracker().report().state, VerificationState.NOT_APPLICABLE)

    def test_explicit_requirement_without_mutation_is_incomplete(self) -> None:
        tracker = VerificationTracker(verification_required=True)

        self.assertIs(tracker.report().state, VerificationState.INCOMPLETE)

    def test_failed_then_successful_verification_is_pass(self) -> None:
        tracker = VerificationTracker()
        tracker.observe(
            observation(content="1 failed", is_error=True, progress_kind=ProgressKind.VERIFICATION)
        )
        tracker.observe(observation(content="1 passed", progress_kind=ProgressKind.VERIFICATION))

        report = tracker.report()
        self.assertIs(report.state, VerificationState.PASS)
        self.assertEqual(len(report.evidence), 2)

    def test_successful_then_failed_verification_is_fail(self) -> None:
        tracker = VerificationTracker()
        tracker.observe(observation(content="1 passed", progress_kind=ProgressKind.VERIFICATION))
        tracker.observe(
            observation(content="1 failed", is_error=True, progress_kind=ProgressKind.VERIFICATION)
        )

        report = tracker.report()
        self.assertIs(report.state, VerificationState.FAIL)
        self.assertEqual(report.latest.outcome, VerificationOutcome.FAILURE)  # type: ignore[union-attr]

    def test_verification_evidence_is_bounded_and_redacted(self) -> None:
        secret = "api_key=plain-secret"
        evidence = VerificationEvidence.from_result(
            tool_name="bash",
            result_content=f"{secret} {'x' * (MAX_VERIFICATION_SUMMARY_BYTES + 100)}",
            is_error=False,
            redaction_values=(secret,),
        )

        self.assertNotIn("plain-secret", evidence.summary)
        self.assertLessEqual(len(evidence.summary.encode("utf-8")), MAX_VERIFICATION_SUMMARY_BYTES)
        tracker = VerificationTracker()
        for _ in range(MAX_VERIFICATION_EVIDENCE_ITEMS + 2):
            tracker.record_verification(evidence)
        self.assertEqual(len(tracker.report().evidence), MAX_VERIFICATION_EVIDENCE_ITEMS)

    def test_verification_evidence_uses_utf8_byte_bounds(self) -> None:
        evidence = VerificationEvidence(
            "bash",
            VerificationOutcome.SUCCESS,
            "验证" * MAX_VERIFICATION_SUMMARY_BYTES,
            ("范围" * MAX_VERIFICATION_SCOPE_BYTES,),
        )

        self.assertLessEqual(
            len(evidence.summary.encode("utf-8")),
            MAX_VERIFICATION_SUMMARY_BYTES,
        )
        self.assertLessEqual(
            len(evidence.scope[0].encode("utf-8")),
            MAX_VERIFICATION_SCOPE_BYTES,
        )

    def test_completed_safe_bash_verification_gets_real_outcome_and_scope(self) -> None:
        observation_value = ToolObservationBuilder(()).build(
            tool_name="bash",
            arguments={"command": "pytest -q tests/unit"},
            result=ToolResult("2 passed", metadata={"exit_code": 0}),
            tool=None,
            change_report=None,
            plan_fingerprint_before=None,
            current_plan_fingerprint=None,
            tool_call_id="call-1",
        )

        self.assertIs(observation_value.progress_kind, ProgressKind.VERIFICATION)
        self.assertIsNotNone(observation_value.verification)
        assert observation_value.verification is not None
        self.assertIs(observation_value.verification.outcome, VerificationOutcome.SUCCESS)
        self.assertEqual(observation_value.verification.scope, ("bash:test",))

    def test_unexecuted_verification_shape_does_not_create_evidence(self) -> None:
        observation_value = ToolObservationBuilder(()).build(
            tool_name="bash",
            arguments={"command": "pytest -q"},
            result=ToolResult("permission denied", is_error=True),
            tool=None,
            change_report=None,
            plan_fingerprint_before=None,
            current_plan_fingerprint=None,
            tool_call_id="call-1",
            verification_eligible=False,
        )

        self.assertIs(observation_value.progress_kind, ProgressKind.NONE)
        self.assertIsNone(observation_value.verification)


if __name__ == "__main__":
    unittest.main()
