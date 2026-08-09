"""Safe snapshots and durable execution projections.

定义安全快照和持久化执行投影."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from neuro_code.domain.execution._validation import (
    require_digest,
    require_non_negative_int,
    require_positive_int,
)
from neuro_code.domain.execution.outcomes import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    ProgressKind,
    SupervisorReasonCode,
)
from neuro_code.domain.execution.tasks import ExecutionCounters, ToolInteractionFingerprint


@dataclass(frozen=True, slots=True)
class SessionExecutionRecord:
    """The last durable terminal execution result for one session.

    This intentionally retains only stable outcome metadata and the matching
    ``TURN_COMPLETED`` event identity. It is safe to load during resume without
    retaining prompts, tool arguments, output, evidence, or supervisor state.

    表示一个会话最后一次持久化的终态执行结果.
    """

    outcome: AgentExecutionOutcome
    event_sequence: int
    completed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AgentExecutionOutcome):
            raise ValueError("session execution record outcome must be canonical")
        require_positive_int(self.event_sequence, field_name="event_sequence")
        if not isinstance(self.completed_at, datetime) or self.completed_at.tzinfo is None:
            raise ValueError("session execution record timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """A safe, bounded in-memory view of the current execution.

    表示当前执行的安全且有界的内存视图."""

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
        require_non_negative_int(
            self.consecutive_error_count,
            field_name="consecutive_error_count",
        )
        require_non_negative_int(
            self.consecutive_no_progress_rounds,
            field_name="consecutive_no_progress_rounds",
        )
        require_non_negative_int(
            self.workspace_progress_count,
            field_name="workspace_progress_count",
        )
        if self.plan_fingerprint is not None:
            require_digest(self.plan_fingerprint, field_name="plan_fingerprint")
        if self.termination_reason is not None and not isinstance(
            self.termination_reason,
            SupervisorReasonCode,
        ):
            raise ValueError("termination_reason must be canonical")


@dataclass(frozen=True, slots=True)
class ExecutionSegmentCheckpoint:
    """A bounded in-turn continuation checkpoint without conversation payloads.

    This checkpoint records why a long-running turn may continue into another
    bounded segment. It is auditable but is not a workspace rollback point or a
    promise of process-crash recovery.

    表示不含会话载荷的有界回合内续段检查点. 它可供审计,但不是工作区回滚点,
    也不承诺进程崩溃后的恢复.
    """

    segment_number: int
    model_calls: int
    tool_rounds: int
    tool_calls: int
    progress_kinds: tuple[ProgressKind, ...]
    plan_steps_total: int
    plan_steps_completed: int
    created_at: datetime

    def __post_init__(self) -> None:
        require_positive_int(self.segment_number, field_name="segment_number")
        require_non_negative_int(self.model_calls, field_name="model_calls")
        require_non_negative_int(self.tool_rounds, field_name="tool_rounds")
        require_non_negative_int(self.tool_calls, field_name="tool_calls")
        kinds = tuple(self.progress_kinds)
        if not all(isinstance(kind, ProgressKind) for kind in kinds):
            raise TypeError("progress_kinds must contain ProgressKind values")
        if ProgressKind.NONE in kinds:
            raise ValueError("progress_kinds must not contain NONE")
        if len(set(kinds)) != len(kinds):
            raise ValueError("progress_kinds must be unique")
        object.__setattr__(
            self, "progress_kinds", tuple(sorted(kinds, key=lambda kind: kind.value))
        )
        require_non_negative_int(self.plan_steps_total, field_name="plan_steps_total")
        require_non_negative_int(self.plan_steps_completed, field_name="plan_steps_completed")
        if self.plan_steps_completed > self.plan_steps_total:
            raise ValueError("plan_steps_completed must not exceed plan_steps_total")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")

    def to_event_data(self) -> dict[str, object]:
        """Return a stable event projection with no raw evidence.

        返回不含原始证据的稳定事件投影。
        """

        return {
            "segment": self.segment_number,
            "next_segment": self.segment_number + 1,
            "model_calls": self.model_calls,
            "tool_rounds": self.tool_rounds,
            "tool_calls": self.tool_calls,
            "progress_kinds": [kind.value for kind in self.progress_kinds],
            "plan_steps_total": self.plan_steps_total,
            "plan_steps_completed": self.plan_steps_completed,
        }


__all__ = ["ExecutionSegmentCheckpoint", "ExecutionSnapshot", "SessionExecutionRecord"]
