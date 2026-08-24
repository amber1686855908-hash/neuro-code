"""Durable persistence contract for the bounded Leader controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from neuro_code.domain.leader import (
    LeaderAttempt,
    LeaderAttemptState,
    LeaderDecision,
    LeaderDecisionRecord,
)


class LeaderStoreError(Exception):
    """Bounded fail-closed error at the durable Leader boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


@dataclass(frozen=True, slots=True)
class LeaderAttemptClaim:
    """Result of an atomic exact-snapshot Leader ownership claim."""

    attempt: LeaderAttempt
    acquired: bool


class LeaderStore(Protocol):
    """Insert-only decisions and CAS-like lifecycle for Leader attempts."""

    async def claim_leader_attempt(
        self,
        attempt: LeaderAttempt,
        *,
        now: datetime,
    ) -> LeaderAttemptClaim: ...

    async def get_leader_attempt_for_snapshot(
        self,
        dag_id: str,
        *,
        dag_generation: int,
        definition_fingerprint: str,
        evidence_fingerprint: str,
        objective_fingerprint: str,
    ) -> LeaderAttempt | None: ...

    async def mark_leader_model_committed(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        turn_id: str,
        model_response: str,
        updated_at: datetime,
    ) -> LeaderAttempt: ...

    async def publish_leader_decision(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        decision_id: str,
        decision: LeaderDecision,
        created_at: datetime,
    ) -> LeaderDecisionRecord: ...

    async def transition_leader_attempt(
        self,
        attempt_id: str,
        *,
        expected_state: LeaderAttemptState,
        state: LeaderAttemptState,
        owner_id: str | None = None,
        updated_at: datetime,
    ) -> LeaderAttempt: ...

    async def get_leader_attempt(self, attempt_id: str) -> LeaderAttempt | None: ...

    async def get_leader_decision(self, decision_id: str) -> LeaderDecisionRecord | None: ...

    async def list_leader_decisions(self, dag_id: str) -> tuple[LeaderDecisionRecord, ...]: ...


__all__ = ["LeaderAttemptClaim", "LeaderStore", "LeaderStoreError"]
