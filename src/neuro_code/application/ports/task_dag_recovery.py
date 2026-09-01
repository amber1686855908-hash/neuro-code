"""Durable cross-process ownership port for safe DAG worker recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.domain.task_dag_recovery import TaskDagRecoveryClaim


class TaskDagRecoveryClaimError(Exception):
    """Bounded failure at the DAG recovery ownership boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


@dataclass(frozen=True, slots=True)
class TaskDagRecoveryClaimResult:
    claim: TaskDagRecoveryClaim
    acquired: bool


class TaskDagRecoveryClaimStore(Protocol):
    """Insert once, inspect read-only, and take over by exact CAS."""

    async def initialize(self) -> None: ...

    async def get_task_dag_recovery_claim(
        self,
        dag_id: str,
        node_id: str,
        node_generation: int,
        /,
    ) -> TaskDagRecoveryClaim | None: ...

    async def insert_task_dag_recovery_claim(
        self,
        claim: TaskDagRecoveryClaim,
        /,
    ) -> TaskDagRecoveryClaimResult: ...

    async def compare_and_takeover_task_dag_recovery_claim(
        self,
        claim: TaskDagRecoveryClaim,
        *,
        expected_version: int,
        expected_owner_pid: int,
        expected_owner_token: str,
    ) -> TaskDagRecoveryClaim: ...


__all__ = [
    "TaskDagRecoveryClaimError",
    "TaskDagRecoveryClaimResult",
    "TaskDagRecoveryClaimStore",
]
