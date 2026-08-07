"""Contracts shared by application permission flow and interactive adapters.

定义应用权限流程与交互适配器共享的契约."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from neuro_code.domain.permissions.bash_commands import analyze_bash_command


class PermissionApprovalKind(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionApproval:
    kind: PermissionApprovalKind
    reason: str

    @property
    def allowed(self) -> bool:
        return self.kind in {
            PermissionApprovalKind.ALLOW_ONCE,
            PermissionApprovalKind.ALLOW_SESSION,
        }

    @classmethod
    def allow_once(cls, reason: str = "approved once by user") -> PermissionApproval:
        return cls(PermissionApprovalKind.ALLOW_ONCE, reason)

    @classmethod
    def allow_session(
        cls,
        reason: str = "approved for this session by user",
    ) -> PermissionApproval:
        return cls(PermissionApprovalKind.ALLOW_SESSION, reason)

    @classmethod
    def deny(cls, reason: str = "denied by user") -> PermissionApproval:
        return cls(PermissionApprovalKind.DENY, reason)


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    call_id: str
    tool_name: str
    summary: str
    reason: str
    scope_key: str | None


def _bounded_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str) or not value:
        return "(not provided)"
    sanitized = value.replace("\x00", "�")
    if len(sanitized) <= limit:
        return sanitized
    return f"{sanitized[:limit]}\n… [truncated]"


def _scope_json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported permission argument type: {type(value).__name__}")


def build_permission_request(
    call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    reason: str,
) -> PermissionRequest:
    """Build a bounded UI description and opaque exact-action session scope.

    构建有界的 UI 描述和不透明的精确动作会话范围."""

    cacheable = True
    if tool_name == "bash":
        command = arguments.get("command")
        summary = f"Run shell command:\n{_bounded_text(command, limit=2_000)}"
        cacheable = isinstance(command, str) and analyze_bash_command(command).complete
    elif tool_name == "create_terminal":
        command = _bounded_text(arguments.get("command"), limit=2_000)
        cwd = _bounded_text(arguments.get("cwd"), limit=500)
        summary = f"Create interactive terminal:\n{command}\nWorking directory: {cwd}"
    elif tool_name == "search_replace":
        path = _bounded_text(arguments.get("path"), limit=500)
        qualifier = (
            "all matching occurrences" if arguments.get("replace_all") is True else "one occurrence"
        )
        summary = (
            f"Edit workspace file: {path}\n"
            f"Replace {qualifier}; replacement text is hidden from the approval UI."
        )
    elif tool_name == "apply_patch":
        summary = "Apply a workspace patch; patch content is hidden from the approval UI."
    else:
        name = _bounded_text(tool_name, limit=200)
        target_path = arguments.get("path")
        target = (
            f"\nTarget: {_bounded_text(target_path, limit=500)}"
            if isinstance(target_path, str)
            else ""
        )
        summary = f"Run side-effecting tool: {name}{target}"

    try:
        scope_payload = json.dumps(
            {"arguments": dict(arguments), "tool": tool_name},
            allow_nan=False,
            default=_scope_json_default,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        scope_key = None
    else:
        scope_key = hashlib.sha256(scope_payload.encode("utf-8")).hexdigest() if cacheable else None
    return PermissionRequest(call_id, tool_name, summary, reason, scope_key)


__all__ = [
    "PermissionApproval",
    "PermissionApprovalKind",
    "PermissionRequest",
    "build_permission_request",
]
