"""Durable persistence contract for the bounded task DAG."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from neuro_code.domain.task_dag import (
    TaskDag,
    TaskDagNode,
    TaskDagNodeState,
    TaskDagState,
)


class TaskDagError(Exception):
    """Bounded fail-closed error at the task-DAG persistence boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


class TaskDagStore(Protocol):
    """Insert-only definitions and CAS lifecycle for one task DAG."""

    async def insert_task_dag(self, dag: TaskDag, /) -> TaskDag: ...

    async def get_task_dag(self, dag_id: str, /) -> TaskDag | None: ...

    async def compare_and_transition_task_dag(
        self,
        dag: TaskDag,
        *,
        expected_generation: int,
        expected_state: TaskDagState,
    ) -> TaskDag: ...

    async def compare_and_transition_task_dag_node(
        self,
        dag_id: str,
        node: TaskDagNode,
        *,
        expected_generation: int,
        expected_state: TaskDagNodeState,
    ) -> TaskDag: ...

    async def claim_task_dag_node(
        self,
        dag_id: str,
        node: TaskDagNode,
        *,
        expected_generation: int,
        expected_state: TaskDagNodeState,
        updated_at: datetime,
        expected_dag_generation: int | None = None,
    ) -> TaskDag: ...

    async def finish_task_dag_node(
        self,
        dag_id: str,
        node: TaskDagNode,
        *,
        expected_generation: int,
        expected_state: TaskDagNodeState,
        updated_at: datetime,
    ) -> TaskDag: ...


__all__ = ["TaskDagError", "TaskDagStore"]
