from __future__ import annotations

from enum import StrEnum


class ReasoningEffort(StrEnum):
    """Provider-neutral review depth requested from the agent runtime."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    ULTRACODE = "ultracode"

    @property
    def glyph(self) -> str:
        return {
            ReasoningEffort.LOW: "○",
            ReasoningEffort.MEDIUM: "◐",
            ReasoningEffort.HIGH: "●",
            ReasoningEffort.XHIGH: "⬤",
            ReasoningEffort.ULTRACODE: "⚡",
        }[self]

    @property
    def effective(self) -> ReasoningEffort:
        """Return the implemented policy depth for this requested level."""

        if self is ReasoningEffort.ULTRACODE:
            return ReasoningEffort.XHIGH
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
}


def reasoning_guidance(effort: ReasoningEffort) -> str:
    """Build an application-owned instruction without claiming native API support."""

    effective = effort.effective
    guidance = _GUIDANCE[effective]
    if effort is ReasoningEffort.ULTRACODE:
        return (
            f"{guidance} Ultracode workflow orchestration is not available in this "
            "runtime yet; use the extra-high policy only and do not claim that sub-agent "
            "workflows were started."
        )
    return guidance


__all__ = ["ReasoningEffort", "reasoning_guidance"]
