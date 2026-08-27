"""Durable persistence port for application-level Ultracode delegation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

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


__all__ = [
    "ProcessLivenessProbe",
    "UltracodeExecutionClaim",
    "UltracodeStore",
    "UltracodeStoreError",
]
