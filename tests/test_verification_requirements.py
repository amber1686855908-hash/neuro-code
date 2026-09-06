from __future__ import annotations

import unittest

from neuro_code.application.runtime.finalization import FinalizationEvidence
from neuro_code.application.runtime.supervision import ToolExecutionObservation
from neuro_code.application.runtime.verification import (
    MAX_VERIFICATION_EVIDENCE_ITEMS,
    RequirementEvaluationState,
    VerificationBlocker,
    VerificationBlockReason,
    VerificationEvidence,
    VerificationOutcome,
    VerificationState,
    VerificationTracker,
)
from neuro_code.domain.execution import (
    MAX_REQUIREMENT_CRITERION_BYTES,
    MAX_VERIFICATION_REQUIREMENTS,
    ProgressKind,
    RequirementActivation,
    RequirementProvenance,
    RequirementSource,
    RequirementStrength,
    SupervisorReasonCode,
    VerificationRequirement,
    VerificationRequirementsSnapshot,
    canonical_requirement_id,
)


def requirement(
    criterion: str,
    *,
    scope: tuple[str, ...] = (),
    strength: RequirementStrength = RequirementStrength.REQUIRED,
    activation: RequirementActivation = RequirementActivation.ALWAYS,
    origin_id: str | None = None,
) -> VerificationRequirement:
    provenance = (
        (RequirementProvenance(RequirementSource.EXPLICIT_USER, origin_id),)
        if origin_id is not None
        else ()
    )
    return VerificationRequirement.create(
        criterion=criterion,
        scope=scope,
        strength=strength,
        activation=activation,
        provenance=provenance,
    )


def evidence(
    requirement_ids: tuple[str, ...] = (),
    *,
    outcome: VerificationOutcome = VerificationOutcome.SUCCESS,
) -> VerificationEvidence:
    return VerificationEvidence(
        "bash",
        outcome,
        "verification result",
        ("bash:test",),
        0,
        requirement_ids,
    )


class RequirementModelTests(unittest.TestCase):
    def test_identity_normalizes_unicode_whitespace_and_scope_order(self) -> None:
        first = canonical_requirement_id("  Verify\t登录\n  ", ("z", " auth/login "))
        second = canonical_requirement_id("Verify 登录", ("auth/login", "z"))

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("req-v1-"))

    def test_strength_and_provenance_do_not_change_identity(self) -> None:
        advisory = requirement(
            "run the checks",
            strength=RequirementStrength.ADVISORY,
            origin_id="planner-1",
        )
        required = requirement(
            "run the checks",
            strength=RequirementStrength.REQUIRED,
            origin_id="user-1",
        )

        self.assertEqual(advisory.requirement_id, required.requirement_id)
        snapshot = VerificationRequirementsSnapshot.from_requirements((advisory, required))
        self.assertEqual(len(snapshot.requirements), 1)
        self.assertIs(snapshot.requirements[0].strength, RequirementStrength.REQUIRED)
        self.assertEqual(
            {item.origin_id for item in snapshot.requirements[0].provenance},
            {"planner-1", "user-1"},
        )

    def test_snapshot_is_bounded_and_round_trips_by_fingerprint(self) -> None:
        values = tuple(
            requirement(f"check {index}") for index in range(MAX_VERIFICATION_REQUIREMENTS)
        )
        snapshot = VerificationRequirementsSnapshot.from_requirements(values)
        restored = VerificationRequirementsSnapshot.from_dict(snapshot.to_dict())

        self.assertEqual(snapshot.fingerprint, restored.fingerprint)
        self.assertEqual(snapshot.requirement_ids, restored.requirement_ids)
        with self.assertRaises(ValueError):
            VerificationRequirementsSnapshot.from_requirements(
                (*values, requirement("one too many"))
            )

    def test_requirement_text_is_redacted_and_strictly_bounded(self) -> None:
        safe = requirement("api_key=secret-value")
        self.assertNotIn("secret-value", safe.criterion)
        with self.assertRaises(ValueError):
            requirement("x" * (MAX_REQUIREMENT_CRITERION_BYTES + 1))
        with self.assertRaises(ValueError):
            requirement("unsafe\x00criterion")


class StructuredVerificationTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.r1 = requirement("tests cover auth/login", scope=("auth/login",))
        self.r2 = requirement("tests cover billing", scope=("billing",))
        self.snapshot = VerificationRequirementsSnapshot.from_requirements((self.r1, self.r2))

    def test_empty_coverage_and_classifier_scope_satisfy_nothing(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        tracker.record_verification(evidence())

        report = tracker.report()
        self.assertIs(report.state, VerificationState.INCOMPLETE)
        self.assertEqual(
            {item.state for item in report.requirement_evaluations},
            {RequirementEvaluationState.NO_EVIDENCE},
        )

    def test_observation_preserves_explicit_requirement_coverage(self) -> None:
        observation = ToolExecutionObservation.from_result(
            tool_name="bash",
            arguments={"command": "pytest -q"},
            result_content="1 passed",
            is_error=False,
            progress_kind=ProgressKind.VERIFICATION,
            covered_requirement_ids=(self.r1.requirement_id,),
        )

        assert observation.verification is not None
        self.assertEqual(
            observation.verification.covered_requirement_ids,
            (self.r1.requirement_id,),
        )

    def test_one_evidence_can_cover_multiple_requirements(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        tracker.record_verification(evidence((self.r1.requirement_id, self.r2.requirement_id)))

        report = tracker.report()
        self.assertIs(report.state, VerificationState.PASS)
        self.assertEqual(
            {item.state for item in report.requirement_evaluations},
            {RequirementEvaluationState.SATISFIED},
        )

    def test_unknown_coverage_id_is_rejected(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        unknown = canonical_requirement_id("not declared")

        with self.assertRaisesRegex(ValueError, "unknown requirement"):
            tracker.record_verification(evidence((unknown,)))
        self.assertEqual(tracker.report().evidence, ())

    def test_requirement_truth_survives_diagnostic_ring_eviction(self) -> None:
        requirements = tuple(requirement(f"requirement {index}") for index in range(6))
        snapshot = VerificationRequirementsSnapshot.from_requirements(requirements)
        tracker = VerificationTracker(requirements=snapshot)

        for item in requirements:
            tracker.record_verification(evidence((item.requirement_id,)))

        report = tracker.report()
        self.assertIs(report.state, VerificationState.PASS)
        self.assertEqual(len(report.evidence), MAX_VERIFICATION_EVIDENCE_ITEMS)
        self.assertEqual(len(report.requirement_evaluations), 6)
        self.assertTrue(
            all(
                item.state is RequirementEvaluationState.SATISFIED
                for item in report.requirement_evaluations
            )
        )

    def test_latest_fact_wins_for_one_requirement_but_unrelated_fact_does_not(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        tracker.record_verification(
            evidence((self.r1.requirement_id,), outcome=VerificationOutcome.FAILURE)
        )
        tracker.record_verification(evidence((self.r2.requirement_id,)))
        report = tracker.report()

        states = {item.requirement_id: item.state for item in report.requirement_evaluations}
        self.assertIs(states[self.r1.requirement_id], RequirementEvaluationState.FAILED)
        self.assertIs(states[self.r2.requirement_id], RequirementEvaluationState.SATISFIED)
        self.assertIs(report.state, VerificationState.FAIL)

        tracker.record_verification(evidence((self.r1.requirement_id,)))
        self.assertIs(tracker.report().state, VerificationState.PASS)

        tracker.record_verification(
            evidence((self.r1.requirement_id,), outcome=VerificationOutcome.FAILURE)
        )
        self.assertIs(tracker.report().state, VerificationState.FAIL)

    def test_mutation_makes_old_success_and_failure_stale(self) -> None:
        for outcome in (VerificationOutcome.SUCCESS, VerificationOutcome.FAILURE):
            tracker = VerificationTracker(requirements=self.snapshot)
            tracker.record_verification(evidence((self.r1.requirement_id,), outcome=outcome))
            tracker.record_workspace_mutation()

            report = tracker.report()
            evaluation = next(
                item
                for item in report.requirement_evaluations
                if item.requirement_id == self.r1.requirement_id
            )
            self.assertIs(evaluation.state, RequirementEvaluationState.STALE)
            self.assertIs(report.state, VerificationState.INCOMPLETE)

    def test_typed_blocker_is_current_but_old_blocker_is_not(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        tracker.record_blocker(
            VerificationBlocker(
                (self.r1.requirement_id,),
                VerificationBlockReason.PERMISSION_DENIED,
                0,
                "permission was denied",
            )
        )
        report = tracker.report()
        evaluation = next(
            item
            for item in report.requirement_evaluations
            if item.requirement_id == self.r1.requirement_id
        )
        self.assertIs(evaluation.state, RequirementEvaluationState.BLOCKED)
        self.assertIs(report.state, VerificationState.INCOMPLETE)

        tracker.record_workspace_mutation()
        evaluation = next(
            item
            for item in tracker.report().requirement_evaluations
            if item.requirement_id == self.r1.requirement_id
        )
        self.assertIs(evaluation.state, RequirementEvaluationState.NO_EVIDENCE)

    def test_current_evidence_wins_over_blocker(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        tracker.record_blocker(
            VerificationBlocker(
                (self.r1.requirement_id,),
                VerificationBlockReason.ENVIRONMENT_UNAVAILABLE,
                0,
            )
        )
        tracker.record_verification(evidence((self.r2.requirement_id,)))
        tracker.record_verification(evidence((self.r1.requirement_id,)))

        self.assertIs(tracker.report().state, VerificationState.PASS)

    def test_no_evidence_is_distinct_from_blocked(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        report = tracker.report()
        self.assertEqual(
            {item.state for item in report.requirement_evaluations},
            {RequirementEvaluationState.NO_EVIDENCE},
        )
        with self.assertRaises(ValueError):
            VerificationTracker().record_blocker(
                VerificationBlocker(
                    (self.r1.requirement_id,),
                    VerificationBlockReason.USER_CONSTRAINT,
                    0,
                )
            )

    def test_advisory_failure_does_not_fail_required_pass(self) -> None:
        advisory = requirement("optional check", strength=RequirementStrength.ADVISORY)
        snapshot = VerificationRequirementsSnapshot.from_requirements((self.r1, advisory))
        tracker = VerificationTracker(requirements=snapshot)
        tracker.record_verification(evidence((self.r1.requirement_id,)))
        tracker.record_verification(
            evidence((advisory.requirement_id,), outcome=VerificationOutcome.FAILURE)
        )

        report = tracker.report()
        self.assertIs(report.state, VerificationState.PASS)
        self.assertEqual(
            report.confirmed_items, ("All required verification requirements are satisfied.",)
        )

    def test_advisory_only_does_not_claim_fully_verified(self) -> None:
        advisory = requirement("optional check", strength=RequirementStrength.ADVISORY)
        tracker = VerificationTracker(
            requirements=VerificationRequirementsSnapshot.from_requirements((advisory,))
        )
        tracker.record_verification(evidence((advisory.requirement_id,)))

        report = tracker.report()
        self.assertIs(report.state, VerificationState.NOT_APPLICABLE)
        self.assertEqual(report.confirmed_items, ())
        self.assertTrue(report.unverified_items)

    def test_on_mutation_requirement_activates_after_generation_changes(self) -> None:
        on_mutation = requirement(
            "verify changed files",
            activation=RequirementActivation.ON_WORKSPACE_MUTATION,
        )
        tracker = VerificationTracker(
            requirements=VerificationRequirementsSnapshot.from_requirements((on_mutation,))
        )
        self.assertIs(tracker.report().state, VerificationState.NOT_APPLICABLE)
        self.assertEqual(tracker.report().requirement_evaluations, ())

        tracker.record_workspace_mutation()
        report = tracker.report()
        self.assertIs(report.state, VerificationState.INCOMPLETE)
        self.assertIs(
            report.requirement_evaluations[0].state,
            RequirementEvaluationState.NO_EVIDENCE,
        )

        tracker.record_verification(evidence((on_mutation.requirement_id,)))
        self.assertIs(tracker.report().state, VerificationState.PASS)

    def test_required_failure_precedes_other_incomplete_requirements(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        tracker.record_verification(
            evidence((self.r1.requirement_id,), outcome=VerificationOutcome.FAILURE)
        )

        self.assertIs(tracker.report().state, VerificationState.FAIL)

    def test_report_exposes_bounded_structured_metadata_without_criteria(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        data = tracker.report().to_event_data()

        self.assertEqual(data["required_count"], 2)
        self.assertEqual(data["advisory_count"], 0)
        self.assertEqual(data["evidence_count"], 0)
        self.assertNotIn(self.r1.criterion, data)

    def test_finalization_evidence_can_consume_structured_projection(self) -> None:
        tracker = VerificationTracker(requirements=self.snapshot)
        tracker.record_verification(evidence((self.r1.requirement_id, self.r2.requirement_id)))
        report = tracker.report()

        finalization_evidence = FinalizationEvidence(
            SupervisorReasonCode.MODEL_STEP_LIMIT,
            verification_state=report.state,
            requirement_evaluations=report.requirement_evaluations,
        )

        self.assertEqual(
            finalization_evidence.requirement_evaluations,
            report.requirement_evaluations,
        )


if __name__ == "__main__":
    unittest.main()
