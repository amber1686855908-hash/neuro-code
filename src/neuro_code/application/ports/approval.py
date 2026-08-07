"""Canonical approval interaction port.

定义规范的审批交互端口."""

from __future__ import annotations

from typing import Protocol

from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest


class PermissionApprover(Protocol):
    async def request(self, request: PermissionRequest) -> PermissionApproval: ...


__all__ = ["PermissionApprover"]
