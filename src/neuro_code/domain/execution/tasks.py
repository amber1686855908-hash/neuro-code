"""Budgets, counters and interaction fingerprints for one execution turn.

定义一次执行回合的预算、计数器和交互指纹."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from neuro_code.domain.execution._validation import (
    require_digest,
    require_non_negative_int,
    require_optional_non_negative_tokens,
    require_optional_positive_int,
    require_optional_positive_seconds,
    require_positive_int,
    require_tool_name,
)
from neuro_code.domain.execution.outcomes import ProgressKind


class TurnCancellationPolicy(StrEnum):
    """Controls whether an unstarted turn may be removed after cancellation.

    控制取消后尚未启动的回合是否可以移除."""

    RETAIN = "retain"
    REWIND_PRISTINE = "rewind_pristine"


class TurnSource(StrEnum):
    """Identifies whether a model turn came from a user or a background wake.

    标识模型回合来自用户还是后台唤醒."""

    USER = "user"
    BACKGROUND_TASK_AUTO_WAKE = "background_task_auto_wake"


@dataclass(frozen=True, slots=True)
class ToolCallBudget:
    """A stricter call limit for one named tool.

    表示某个指定工具使用的更严格调用上限."""

    tool_name: str
    max_calls: int

    def __post_init__(self) -> None:
        require_tool_name(self.tool_name, field_name="tool call budget tool_name")
        require_positive_int(self.max_calls, field_name="tool call budget max_calls")


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """Independent bounded resources for one agent turn.

    定义一次 Agent 回合使用的彼此独立的有界资源."""

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
        require_positive_int(self.max_model_calls, field_name="max_model_calls")
        require_positive_int(self.max_tool_rounds, field_name="max_tool_rounds")
        require_positive_int(self.max_tool_calls, field_name="max_tool_calls")
        require_positive_int(self.max_calls_per_tool, field_name="max_calls_per_tool")
        require_optional_positive_seconds(self.max_wall_seconds, field_name="max_wall_seconds")
        require_optional_positive_int(self.max_input_tokens, field_name="max_input_tokens")
        require_optional_positive_int(self.max_output_tokens, field_name="max_output_tokens")
        require_optional_positive_int(self.max_total_tokens, field_name="max_total_tokens")
        require_positive_int(self.finalizer_model_calls, field_name="finalizer_model_calls")
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
        """Return the effective call limit for one tool.

        返回某个工具的有效调用上限."""

        require_tool_name(tool_name, field_name="tool_name")
        for limit in self.per_tool_limits:
            if limit.tool_name == tool_name:
                return limit.max_calls
        return self.max_calls_per_tool


@dataclass(frozen=True, slots=True)
class SupervisionThresholds:
    """Bounded detector thresholds for one supervised turn.

    定义一次受监督回合使用的有界检测器阈值."""

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
        require_positive_int(
            self.repeating_action_observation,
            field_name="repeating_action_observation",
        )
        require_positive_int(
            self.repeating_action_observation_stuck,
            field_name="repeating_action_observation_stuck",
        )
        require_positive_int(self.repeating_action_error, field_name="repeating_action_error")
        require_positive_int(
            self.repeating_action_error_stuck,
            field_name="repeating_action_error_stuck",
        )
        require_positive_int(
            self.alternating_cycle_repetitions,
            field_name="alternating_cycle_repetitions",
        )
        require_positive_int(self.max_cycle_period, field_name="max_cycle_period")
        require_positive_int(
            self.no_progress_replan_rounds,
            field_name="no_progress_replan_rounds",
        )
        require_positive_int(
            self.no_progress_stuck_rounds,
            field_name="no_progress_stuck_rounds",
        )
        require_positive_int(
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
    """A redacted, stable tool action/result identity used for detection.

    表示用于检测的脱敏且稳定的工具动作/结果身份."""

    tool_name: str
    action_digest: str
    observation_digest: str
    is_error: bool
    progress_kind: ProgressKind

    def __post_init__(self) -> None:
        require_tool_name(self.tool_name, field_name="fingerprint tool_name")
        require_digest(self.action_digest, field_name="action_digest")
        require_digest(self.observation_digest, field_name="observation_digest")
        if not isinstance(self.is_error, bool):
            raise ValueError("is_error must be a bool")
        if not isinstance(self.progress_kind, ProgressKind):
            raise ValueError("progress_kind must be canonical")

    @property
    def behavior_signature(self) -> tuple[str, str, bool]:
        """Return the bounded comparison key used by repetition detectors.

        返回重复检测器使用的有界比较键."""

        return (self.action_digest, self.observation_digest, self.is_error)


@dataclass(frozen=True, slots=True)
class ToolCallCount:
    """One immutable, comparable per-tool reservation count.

    表示每个工具一个不可变且可比较的预留计数."""

    tool_name: str
    count: int

    def __post_init__(self) -> None:
        require_tool_name(self.tool_name, field_name="tool call count tool_name")
        require_non_negative_int(self.count, field_name="tool call count")


@dataclass(frozen=True, slots=True)
class ExecutionCounters:
    """Monotonic counts accumulated within one supervised turn.

    表示一次受监督回合中累积且单调增加的计数."""

    model_requests: int = 0
    model_completions: int = 0
    tool_rounds: int = 0
    tool_calls_requested: int = 0
    tool_calls_executed: int = 0
    per_tool_counts: tuple[ToolCallCount, ...] = ()
    input_tokens: int | None = 0
    output_tokens: int | None = 0

    def __post_init__(self) -> None:
        require_non_negative_int(self.model_requests, field_name="model_requests")
        require_non_negative_int(self.model_completions, field_name="model_completions")
        require_non_negative_int(self.tool_rounds, field_name="tool_rounds")
        require_non_negative_int(self.tool_calls_requested, field_name="tool_calls_requested")
        require_non_negative_int(self.tool_calls_executed, field_name="tool_calls_executed")
        if self.model_completions > self.model_requests:
            raise ValueError("model_completions must not exceed model_requests")
        if self.tool_calls_executed > self.tool_calls_requested:
            raise ValueError("tool_calls_executed must not exceed tool_calls_requested")
        if self.tool_rounds > self.tool_calls_requested:
            raise ValueError("tool_rounds must not exceed tool_calls_requested")
        require_optional_non_negative_tokens(self.input_tokens, field_name="input_tokens")
        require_optional_non_negative_tokens(self.output_tokens, field_name="output_tokens")

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
        require_tool_name(tool_name, field_name="tool_name")
        for count in self.per_tool_counts:
            if count.tool_name == tool_name:
                return count.count
        return 0


__all__ = [
    "ExecutionBudget",
    "ExecutionCounters",
    "SupervisionThresholds",
    "ToolCallBudget",
    "ToolCallCount",
    "ToolInteractionFingerprint",
    "TurnCancellationPolicy",
    "TurnSource",
]
