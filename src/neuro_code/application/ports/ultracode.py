"""Durable persistence port for application-level Ultracode delegation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from neuro_code.application.ports.result_adoption import ResultAdoptionRecord
from neuro_code.domain.agent_swarm import AgentSwarmResult
from neuro_code.domain.result_adoption import ResultAdoptionRequest
from neuro_code.domain.ultracode import UltracodeExecution, UltracodeExecutionState

ProcessLivenessProbe = Callable[[int | None], bool]


class UltracodeStoreError(Exception):
    """Fail-closed error at the Ultracode persistence boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


@dataclass(frozen=True, slots=True)
class UltracodeExecutionClaim:
    """Result of one atomic insert-or-recover Ultracode identity claim."""

    execution: UltracodeExecution
    acquired: bool


@runtime_checkable
class UltracodeStore(Protocol):
    """Insert-once identity and generation-CAS Ultracode lifecycle."""

    async def get_ultracode_execution(
        self,
        execution_id: str,
        /,
    ) -> UltracodeExecution | None: ...

    async def claim_ultracode_execution(
        self,
        execution: UltracodeExecution,
        *,
        now: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> UltracodeExecutionClaim: ...

    async def compare_and_transition_ultracode_execution(
        self,
        execution: UltracodeExecution,
        *,
        expected_generation: int,
        expected_state: UltracodeExecutionState,
    ) -> UltracodeExecution: ...


@runtime_checkable
class UltracodeResultAdoption(Protocol):
    """Typed internal seam for adopting one exact completed Swarm result."""

    async def get_result_adoption(
        self,
        adoption_id: str,
        /,
    ) -> ResultAdoptionRecord | None: ...

    async def adopt(
        self,
        request: ResultAdoptionRequest,
        *,
        swarm_result: AgentSwarmResult,
    ) -> ResultAdoptionRecord: ...


ResultAdoptionFactory = Callable[[], Awaitable[UltracodeResultAdoption]]


__all__ = [
    "ProcessLivenessProbe",
    "ResultAdoptionFactory",
    "UltracodeExecutionClaim",
    "UltracodeResultAdoption",
    "UltracodeStore",
    "UltracodeStoreError",
]
