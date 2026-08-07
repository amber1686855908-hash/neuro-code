"""Terminal statuses and deterministic supervision decisions.

定义终态状态以及确定性的监督决策."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AgentExecutionStatus(StrEnum):
    """Lifecycle state for one supervised agent execution.

    定义一次受监督 Agent 执行的生命周期状态."""

    RUNNING = "running"
    FINALIZING = "finalizing"
    BLOCKED = "blocked"
    STUCK = "stuck"
    BUDGET_LIMITED = "budget_limited"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self not in {self.RUNNING, self.FINALIZING}


class SupervisorDecisionKind(StrEnum):
    """A deterministic action selected by the execution supervisor.

    表示执行监督器选出的确定性动作."""

    CONTINUE = "continue"
    REPLAN = "replan"
    FINALIZE = "finalize"
    BLOCK = "block"
    MARK_STUCK = "mark_stuck"
    MARK_BUDGET_LIMITED = "mark_budget_limited"
    FAIL = "fail"


class SupervisorReasonCode(StrEnum):
    """Stable, machine-readable reasons for a supervision decision.

    表示监督决策使用的稳定机器可读原因."""

    NONE = "none"
    MODEL_CALL_RESERVE = "model_call_reserve"
    MODEL_CALL_BUDGET = "model_call_budget"
    MODEL_STEP_LIMIT = "model_step_limit"
    TOOL_ROUND_BUDGET = "tool_round_budget"
    TOOL_CALL_BUDGET = "tool_call_budget"
    PER_TOOL_CALL_BUDGET = "per_tool_call_budget"
    WALL_TIME_BUDGET = "wall_time_budget"
    INPUT_TOKEN_BUDGET = "input_token_budget"
    OUTPUT_TOKEN_BUDGET = "output_token_budget"
    TOTAL_TOKEN_BUDGET = "total_token_budget"
    REPEATED_ACTION_OBSERVATION = "repeated_action_observation"
    REPEATED_ACTION_ERROR = "repeated_action_error"
    PERIODIC_CYCLE = "periodic_cycle"
    NO_PROGRESS = "no_progress"
    EXTERNAL_BLOCKED = "external_blocked"
    INTERNAL_FAILURE = "internal_failure"


class ProgressKind(StrEnum):
    """The strongest known progress produced by one tool interaction.

    表示一次工具交互产生的最高等级已知进展."""

    NONE = "none"
    EVIDENCE = "evidence"
    WORKSPACE = "workspace"
    PLAN = "plan"
    VERIFICATION = "verification"
    EXTERNAL_STATE = "external_state"


@dataclass(frozen=True, slots=True)
class AgentExecutionOutcome:
    """A recoverable terminal result produced by controlled execution supervision.

    表示受控执行监督产生的可恢复终态结果."""

    status: AgentExecutionStatus
    reason_code: SupervisorReasonCode | None
    finalized: bool
    recoverable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.status, AgentExecutionStatus):
            raise ValueError("execution outcome status must be canonical")
        if self.status in {AgentExecutionStatus.RUNNING, AgentExecutionStatus.FINALIZING}:
            raise ValueError("execution outcome must be terminal")
        if self.reason_code is not None and not isinstance(
            self.reason_code,
            SupervisorReasonCode,
        ):
            raise ValueError("execution outcome reason_code must be canonical or None")
        if not isinstance(self.finalized, bool):
            raise ValueError("execution outcome finalized must be a bool")
        if not isinstance(self.recoverable, bool):
            raise ValueError("execution outcome recoverable must be a bool")
        if (
            self.status in {AgentExecutionStatus.STUCK, AgentExecutionStatus.BUDGET_LIMITED}
            and not self.recoverable
        ):
            raise ValueError("stuck and budget-limited outcomes must be recoverable")
        if self.status is AgentExecutionStatus.COMPLETED and self.reason_code is not None:
            raise ValueError("completed outcomes must not have a reason_code")


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    """One typed outcome from a deterministic supervision evaluation.

    表示一次确定性监督评估产生的类型化结果."""

    kind: SupervisorDecisionKind
    reason: str
    status: AgentExecutionStatus
    should_finalize: bool
    reason_code: SupervisorReasonCode = SupervisorReasonCode.NONE

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SupervisorDecisionKind):
            raise ValueError("supervisor decision kind must be canonical")
        if not isinstance(self.status, AgentExecutionStatus):
            raise ValueError("supervisor decision status must be canonical")
        if not isinstance(self.reason_code, SupervisorReasonCode):
            raise ValueError("supervisor decision reason_code must be canonical")
        if (
            not isinstance(self.reason, str)
            or not self.reason
            or "\x00" in self.reason
            or any(ord(character) < 32 and character not in "\n\t" for character in self.reason)
        ):
            raise ValueError("supervisor decision reason must be non-empty safe text")
        if not isinstance(self.should_finalize, bool):
            raise ValueError("should_finalize must be a bool")

        expected_status = {
            SupervisorDecisionKind.CONTINUE: AgentExecutionStatus.RUNNING,
            SupervisorDecisionKind.REPLAN: AgentExecutionStatus.RUNNING,
            SupervisorDecisionKind.FINALIZE: AgentExecutionStatus.FINALIZING,
            SupervisorDecisionKind.BLOCK: AgentExecutionStatus.BLOCKED,
            SupervisorDecisionKind.MARK_STUCK: AgentExecutionStatus.STUCK,
            SupervisorDecisionKind.MARK_BUDGET_LIMITED: AgentExecutionStatus.BUDGET_LIMITED,
            SupervisorDecisionKind.FAIL: AgentExecutionStatus.FAILED,
        }[self.kind]
        if self.status is not expected_status:
            raise ValueError("supervisor decision status does not match its kind")
        if self.should_finalize != (self.kind is SupervisorDecisionKind.FINALIZE):
            raise ValueError("should_finalize must match a FINALIZE decision")


__all__ = [
    "AgentExecutionOutcome",
    "AgentExecutionStatus",
    "ProgressKind",
    "SupervisorDecision",
    "SupervisorDecisionKind",
    "SupervisorReasonCode",
]
