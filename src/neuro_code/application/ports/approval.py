"""Canonical approval interaction port."""

from __future__ import annotations

from typing import Protocol

from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest


class PermissionApprover(Protocol):
    async def request(self, request: PermissionRequest) -> PermissionApproval: ...


__all__ = ["PermissionApprover"]
