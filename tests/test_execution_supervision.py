from __future__ import annotations

import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.runtime.supervision import (
    AgentExecutionSupervisor,
    ExecutionControlMode,
    PathNormalizationContext,
    SupervisionCheckpoint,
    SupervisionMode,
    SupervisionTraceRecord,
    ToolExecutionObservation,
    stable_action_digest,
)
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    ExecutionBudget,
    ExecutionCounters,
    ExecutionSnapshot,
    ProgressKind,
    SessionExecutionRecord,
    SupervisionThresholds,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorReasonCode,
    ToolCallBudget,
    ToolCallCount,
    ToolInteractionFingerprint,
)


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def execution_budget(**overrides: object) -> ExecutionBudget:
    values: dict[str, object] = {
        "max_model_calls": 20,
        "max_tool_rounds": 20,
        "max_tool_calls": 30,
        "max_calls_per_tool": 10,
        "max_wall_seconds": 60.0,
        "max_input_tokens": 1_000,
        "max_output_tokens": 1_000,
        "max_total_tokens": 2_000,
    }
    values.update(overrides)
    return ExecutionBudget(**values)  # type: ignore[arg-type]


def observation(
    tool_name: str = "read_file",
    arguments: dict[str, object] | None = None,
    content: str = "evidence",
    *,
    is_error: bool = False,
    progress_kind: ProgressKind = ProgressKind.EVIDENCE,
    workspace_changed: bool = False,
    workspace_progress_token: str | None = None,
    plan_fingerprint: str | None = None,
    verification_token: str | None = None,
    external_state_token: str | None = None,
    redaction_values: Sequence[str] = (),
) -> ToolExecutionObservation:
    return ToolExecutionObservation.from_result(
        tool_name=tool_name,
        arguments=arguments or {"path": "README.md"},
        result_content=content,
        is_error=is_error,
        workspace_changed=workspace_changed,
        workspace_progress_token=workspace_progress_token,
        plan_fingerprint=plan_fingerprint,
        verification_token=verification_token,
        external_state_token=external_state_token,
        progress_kind=progress_kind,
        redaction_values=redaction_values,
    )


