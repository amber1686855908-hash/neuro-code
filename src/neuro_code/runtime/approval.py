from __future__ import annotations

from collections.abc import Awaitable, Callable

from neuro_code.permissions import (
    PermissionApproval,
    PermissionApprovalKind,
    PermissionRequest,
)

ApprovalHandler = Callable[[PermissionRequest], Awaitable[PermissionApproval]]


class SessionApprovalBroker:
    """Bridge runtime approval requests to one UI and cache exact session scopes."""

    def __init__(self) -> None:
        self._handler: ApprovalHandler | None = None
        self._approved_scopes: set[str] = set()

    def set_handler(self, handler: ApprovalHandler | None) -> None:
        self._handler = handler

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        if request.scope_key is not None and request.scope_key in self._approved_scopes:
            return PermissionApproval.allow_session(
                "matched an identical action approved for this session"
            )
        if self._handler is None:
            return PermissionApproval.deny("interactive approval UI is unavailable")

        approval = await self._handler(request)
        if approval.kind is PermissionApprovalKind.ALLOW_SESSION:
            if request.scope_key is None:
                return PermissionApproval.allow_once(
                    "action arguments could not be scoped safely; approval applied once"
                )
            self._approved_scopes.add(request.scope_key)
        return approval


__all__ = ["ApprovalHandler", "SessionApprovalBroker"]
