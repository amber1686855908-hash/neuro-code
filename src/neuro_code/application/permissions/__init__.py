"""Application-owned contracts for interactive permission approval.

定义由应用层拥有的交互式权限审批契约."""

from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionApprovalKind,
    PermissionRequest,
    build_permission_request,
)
from neuro_code.application.permissions.scopes import (
    PermissionCommandFamily,
    PermissionScopeCandidate,
    PermissionScopeContext,
    PermissionScopeKind,
)

__all__ = [
    "PermissionApproval",
    "PermissionApprovalKind",
    "PermissionCommandFamily",
    "PermissionRequest",
    "PermissionScopeCandidate",
    "PermissionScopeContext",
    "PermissionScopeKind",
    "build_permission_request",
]
