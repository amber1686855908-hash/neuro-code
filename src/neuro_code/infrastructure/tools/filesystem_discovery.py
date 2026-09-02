"""Bounded local workspace discovery tools.

This module owns directory traversal, git-ignore interpretation, list/tree
projection, and path-pattern globbing. Content search is kept in the sibling
filesystem_search module and all boundary checks come from filesystem_security.

有界本地工作区发现工具. 本模块拥有目录遍历、git-ignore 解释、list/tree
投影与路径模式 glob; 内容搜索位于 filesystem_search, 边界检查统一来自
filesystem_security.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
    FilesystemTargetRequest,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.tools.filesystem_output import (
    _bounded_output,
    _safe_bounded_output,
)
from neuro_code.infrastructure.tools.filesystem_security import (
    _display_path,
    _ensure_no_link_components,
    _is_link_like,
    _prepare_local_targets,
    _require_bool,
    _require_bounded_integer,
    _require_string,
    _resolve_path,
    _track_primary_workspace_path,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError

MAX_TREE_DEPTH = 8
MAX_TREE_ENTRIES = 2000
MAX_GLOB_RESULTS = 2000
MAX_GLOB_PATTERN_LENGTH = 500
MAX_FILE_SCAN_ENTRIES = 100_000

_DEFAULT_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".cache",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
    }
)


@dataclass(frozen=True, slots=True)
class _FileSelectionResult:
    files: tuple[Path, ...]
    scan_limited: bool
    ignored_directories: int
    ignored_links: int
    scanned_entries: int


@dataclass(frozen=True, slots=True)
class _GitIgnoreRule:
    base: Path
    pattern: str
    negated: bool
    directory_only: bool

    def matches(self, path: Path, *, is_directory: bool) -> bool:
        if self.directory_only and not is_directory:
            return False
        try:
            relative = path.relative_to(self.base).as_posix()
        except ValueError:
            return False
        pattern = self.pattern
        if pattern.startswith("/"):
            pattern = pattern[1:]
        candidates = [relative]
        if "/" not in pattern:
            candidates.append(path.name)
        return any(
            _glob_pattern_matches(candidate, pattern, case_sensitive=True)
            for candidate in candidates
        )


def _glob_pattern_matches(relative: str, pattern: str, *, case_sensitive: bool) -> bool:
    """Match a workspace-relative path with forgiving ``**`` semantics.

    ``pathlib.PurePath.match`` does not treat ``src/**/*.py`` as matching
    ``src/main.py``.  Tool callers reasonably expect the usual glob meaning,
    so the zero-directory form is checked explicitly while keeping matching
    deterministic and dependency-free.

    使用宽松的 ``**`` 语义匹配工作区相对路径.
    """

    normalized_path = relative.replace("\\", "/")
    normalized_pattern = pattern.replace("\\", "/")
    if normalized_pattern.startswith("./"):
        normalized_pattern = normalized_pattern[2:]

    def matches_segments(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
        if not pattern_parts:
            return not path_parts
        if pattern_parts[0] == "**":
            return matches_segments(path_parts, pattern_parts[1:]) or bool(
                path_parts and matches_segments(path_parts[1:], pattern_parts)
            )
        return bool(
            path_parts
            and fnmatch.fnmatchcase(path_parts[0], pattern_parts[0])
            and matches_segments(path_parts[1:], pattern_parts[1:])
        )

    if not case_sensitive:
        normalized_path = normalized_path.casefold()
        normalized_pattern = normalized_pattern.casefold()
    path_parts = tuple(part for part in normalized_path.split("/") if part)
    pattern_parts = tuple(part for part in normalized_pattern.split("/") if part)
    candidates = (path_parts, (path_parts[-1],)) if "/" not in normalized_pattern else (path_parts,)
    return any(matches_segments(candidate, pattern_parts) for candidate in candidates)


def _git_root(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            continue
    return None


def _read_gitignore_rules(directory: Path) -> tuple[_GitIgnoreRule, ...]:
    ignore_file = directory / ".gitignore"
    if _is_link_like(ignore_file):
        return ()
    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ()
    rules: list[_GitIgnoreRule] = []
    for raw_line in lines:
        pattern = raw_line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        if negated:
            pattern = pattern[1:]
        if not pattern:
            continue
        directory_only = pattern.endswith("/")
        if directory_only:
            pattern = pattern.rstrip("/")
        if pattern:
            rules.append(_GitIgnoreRule(directory, pattern, negated, directory_only))
    return tuple(rules)


class _WorkspaceFileSelector:
    """Select bounded local workspace files with one shared ignore policy.

    The selector deliberately refuses delegated ACP filesystems: the current
    client capability only exposes text-file reads/writes, not directory
    enumeration or globbing.  Falling back to the local process filesystem
    would violate the delegated workspace boundary.

    使用统一忽略策略选择有界的本地工作区文件. 对当前不支持目录枚举的 ACP
    委托文件系统直接失败关闭,避免绕过客户端边界.
    """

    __slots__ = (
        "_base",
        "_case_sensitive",
        "_context",
        "_exclude_globs",
        "_git_root",
        "_include_globs",
        "_respect_git_ignore",
        "_root",
    )

    def __init__(
        self,
        context: ToolContext,
        root: Path,
        *,
        include_globs: tuple[str, ...] = (),
        exclude_globs: tuple[str, ...] = (),
        case_sensitive: bool = True,
        respect_git_ignore: bool = True,
    ) -> None:
        if context.client_file_system is not None:
            raise ToolError(
                "ACP client does not support local workspace file discovery; "
                "directory enumeration capability is required"
            )
        self._context = context
        self._root = root
        self._base = root if root.is_dir() else root.parent
        self._include_globs = include_globs
        self._exclude_globs = exclude_globs
        self._case_sensitive = case_sensitive
        self._respect_git_ignore = respect_git_ignore
        self._git_root = _git_root(self._base) if respect_git_ignore else None

    def _matches(self, path: Path, patterns: tuple[str, ...]) -> bool:
        try:
            relative = path.relative_to(self._base).as_posix()
        except ValueError:
            return False
        return any(
            _glob_pattern_matches(relative, pattern, case_sensitive=self._case_sensitive)
            for pattern in patterns
        )

    def _rules_for(
        self, directory: Path, inherited: tuple[_GitIgnoreRule, ...]
    ) -> tuple[_GitIgnoreRule, ...]:
        if not self._respect_git_ignore:
            return inherited
        return inherited + _read_gitignore_rules(directory)

    def _base_rules(self) -> tuple[_GitIgnoreRule, ...]:
        if self._git_root is None:
            return ()
        chain = [self._base, *self._base.parents]
        try:
            git_index = chain.index(self._git_root)
            directories = tuple(reversed(chain[1 : git_index + 1]))
        except ValueError:
            directories = (self._git_root,)
        rules: tuple[_GitIgnoreRule, ...] = ()
        for directory in directories:
            rules = self._rules_for(directory, rules)
        return rules

    def _ignored(
        self,
        path: Path,
        *,
        is_directory: bool,
        rules: tuple[_GitIgnoreRule, ...],
    ) -> bool:
        ignored = False
        for rule in rules:
            if rule.matches(path, is_directory=is_directory):
                ignored = not rule.negated
        return ignored

    def select_files(self, *, max_files: int) -> _FileSelectionResult:
        files: list[Path] = []
        ignored_directories = 0
        ignored_links = 0
        scanned_entries = 0
        scan_limited = False
        base_rules = self._base_rules()

        def visit(directory: Path, rules: tuple[_GitIgnoreRule, ...]) -> None:
            nonlocal ignored_directories, ignored_links, scanned_entries, scan_limited
            try:
                children = sorted(
                    directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
                )
            except OSError:
                return
            local_rules = self._rules_for(directory, rules)
            for child in children:
                if _is_link_like(child):
                    ignored_links += 1
                    continue
                if scanned_entries >= MAX_FILE_SCAN_ENTRIES:
                    scan_limited = True
                    return
                scanned_entries += 1
                try:
                    is_directory = child.is_dir()
                except OSError:
                    continue
                if is_directory:
                    if child.name.casefold() in _DEFAULT_IGNORED_DIRECTORY_NAMES:
                        ignored_directories += 1
                        continue
                    if self._ignored(child, is_directory=True, rules=local_rules):
                        ignored_directories += 1
                        continue
                    visit(child, local_rules)
                    if scan_limited:
                        return
                    continue
                if not child.is_file() or self._ignored(
                    child, is_directory=False, rules=local_rules
                ):
                    continue
                if self._include_globs and not self._matches(child, self._include_globs):
                    continue
                if self._exclude_globs and self._matches(child, self._exclude_globs):
                    continue
                if len(files) >= max_files:
                    scan_limited = True
                    return
                files.append(child)

        if self._root.is_file():
            file_rules = self._rules_for(self._base, base_rules)
            if (
                not _is_link_like(self._root)
                and not self._ignored(self._root, is_directory=False, rules=file_rules)
                and (not self._include_globs or self._matches(self._root, self._include_globs))
                and (not self._exclude_globs or not self._matches(self._root, self._exclude_globs))
            ):
                files.append(self._root)
        elif self._root.is_dir():
            visit(self._root, base_rules)
        else:
            raise ToolError(f"not a file or directory: {self._root}")
        return _FileSelectionResult(
            tuple(files), scan_limited, ignored_directories, ignored_links, scanned_entries
        )

    def tree_entries(
        self, *, max_depth: int, max_entries: int
    ) -> tuple[list[tuple[Path, bool, int]], bool, int, int]:
        entries: list[tuple[Path, bool, int]] = []
        entry_limited = False
        ignored_directories = 0
        ignored_links = 0
        base_rules = self._base_rules()

        def visit(directory: Path, depth: int, rules: tuple[_GitIgnoreRule, ...]) -> None:
            nonlocal entry_limited, ignored_directories, ignored_links
            try:
                children = sorted(
                    directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
                )
            except OSError:
                return
            local_rules = self._rules_for(directory, rules)
            for child in children:
                if len(entries) >= max_entries:
                    entry_limited = True
                    return
                if _is_link_like(child):
                    ignored_links += 1
                    continue
                try:
                    is_directory = child.is_dir()
                except OSError:
                    continue
                if is_directory and child.name.casefold() in _DEFAULT_IGNORED_DIRECTORY_NAMES:
                    ignored_directories += 1
                    continue
                if self._ignored(child, is_directory=is_directory, rules=local_rules):
                    if is_directory:
                        ignored_directories += 1
                    continue
                entries.append((child, is_directory, depth))
                if is_directory and depth < max_depth:
                    visit(child, depth + 1, local_rules)
                    if entry_limited:
                        return

        if not self._root.is_dir():
            raise ToolError(f"not a directory: {self._root}")
        visit(self._root, 1, base_rules)
        return entries, entry_limited, ignored_directories, ignored_links


class ListDirTool:
    definition = ToolDefinition(
        name="list_dir",
        description=(
            "List files and directories directly beneath a workspace path. "
            "Use list_tree for a bounded structure overview and glob for a known pattern."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
            "additionalProperties": False,
        },
    )
    side_effecting = False

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        return _prepare_local_targets(
            "list_dir",
            context,
            (FilesystemTargetRequest(raw_path, FilesystemAccessOperation.LIST, must_exist=True),),
        )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.client_file_system is not None:
            raise ToolError(
                "ACP client does not support local directory discovery; "
                "directory enumeration capability is required"
            )
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        _ensure_no_link_components(context, raw_path)
        path = _resolve_path(
            context,
            raw_path,
            must_exist=True,
            operation=FilesystemAccessOperation.LIST,
        )
        if not path.is_dir():
            raise ToolError(f"not a directory: {path}")
        _track_primary_workspace_path(context, path)

        def list_entries() -> list[str]:
            entries: list[str] = []
            for child in sorted(
                path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())
            ):
                suffix = "/" if child.is_dir() else ""
                entries.append(f"{child.name}{suffix}")
            return entries

        entries = await run_blocking(list_entries)
        return ToolResult("\n".join(entries), metadata={"path": str(path), "count": len(entries)})


class ListTreeTool:
    """List a deterministic, bounded workspace tree without following links.

    以确定、有界且不跟随链接的方式列出工作区目录树。
    """

    definition = ToolDefinition(
        name="list_tree",
        description=(
            "List a bounded workspace directory tree, skipping common metadata, "
            "dependency, cache, and build directories. Use glob to locate files "
            "matching a known pattern."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TREE_DEPTH,
                },
                "max_entries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TREE_ENTRIES,
                },
                "respect_git_ignore": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    )
    side_effecting = False

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        return _prepare_local_targets(
            "list_tree",
            context,
            (FilesystemTargetRequest(raw_path, FilesystemAccessOperation.LIST, must_exist=True),),
        )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        _ensure_no_link_components(context, raw_path)
        max_depth = _require_bounded_integer(
            arguments.get("max_depth", 3),
            field_name="max_depth",
            minimum=1,
            maximum=MAX_TREE_DEPTH,
        )
        max_entries = _require_bounded_integer(
            arguments.get("max_entries", 500),
            field_name="max_entries",
            minimum=1,
            maximum=MAX_TREE_ENTRIES,
        )
        respect_git_ignore = arguments.get("respect_git_ignore", True)
        if not isinstance(respect_git_ignore, bool):
            raise ToolError("respect_git_ignore must be a boolean")
        root = _resolve_path(
            context,
            raw_path,
            must_exist=True,
            operation=FilesystemAccessOperation.LIST,
        )
        if not root.is_dir():
            raise ToolError(f"not a directory: {root}")
        _track_primary_workspace_path(context, root)
        selector = _WorkspaceFileSelector(
            context,
            root,
            respect_git_ignore=respect_git_ignore,
        )
        raw_entries, entry_limited, ignored_directories, ignored_links = await run_blocking(
            selector.tree_entries,
            max_depth=max_depth,
            max_entries=max_entries,
        )
        entries = [
            f"{'  ' * (depth - 1)}{path.name}{'/' if is_directory else ''}"
            for path, is_directory, depth in raw_entries
        ]
        rendered = "\n".join(entries) if entries else "[empty tree]"
        content, byte_limited = _safe_bounded_output(rendered, context)
        return ToolResult(
            content,
            metadata={
                "path": str(root),
                "count": len(entries),
                "max_depth": max_depth,
                "entry_limited": entry_limited,
                "byte_limited": byte_limited,
                "ignored_directories": ignored_directories,
                "ignored_links": ignored_links,
                "respect_git_ignore": respect_git_ignore,
            },
        )


class GlobTool:
    """Find workspace files by path pattern without following links."""

    definition = ToolDefinition(
        name="glob",
        description=(
            "Find workspace files by a bounded path pattern. Use glob when you know "
            "the filename or path shape but not its exact location; use grep for "
            "file contents and list_tree for a structural overview."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "case_sensitive": {"type": "boolean", "default": True},
                "respect_git_ignore": {"type": "boolean", "default": True},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_GLOB_RESULTS,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        return _prepare_local_targets(
            "glob",
            context,
            (FilesystemTargetRequest(raw_path, FilesystemAccessOperation.SEARCH, must_exist=True),),
        )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        pattern = _require_string(arguments, "pattern")
        if len(pattern) > MAX_GLOB_PATTERN_LENGTH:
            raise ToolError(f"pattern must be at most {MAX_GLOB_PATTERN_LENGTH} characters")
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        _ensure_no_link_components(context, raw_path)
        case_sensitive = _require_bool(arguments, "case_sensitive", default=True)
        respect_git_ignore = _require_bool(arguments, "respect_git_ignore", default=True)
        max_results = _require_bounded_integer(
            arguments.get("max_results", 500),
            field_name="max_results",
            minimum=1,
            maximum=MAX_GLOB_RESULTS,
        )
        root = _resolve_path(
            context,
            raw_path,
            must_exist=True,
            operation=FilesystemAccessOperation.SEARCH,
        )
        if not root.is_dir() and not root.is_file():
            raise ToolError(f"not a file or directory: {root}")
        _track_primary_workspace_path(context, root)
        selector = _WorkspaceFileSelector(
            context,
            root,
            include_globs=(pattern,),
            case_sensitive=case_sensitive,
            respect_git_ignore=respect_git_ignore,
        )
        selection = await run_blocking(selector.select_files, max_files=max_results)
        rendered = (
            "\n".join(_display_path(context, path) for path in selection.files) or "[no matches]"
        )
        content, byte_limited = _safe_bounded_output(rendered, context)
        result_limited = selection.scan_limited
        if byte_limited:
            result_limited = True
        if result_limited:
            suffix = "\n[glob truncated: result or output limit reached]"
            content = _bounded_output(f"{content}{suffix}", byte_limit=context.output_byte_limit)[0]
        return ToolResult(
            content,
            metadata={
                "pattern": pattern,
                "path": str(root),
                "count": len(selection.files),
                "truncated": result_limited,
                "byte_limited": byte_limited,
                "scan_limited": selection.scan_limited,
                "scanned_entries": selection.scanned_entries,
                "ignored_directories": selection.ignored_directories,
                "ignored_links": selection.ignored_links,
                "respect_git_ignore": respect_git_ignore,
            },
        )


__all__ = ["GlobTool", "ListDirTool", "ListTreeTool"]
