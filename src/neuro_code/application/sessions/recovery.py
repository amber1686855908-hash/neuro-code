"""Explicit crash-recovery operations for persisted session turns.

The service only inspects durable attempt facts and performs explicit abandon.
It never infers abandonment from a restart and never retries an indeterminate
attempt.

为持久化会话回合提供显式崩溃恢复操作。

该服务只读取持久化回合事实并执行明确的 abandon,不会把重启推断为放弃,也不会
重试 INDETERMINATE 回合。
"""

from __future__ import annotations

from dataclasses import dataclass

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.execution import (
    TurnInput,
    TurnRecoveryAttempt,
    TurnRecoveryResolution,
    TurnRecoveryStatus,
)
from neuro_code.shared.errors import ConfigurationError

MAX_ABANDON_REASON_BYTES = 512


@dataclass(frozen=True, slots=True)
class TurnRecoveryInspection:
    """Bounded application projection for one persisted attempt."""

    attempt: TurnRecoveryAttempt

    @property
    def status(self) -> TurnRecoveryStatus:
        return self.attempt.status

    def to_dict(self) -> dict[str, object]:
        return self.attempt.safe_projection()


@dataclass(frozen=True, slots=True)
class TurnInputForRetry:
    """Small typed handoff from recovery inspection to a runner."""

    input: TurnInput


class TurnRecoveryService:
    """Read and explicitly resolve interrupted turn attempts."""

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def inspect(self, session_id: str) -> tuple[TurnRecoveryInspection, ...]:
        attempts = await self._store.load_turn_attempts(session_id)
        # Normal ProviderError/ToolError/cancellation terminalizations are
        # execution history, not orphaned crash attempts.  Keep committed and
        # explicitly abandoned records visible for audit, while excluding
        # ordinary failed/cancelled turns from recovery UX.
        return tuple(
            TurnRecoveryInspection(attempt)
            for attempt in attempts
            if attempt.resolution
            in {
                None,
                TurnRecoveryResolution.COMMITTED,
                TurnRecoveryResolution.ABANDONED,
            }
        )

    async def inspect_open(self, session_id: str) -> tuple[TurnRecoveryInspection, ...]:
        attempts = await self._store.load_open_turn_attempts(session_id)
        return tuple(TurnRecoveryInspection(attempt) for attempt in attempts)

    async def abandon(
        self,
        session_id: str,
        turn_id: str,
        *,
        reason: str = "explicit_user_resolution",
    ) -> TurnRecoveryInspection:
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or "\x00" in reason
            or len(reason.encode("utf-8")) > MAX_ABANDON_REASON_BYTES
        ):
            raise ConfigurationError("recovery abandon reason is invalid")
        attempts = await self._store.load_open_turn_attempts(session_id)
        try:
            attempt = next(item for item in attempts if item.turn_id == turn_id)
        except StopIteration:
            raise ConfigurationError(f"open turn attempt does not exist: {turn_id}") from None
        sequence = await self._store.next_event_sequence(session_id)
        event = AgentEvent.create(
            sequence,
            AgentEventKind.TURN_ABANDONED,
            {
                "turn_id": turn_id,
                "reason": reason,
                "previous_status": attempt.status.value,
            },
        )
        await self._store.abandon_turn_attempt(session_id, turn_id, event, reason)
        resolved = [
            item
            for item in await self._store.load_turn_attempts(session_id)
            if item.turn_id == turn_id
        ]
        if not resolved:
            raise ConfigurationError(f"turn attempt disappeared after abandon: {turn_id}")
        return TurnRecoveryInspection(resolved[0])

    async def require_safe_retry(self, session_id: str, turn_id: str) -> TurnInputForRetry:
        attempts = await self._store.load_open_turn_attempts(session_id)
        try:
            attempt = next(item for item in attempts if item.turn_id == turn_id)
        except StopIteration:
            raise ConfigurationError(f"open turn attempt does not exist: {turn_id}") from None
        if attempt.status is not TurnRecoveryStatus.SAFELY_RETRYABLE:
            raise ConfigurationError(
                f"turn attempt {turn_id} is {attempt.status.value}; explicit abandon is required"
            )
        if attempt.input is None:
            raise ConfigurationError("the exact original turn input is unavailable")
        return TurnInputForRetry(attempt.input)


__all__ = [
    "MAX_ABANDON_REASON_BYTES",
    "TurnInputForRetry",
    "TurnRecoveryInspection",
    "TurnRecoveryService",
]
