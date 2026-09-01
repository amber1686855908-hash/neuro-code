"""Durable insert-only port for DAG predecessor result relays."""

from __future__ import annotations

from typing import Protocol

from neuro_code.domain.task_dag_result_relay import TaskDagDependencyResultRelay


class TaskDagDependencyResultRelayError(Exception):
    """Bounded fail-closed error at the DAG dataflow persistence boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


class TaskDagDependencyResultRelayStore(Protocol):
    async def initialize(self) -> None: ...

    async def insert_task_dag_dependency_relay(
        self,
        relay: TaskDagDependencyResultRelay,
        /,
    ) -> TaskDagDependencyResultRelay: ...

    async def get_task_dag_dependency_relay(
        self,
        relay_id: str,
        /,
    ) -> TaskDagDependencyResultRelay | None: ...

    async def get_task_dag_dependency_relay_for_target(
        self,
        dag_id: str,
        target_node_id: str,
        target_node_generation: int,
        /,
    ) -> TaskDagDependencyResultRelay | None: ...


__all__ = [
    "TaskDagDependencyResultRelayError",
    "TaskDagDependencyResultRelayStore",
]
