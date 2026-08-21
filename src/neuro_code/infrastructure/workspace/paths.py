"""Canonical filesystem workspace identity and path-boundary infrastructure.

The legacy top-level module remains a compatibility facade.

定义规范的文件系统工作区身份和路径边界基础设施. 顶层旧模块保留为兼容门面.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

from neuro_code.application.ports.workspace import (
    FilesystemAccessPlan,
    FilesystemAccessTarget,
    FilesystemTargetRequest,
)
from neuro_code.shared.errors import ToolError

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def workspaces_match(recorded: str | Path, requested: str | Path) -> bool:
    """Return whether two workspace spellings identify the same filesystem location.

    返回两个工作区路径写法是否指向同一个文件系统位置."""

    try:
        recorded_path = Path(recorded).expanduser()
        requested_path = Path(requested).expanduser()
    except RuntimeError:
        return False

    try:
        return recorded_path.samefile(requested_path)
    except (OSError, ValueError):
        pass

    try:
        recorded_resolved = recorded_path.resolve(strict=False)
        requested_resolved = requested_path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return os.path.normcase(os.fspath(recorded_resolved)) == os.path.normcase(
        os.fspath(requested_resolved)
    )


class FilesystemWorkspaceIdentity:
    """Filesystem-backed workspace identity implementation.

    提供基于文件系统的工作区身份实现."""

    def matches(
        self,
        recorded: str | Path,
        requested: str | Path,
        /,
    ) -> bool:
        return workspaces_match(recorded, requested)


def _resolved_workspace_roots(
    cwd: Path,
    additional_workspace_roots: tuple[Path, ...],
) -> tuple[Path, ...]:
    """Return the primary root followed by independently accessible roots.

    Callers that accept untrusted directories must validate their shape and
    overlap before constructing a tool context.  The defensive overlap check
    here still keeps an accidentally malformed context from broadening the
    resolver's boundary.

    返回主根目录,随后返回可独立访问的附加根目录.
    """

    root = cwd.expanduser().resolve(strict=False)
    roots = [root]
    for additional in additional_workspace_roots:
        try:
            candidate = additional.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ToolError(f"cannot resolve additional workspace root: {error}") from error
        if any(
            candidate == existing
            or candidate.is_relative_to(existing)
            or existing.is_relative_to(candidate)
            for existing in roots
        ):
            raise ToolError("additional workspace roots must not overlap")
        roots.append(candidate)
    return tuple(roots)


def _is_link_like(path: Path) -> bool:
    """Return whether a path component is a symlink, junction, or reparse point."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _reject_ambiguous_windows_path(requested: str) -> None:
    if os.name != "nt":
        return
    rendered = requested.replace("/", "\\")
    if rendered.startswith(("\\\\?\\", "\\\\.\\")):
        raise ToolError("Windows device and extended path namespaces are unsupported")
    windows_path = PureWindowsPath(requested)
    if windows_path.drive and not windows_path.root:
        raise ToolError("Windows drive-relative paths are unsupported")
    for part in windows_path.parts:
        if part in {windows_path.drive, "\\"}:
            continue
        if ":" in part:
            raise ToolError("Windows alternate data streams are unsupported")
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ToolError("Windows device names are unsupported")


def _lexical_candidate(root: Path, requested: str) -> Path:
    candidate = Path(requested).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        path_text = os.path.normcase(os.path.abspath(os.fspath(path)))
        root_text = os.path.normcase(os.path.abspath(os.fspath(root)))
        return os.path.commonpath((path_text, root_text)) == root_text
    except (OSError, ValueError):
        return False


def _contains_link_like_component(candidate: Path, roots: tuple[Path, ...]) -> bool:
    """Inspect the lexical path so an alias cannot hide a link-like component."""

    for root in roots:
        try:
            candidate_text = os.path.normcase(os.path.abspath(os.fspath(candidate)))
            root_text = os.path.normcase(os.path.abspath(os.fspath(root)))
            if os.path.commonpath((candidate_text, root_text)) != root_text:
                continue
            relative = Path(os.path.relpath(candidate_text, root_text))
        except (OSError, ValueError):
            continue
        current = root
        for part in relative.parts:
            if part in {"", "."}:
                continue
            current /= part
            if _is_link_like(current):
                return True
        return False
    return False


def _policy_path(
    canonical_path: Path,
    workspace_root: Path,
    *,
    is_primary: bool,
) -> str:
    if is_primary:
        try:
            relative = canonical_path.relative_to(workspace_root)
            return relative.as_posix()
        except ValueError:
            pass
    rendered = os.path.normcase(os.fspath(canonical_path)).replace("\\", "/")
    return rendered or "."