def execute_tool(
    supervisor: AgentExecutionSupervisor,
    tool_observation: ToolExecutionObservation,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> SupervisorDecisionKind:
    allowed = {
        SupervisorDecisionKind.CONTINUE,
        SupervisorDecisionKind.REPLAN,
    }
    if supervisor.mode is SupervisionMode.OBSERVE:
        allowed.update(
            {
                SupervisorDecisionKind.FINALIZE,
                SupervisorDecisionKind.MARK_STUCK,
                SupervisorDecisionKind.MARK_BUDGET_LIMITED,
            }
        )
    assert supervisor.authorize_model_request().kind in allowed
    assert (
        supervisor.observe_model_completion(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ).kind
        in allowed
    )
    assert supervisor.assess_tool_batch((tool_observation.tool_name,)).kind in allowed
    return supervisor.observe_tool_outcome(tool_observation).kind


class ExecutionSupervisionTests(unittest.TestCase):
    def test_execution_control_mode_and_terminal_outcome_invariants(self) -> None:
        self.assertIs(ExecutionControlMode.OBSERVE_ONLY, ExecutionControlMode("observe_only"))
        self.assertIs(
            ExecutionControlMode.FINALIZE_TERMINAL,
            ExecutionControlMode("finalize_terminal"),
        )
        stuck = AgentExecutionOutcome(
            AgentExecutionStatus.STUCK,
            SupervisorReasonCode.NO_PROGRESS,
            finalized=True,
            recoverable=True,
        )
        budget_limited = AgentExecutionOutcome(
            AgentExecutionStatus.BUDGET_LIMITED,
            SupervisorReasonCode.MODEL_STEP_LIMIT,
            finalized=True,
            recoverable=True,
        )

        self.assertTrue(stuck.recoverable)
        self.assertTrue(budget_limited.finalized)
        with self.assertRaisesRegex(ValueError, "terminal"):
            AgentExecutionOutcome(AgentExecutionStatus.RUNNING, None, False, False)
        with self.assertRaisesRegex(ValueError, "recoverable"):
            AgentExecutionOutcome(
                AgentExecutionStatus.BUDGET_LIMITED,
                SupervisorReasonCode.MODEL_CALL_BUDGET,
                finalized=True,
                recoverable=False,
            )
        with self.assertRaisesRegex(ValueError, "reason_code"):
            AgentExecutionOutcome(
                AgentExecutionStatus.COMPLETED, SupervisorReasonCode.NONE, False, False
            )

    def test_session_execution_record_accepts_only_a_terminal_outcome_and_a_safe_event_identity(
        self,
    ) -> None:
        record = SessionExecutionRecord(
            AgentExecutionOutcome(
                AgentExecutionStatus.STUCK,
                SupervisorReasonCode.NO_PROGRESS,
                finalized=True,
                recoverable=True,
            ),
            12,
            datetime(2026, 8, 1, 9, tzinfo=UTC),
        )
        self.assertIs(record.outcome.status, AgentExecutionStatus.STUCK)
        self.assertEqual(record.event_sequence, 12)
        with self.assertRaisesRegex(ValueError, "event_sequence"):
            SessionExecutionRecord(record.outcome, 0, record.completed_at)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            SessionExecutionRecord(
                record.outcome,
                record.event_sequence,
                datetime(2026, 8, 1, 9, tzinfo=UTC).replace(tzinfo=None),
            )

    def supervisor(
        self,
        *,
        budget: ExecutionBudget | None = None,
        clock: Clock | None = None,
        mode: SupervisionMode = SupervisionMode.ENFORCE,
    ) -> AgentExecutionSupervisor:
        supervisor = AgentExecutionSupervisor(
            budget or execution_budget(),
            clock=clock or Clock(),
            mode=mode,
        )
        supervisor.start_turn()
        return supervisor

    def test_execution_budget_rejects_invalid_limits(self) -> None:
        for field_name in (
            "max_model_calls",
            "max_tool_rounds",
            "max_tool_calls",
            "max_calls_per_tool",
        ):
            with (
                self.subTest(field_name=field_name, value=0),
                self.assertRaisesRegex(ValueError, "positive integer"),
            ):
                execution_budget(**{field_name: 0})
            with (
                self.subTest(field_name=field_name, value=-1),
                self.assertRaisesRegex(ValueError, "positive integer"),
            ):
                execution_budget(**{field_name: -1})
        for field_name in (
            "max_wall_seconds",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
        ):
            with self.subTest(field_name=field_name):
                self.assertIsNotNone(execution_budget(**{field_name: None}))
        ordinary_budget = execution_budget(max_model_calls=2)
        self.assertEqual(ordinary_budget.max_model_calls, 2)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            execution_budget(
                per_tool_limits=(ToolCallBudget("read_file", 2), ToolCallBudget("read_file", 1))
            )

    def test_execution_counter_invariants_are_explicit(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_completions"):
            ExecutionCounters(model_requests=0, model_completions=1)
        with self.assertRaisesRegex(ValueError, "tool_calls_executed"):
            ExecutionCounters(tool_calls_requested=1, tool_calls_executed=2)
        with self.assertRaisesRegex(ValueError, "sum"):
            ExecutionCounters(
                tool_calls_requested=2,
                per_tool_counts=(ToolCallCount("read_file", 1),),
            )

    def test_execution_value_objects_reject_invalid_boundaries_and_keep_typed_lookups(
        self,
    ) -> None:
        self.assertFalse(AgentExecutionStatus.RUNNING.terminal)
        self.assertFalse(AgentExecutionStatus.FINALIZING.terminal)
        self.assertTrue(AgentExecutionStatus.COMPLETED.terminal)

        with self.assertRaisesRegex(ValueError, "tool call budget tool_name"):
            ToolCallBudget("", 1)
        with self.assertRaisesRegex(ValueError, "tool call budget max_calls"):
            ToolCallBudget("read_file", True)
        with self.assertRaisesRegex(ValueError, "ToolCallBudget"):
            execution_budget(per_tool_limits=("read_file",))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            execution_budget(
                max_calls_per_tool=2,
                per_tool_limits=(ToolCallBudget("read_file", 3),),
            )

        budget = execution_budget(
            max_calls_per_tool=5,
            per_tool_limits=(ToolCallBudget("write_file", 3), ToolCallBudget("read_file", 1)),
        )
        self.assertEqual(
            tuple(limit.tool_name for limit in budget.per_tool_limits),
            ("read_file", "write_file"),
        )
        self.assertEqual(budget.limit_for_tool("read_file"), 1)
        self.assertEqual(budget.limit_for_tool("search"), 5)
        with self.assertRaisesRegex(ValueError, "tool_name"):
            budget.limit_for_tool("\x00")

        with self.assertRaisesRegex(ValueError, "tool_rounds"):
            ExecutionCounters(tool_rounds=1)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ExecutionCounters(
                tool_calls_requested=2,
                per_tool_counts=(ToolCallCount("read_file", 1), ToolCallCount("read_file", 1)),
            )
        counters = ExecutionCounters(
            tool_calls_requested=2,
            per_tool_counts=(ToolCallCount("write_file", 1), ToolCallCount("read_file", 1)),
        )
        self.assertEqual(counters.count_for_tool("read_file"), 1)
        self.assertEqual(counters.count_for_tool("missing"), 0)

    def test_threshold_snapshot_and_decision_invariants_reject_incoherent_state(self) -> None:
        invalid_thresholds = (
            {"repeating_action_observation_stuck": 3},
            {"repeating_action_error_stuck": 2},
            {"alternating_cycle_repetitions": 1},
            {"max_cycle_period": 1},
            {"no_progress_stuck_rounds": 5},
            {"recent_interaction_window": 5},
        )
        for overrides in invalid_thresholds:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                SupervisionThresholds(**overrides)

        fingerprint = ToolInteractionFingerprint(
            "read_file",
            "0" * 64,
            "1" * 64,
            False,
            ProgressKind.EVIDENCE,
        )
        with self.assertRaisesRegex(ValueError, "elapsed_seconds"):
            ExecutionSnapshot(
                AgentExecutionStatus.RUNNING,
                ExecutionCounters(),
                -1,
                (fingerprint,),
                0,
                0,
                0,
            )
        with self.assertRaisesRegex(ValueError, "plan_fingerprint"):
            ExecutionSnapshot(
                AgentExecutionStatus.RUNNING,
                ExecutionCounters(),
                0,
                (fingerprint,),
                0,
                0,
                0,
                plan_fingerprint="not-a-digest",
            )
        with self.assertRaisesRegex(ValueError, "status does not match"):
            SupervisorDecision(
                SupervisorDecisionKind.MARK_STUCK,
                "the loop is stuck",
                AgentExecutionStatus.RUNNING,
                False,
                SupervisorReasonCode.PERIODIC_CYCLE,
            )
        with self.assertRaisesRegex(ValueError, "should_finalize"):
            SupervisorDecision(
                SupervisorDecisionKind.FINALIZE,
                "reserve a final response",
                AgentExecutionStatus.FINALIZING,
                False,
                SupervisorReasonCode.MODEL_CALL_RESERVE,
            )

    def test_action_digest_is_typed_and_canonical(self) -> None:
        first = stable_action_digest(
            "read_file",
            {"path": "README.md", "options": {"line_end": 20, "line_start": 1}},
        )
        reordered = stable_action_digest(
            "read_file",
            {"options": {"line_start": 1, "line_end": 20}, "path": "README.md"},
        )
        changed_list = stable_action_digest("read_file", {"items": ["one", "two"]})
        reordered_list = stable_action_digest("read_file", {"items": ["two", "one"]})
        bool_value = stable_action_digest("read_file", {"value": True})
        int_value = stable_action_digest("read_file", {"value": 1})

        self.assertEqual(first, reordered)
        self.assertNotEqual(changed_list, reordered_list)
        self.assertNotEqual(bool_value, int_value)

    def test_paths_use_only_explicit_normalization_context(self) -> None:
        context = PathNormalizationContext(
            workspace_root=Path("/work/project"),
            ephemeral_roots=(Path("/run/neuro"),),
        )
        self.assertEqual(
            stable_action_digest(
                "read_file", {"path": Path("/work/project/src/main.py")}, path_context=context
            ),
            stable_action_digest("read_file", {"path": Path("src/main.py")}, path_context=context),
        )
        self.assertNotEqual(
            stable_action_digest("read_file", {"path": Path("/tmp/one.txt")}, path_context=context),
            stable_action_digest("read_file", {"path": Path("/tmp/two.txt")}, path_context=context),
        )

    def test_transport_identifiers_and_usage_do_not_affect_observation(self) -> None:
        first = ToolExecutionObservation.from_result(
            tool_name="read_file",
            arguments={"path": "README.md"},
            result_content="hello",
            is_error=False,
            tool_call_id="call-first",
            event_id="event-first",
            timestamp="2026-08-01T00:00:00Z",
            duration_seconds=0.01,
            input_tokens=12,
            output_tokens=3,
        )
        second = ToolExecutionObservation.from_result(
            tool_name="read_file",
            arguments={"path": "README.md"},
            result_content="hello",
            is_error=False,
            tool_call_id="call-second",
            event_id="event-second",
            timestamp="2030-01-01T00:00:00Z",
            duration_seconds=99.0,
            input_tokens=999,
            output_tokens=999,
        )
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_explicit_secrets_never_appear_in_observations_or_snapshots(self) -> None:
        secret = "plain-sensitive-credential"
        tool_observation = observation(
            arguments={"api_key": secret},
            content=f"authorization={secret}",
            redaction_values=(secret,),
        )
        supervisor = self.supervisor(
            budget=execution_budget(max_tool_calls=30, max_calls_per_tool=20)
        )
        execute_tool(supervisor, tool_observation)

        self.assertNotIn(secret, repr(tool_observation))
        self.assertNotIn(secret, repr(supervisor.snapshot))
        self.assertNotIn(secret, tool_observation.result_summary)

    def test_repeated_action_observation_replans_then_marks_stuck(self) -> None:
        supervisor = self.supervisor()
        tool_observation = observation(progress_kind=ProgressKind.NONE)

        self.assertIs(execute_tool(supervisor, tool_observation), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, tool_observation), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, tool_observation), SupervisorDecisionKind.REPLAN)
        self.assertIs(execute_tool(supervisor, tool_observation), SupervisorDecisionKind.MARK_STUCK)
        self.assertIs(supervisor.snapshot.status, AgentExecutionStatus.STUCK)

    def test_repeated_action_error_replans_then_marks_stuck(self) -> None:
        supervisor = self.supervisor()
        failed = observation(content="exit code 1", is_error=True, progress_kind=ProgressKind.NONE)

        self.assertIs(execute_tool(supervisor, failed), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, failed), SupervisorDecisionKind.REPLAN)
        self.assertIs(execute_tool(supervisor, failed), SupervisorDecisionKind.MARK_STUCK)

    def test_abab_cycle_marks_stuck(self) -> None:
        supervisor = self.supervisor()
        first = observation("search", {"query": "one"}, "one", progress_kind=ProgressKind.NONE)
        second = observation("search", {"query": "two"}, "two", progress_kind=ProgressKind.NONE)

        self.assertIs(execute_tool(supervisor, first), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, second), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, first), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, second), SupervisorDecisionKind.MARK_STUCK)

    def test_abcabc_cycle_marks_stuck_but_single_sequence_does_not(self) -> None:
        supervisor = self.supervisor()
        sequence = (
            observation("search", {"query": "one"}, "one", progress_kind=ProgressKind.NONE),
            observation("search", {"query": "two"}, "two", progress_kind=ProgressKind.NONE),
            observation("search", {"query": "three"}, "three", progress_kind=ProgressKind.NONE),
        )
        for item in sequence:
            self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.CONTINUE)
        for item in sequence[:-1]:
            self.assertIn(
                execute_tool(supervisor, item),
                {SupervisorDecisionKind.CONTINUE, SupervisorDecisionKind.REPLAN},
            )
        self.assertIs(execute_tool(supervisor, sequence[-1]), SupervisorDecisionKind.MARK_STUCK)

    def test_different_files_and_new_search_evidence_are_not_marked_stuck(self) -> None:
        supervisor = self.supervisor()
        trail = (
            observation(arguments={"path": "a.py"}, content="class A"),
            observation(arguments={"path": "b.py"}, content="class B"),
            observation("search", {"query": "first"}, "first finding"),
            observation("search", {"query": "second"}, "second finding"),
        )
        for item in trail:
            self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.CONTINUE)
        self.assertIs(supervisor.snapshot.status, AgentExecutionStatus.RUNNING)
        self.assertEqual(supervisor.snapshot.consecutive_no_progress_rounds, 0)

    def test_workspace_and_verification_progress_avoid_edit_test_cycle_false_positive(self) -> None:
        supervisor = self.supervisor()
        trail = (
            observation(
                "write_file",
                {"path": "app.py", "content": "first"},
                "wrote app.py",
                progress_kind=ProgressKind.WORKSPACE,
                workspace_changed=True,
                workspace_progress_token="revision-1",
            ),
            observation(
                "bash",
                {"command": "pytest"},
                "2 failures",
                is_error=True,
                progress_kind=ProgressKind.VERIFICATION,
                verification_token="2 failures",
            ),
            observation(
                "write_file",
                {"path": "app.py", "content": "second"},
                "wrote app.py again",
                progress_kind=ProgressKind.WORKSPACE,
                workspace_changed=True,
                workspace_progress_token="revision-2",
            ),
            observation(
                "bash",
                {"command": "pytest"},
                "1 failure",
                is_error=True,
                progress_kind=ProgressKind.VERIFICATION,
                verification_token="1 failure",
            ),
        )
        for item in trail:
            self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.CONTINUE)
        self.assertEqual(supervisor.snapshot.consecutive_no_progress_rounds, 0)

    def test_improved_test_result_resets_no_progress_rounds(self) -> None:
        supervisor = self.supervisor()
        static = observation(
            "bash",
            {"command": "pytest"},
            "3 failures",
            is_error=True,
            progress_kind=ProgressKind.NONE,
        )
        self.assertIs(execute_tool(supervisor, static), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, static), SupervisorDecisionKind.REPLAN)
        improved = observation(
            "bash",
            {"command": "pytest"},
            "2 failures",
            is_error=True,
            progress_kind=ProgressKind.VERIFICATION,
            verification_token="2 failures",
        )
        self.assertIs(execute_tool(supervisor, improved), SupervisorDecisionKind.CONTINUE)
        self.assertEqual(supervisor.snapshot.consecutive_no_progress_rounds, 0)

    def test_background_state_changes_are_progress_but_static_polling_is_not(self) -> None:
        supervisor = self.supervisor()
        changing = (
            observation(
                "background_status",
                {"task": "task-1"},
                "running",
                progress_kind=ProgressKind.EXTERNAL_STATE,
                external_state_token="running:10",
            ),
            observation(
                "background_status",
                {"task": "task-1"},
                "running",
                progress_kind=ProgressKind.EXTERNAL_STATE,
                external_state_token="running:30",
            ),
            observation(
                "background_status",
                {"task": "task-1"},
                "completed",
                progress_kind=ProgressKind.EXTERNAL_STATE,
                external_state_token="completed:0",
            ),
        )
        for item in changing:
            self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.CONTINUE)
        self.assertEqual(supervisor.snapshot.consecutive_no_progress_rounds, 0)

        static_supervisor = self.supervisor()
        static = observation(
            "background_status",
            {"task": "task-2"},
            "running",
            progress_kind=ProgressKind.NONE,
        )
        self.assertIs(execute_tool(static_supervisor, static), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(static_supervisor, static), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(static_supervisor, static), SupervisorDecisionKind.REPLAN)

    def test_tool_batch_budget_preflight_is_atomic(self) -> None:
        supervisor = self.supervisor(budget=execution_budget(max_tool_calls=1))
        self.assertIs(supervisor.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            supervisor.observe_model_completion(input_tokens=1, output_tokens=1).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        decision = supervisor.assess_tool_batch(("read_file", "write_file"))

        self.assertIs(decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
        self.assertEqual(supervisor.snapshot.counters.tool_rounds, 0)
        self.assertEqual(supervisor.snapshot.counters.tool_calls_requested, 0)
        self.assertEqual(supervisor.snapshot.counters.per_tool_counts, ())

    def test_single_tool_budget_is_checked_for_an_entire_batch(self) -> None:
        supervisor = self.supervisor(budget=execution_budget(max_calls_per_tool=1))
        self.assertIs(supervisor.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            supervisor.observe_model_completion(input_tokens=1, output_tokens=1).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        decision = supervisor.assess_tool_batch(("read_file", "read_file"))

        self.assertIs(decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
        self.assertEqual(supervisor.snapshot.counters.tool_calls_requested, 0)

    def test_valid_multi_tool_batch_counts_one_round_and_all_calls(self) -> None:
        supervisor = self.supervisor()
        self.assertIs(supervisor.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            supervisor.observe_model_completion(input_tokens=1, output_tokens=1).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertIs(
            supervisor.assess_tool_batch(("read_file", "search")).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertIs(
            supervisor.observe_tool_outcome(observation("read_file", content="one")).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertIs(
            supervisor.observe_tool_outcome(observation("search", content="two")).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertEqual(supervisor.snapshot.counters.tool_rounds, 1)
        self.assertEqual(supervisor.snapshot.counters.tool_calls_requested, 2)
        self.assertEqual(supervisor.snapshot.counters.tool_calls_executed, 2)

    def test_model_budget_is_not_reduced_by_finalizer_calls(self) -> None:
        supervisor = self.supervisor(budget=execution_budget(max_model_calls=3))
        first = observation(progress_kind=ProgressKind.NONE)
        second = observation(arguments={"path": "other.py"}, progress_kind=ProgressKind.NONE)
        third = observation(arguments={"path": "third.py"}, progress_kind=ProgressKind.NONE)

        self.assertIs(execute_tool(supervisor, first), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, second), SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            execute_tool(supervisor, third),
            SupervisorDecisionKind.MARK_BUDGET_LIMITED,
        )
        self.assertIs(
            supervisor.authorize_model_request().kind,
            SupervisorDecisionKind.MARK_BUDGET_LIMITED,
        )
        self.assertIs(supervisor.snapshot.status, AgentExecutionStatus.BUDGET_LIMITED)

    def test_execution_budget_contains_only_ordinary_agent_limits(self) -> None:
        supervisor = self.supervisor(budget=execution_budget(max_model_calls=1))
        self.assertEqual(supervisor._budget.max_model_calls, 1)

    def test_missing_usage_is_deterministic_and_history_is_bounded(self) -> None:
        supervisor = self.supervisor(
            budget=execution_budget(max_tool_calls=30, max_calls_per_tool=20)
        )
        for index in range(15):
            current = observation(
                arguments={"path": f"file-{index}.py"},
                content=f"evidence-{index}",
            )
            self.assertIs(execute_tool(supervisor, current), SupervisorDecisionKind.CONTINUE)
        self.assertIsNone(supervisor.snapshot.counters.input_tokens)
        self.assertIsNone(supervisor.snapshot.counters.output_tokens)
        self.assertEqual(len(supervisor.snapshot.recent_interactions), 12)

    def test_supervisors_do_not_share_turn_state(self) -> None:
        first = self.supervisor()
        second = self.supervisor()
        tool_observation = observation(progress_kind=ProgressKind.NONE)

        execute_tool(first, tool_observation)
        self.assertEqual(first.snapshot.counters.tool_calls_executed, 1)
        self.assertEqual(second.snapshot.counters.tool_calls_executed, 0)
        self.assertEqual(second.snapshot.status, AgentExecutionStatus.RUNNING)

    def test_observe_mode_records_model_calls_after_finalizer_decisions(self) -> None:
        supervisor = self.supervisor(
            budget=execution_budget(max_model_calls=2),
            mode=SupervisionMode.OBSERVE,
        )
        first = observation(progress_kind=ProgressKind.NONE)
        second = observation(arguments={"path": "second.py"}, progress_kind=ProgressKind.NONE)

        self.assertIs(execute_tool(supervisor, first), SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            execute_tool(supervisor, second),
            SupervisorDecisionKind.MARK_BUDGET_LIMITED,
        )
        decision = supervisor.authorize_model_request()
        self.assertIs(decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
        self.assertEqual(supervisor.snapshot.counters.model_requests, 3)
        self.assertIs(supervisor.snapshot.status, AgentExecutionStatus.RUNNING)
        self.assertIs(
            supervisor.observe_model_completion(input_tokens=None, output_tokens=None).kind,
            SupervisorDecisionKind.CONTINUE,
        )

    def test_observe_mode_records_an_entire_over_budget_tool_batch(self) -> None:
        supervisor = self.supervisor(
            budget=execution_budget(max_tool_calls=1),
            mode=SupervisionMode.OBSERVE,
        )
        self.assertIs(supervisor.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            supervisor.observe_model_completion(input_tokens=1, output_tokens=1).kind,
            SupervisorDecisionKind.CONTINUE,
        )

        decision = supervisor.assess_tool_batch(("read_file", "write_file"))

        self.assertIs(decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
        self.assertIs(supervisor.snapshot.status, AgentExecutionStatus.RUNNING)
        self.assertEqual(supervisor.snapshot.counters.tool_rounds, 1)
        self.assertEqual(supervisor.snapshot.counters.tool_calls_requested, 2)
        self.assertIs(
            supervisor.observe_tool_outcome(observation("read_file", content="one")).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertIs(
            supervisor.observe_tool_outcome(observation("write_file", content="two")).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertEqual(supervisor.snapshot.counters.tool_calls_executed, 2)

    def test_observe_mode_continues_after_a_stuck_decision(self) -> None:
        supervisor = self.supervisor(mode=SupervisionMode.OBSERVE)
        repeated = observation(progress_kind=ProgressKind.NONE)

        self.assertIs(execute_tool(supervisor, repeated), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, repeated), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, repeated), SupervisorDecisionKind.REPLAN)
        self.assertIs(execute_tool(supervisor, repeated), SupervisorDecisionKind.MARK_STUCK)
        self.assertIs(supervisor.snapshot.status, AgentExecutionStatus.RUNNING)
        self.assertIs(execute_tool(supervisor, repeated), SupervisorDecisionKind.MARK_STUCK)
        self.assertEqual(supervisor.snapshot.counters.tool_calls_executed, 5)

    def test_observe_mode_history_remains_bounded(self) -> None:
        supervisor = self.supervisor(
            budget=execution_budget(max_tool_calls=30, max_calls_per_tool=30),
            mode=SupervisionMode.OBSERVE,
        )
        for index in range(15):
            self.assertIs(
                execute_tool(
                    supervisor,
                    observation(
                        arguments={"path": f"file-{index}.py"},
                        content=f"evidence-{index}",
                    ),
                ),
                SupervisorDecisionKind.CONTINUE,
            )
        self.assertEqual(len(supervisor.snapshot.recent_interactions), 12)

    def test_each_budget_reports_its_specific_terminal_reason(self) -> None:
        usage_cases = (
            ("input", {"max_input_tokens": 3}, 3, 1, SupervisorReasonCode.INPUT_TOKEN_BUDGET),
            ("output", {"max_output_tokens": 3}, 1, 3, SupervisorReasonCode.OUTPUT_TOKEN_BUDGET),
            ("total", {"max_total_tokens": 4}, 2, 2, SupervisorReasonCode.TOTAL_TOKEN_BUDGET),
        )
        for name, overrides, input_tokens, output_tokens, reason in usage_cases:
            with self.subTest(name=name):
                current = self.supervisor(budget=execution_budget(**overrides))
                self.assertIs(
                    current.authorize_model_request().kind,
                    SupervisorDecisionKind.CONTINUE,
                )
                decision = current.observe_model_completion(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                self.assertIs(decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
                self.assertIs(decision.reason_code, reason)

        clock = Clock()
        wall_limited = self.supervisor(
            budget=execution_budget(max_wall_seconds=1),
            clock=clock,
        )
        clock.value += 1
        wall_decision = wall_limited.evaluate()
        self.assertIs(wall_decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
        self.assertIs(wall_decision.reason_code, SupervisorReasonCode.WALL_TIME_BUDGET)

        round_limited = self.supervisor(budget=execution_budget(max_tool_rounds=1))
        self.assertIs(round_limited.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            round_limited.observe_model_completion(input_tokens=1, output_tokens=1).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertIs(
            round_limited.assess_tool_batch(("read_file",)).kind, SupervisorDecisionKind.CONTINUE
        )
        self.assertIs(
            round_limited.observe_tool_outcome(observation()).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertIs(round_limited.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        self.assertIs(
            round_limited.observe_model_completion(input_tokens=1, output_tokens=1).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        round_decision = round_limited.assess_tool_batch(("read_file",))
        self.assertIs(round_decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
        self.assertIs(round_decision.reason_code, SupervisorReasonCode.TOOL_ROUND_BUDGET)

    def test_observe_mode_records_after_usage_budget_and_rejects_invalid_operation_order(
        self,
    ) -> None:
        supervisor = self.supervisor(
            budget=execution_budget(max_input_tokens=1),
            mode=SupervisionMode.OBSERVE,
        )
        self.assertIs(supervisor.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        decision = supervisor.observe_model_completion(input_tokens=1, output_tokens=0)
        self.assertIs(decision.kind, SupervisorDecisionKind.MARK_BUDGET_LIMITED)
        self.assertIs(supervisor.snapshot.status, AgentExecutionStatus.RUNNING)
        self.assertIs(
            supervisor.assess_tool_batch(("read_file",)).kind,
            SupervisorDecisionKind.MARK_BUDGET_LIMITED,
        )
        self.assertIs(
            supervisor.observe_tool_outcome(observation()).kind,
            SupervisorDecisionKind.MARK_BUDGET_LIMITED,
        )
        self.assertIs(
            supervisor.authorize_model_request().kind,
            SupervisorDecisionKind.MARK_BUDGET_LIMITED,
        )
        self.assertEqual(supervisor.snapshot.counters.model_requests, 2)

        ordered = self.supervisor()
        with self.assertRaisesRegex(RuntimeError, "pending completed"):
            ordered.assess_tool_batch(("read_file",))
        self.assertIs(ordered.authorize_model_request().kind, SupervisorDecisionKind.CONTINUE)
        with self.assertRaisesRegex(ValueError, "input_tokens"):
            ordered.observe_model_completion(input_tokens=True, output_tokens=1)
        self.assertEqual(ordered.snapshot.counters.model_completions, 0)
        self.assertIs(
            ordered.observe_model_completion(input_tokens=1, output_tokens=1).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        with self.assertRaisesRegex(RuntimeError, "before handling"):
            ordered.authorize_model_request()
        with self.assertRaisesRegex(ValueError, "must contain"):
            ordered.assess_tool_batch(())
        self.assertIs(
            ordered.assess_tool_batch(("read_file", "write_file")).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        with self.assertRaisesRegex(TypeError, "observation"):
            ordered.observe_tool_outcome("not-an-observation")  # type: ignore[arg-type]
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            ordered.observe_tool_outcome(observation("write_file"))
        self.assertIs(
            ordered.observe_tool_outcome(observation("read_file")).kind,
            SupervisorDecisionKind.CONTINUE,
        )
        self.assertIs(
            ordered.observe_tool_outcome(observation("write_file")).kind,
            SupervisorDecisionKind.CONTINUE,
        )

    def test_trace_record_keeps_only_redacted_snapshot_data(self) -> None:
        secret = "trace-secret-value"
        supervisor = self.supervisor(mode=SupervisionMode.OBSERVE)
        tool_observation = observation(
            arguments={"token": secret},
            content=f"token={secret}",
            redaction_values=(secret,),
        )
        execute_tool(supervisor, tool_observation)
        record = SupervisionTraceRecord(
            SupervisionCheckpoint.AFTER_TOOL,
            1,
            tool_observation.tool_name,
            supervisor.snapshot,
            supervisor.evaluate(),
        )

        self.assertNotIn(secret, repr(record))
        self.assertNotIn("arguments", repr(record))
        self.assertNotIn("token=", repr(record))

    def test_call_order_and_clock_errors_are_explicit(self) -> None:
        supervisor = AgentExecutionSupervisor(execution_budget())
        with self.assertRaisesRegex(RuntimeError, "has not started"):
            supervisor.authorize_model_request()
        supervisor.start_turn()
        with self.assertRaisesRegex(RuntimeError, "authorized model request"):
            supervisor.observe_model_completion(input_tokens=1, output_tokens=1)
        with self.assertRaisesRegex(RuntimeError, "reserved tool call"):
            supervisor.observe_tool_outcome(observation())

        clock = Clock()
        timed = self.supervisor(clock=clock)
        clock.value = 99.0
        with self.assertRaisesRegex(RuntimeError, "moved backwards"):
            timed.evaluate()


if __name__ == "__main__":
    unittest.main()
