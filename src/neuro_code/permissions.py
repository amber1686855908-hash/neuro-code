"""Compatibility facade for the canonical application permission policy.

提供应用权限策略的兼容门面,并转发到规范实现."""

from neuro_code.application.permissions.policy import (
    PermissionDecision,
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)

__all__ = [
    "PermissionDecision",
    "PermissionEffect",
    "PermissionManager",
    "PermissionMode",
    "PermissionRule",
]
