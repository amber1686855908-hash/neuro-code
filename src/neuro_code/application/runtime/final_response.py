"""Canonical provisional and committed final-response contract.

This module describes the boundary between a response that a runtime may still
replace and a response that a completed turn is allowed to expose or persist.
It deliberately projects verification truth from ``VerificationReport`` rather
than owning verification state.  A later runtime gate may create a provisional
value while a committed value is the only value accepted by ``AgentRunResult``
and turn completion persistence.

定义临时最终响应与已提交最终响应之间的规范契约。

本模块只描述运行时仍可替换的响应与已完成回合可公开或持久化的响应之间的边界。
它从 ``VerificationReport`` 投影验证事实,而不拥有验证状态。后续运行时 gate 可以
创建 provisional 值,但只有 committed 值能被 ``AgentRunResult`` 和回合完成持久化接受。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from neuro_code.application.runtime.verification import VerificationReport, VerificationState


class ResponseCommitState(StrEnum):
    """Whether a terminal response is still replaceable or durably committed."""

    PROVISIONAL = "provisional"
    COMMITTED = "committed"


class ResponseSource(StrEnum):
    """The runtime owner that produced a terminal response value."""

    NORMAL_MODEL = "normal_model"
    EVIDENCE_AWARE_FINALIZER = "evidence_aware_finalizer"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    EXTERNAL_RESULT = "external_result"


def _verification_projection(
    verification: VerificationReport | None,
) -> tuple[VerificationState, int]:
    if verification is None:
        return VerificationState.NOT_APPLICABLE, 0
    if not isinstance(verification, VerificationReport):
        raise TypeError("verification must be a VerificationReport or None")
    return verification.state, verification.workspace_generation


@dataclass(frozen=True, slots=True)
class FinalResponseContract:
    """Typed metadata and text for one terminal response candidate.

    ``PROVISIONAL`` values are runtime-only candidates.  They may be replaced
    after verification or recovery decisions and must never be passed to a
    turn-completion persistence call.  ``COMMITTED`` values are the only values
    that represent ``AgentRunResult.response`` or a durable ``TURN_COMPLETED``
    event.

    ``verification_state`` and ``verification_workspace_generation`` are a
    snapshot projection of the existing verification owner.  They do not
    contain evidence and cannot create a second verification state machine.

    一条终态响应候选的有界元数据和文本。

    ``PROVISIONAL`` 只属于运行时临时候选,可以在验证或恢复决策后被替换,绝不能传给
    回合完成持久化。``COMMITTED`` 才能代表 ``AgentRunResult.response`` 或持久化的
    ``TURN_COMPLETED`` 事件。
    """

    response: str
    state: ResponseCommitState
    source: ResponseSource
    verification_state: VerificationState
    verification_workspace_generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.response, str):
            raise TypeError("response must be a string")
        if not isinstance(self.state, ResponseCommitState):
            raise TypeError("response state must be a ResponseCommitState")
        if not isinstance(self.source, ResponseSource):
            raise TypeError("response source must be a ResponseSource")
        if not isinstance(self.verification_state, VerificationState):
            raise TypeError("verification_state must be a VerificationState")
        if (
            not isinstance(self.verification_workspace_generation, int)
            or isinstance(self.verification_workspace_generation, bool)
            or self.verification_workspace_generation < 0
        ):
            raise ValueError("verification_workspace_generation must be non-negative")

    @property
    def is_committed(self) -> bool:
        """Return whether this value may represent a completed turn."""

        return self.state is ResponseCommitState.COMMITTED

    @classmethod
    def provisional(
        cls,
        response: str,
        *,
        source: ResponseSource = ResponseSource.NORMAL_MODEL,
        verification: VerificationReport | None = None,
    ) -> FinalResponseContract:
        """Create a replaceable terminal candidate without any durability claim."""

        state, generation = _verification_projection(verification)
        return cls(response, ResponseCommitState.PROVISIONAL, source, state, generation)

    @classmethod
    def committed(
        cls,
        response: str,
        *,
        source: ResponseSource,
        verification: VerificationReport | None = None,
    ) -> FinalResponseContract:
        """Create the only response form accepted by result/event finalization."""

        state, generation = _verification_projection(verification)
        return cls(response, ResponseCommitState.COMMITTED, source, state, generation)

    def commit(
        self,
        *,
        source: ResponseSource | None = None,
        verification: VerificationReport | None = None,
    ) -> FinalResponseContract:
        """Return a committed snapshot with its final source and verification truth."""

        committed_source = self.source if source is None else source
        if self.is_committed and source is None and verification is None:
            return self
        if verification is None:
            state = self.verification_state
            generation = self.verification_workspace_generation
        else:
            state, generation = _verification_projection(verification)
        return type(self)(
            self.response,
            ResponseCommitState.COMMITTED,
            committed_source,
            state,
            generation,
        )

    def to_completion_metadata(self) -> dict[str, object]:
        """Return the fixed-shape projection allowed on ``TURN_COMPLETED``."""

        return {
            "response_committed": self.is_committed,
            "response_source": self.source.value,
            "verification_state": self.verification_state.value,
            "verification_workspace_generation": self.verification_workspace_generation,
        }


__all__ = [
    "FinalResponseContract",
    "ResponseCommitState",
    "ResponseSource",
]
