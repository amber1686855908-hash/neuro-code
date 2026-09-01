"""Insert-only persistence port for bounded parent context relays."""

from __future__ import annotations

from typing import Protocol

from neuro_code.domain.parent_context_relay import ParentContextRelay


class ParentContextRelayError(Exception):
    """Bounded fail-closed error at the relay persistence boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


class ParentContextRelayStore(Protocol):
    async def initialize(self) -> None: ...

    async def insert_parent_context_relay(
        self,
        relay: ParentContextRelay,
        /,
    ) -> ParentContextRelay: ...

    async def get_parent_context_relay(
        self,
        relay_id: str,
        /,
    ) -> ParentContextRelay | None: ...

    async def get_parent_context_relay_for_lease(
        self,
        lease_id: str,
        /,
    ) -> ParentContextRelay | None: ...


__all__ = ["ParentContextRelayError", "ParentContextRelayStore"]
