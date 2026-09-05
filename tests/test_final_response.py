from __future__ import annotations

import unittest

from neuro_code.application.runtime.agent_loop import AgentRunResult
from neuro_code.application.runtime.final_response import (
    FinalResponseContract,
    ResponseCommitState,
    ResponseSource,
)
from neuro_code.application.runtime.verification import VerificationReport, VerificationState


class FinalResponseContractTests(unittest.TestCase):
    def test_provisional_candidate_is_not_a_committed_response(self) -> None:
        report = VerificationReport(VerificationState.INCOMPLETE, (), 2, True)
        candidate = FinalResponseContract.provisional(
            "candidate",
            source=ResponseSource.NORMAL_MODEL,
            verification=report,
        )

        self.assertIs(candidate.state, ResponseCommitState.PROVISIONAL)
        self.assertFalse(candidate.is_committed)
        self.assertEqual(
            candidate.to_completion_metadata(),
            {
                "response_committed": False,
                "response_source": "normal_model",
                "verification_state": "incomplete",
                "verification_workspace_generation": 2,
            },
        )

    def test_commit_can_refresh_the_verification_projection(self) -> None:
        candidate = FinalResponseContract.provisional(
            "candidate",
            source=ResponseSource.EVIDENCE_AWARE_FINALIZER,
        )
        report = VerificationReport(VerificationState.PASS, (), 3, True)

        committed = candidate.commit(verification=report)

        self.assertTrue(committed.is_committed)
        self.assertIs(committed.state, ResponseCommitState.COMMITTED)
        self.assertIs(committed.source, ResponseSource.EVIDENCE_AWARE_FINALIZER)
        self.assertIs(committed.verification_state, VerificationState.PASS)
        self.assertEqual(committed.verification_workspace_generation, 3)

    def test_commit_can_replace_candidate_source_with_finalizer_source(self) -> None:
        candidate = FinalResponseContract.provisional(
            "candidate",
            source=ResponseSource.NORMAL_MODEL,
        )

        committed = candidate.commit(source=ResponseSource.EVIDENCE_AWARE_FINALIZER)

        self.assertTrue(committed.is_committed)
        self.assertIs(committed.source, ResponseSource.EVIDENCE_AWARE_FINALIZER)

    def test_agent_run_result_only_accepts_committed_response_contracts(self) -> None:
        candidate = FinalResponseContract.provisional(
            "candidate",
            source=ResponseSource.NORMAL_MODEL,
        )

        with self.assertRaisesRegex(ValueError, "committed final response"):
            AgentRunResult(
                None,
                "candidate",
                (),
                (),
                (),
                1,
                response_contract=candidate,
            )

    def test_legacy_result_construction_gets_a_committed_compatibility_projection(self) -> None:
        result = AgentRunResult(None, "legacy", (), (), (), 1)

        assert result.response_contract is not None
        self.assertTrue(result.response_contract.is_committed)
        self.assertIs(result.response_contract.source, ResponseSource.NORMAL_MODEL)
        self.assertEqual(result.response_contract.response, result.response)
