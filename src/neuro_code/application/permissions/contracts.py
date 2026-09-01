"""Contracts shared by application permission flow and interactive adapters.

定义应用权限流程与交互适配器共享的契约."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from neuro_code.application.permissions.scopes import (
    PermissionScopeCandidate,
    PermissionScopeContext,
)
from neuro_code.domain.permissions.bash_commands import analyze_bash_command


class PermissionApprovalKind(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    ALLOW_SCOPE = "allow_scope"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionApproval:
    kind: PermissionApprovalKind
    reason: str
    scope_candidate: PermissionScopeCandidate | None = None
    cache_hit: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PermissionApprovalKind):
            raise TypeError("permission approval kind must be a PermissionApprovalKind")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("permission approval reason must be non-empty")
        if not isinstance(self.cache_hit, bool):
            raise TypeError("permission approval cache_hit must be a bool")
        if self.kind is PermissionApprovalKind.ALLOW_SCOPE:
            if not isinstance(self.scope_candidate, PermissionScopeCandidate):
                raise ValueError("scoped approval requires a scope candidate")
        elif self.scope_candidate is not None:
            raise ValueError("only scoped approval may contain a scope candidate")

    @property
    def allowed(self) -> bool:
        return self.kind in {
            PermissionApprovalKind.ALLOW_ONCE,
            PermissionApprovalKind.ALLOW_SESSION,
            PermissionApprovalKind.ALLOW_SCOPE,
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
    def allow_scope(
        cls,
        scope_candidate: PermissionScopeCandidate,
        reason: str = "approved for this scope by user",
        *,
        cache_hit: bool = False,
    ) -> PermissionApproval:
        return cls(PermissionApprovalKind.ALLOW_SCOPE, reason, scope_candidate, cache_hit)

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
    scope_candidates: tuple[PermissionScopeCandidate, ...] = ()
    scope_context: PermissionScopeContext | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id or "\x00" in self.call_id:
            raise ValueError("permission request call_id must be non-empty")
        if not isinstance(self.tool_name, str) or not self.tool_name or "\x00" in self.tool_name:
            raise ValueError("permission request tool_name must be non-empty")
        for name in ("summary", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"permission request {name} must be non-empty")
        if self.scope_key is not None and (
            not isinstance(self.scope_key, str) or not self.scope_key or "\x00" in self.scope_key
        ):
            raise ValueError("permission request scope_key must be non-empty when present")
        candidates = tuple(self.scope_candidates)
        if not all(isinstance(candidate, PermissionScopeCandidate) for candidate in candidates):
            raise TypeError("permission request scope candidates must be canonical")
        if len(set(candidates)) != len(candidates):
            raise ValueError("permission request scope candidates must be unique")
        if self.scope_context is not None and not isinstance(
            self.scope_context, PermissionScopeContext
        ):
            raise TypeError("permission request scope context must be canonical")
        if any(candidate.is_broad for candidate in candidates) and self.scope_context is None:
            raise ValueError("broad permission scopes require a scope context")
        if self.scope_context is not None and any(
            candidate.workspace_root != self.scope_context.workspace_root
            for candidate in candidates
            if candidate.is_broad
        ):
            raise ValueError("permission scope candidates must use the request workspace root")
        object.__setattr__(self, "scope_candidates", candidates)


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
    *,
    scope_candidates: tuple[PermissionScopeCandidate, ...] = (),
    scope_context: PermissionScopeContext | None = None,
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
    return PermissionRequest(
        call_id,
        tool_name,
        summary,
        reason,
        scope_key,
        tuple(scope_candidates),
        scope_context,
    )


__all__ = [
    "PermissionApproval",
    "PermissionApprovalKind",
    "PermissionRequest",
    "build_permission_request",
]
