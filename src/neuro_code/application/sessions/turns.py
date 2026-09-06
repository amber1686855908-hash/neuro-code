"""Typed application boundary for running one session turn.

为运行单个会话回合提供类型化应用边界.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from neuro_code.domain.conversation.messages import ContentPart
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    TurnCancellationPolicy,
    TurnSource,
    VerificationRequirementsSnapshot,
)
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.application.runtime.agent import AgentRunResult, EventSink


@dataclass(frozen=True, slots=True)
class RunTurnRequest:
    """Validated input for one existing conversation turn.

    The request contains only turn intent. The runner remains responsible for
    its own lock, persisted context, event delivery, and cancellation recovery;
    this DTO does not carry messages or a tool registry.

    现有会话单个回合的经过验证输入.
    请求只包含回合意图;锁、持久化上下文、事件发送和取消恢复仍由运行器负责;
    此 DTO 不携带消息或工具注册表.
    """

    prompt: str
    content_parts: tuple[ContentPart, ...] = ()
    cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN
    turn_source: TurnSource = TurnSource.USER
    expected_session_id: str | None = None
    turn_id: str | None = None
    verification_requirements: VerificationRequirementsSnapshot | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str):
            raise ValueError("prompt must be a string")
        parts = tuple(self.content_parts)
        if not all(isinstance(part, ContentPart) for part in parts):
            raise ValueError("content_parts must contain canonical ContentPart values")
        object.__setattr__(self, "content_parts", parts)
        if not isinstance(self.cancellation_policy, TurnCancellationPolicy):
            raise ValueError("cancellation_policy must be canonical")
        if not isinstance(self.turn_source, TurnSource):
            raise ValueError("turn_source must be canonical")
        if self.expected_session_id is not None and (
            not isinstance(self.expected_session_id, str) or not self.expected_session_id.strip()
        ):
            raise ValueError("expected_session_id must be non-empty when provided")
        if self.turn_id is not None and (
            not isinstance(self.turn_id, str) or not self.turn_id.strip()
        ):
            raise ValueError("turn_id must be non-empty when provided")
        if self.verification_requirements is not None and not isinstance(
            self.verification_requirements,
            VerificationRequirementsSnapshot,
        ):
            raise ValueError(
                "verification_requirements must be a VerificationRequirementsSnapshot or None"
            )


class SessionTurnRunner(Protocol):
    """Minimal runner contract consumed by the application turn seam.

    应用层回合接缝所消费的最小运行器契约.
    """

    @property
    def session_id(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> ReasoningEffort: ...

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: Sequence[ContentPart] = (),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
        turn_id: str | None = None,
        ultracode_execution_id: str | None = None,
        verification_requirements: VerificationRequirementsSnapshot | None = None,
    ) -> AgentRunResult: ...


type UltracodeDelegate = Callable[
    ["RunTurnRequest", "EventSink | None"], "Awaitable[AgentRunResult]"
]


class SessionTurnService:
    """Bind the turn contract to an existing conversation runner.

    将回合契约绑定到现有会话运行器.
    """

    __slots__ = ("_runner", "_ultracode_delegate")

    def __init__(
        self,
        runner: SessionTurnRunner,
        *,
        ultracode_delegate: UltracodeDelegate | None = None,
    ) -> None:
        self._runner = runner
        self._ultracode_delegate = ultracode_delegate

    @property
    def session_id(self) -> str | None:
        return self._runner.session_id

    async def run_turn(
        self,
        request: RunTurnRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Delegate one turn without taking ownership of runner lifecycle.

        委托一个回合,但不接管运行器生命周期.
        """

        if not isinstance(request, RunTurnRequest):
            raise ValueError("run turn request must be canonical")
        if (
            request.expected_session_id is not None
            and self._runner.session_id != request.expected_session_id
        ):
            raise ValueError("conversation runner is bound to a different session")
        effort = getattr(self._runner, "reasoning_effort", None)
        if request.turn_source is TurnSource.USER and effort is ReasoningEffort.ULTRACODE:
            if request.verification_requirements is not None:
                raise ConfigurationError(
                    "Ultracode delegation does not support structured verification requirements"
                )
            if self._ultracode_delegate is None:
                raise ConfigurationError("Ultracode delegation entry is not configured")
            return await self._ultracode_delegate(request, sink)
        kwargs: dict[str, Any] = {
            "sink": sink,
            "content_parts": request.content_parts,
            "cancellation_policy": request.cancellation_policy,
            "turn_source": request.turn_source,
        }
        if request.turn_id is not None:
            kwargs["turn_id"] = request.turn_id
        if request.verification_requirements is not None:
            kwargs["verification_requirements"] = request.verification_requirements
        return await self._runner.run(
            request.prompt,
            **kwargs,
        )


__all__ = ["RunTurnRequest", "SessionTurnRunner", "SessionTurnService", "UltracodeDelegate"]