def resolve_filesystem_access_targets(
    tool_name: str,
    cwd: Path,
    requests: Sequence[FilesystemTargetRequest],
    *,
    additional_workspace_roots: tuple[Path, ...] = (),
) -> FilesystemAccessPlan:
    """Resolve every local structured target once and return the immutable plan.

    This is the only local canonical target resolver.  Permission and tool
    execution consume its result; neither stage resolves the raw spelling again.

    一次性解析所有本地结构化目标并返回不可变计划. 这是唯一的本地规范目标解析器;
    permission 与 tool execution 都消费其结果,不会再次解析原始路径.
    """

    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("filesystem target tool name must be non-empty")
    normalized_requests = tuple(requests)
    if not normalized_requests:
        raise ToolError("structured filesystem call has no targets")
    roots = _resolved_workspace_roots(cwd, additional_workspace_roots)
    primary_root = roots[0]
    targets: list[FilesystemAccessTarget] = []
    for request in normalized_requests:
        if not isinstance(request, FilesystemTargetRequest):
            raise TypeError("filesystem target requests must be canonical")
        _reject_ambiguous_windows_path(request.requested_path)
        try:
            lexical = _lexical_candidate(primary_root, request.requested_path)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise ToolError("filesystem target path is invalid") from error
        contains_link_like = _contains_link_like_component(lexical, roots)
        if request.reject_link_like and contains_link_like:
            raise ToolError(
                f"structured filesystem target must not traverse symlinks, junctions, "
                f"or reparse points: {request.requested_path!r}"
            )
        try:
            canonical = lexical.resolve(strict=request.must_exist)
        except (OSError, RuntimeError) as error:
            raise ToolError(
                f"cannot resolve filesystem target {request.requested_path!r}: {error}"
            ) from error
        if not any(_path_is_within(canonical, root) for root in roots):
            raise ToolError(f"path escapes the workspace: {request.requested_path!r}")

        # For a create target, resolve the existing ancestor separately.  This
        # makes the workspace proof explicit and prevents a missing leaf from
        # becoming a string-only authorization decision.
        if not request.must_exist and not canonical.exists():
            ancestor = canonical
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            try:
                existing_ancestor = ancestor.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ToolError(
                    f"cannot resolve filesystem target parent {request.requested_path!r}: {error}"
                ) from error
            if not any(_path_is_within(existing_ancestor, root) for root in roots):
                raise ToolError(f"path escapes the workspace: {request.requested_path!r}")

        owner: Path | None = next(
            (root for root in roots if _path_is_within(canonical, root)),
            None,
        )
        if owner is None:
            raise ToolError(f"path escapes the workspace: {request.requested_path!r}")
        is_primary = owner == primary_root
        targets.append(
            FilesystemAccessTarget(
                requested_path=request.requested_path,
                canonical_path=canonical,
                owning_workspace_root=owner,
                policy_path=_policy_path(canonical, owner, is_primary=is_primary),
                operation=request.operation,
                exists=canonical.exists(),
                is_primary_workspace=is_primary,
                additional_workspace_root=None if is_primary else owner,
                contains_link_like_component=contains_link_like,
            )
        )
    return FilesystemAccessPlan(tool_name, tuple(targets))


def resolve_workspace_path(
    cwd: Path,
    requested: str,
    *,
    must_exist: bool = False,
    additional_workspace_roots: tuple[Path, ...] = (),
) -> Path:
    if not requested or "\x00" in requested:
        raise ToolError("path must be a non-empty filesystem path")
    roots = _resolved_workspace_roots(cwd, additional_workspace_roots)
    root = roots[0]
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise ToolError(f"cannot resolve path {requested!r}: {error}") from error
    if not any(
        resolved == workspace_root or resolved.is_relative_to(workspace_root)
        for workspace_root in roots
    ):
        raise ToolError(f"path escapes the workspace: {requested!r}")
    return resolved


def resolve_delegated_workspace_path(
    cwd: Path,
    requested: str,
    *,
    additional_workspace_roots: tuple[Path, ...] = (),
) -> Path:
    """Build a bounded delegated path without resolving it on the host.

    ACP client filesystem paths belong to the connected client.  This helper
    performs only lexical validation against the session roots and deliberately
    does not call ``Path.resolve()``, ``exists()``, or link inspection.

    构造委托文件系统路径时不在宿主机上解析. ACP 客户端路径属于连接的 client;
    此 helper 只做相对于会话根目录的词法校验,刻意不调用 ``Path.resolve()``、
    ``exists()`` 或链接检查.
    """

    if not requested or "\x00" in requested:
        raise ToolError("path must be a non-empty delegated filesystem path")
    _reject_ambiguous_windows_path(requested)
    root = Path(cwd).expanduser()
    candidate = _lexical_candidate(root, requested)
    candidate = Path(os.path.normpath(os.fspath(candidate)))
    roots = (root, *tuple(Path(path).expanduser() for path in additional_workspace_roots))
    if not any(_path_is_within(candidate, workspace_root) for workspace_root in roots):
        raise ToolError(f"path escapes the delegated workspace: {requested!r}")
    return candidate


def is_additional_workspace_path(
    cwd: Path,
    path: Path,
    additional_workspace_roots: tuple[Path, ...],
) -> bool:
    """Return whether a resolved path belongs to an additional root.

    返回解析后的路径是否属于某个附加根目录."""

    roots = _resolved_workspace_roots(cwd, additional_workspace_roots)
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in roots[1:])


def workspace_display_path(
    cwd: Path,
    path: Path,
    additional_workspace_roots: tuple[Path, ...] = (),
) -> str:
    """Render primary-workspace paths relatively and extra-root paths absolutely.

    将主工作区路径渲染为相对路径,将附加根目录路径渲染为绝对路径."""

    roots = _resolved_workspace_roots(cwd, additional_workspace_roots)
    try:
        resolved = path.resolve(strict=False)
        return resolved.relative_to(roots[0]).as_posix()
    except (OSError, RuntimeError, ValueError):
        return str(path)


class FilesystemWorkspacePathResolver:
    """Filesystem-backed resolver for existing workspace paths.

    提供基于文件系统的现有工作区路径解析器."""

    def resolve_existing(
        self,
        workspace: Path,
        requested: str,
        /,
    ) -> Path:
        return resolve_workspace_path(workspace, requested, must_exist=True)


__all__ = [
    "FilesystemWorkspaceIdentity",
    "FilesystemWorkspacePathResolver",
    "is_additional_workspace_path",
    "resolve_delegated_workspace_path",
    "resolve_filesystem_access_targets",
    "resolve_workspace_path",
    "workspace_display_path",
    "workspaces_match",
]
