"""Pure execution-supervision values for one agent turn.

These values deliberately describe execution state without depending on a
provider, a session store, a user interface, or a concrete tool adapter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AgentExecutionStatus(StrEnum):
    """Lifecycle state for one supervised agent execution."""

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


class TurnCancellationPolicy(StrEnum):
    """Controls whether an unstarted turn may be removed after cancellation."""

    RETAIN = "retain"
    REWIND_PRISTINE = "rewind_pristine"


class TurnSource(StrEnum):
    """Identifies whether a model turn came from a user or a background wake."""

    USER = "user"
    BACKGROUND_TASK_AUTO_WAKE = "background_task_auto_wake"


class SupervisorDecisionKind(StrEnum):
    """A deterministic action selected by the execution supervisor."""

    CONTINUE = "continue"
    REPLAN = "replan"
    FINALIZE = "finalize"
    BLOCK = "block"
    MARK_STUCK = "mark_stuck"
    MARK_BUDGET_LIMITED = "mark_budget_limited"
    FAIL = "fail"


class SupervisorReasonCode(StrEnum):
    """Stable, machine-readable reasons for a supervision decision."""

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
    """The strongest known progress produced by one tool interaction."""

    NONE = "none"
    EVIDENCE = "evidence"
    WORKSPACE = "workspace"
    PLAN = "plan"
    VERIFICATION = "verification"
    EXTERNAL_STATE = "external_state"


@dataclass(frozen=True, slots=True)
class AgentExecutionOutcome:
    """A recoverable terminal result produced by controlled execution supervision."""

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
class SessionExecutionRecord:
    """The last durable terminal execution result for one session.

    This intentionally retains only stable outcome metadata and the matching
    ``TURN_COMPLETED`` event identity. It is safe to load during resume without
    retaining prompts, tool arguments, output, evidence, or supervisor state.
    """

    outcome: AgentExecutionOutcome
    event_sequence: int
    completed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AgentExecutionOutcome):
            raise ValueError("session execution record outcome must be canonical")
        _require_positive_int(self.event_sequence, field_name="event_sequence")
        if not isinstance(self.completed_at, datetime) or self.completed_at.tzinfo is None:
            raise ValueError("session execution record timestamp must be timezone-aware")


def _require_positive_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_optional_positive_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_positive_int(value, field_name=field_name)


def _require_optional_positive_seconds(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be a finite positive number or None")
    return float(value)


def _require_digest(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_tool_name(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty canonical tool name")
    return value


@dataclass(frozen=True, slots=True)
class ToolCallBudget:
    """A stricter call limit for one named tool."""

    tool_name: str
    max_calls: int

    def __post_init__(self) -> None:
        _require_tool_name(self.tool_name, field_name="tool call budget tool_name")
        _require_positive_int(self.max_calls, field_name="tool call budget max_calls")


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """Independent bounded resources for one agent turn."""

    max_model_calls: int
    max_tool_rounds: int
    max_tool_calls: int
    max_calls_per_tool: int
    max_wall_seconds: float | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_total_tokens: int | None
    finalizer_model_calls: int
    per_tool_limits: tuple[ToolCallBudget, ...] = ()

    def __post_init__(self) -> None:
        _require_positive_int(self.max_model_calls, field_name="max_model_calls")
        _require_positive_int(self.max_tool_rounds, field_name="max_tool_rounds")
        _require_positive_int(self.max_tool_calls, field_name="max_tool_calls")
        _require_positive_int(self.max_calls_per_tool, field_name="max_calls_per_tool")
        _require_optional_positive_seconds(self.max_wall_seconds, field_name="max_wall_seconds")
        _require_optional_positive_int(self.max_input_tokens, field_name="max_input_tokens")
        _require_optional_positive_int(self.max_output_tokens, field_name="max_output_tokens")
        _require_optional_positive_int(self.max_total_tokens, field_name="max_total_tokens")
        _require_positive_int(self.finalizer_model_calls, field_name="finalizer_model_calls")
        if self.finalizer_model_calls > self.max_model_calls:
            raise ValueError("finalizer_model_calls must not exceed max_model_calls")

        limits = tuple(self.per_tool_limits)
        if not all(isinstance(limit, ToolCallBudget) for limit in limits):
            raise ValueError("per_tool_limits must contain ToolCallBudget values")
        names = tuple(limit.tool_name for limit in limits)
        if len(set(names)) != len(names):
            raise ValueError("per_tool_limits must not contain duplicate tool names")
        if any(limit.max_calls > self.max_calls_per_tool for limit in limits):
            raise ValueError("per-tool limits must not exceed max_calls_per_tool")
        object.__setattr__(
            self, "per_tool_limits", tuple(sorted(limits, key=lambda limit: limit.tool_name))
        )

    def limit_for_tool(self, tool_name: str) -> int:
        """Return the effective call limit for one tool."""

        _require_tool_name(tool_name, field_name="tool_name")
        for limit in self.per_tool_limits:
            if limit.tool_name == tool_name:
                return limit.max_calls
        return self.max_calls_per_tool


@dataclass(frozen=True, slots=True)
class SupervisionThresholds:
    """Bounded detector thresholds for one supervised turn."""

    repeating_action_observation: int = 3
    repeating_action_observation_stuck: int = 4
    repeating_action_error: int = 2
    repeating_action_error_stuck: int = 3
    alternating_cycle_repetitions: int = 2
    max_cycle_period: int = 3
    no_progress_replan_rounds: int = 5
    no_progress_stuck_rounds: int = 6
    recent_interaction_window: int = 12

    def __post_init__(self) -> None:
        _require_positive_int(
            self.repeating_action_observation,
            field_name="repeating_action_observation",
        )
        _require_positive_int(
            self.repeating_action_observation_stuck,
            field_name="repeating_action_observation_stuck",
        )
        _require_positive_int(self.repeating_action_error, field_name="repeating_action_error")
        _require_positive_int(
            self.repeating_action_error_stuck,
            field_name="repeating_action_error_stuck",
        )
        _require_positive_int(
            self.alternating_cycle_repetitions,
            field_name="alternating_cycle_repetitions",
        )
        _require_positive_int(self.max_cycle_period, field_name="max_cycle_period")
        _require_positive_int(
            self.no_progress_replan_rounds,
            field_name="no_progress_replan_rounds",
        )
        _require_positive_int(
            self.no_progress_stuck_rounds,
            field_name="no_progress_stuck_rounds",
        )
        _require_positive_int(
            self.recent_interaction_window,
            field_name="recent_interaction_window",
        )
        if self.repeating_action_observation_stuck <= self.repeating_action_observation:
            raise ValueError(
                "repeating_action_observation_stuck must exceed repeating_action_observation"
            )
        if self.repeating_action_error_stuck <= self.repeating_action_error:
            raise ValueError("repeating_action_error_stuck must exceed repeating_action_error")
        if self.alternating_cycle_repetitions < 2:
            raise ValueError("alternating_cycle_repetitions must be at least 2")
        if self.max_cycle_period < 2:
            raise ValueError("max_cycle_period must be at least 2")
        if self.no_progress_stuck_rounds <= self.no_progress_replan_rounds:
            raise ValueError("no_progress_stuck_rounds must exceed no_progress_replan_rounds")
        required_window = self.max_cycle_period * self.alternating_cycle_repetitions
        if self.recent_interaction_window < required_window:
            raise ValueError("recent_interaction_window is too small for cycle detection")


@dataclass(frozen=True, slots=True)
class ToolInteractionFingerprint:
    """A redacted, stable tool action/result identity used for detection."""

    tool_name: str
    action_digest: str
    observation_digest: str
    is_error: bool
    progress_kind: ProgressKind

    def __post_init__(self) -> None:
        _require_tool_name(self.tool_name, field_name="fingerprint tool_name")
        _require_digest(self.action_digest, field_name="action_digest")
        _require_digest(self.observation_digest, field_name="observation_digest")
        if not isinstance(self.is_error, bool):
            raise ValueError("is_error must be a bool")
        if not isinstance(self.progress_kind, ProgressKind):
            raise ValueError("progress_kind must be canonical")

    @property
    def behavior_signature(self) -> tuple[str, str, bool]:
        """Return the bounded comparison key used by repetition detectors."""

        return (self.action_digest, self.observation_digest, self.is_error)


@dataclass(frozen=True, slots=True)
class ToolCallCount:
    """One immutable, comparable per-tool reservation count."""

    tool_name: str
    count: int

    def __post_init__(self) -> None:
        _require_tool_name(self.tool_name, field_name="tool call count tool_name")
        _require_non_negative_int(self.count, field_name="tool call count")


@dataclass(frozen=True, slots=True)
class ExecutionCounters:
    """Monotonic counts accumulated within one supervised turn."""

    model_requests: int = 0
    model_completions: int = 0
    tool_rounds: int = 0
    tool_calls_requested: int = 0
    tool_calls_executed: int = 0
    per_tool_counts: tuple[ToolCallCount, ...] = ()
    input_tokens: int | None = 0
    output_tokens: int | None = 0

    def __post_init__(self) -> None:
        _require_non_negative_int(self.model_requests, field_name="model_requests")
        _require_non_negative_int(self.model_completions, field_name="model_completions")
        _require_non_negative_int(self.tool_rounds, field_name="tool_rounds")
        _require_non_negative_int(self.tool_calls_requested, field_name="tool_calls_requested")
        _require_non_negative_int(self.tool_calls_executed, field_name="tool_calls_executed")
        if self.model_completions > self.model_requests:
            raise ValueError("model_completions must not exceed model_requests")
        if self.tool_calls_executed > self.tool_calls_requested:
            raise ValueError("tool_calls_executed must not exceed tool_calls_requested")
        if self.tool_rounds > self.tool_calls_requested:
            raise ValueError("tool_rounds must not exceed tool_calls_requested")
        _require_optional_non_negative_tokens(self.input_tokens, field_name="input_tokens")
        _require_optional_non_negative_tokens(self.output_tokens, field_name="output_tokens")

        counts = tuple(self.per_tool_counts)
        if not all(isinstance(count, ToolCallCount) for count in counts):
            raise ValueError("per_tool_counts must contain ToolCallCount values")
        names = tuple(count.tool_name for count in counts)
        if len(set(names)) != len(names):
            raise ValueError("per_tool_counts must not contain duplicate tool names")
        if sum(count.count for count in counts) != self.tool_calls_requested:
            raise ValueError("per_tool_counts must sum to tool_calls_requested")
        object.__setattr__(
            self, "per_tool_counts", tuple(sorted(counts, key=lambda count: count.tool_name))
        )

    def count_for_tool(self, tool_name: str) -> int:
        _require_tool_name(tool_name, field_name="tool_name")
        for count in self.per_tool_counts:
            if count.tool_name == tool_name:
                return count.count
        return 0


def _require_optional_non_negative_tokens(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_non_negative_int(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """A safe, bounded in-memory view of the current execution."""

    status: AgentExecutionStatus
    counters: ExecutionCounters
    elapsed_seconds: float
    recent_interactions: tuple[ToolInteractionFingerprint, ...]
    consecutive_error_count: int
    consecutive_no_progress_rounds: int
    workspace_progress_count: int
    plan_fingerprint: str | None = None
    termination_reason: SupervisorReasonCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AgentExecutionStatus):
            raise ValueError("execution status must be canonical")
        if not isinstance(self.counters, ExecutionCounters):
            raise ValueError("execution counters must be canonical")
        if (
            not isinstance(self.elapsed_seconds, int | float)
            or isinstance(self.elapsed_seconds, bool)
            or not math.isfinite(float(self.elapsed_seconds))
            or self.elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be a finite non-negative number")
        object.__setattr__(self, "recent_interactions", tuple(self.recent_interactions))
        if not all(
            isinstance(interaction, ToolInteractionFingerprint)
            for interaction in self.recent_interactions
        ):
            raise ValueError("recent_interactions must contain ToolInteractionFingerprint values")
        _require_non_negative_int(
            self.consecutive_error_count,
            field_name="consecutive_error_count",
        )
        _require_non_negative_int(
            self.consecutive_no_progress_rounds,
            field_name="consecutive_no_progress_rounds",
        )
        _require_non_negative_int(
            self.workspace_progress_count,
            field_name="workspace_progress_count",
        )
        if self.plan_fingerprint is not None:
            _require_digest(self.plan_fingerprint, field_name="plan_fingerprint")
        if self.termination_reason is not None and not isinstance(
            self.termination_reason,
            SupervisorReasonCode,
        ):
            raise ValueError("termination_reason must be canonical")


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    """One typed outcome from a deterministic supervision evaluation."""

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
    "ExecutionBudget",
    "ExecutionCounters",
    "ExecutionSnapshot",
    "ProgressKind",
    "SessionExecutionRecord",
    "SupervisionThresholds",
    "SupervisorDecision",
    "SupervisorDecisionKind",
    "SupervisorReasonCode",
    "ToolCallBudget",
    "ToolCallCount",
    "ToolInteractionFingerprint",
    "TurnCancellationPolicy",
    "TurnSource",
]
