"""Filesystem tool path and workspace-boundary policy.

This module is the single owner of filesystem-tool target planning, delegated
path resolution, link/reparse-point rejection, workspace projection, and
instruction/skill tracking. Concrete read, discovery, search, and mutation
tools must use these functions instead of implementing their own boundary
checks.

文件系统工具路径与工作区边界策略. 本模块是工具目标规划、委托路径解析、
链接/重解析点拒绝、工作区投影以及指令/技能跟踪的唯一所有者.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
    FilesystemTargetRequest,
)
from neuro_code.infrastructure.workspace.paths import (
    is_additional_workspace_path,
    resolve_delegated_workspace_path,
    resolve_filesystem_access_targets,
    resolve_workspace_path,
    workspace_display_path,
)
from neuro_code.shared.errors import ToolError


def _require_bounded_integer(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        return True


def _require_string_sequence(
    value: object,
    *,
    field_name: str,
    minimum_items: int,
    maximum_items: int,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ToolError(f"{field_name} must be an array of strings")
    items = tuple(value)
    if not minimum_items <= len(items) <= maximum_items:
        raise ToolError(
            f"{field_name} must contain between {minimum_items} and {maximum_items} items"
        )
    if any(not isinstance(item, str) or not item or "\x00" in item for item in items):
        raise ToolError(f"{field_name} must contain non-empty strings")
    return items


def _require_string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{key} must be a non-empty string")
    return value


def _require_bool(arguments: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be a boolean")
    return value


def _prepare_local_targets(
    tool_name: str,
    context: ToolContext,
    requests: Sequence[FilesystemTargetRequest],
) -> FilesystemAccessPlan | None:
    if context.client_file_system is not None:
        return None
    return resolve_filesystem_access_targets(
        tool_name,
        context.cwd,
        requests,
        additional_workspace_roots=context.additional_workspace_roots,
    )


def _resolve_path(
    context: ToolContext,
    requested: str,
    *,
    must_exist: bool,
    operation: FilesystemAccessOperation | None = None,
    target_index: int = 0,
) -> Path:
    if context.client_file_system is not None:
        return resolve_delegated_workspace_path(
            context.cwd,
            requested,
            additional_workspace_roots=context.additional_workspace_roots,
        )
    plan = context.filesystem_access_plan
    if plan is not None:
        try:
            target = plan.target_at(target_index)
        except IndexError as error:
            raise ToolError("filesystem execution target plan is incomplete") from error
        if target.requested_path != requested or (
            operation is not None and target.operation is not operation
        ):
            raise ToolError("filesystem execution target plan does not match the request")
        return target.canonical_path
    return resolve_workspace_path(
        context.cwd,
        requested,
        must_exist=must_exist,
        additional_workspace_roots=context.additional_workspace_roots,
    )


def _is_primary_workspace_path(context: ToolContext, path: Path) -> bool:
    if context.client_file_system is not None:
        roots = (context.cwd, *context.additional_workspace_roots)
        try:
            path_text = os.path.normcase(os.path.normpath(os.fspath(path)))
            return not any(
                os.path.commonpath((path_text, os.path.normcase(os.path.normpath(os.fspath(root)))))
                == os.path.normcase(os.path.normpath(os.fspath(root)))
                for root in roots[1:]
            )
        except (OSError, ValueError):
            return False
    plan = context.filesystem_access_plan
    if plan is not None:
        for target in plan.targets:
            if target.canonical_path == path:
                return target.is_primary_workspace
    return not is_additional_workspace_path(
        context.cwd,
        path,
        context.additional_workspace_roots,
    )


def _display_path(context: ToolContext, path: Path) -> str:
    plan = context.filesystem_access_plan
    if plan is not None:
        for target in plan.targets:
            if target.canonical_path == path:
                return target.policy_path
    if context.client_file_system is not None:
        return os.path.normpath(os.fspath(path)).replace("\\", "/")
    return workspace_display_path(
        context.cwd,
        path,
        context.additional_workspace_roots,
    )


def _track_primary_workspace_path(context: ToolContext, path: Path) -> None:
    """Refresh primary-workspace instructions only.

    ACP's additional directories are explicit file-access roots, not an
    instruction or skill-discovery expansion.  Treating their AGENTS.md or
    SKILL.md files as project policy would silently cross the primary
    workspace's trust boundary.

    仅刷新主工作区的指令和技能发现状态.
    """

    if not _is_primary_workspace_path(context, path):
        return
    if context.instruction_tracker is not None:
        context.instruction_tracker.check_path(path)
    if context.skill_tracker is not None:
        context.skill_tracker.check_path(path)


def _ensure_no_link_components(
    context: ToolContext,
    requested: str,
    *,
    traversal_error_message: str = "patch paths must not traverse symlinks or junctions",
    target_error_message: str = "patch paths must not target symlinks or junctions",
) -> None:
    if context.client_file_system is not None:
        # A delegated client owns link/reparse semantics.  Inspecting the host
        # path here would create a false local security proof.
        return
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = context.cwd / candidate
    # Do not inspect symlink aliases that belong to the platform's temporary
    # directory (macOS commonly exposes ``/var`` as a symlink to
    # ``/private/var``).  The workspace root itself is an accepted boundary;
    # only components below an explicitly allowed root are unsafe.
    roots = (context.cwd, *context.additional_workspace_roots)
    for root in roots:
        root_path = root.expanduser()
        try:
            relative = candidate.relative_to(root_path)
        except ValueError:
            continue
        current = root_path
        relative_parts = relative.parts
        for part in relative_parts[:-1]:
            current /= part
            if _is_link_like(current):
                raise ToolError(traversal_error_message)
        if _is_link_like(candidate):
            raise ToolError(target_error_message)
        return


__all__: list[str] = []
