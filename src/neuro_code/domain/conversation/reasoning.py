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
        """Return the provider-compatible ordinary policy projection.

        ``ULTRACODE`` is an application strategy, not a provider wire value.
        Keeping this compatibility projection preserves the existing public
        selection surface while the orchestration layer makes the actual
        ``MAIN_MAX`` versus ``BOUNDED_SWARM`` decision.

        返回与 Provider 兼容的普通策略投影.``ULTRACODE`` 是应用策略而不是
        Provider wire 值;保留这个兼容投影以维持现有选择界面,同时由编排层
        负责真正决定 ``MAIN_MAX`` 或 ``BOUNDED_SWARM``。"""

        if self is ReasoningEffort.ULTRACODE:
            return ReasoningEffort.MAX
        return self

    @property
    def provider_effort(self) -> ReasoningEffort:
        """Return the only effort value allowed to reach provider adapters."""

        return self.effective

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

    if not isinstance(effort, ReasoningEffort):
        raise TypeError("effort must be a ReasoningEffort")
    effective = effort.provider_effort
    guidance = _GUIDANCE[effective]
    if effort is ReasoningEffort.ULTRACODE:
        return (
            f"{guidance} Ultracode is an application-level delegation strategy. "
            "The application will select exactly one bounded path: ordinary maximum "
            "single-agent execution or the existing bounded Agent Swarm. Never claim "
            "that a sub-agent workflow ran unless the application reports that path."
        )
    return guidance


__all__ = ["ReasoningEffort", "reasoning_guidance"]
