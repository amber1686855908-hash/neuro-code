"""Typed, process-local permission scope values.

定义进程内使用的有类型权限范围值. 这些值只描述可信运行时生成的候选范围,
不承载模型输入、工具参数或持久化授权状态.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PermissionScopeKind(StrEnum):
    EXACT_ACTION = "exact_action"
    WORKSPACE_EDITS = "workspace_edits"
    COMMAND_FAMILY = "command_family"


class PermissionCommandFamily(StrEnum):
    TEST = "test"
    STATIC_CHECK = "static_check"
    GIT_READ = "git_read"


def _canonical_scope_root(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field_name} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{field_name} cannot be resolved") from error
    return os.path.normcase(os.fspath(resolved))


@dataclass(frozen=True, slots=True)
class PermissionScopeContext:
    """Trusted identity joining one logical session to one workspace root."""

    session_identity: str
    workspace_root: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_identity, str)
            or not self.session_identity
            or "\x00" in self.session_identity
            or len(self.session_identity.encode("utf-8")) > 512
        ):
            raise ValueError("permission scope session identity is invalid")
        object.__setattr__(
            self,
            "workspace_root",
            _canonical_scope_root(self.workspace_root, field_name="permission scope workspace"),
        )


@dataclass(frozen=True, slots=True)
class PermissionScopeCandidate:
    """One runtime-generated, bounded alternative to exact approval."""

    kind: PermissionScopeKind
    workspace_root: str | None = None
    command_family: PermissionCommandFamily | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PermissionScopeKind):
            raise TypeError("permission scope kind must be a PermissionScopeKind")
        if self.kind is PermissionScopeKind.EXACT_ACTION:
            if self.workspace_root is not None or self.command_family is not None:
                raise ValueError("exact action scope cannot contain broad scope metadata")
            return
        if self.workspace_root is None:
            raise ValueError("broad permission scope requires a workspace root")
        object.__setattr__(
            self,
            "workspace_root",
            _canonical_scope_root(
                self.workspace_root,
                field_name="permission scope candidate workspace",
            ),
        )
        if self.kind is PermissionScopeKind.WORKSPACE_EDITS and self.command_family is not None:
            raise ValueError("workspace edit scope cannot contain a command family")
        if self.kind is PermissionScopeKind.COMMAND_FAMILY and not isinstance(
            self.command_family, PermissionCommandFamily
        ):
            raise ValueError("command family scope requires a command family")

    @property
    def is_broad(self) -> bool:
        return self.kind is not PermissionScopeKind.EXACT_ACTION

    def audit_metadata(self) -> dict[str, str]:
        """Return bounded, non-payload metadata suitable for events and UI."""

        result = {"kind": self.kind.value}
        if self.workspace_root is not None:
            result["workspace_root"] = self.workspace_root
        if self.command_family is not None:
            result["command_family"] = self.command_family.value
        return result


__all__ = [
    "PermissionCommandFamily",
    "PermissionScopeCandidate",
    "PermissionScopeContext",
    "PermissionScopeKind",
]
