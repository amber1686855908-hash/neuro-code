"""Durable lease port for writable subagent workspace linkage."""

from __future__ import annotations

from typing import Protocol

from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceLease


class WritableSubagentLeaseError(Exception):
    """Bounded failure at the writable-subagent lease persistence boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


class WritableSubagentLeaseStore(Protocol):
    """Insert-only/CAS persistence for one writable child workspace lease."""

    async def initialize(self) -> None: ...

    async def get_writable_subagent_lease(
        self,
        lease_id: str,
        /,
    ) -> WritableSubagentWorkspaceLease | None: ...

    async def get_writable_subagent_lease_for_parent_task(
        self,
        parent_session_id: str,
        parent_task_id: str,
        /,
    ) -> WritableSubagentWorkspaceLease | None: ...

    async def list_writable_subagent_leases(
        self,
        *,
        parent_session_id: str | None = None,
        include_terminal: bool = True,
    ) -> tuple[WritableSubagentWorkspaceLease, ...]: ...

    async def insert_writable_subagent_lease(
        self,
        lease: WritableSubagentWorkspaceLease,
        /,
    ) -> WritableSubagentWorkspaceLease: ...

    async def compare_and_transition_writable_subagent_lease(
        self,
        lease: WritableSubagentWorkspaceLease,
        *,
        expected_version: int,
        expected_state: object,
    ) -> WritableSubagentWorkspaceLease: ...


__all__ = ["WritableSubagentLeaseError", "WritableSubagentLeaseStore"]
