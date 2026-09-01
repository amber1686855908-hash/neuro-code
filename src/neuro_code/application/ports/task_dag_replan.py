"""Durable application port for bounded Task DAG revision / replan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from neuro_code.domain.task_dag_replan import (
    DagReplanAttempt,
    DagReplanAttemptState,
    DagReplanProposalRecord,
)


class TaskDagReplanStoreError(Exception):
    """Fail-closed error at the durable replan boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


@dataclass(frozen=True, slots=True)
class DagReplanAttemptClaim:
    """Result of one atomic exact replan-identity claim."""

    attempt: DagReplanAttempt
    acquired: bool


class TaskDagReplanStore(Protocol):
    async def get_task_dag_replan_attempt(self, revision_id: str) -> DagReplanAttempt | None: ...

    async def get_task_dag_replan_source_depth(self, source_dag_id: str) -> int: ...

    async def claim_task_dag_replan_attempt(
        self,
        attempt: DagReplanAttempt,
        *,
        now: datetime,
    ) -> DagReplanAttemptClaim: ...

    async def fence_task_dag_replan_attempt(
        self,
        revision_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        source_dag_id: str,
        source_definition_fingerprint: str,
        source_generation: int,
        source_state: str,
        evidence_fingerprint: str,
        updated_at: datetime,
    ) -> DagReplanAttempt: ...

    async def mark_task_dag_replan_model_committed(
        self,
        revision_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        model_response: str,
        updated_at: datetime,
    ) -> DagReplanAttempt: ...

    async def publish_task_dag_replan_proposal(
        self,
        proposal: DagReplanProposalRecord,
    ) -> DagReplanProposalRecord: ...

    async def get_task_dag_replan_proposal(
        self,
        revision_id: str,
    ) -> DagReplanProposalRecord | None: ...

    async def mark_task_dag_replan_successor_published(
        self,
        revision_id: str,
        *,
        successor_dag_id: str,
        proposal_fingerprint: str,
        updated_at: datetime,
    ) -> DagReplanAttempt: ...

    async def transition_task_dag_replan_attempt(
        self,
        revision_id: str,
        *,
        expected_state: DagReplanAttemptState,
        state: DagReplanAttemptState,
        updated_at: datetime,
    ) -> DagReplanAttempt: ...


__all__ = [
    "DagReplanAttemptClaim",
    "TaskDagReplanStore",
    "TaskDagReplanStoreError",
]
