"""Canonical execution domain package.

The package keeps ``neuro_code.domain.execution`` as the public compatibility
entry point while assigning each value-object family to a focused module.

定义规范的执行领域包. 公共兼容入口仍为 `neuro_code.domain.execution`,各值对象分布在专用模块中.
"""

from neuro_code.domain.execution.checkpoints import ExecutionSnapshot, SessionExecutionRecord
from neuro_code.domain.execution.outcomes import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    ProgressKind,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorReasonCode,
)
from neuro_code.domain.execution.tasks import (
    ExecutionBudget,
    ExecutionCounters,
    SupervisionThresholds,
    ToolCallBudget,
    ToolCallCount,
    ToolInteractionFingerprint,
    TurnCancellationPolicy,
    TurnSource,
)

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
