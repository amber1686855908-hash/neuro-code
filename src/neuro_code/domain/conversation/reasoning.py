"""Conversation reasoning-depth values and provider-neutral guidance.

定义会话推理深度值及与 Provider 无关的指引."""

from __future__ import annotations

from enum import StrEnum


class ReasoningEffort(StrEnum):
    """Provider-neutral review depth requested from the agent runtime.

    表示 Agent 运行时请求的、与 Provider 无关的审查深度."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRACODE = "ultracode"

    @property
    def glyph(self) -> str:
        return {
            ReasoningEffort.LOW: "○",
            ReasoningEffort.MEDIUM: "◐",
            ReasoningEffort.HIGH: "●",
            ReasoningEffort.XHIGH: "⬤",
            ReasoningEffort.MAX: "◆",
            ReasoningEffort.ULTRACODE: "⚡",
        }[self]

    @property
    def effective(self) -> ReasoningEffort:
        """Return the implemented policy depth for this requested level.

        返回所请求等级实际实现的策略深度."""

        if self is ReasoningEffort.ULTRACODE:
            return ReasoningEffort.MAX
        return self

    @property
    def requires_workflow_orchestration(self) -> bool:
        return self is ReasoningEffort.ULTRACODE


_GUIDANCE = {
    ReasoningEffort.LOW: (
        "Use low review depth: answer directly, keep repository exploration and "
        "verification to the minimum needed for correctness, and prefer the smallest "
        "safe change. Do not skip mandatory safety checks."
    ),
    ReasoningEffort.MEDIUM: (
        "Use medium review depth: inspect the relevant code path, perform routine "
        "self-review, and run focused verification appropriate for everyday development."
    ),
    ReasoningEffort.HIGH: (
        "Use high review depth: investigate non-trivial dependencies, verify the change "
        "carefully, and proactively check likely regressions before concluding."
    ),
    ReasoningEffort.XHIGH: (
        "Use extra-high review depth: explore difficult edge cases, challenge initial "
        "assumptions, perform multiple validation passes, and reconcile conflicting "
        "evidence before concluding."
    ),
    ReasoningEffort.MAX: (
        "Use maximum ordinary single-agent review depth: trace all relevant dependencies, "
        "inspect adversarial edge cases, challenge every plausible explanation, and run "
        "broad but bounded verification. Exhaust the relevant single-agent investigation "
        "before concluding; do not start child agents or claim workflow orchestration."
    ),
}


def reasoning_guidance(effort: ReasoningEffort) -> str:
    """Build an application-owned instruction without claiming native API support.

    构建由应用层拥有的指令,不声称 Provider 原生支持该能力."""

    effective = effort.effective
    guidance = _GUIDANCE[effective]
    if effort is ReasoningEffort.ULTRACODE:
        return (
            f"{guidance} Ultracode workflow orchestration is not available in this "
            "runtime yet; use the maximum ordinary single-agent policy only and do not claim that sub-agent "
            "workflows were started."
        )
    return guidance


__all__ = ["ReasoningEffort", "reasoning_guidance"]
