"""Durable port for bounded model-generated DAG planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from neuro_code.domain.model_planning import (
    PlanningAttempt,
    PlanningAttemptState,
    PlanningProposalRecord,
)


class ModelPlanningStoreError(Exception):
    """Fail-closed error at the durable planning boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


@dataclass(frozen=True, slots=True)
class PlanningAttemptClaim:
    """Result of one atomic exact planning-identity claim."""

    attempt: PlanningAttempt
    acquired: bool


class ModelPlanningStore(Protocol):
    async def get_model_planning_attempt(self, planning_id: str) -> PlanningAttempt | None: ...

    async def claim_model_planning_attempt(
        self,
        attempt: PlanningAttempt,
        *,
        now: datetime,
    ) -> PlanningAttemptClaim: ...

    async def fence_model_planning_attempt(
        self,
        planning_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        updated_at: datetime,
    ) -> PlanningAttempt: ...

    async def mark_model_planning_model_committed(
        self,
        planning_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        model_response: str,
        updated_at: datetime,
    ) -> PlanningAttempt: ...

    async def publish_model_planning_proposal(
        self,
        proposal: PlanningProposalRecord,
        *,
        owner_id: str,
    ) -> PlanningProposalRecord: ...

    async def get_model_planning_proposal(
        self,
        planning_id: str,
    ) -> PlanningProposalRecord | None: ...

    async def mark_model_planning_dag_published(
        self,
        planning_id: str,
        *,
        owner_id: str,
        dag_id: str,
        proposal_fingerprint: str,
        updated_at: datetime,
    ) -> PlanningAttempt: ...

    async def transition_model_planning_attempt(
        self,
        planning_id: str,
        *,
        expected_state: PlanningAttemptState,
        state: PlanningAttemptState,
        owner_id: str | None = None,
        updated_at: datetime,
    ) -> PlanningAttempt: ...


__all__ = ["ModelPlanningStore", "ModelPlanningStoreError", "PlanningAttemptClaim"]
