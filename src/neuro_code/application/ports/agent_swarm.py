"""Durable persistence port for bounded Agent Swarm runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from neuro_code.domain.agent_swarm import AgentSwarmRun, AgentSwarmRunState

ProcessLivenessProbe = Callable[[int | None], bool]


class AgentSwarmStoreError(Exception):
    """Fail-closed error at the Swarm persistence boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


@dataclass(frozen=True, slots=True)
class AgentSwarmRunClaim:
    """Result of one atomic insert-or-recover Swarm identity claim."""

    run: AgentSwarmRun
    acquired: bool


@runtime_checkable
class AgentSwarmStore(Protocol):
    """Insert-once identity and generation-CAS Swarm lifecycle."""

    async def get_swarm_run(self, swarm_run_id: str, /) -> AgentSwarmRun | None: ...

    async def claim_swarm_run(
        self,
        run: AgentSwarmRun,
        *,
        now: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> AgentSwarmRunClaim: ...

    async def compare_and_transition_swarm_run(
        self,
        run: AgentSwarmRun,
        *,
        expected_generation: int,
        expected_state: AgentSwarmRunState,
    ) -> AgentSwarmRun: ...


# The shorter names are intentional aliases, not independent contracts.
SwarmRunStore = AgentSwarmStore
SwarmRunStoreError = AgentSwarmStoreError


__all__ = [
    "AgentSwarmRunClaim",
    "AgentSwarmStore",
    "AgentSwarmStoreError",
    "ProcessLivenessProbe",
    "SwarmRunStore",
    "SwarmRunStoreError",
]
