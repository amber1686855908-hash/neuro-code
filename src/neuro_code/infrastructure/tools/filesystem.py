"""Canonical filesystem tool infrastructure adapters.

This module owns bounded workspace discovery/readers and atomic text patch writers.
The retired ``neuro_code.tools.filesystem`` facade has been removed. Path,
instruction-tracker, sandbox, client-filesystem, and write semantics have one
implementation owner here.

定义规范的文件系统工具基础设施适配器. 本模块拥有有界发现/读取器和原子文本补丁写入器.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.workspace.paths import (
    is_additional_workspace_path,
    resolve_workspace_path,
    workspace_display_path,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text

MAX_BATCH_READ_FILES = 16
MAX_BATCH_READ_LINES_PER_FILE = 5000
MAX_TREE_DEPTH = 8
MAX_TREE_ENTRIES = 2000
MAX_GREP_QUERIES = 16
MAX_GREP_GLOBS = 32
MAX_GREP_RESULTS_PER_QUERY = 200
MAX_GREP_TOTAL_RESULTS = 1000
MAX_GREP_SCANNED_FILES = 20_000
MAX_GLOB_RESULTS = 2000
MAX_GLOB_PATTERN_LENGTH = 500
MAX_GREP_CONTEXT_LINES = 20
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
_OUTPUT_TRUNCATION_MARKER = "\n[output truncated]"


@dataclass(frozen=True, slots=True)
class _FileReadRequest:
    requested_path: str
    start_line: int
    max_lines: int


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


def _bounded_output(value: str, *, byte_limit: int) -> tuple[str, bool]:
    if isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit < 1:
        raise ToolError("output_byte_limit must be positive")
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value, False
    marker = _OUTPUT_TRUNCATION_MARKER.encode("utf-8")
    if byte_limit <= len(marker):
        return marker[:byte_limit].decode("utf-8", "ignore"), True
    prefix = encoded[: byte_limit - len(marker)].decode("utf-8", "ignore")
    return f"{prefix}{_OUTPUT_TRUNCATION_MARKER}", True


def _safe_bounded_output(value: str, context: ToolContext) -> tuple[str, bool]:
    redacted = redact_sensitive_text(value, explicit_values=context.redaction_values)
    return _bounded_output(redacted, byte_limit=context.output_byte_limit)


def _numbered_lines(content: str, *, start_line: int) -> str:
    return "\n".join(
        f"{number:>6}\t{line}" for number, line in enumerate(content.splitlines(), start=start_line)
    )


def _parse_file_read_request(value: object, *, index: int) -> _FileReadRequest:
    if not isinstance(value, Mapping):
        raise ToolError(f"files[{index}] must be an object")
    unsupported = set(value).difference({"path", "start_line", "max_lines"})
    if unsupported:
        raise ToolError(f"files[{index}] contains unsupported fields")
    requested_path = value.get("path")
    if not isinstance(requested_path, str) or not requested_path:
        raise ToolError(f"files[{index}].path must be a non-empty string")
    start_line = _require_bounded_integer(
        value.get("start_line", 1),
        field_name=f"files[{index}].start_line",
        minimum=1,
        maximum=2_147_483_647,
    )
    max_lines = _require_bounded_integer(
        value.get("max_lines", 500),
        field_name=f"files[{index}].max_lines",
        minimum=1,
        maximum=MAX_BATCH_READ_LINES_PER_FILE,
    )
    return _FileReadRequest(requested_path, start_line, max_lines)


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


def _resolve_path(context: ToolContext, requested: str, *, must_exist: bool) -> Path:
    return resolve_workspace_path(
        context.cwd,
        requested,
        must_exist=must_exist,
        additional_workspace_roots=context.additional_workspace_roots,
    )


def _is_primary_workspace_path(context: ToolContext, path: Path) -> bool:
    return not is_additional_workspace_path(
        context.cwd,
        path,
        context.additional_workspace_roots,
    )


def _display_path(context: ToolContext, path: Path) -> str:
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


class ReadFileTool:
    definition = ToolDefinition(
        name="read_file",
        description=(
            "Read one targeted UTF-8 workspace file or line range. "
            "Use read_files when several known files can be read independently."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 5000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        start_line = arguments.get("start_line", 1)
        max_lines = arguments.get("max_lines", 500)
        if not isinstance(start_line, int) or start_line < 1:
            raise ToolError("start_line must be a positive integer")
        if not isinstance(max_lines, int) or not 1 <= max_lines <= 5000:
            raise ToolError("max_lines must be between 1 and 5000")

        requested = _require_string(arguments, "path")
        client_file_system = context.client_file_system
        path = _resolve_path(
            context,
            requested,
            must_exist=client_file_system is None,
        )
        if client_file_system is not None:
            if not client_file_system.supports_read:
                raise ToolError("ACP client does not support text-file reads")
            content = await client_file_system.read_text_file(
                path,
                line=start_line,
                limit=max_lines,
            )
            numbered = "\n".join(
                f"{number:>6}\t{line}"
                for number, line in enumerate(content.splitlines(), start=start_line)
            )
            if len(numbered.encode()) > context.output_byte_limit:
                numbered = numbered.encode()[: context.output_byte_limit].decode("utf-8", "ignore")
                numbered += "\n[output truncated]"
            _track_primary_workspace_path(context, path)
            return ToolResult(
                numbered,
                metadata={"path": str(path), "client_delegated": True},
            )

        if not path.is_file():
            raise ToolError(f"not a file: {path}")
        # Notify the instruction tracker so AGENTS.md files from root to this
        # directory are discovered for the next model step.
        _track_primary_workspace_path(context, path)

        def read() -> tuple[str, int]:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            selected = lines[start_line - 1 : start_line - 1 + max_lines]
            numbered = "\n".join(
                f"{number:>6}\t{line}" for number, line in enumerate(selected, start=start_line)
            )
            return numbered, len(lines)

        content, total_lines = await run_blocking(read)
        if len(content.encode()) > context.output_byte_limit:
            content = content.encode()[: context.output_byte_limit].decode("utf-8", "ignore")
            content += "\n[output truncated]"
        return ToolResult(content, metadata={"path": str(path), "total_lines": total_lines})


class ReadFilesTool:
    """Read a bounded ordered batch while isolating failures per file.

    按顺序读取一组有界文件,并隔离每个文件的失败。
    """

    definition = ToolDefinition(
        name="read_files",
        description=(
            "Read several known UTF-8 workspace files in one bounded request. "
            "Use it when multiple independent files are already identified; each "
            "file reports success or error independently."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_BATCH_READ_FILES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1},
                            "max_lines": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MAX_BATCH_READ_LINES_PER_FILE,
                            },
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["files"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw_files = arguments.get("files")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
            raise ToolError("files must be an array")
        files = tuple(raw_files)
        if not 1 <= len(files) <= MAX_BATCH_READ_FILES:
            raise ToolError(f"files must contain between 1 and {MAX_BATCH_READ_FILES} items")

        sections: list[str] = []
        succeeded = 0
        failed = 0
        client_file_system = context.client_file_system
        for index, value in enumerate(files):
            label = f"request {index + 1}"
            try:
                request = _parse_file_read_request(value, index=index)
                label = request.requested_path
                path = _resolve_path(
                    context,
                    request.requested_path,
                    must_exist=client_file_system is None,
                )
                label = _display_path(context, path)
                if client_file_system is not None:
                    if not client_file_system.supports_read:
                        raise ToolError("ACP client does not support text-file reads")
                    content = await client_file_system.read_text_file(
                        path,
                        line=request.start_line,
                        limit=request.max_lines,
                    )
                    numbered = _numbered_lines(content, start_line=request.start_line)
                else:
                    if not path.is_file():
                        raise ToolError(f"not a file: {path}")

                    def read_local(
                        path: Path = path,
                        request: _FileReadRequest = request,
                    ) -> str:
                        selected: list[str] = []
                        stop_line = request.start_line + request.max_lines
                        with path.open("r", encoding="utf-8") as file:
                            for line_number, line in enumerate(file, start=1):
                                if line_number < request.start_line:
                                    continue
                                if line_number >= stop_line:
                                    break
                                selected.append(line.rstrip("\r\n"))
                        return "\n".join(
                            f"{number:>6}\t{line}"
                            for number, line in enumerate(selected, start=request.start_line)
                        )

                    numbered = await run_blocking(read_local)
                _track_primary_workspace_path(context, path)
                sections.append(f"=== file: {label} ===\nstatus: success\n{numbered}")
                succeeded += 1
            except (KeyError, OSError, ToolError, UnicodeError) as error:
                sections.append(
                    f"=== file: {label} ===\nstatus: error\nerror: {type(error).__name__}: {error}"
                )
                failed += 1

        content, truncated = _safe_bounded_output("\n\n".join(sections), context)
        return ToolResult(
            content,
            is_error=succeeded == 0,
            metadata={
                "requested": len(files),
                "succeeded": succeeded,
                "failed": failed,
                "truncated": truncated,
                "client_delegated": client_file_system is not None,
            },
        )


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
        path = _resolve_path(context, raw_path, must_exist=True)
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
        root = _resolve_path(context, raw_path, must_exist=True)
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


class GrepTool:
    definition = ToolDefinition(
        name="grep",
        description=(
            "Search workspace file contents for one regular expression or fixed string. "
            "Use grep for one query; use grep_many for several independent queries. "
            "Use glob when searching paths rather than contents."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "include_globs": {"type": "array", "items": {"type": "string"}},
                "exclude_globs": {"type": "array", "items": {"type": "string"}},
                "fixed_strings": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": True},
                "names_only": {"type": "boolean", "default": False},
                "context": {"type": "integer", "minimum": 0, "maximum": MAX_GREP_CONTEXT_LINES},
                "before": {"type": "integer", "minimum": 0, "maximum": MAX_GREP_CONTEXT_LINES},
                "after": {"type": "integer", "minimum": 0, "maximum": MAX_GREP_CONTEXT_LINES},
                "max_matches_per_file": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_total_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        query = _require_string(arguments, "query")
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        _ensure_no_link_components(context, raw_path)
        include_globs = self._optional_globs(arguments.get("include_globs"), "include_globs")
        exclude_globs = self._optional_globs(arguments.get("exclude_globs"), "exclude_globs")
        fixed_strings = _require_bool(arguments, "fixed_strings", default=False)
        case_sensitive = _require_bool(arguments, "case_sensitive", default=True)
        names_only = _require_bool(arguments, "names_only", default=False)
        context_lines = _require_bounded_integer(
            arguments.get("context", 0),
            field_name="context",
            minimum=0,
            maximum=MAX_GREP_CONTEXT_LINES,
        )
        before = _require_bounded_integer(
            arguments.get("before", context_lines),
            field_name="before",
            minimum=0,
            maximum=MAX_GREP_CONTEXT_LINES,
        )
        after = _require_bounded_integer(
            arguments.get("after", context_lines),
            field_name="after",
            minimum=0,
            maximum=MAX_GREP_CONTEXT_LINES,
        )
        max_per_file = _require_bounded_integer(
            arguments.get("max_matches_per_file", 100),
            field_name="max_matches_per_file",
            minimum=1,
            maximum=100,
        )
        raw_limit = arguments.get("max_total_results", arguments.get("max_results", 200))
        max_total = _require_bounded_integer(
            raw_limit,
            field_name="max_total_results",
            minimum=1,
            maximum=1000,
        )
        root = _resolve_path(context, raw_path, must_exist=True)
        _track_primary_workspace_path(context, root)
        selector = _WorkspaceFileSelector(
            context,
            root,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
        )
        selection = await run_blocking(selector.select_files, max_files=MAX_GREP_SCANNED_FILES)
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern: re.Pattern[str] | None = None
        if not fixed_strings:
            try:
                pattern = re.compile(query, flags)
            except re.error as error:
                raise ToolError(f"invalid regular expression: {error}") from error

        def matches_line(line: str) -> bool:
            if fixed_strings:
                if case_sensitive:
                    return query in line
                return query.casefold() in line.casefold()
            assert pattern is not None
            return pattern.search(line) is not None

        def search() -> tuple[list[str], Path | None, int, bool, int]:
            rendered: list[str] = []
            last_matched_path: Path | None = None
            total_matches = 0
            result_limited = selection.scan_limited
            files_matched = 0
            for path in selection.files:
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError):
                    continue
                matching_lines = [
                    line_number
                    for line_number, line in enumerate(lines, start=1)
                    if matches_line(line)
                ][:max_per_file]
                if not matching_lines:
                    continue
                last_matched_path = path
                files_matched += 1
                if names_only:
                    rendered.append(_display_path(context, path))
                    total_matches += 1
                else:
                    for line_number in matching_lines:
                        if total_matches >= max_total:
                            result_limited = True
                            break
                        start = max(1, line_number - before)
                        end = min(len(lines), line_number + after)
                        for current in range(start, end + 1):
                            rendered.append(
                                f"{_display_path(context, path)}:{current}:{lines[current - 1]}"
                            )
                        total_matches += 1
                    if total_matches >= max_total:
                        result_limited = True
                if total_matches >= max_total:
                    break
            return rendered, last_matched_path, total_matches, result_limited, files_matched

        (
            matches,
            last_matched_path,
            total_matches,
            result_limited,
            files_matched,
        ) = await run_blocking(search)
        if last_matched_path is not None:
            _track_primary_workspace_path(context, last_matched_path)
        content, byte_limited = _safe_bounded_output("\n".join(matches), context)
        return ToolResult(
            content,
            metadata={
                "count": total_matches,
                "files_matched": files_matched,
                "scanned_files": len(selection.files),
                "scan_limited": selection.scan_limited,
                "result_limited": result_limited,
                "byte_limited": byte_limited,
                "names_only": names_only,
            },
        )

    @staticmethod
    def _optional_globs(value: object, field_name: str) -> tuple[str, ...]:
        if value is None:
            return ()
        return _require_string_sequence(
            value,
            field_name=field_name,
            minimum_items=0,
            maximum_items=MAX_GREP_GLOBS,
        )


class GrepManyTool:
    """Search several expressions over one deterministic bounded file set.

    在一个确定且有界的文件集合中搜索多个表达式。
    """

    definition = ToolDefinition(
        name="grep_many",
        description=(
            "Search workspace files for several independent regular expressions or fixed "
            "strings in one bounded request. Use grep for one query; use glob for path "
            "patterns. Results are grouped in query order."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_GREP_QUERIES,
                    "items": {"type": "string"},
                },
                "path": {"type": "string", "default": "."},
                "include_globs": {
                    "type": "array",
                    "maxItems": MAX_GREP_GLOBS,
                    "items": {"type": "string"},
                },
                "exclude_globs": {
                    "type": "array",
                    "maxItems": MAX_GREP_GLOBS,
                    "items": {"type": "string"},
                },
                "fixed_strings": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": True},
                "names_only": {"type": "boolean", "default": False},
                "max_results_per_query": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_GREP_RESULTS_PER_QUERY,
                },
                "max_total_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_GREP_TOTAL_RESULTS,
                },
            },
            "required": ["queries"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        queries = _require_string_sequence(
            arguments.get("queries"),
            field_name="queries",
            minimum_items=1,
            maximum_items=MAX_GREP_QUERIES,
        )
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        _ensure_no_link_components(context, raw_path)
        include_globs = self._optional_globs(arguments.get("include_globs"), "include_globs")
        exclude_globs = self._optional_globs(arguments.get("exclude_globs"), "exclude_globs")
        fixed_strings = _require_bool(arguments, "fixed_strings", default=False)
        case_sensitive = _require_bool(arguments, "case_sensitive", default=True)
        names_only = _require_bool(arguments, "names_only", default=False)
        max_results_per_query = _require_bounded_integer(
            arguments.get("max_results_per_query", 100),
            field_name="max_results_per_query",
            minimum=1,
            maximum=MAX_GREP_RESULTS_PER_QUERY,
        )
        max_total_results = _require_bounded_integer(
            arguments.get("max_total_results", 500),
            field_name="max_total_results",
            minimum=1,
            maximum=MAX_GREP_TOTAL_RESULTS,
        )
        root = _resolve_path(context, raw_path, must_exist=True)
        _track_primary_workspace_path(context, root)
        selector = _WorkspaceFileSelector(
            context,
            root,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            case_sensitive=case_sensitive,
        )
        selection = await run_blocking(selector.select_files, max_files=MAX_GREP_SCANNED_FILES)

        compiled: list[re.Pattern[str] | None] = []
        query_errors: list[str | None] = []
        flags = 0 if case_sensitive else re.IGNORECASE
        for query in queries:
            try:
                compiled.append(None if fixed_strings else re.compile(query, flags))
                query_errors.append(None)
            except re.error as error:
                compiled.append(None)
                query_errors.append(f"invalid regular expression: {error}")

        def search() -> tuple[list[list[str]], int, int, bool, bool, Path | None]:
            matches: list[list[str]] = [[] for _query in queries]
            total_matches = 0
            unreadable_files = 0
            total_limited = False
            last_matched_path: Path | None = None
            for path in selection.files:
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                    for line_number, line in enumerate(lines, start=1):
                        for query_index, pattern in enumerate(compiled):
                            if len(matches[query_index]) >= max_results_per_query:
                                continue
                            if pattern is None:
                                matched = (
                                    query_index < len(query_errors)
                                    and query_errors[query_index] is None
                                    and (
                                        queries[query_index] in line
                                        if case_sensitive
                                        else queries[query_index].casefold() in line.casefold()
                                    )
                                )
                            else:
                                matched = pattern.search(line) is not None
                            if not matched:
                                continue
                            display_path = _display_path(context, path)
                            matches[query_index].append(
                                display_path
                                if names_only
                                else f"{display_path}:{line_number}:{line.rstrip()}"
                            )
                            last_matched_path = path
                            total_matches += 1
                            if total_matches >= max_total_results:
                                total_limited = True
                                break
                        if total_limited:
                            break
                    if total_limited:
                        break
                except (OSError, UnicodeError):
                    unreadable_files += 1
            return (
                matches,
                len(selection.files),
                unreadable_files,
                selection.scan_limited,
                total_limited,
                last_matched_path,
            )

        (
            matches,
            scanned_files,
            unreadable_files,
            scan_limited,
            total_limited,
            last_matched_path,
        ) = await run_blocking(search)
        if last_matched_path is not None:
            _track_primary_workspace_path(context, last_matched_path)

        sections: list[str] = []
        for index, query in enumerate(queries):
            header = f"=== query {index + 1}: {query!r} ==="
            if query_errors[index] is not None:
                sections.append(f"{header}\nstatus: error\nerror: {query_errors[index]}")
                continue
            rendered_matches = "\n".join(matches[index]) or "[no matches]"
            sections.append(f"{header}\nstatus: success\n{rendered_matches}")
        if scan_limited or total_limited:
            limits: list[str] = []
            if scan_limited:
                limits.append(f"file scan limit {MAX_GREP_SCANNED_FILES}")
            if total_limited:
                limits.append(f"total result limit {max_total_results}")
            sections.append(f"[search truncated: {', '.join(limits)}]")
        content, byte_limited = _safe_bounded_output("\n\n".join(sections), context)
        match_count = sum(len(query_matches) for query_matches in matches)
        valid_queries = sum(error is None for error in query_errors)
        return ToolResult(
            content,
            is_error=valid_queries == 0,
            metadata={
                "query_count": len(queries),
                "valid_queries": valid_queries,
                "match_count": match_count,
                "scanned_files": scanned_files,
                "unreadable_files": unreadable_files,
                "scan_limited": scan_limited,
                "result_limited": total_limited,
                "byte_limited": byte_limited,
                "fixed_strings": fixed_strings,
                "case_sensitive": case_sensitive,
                "names_only": names_only,
            },
        )

    @staticmethod
    def _optional_globs(value: object, field_name: str) -> tuple[str, ...]:
        if value is None:
            return ()
        return _require_string_sequence(
            value,
            field_name=field_name,
            minimum_items=0,
            maximum_items=MAX_GREP_GLOBS,
        )


@dataclass(frozen=True, slots=True)
class _PatchHunk:
    old_start: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PatchOperation:
    kind: str
    path: str
    move_to: str | None = None
    add_lines: tuple[str, ...] = ()
    hunks: tuple[_PatchHunk, ...] = ()


_PATCH_HEADER_PREFIXES = (
    "*** Add File: ",
    "*** Update File: ",
    "*** Delete File: ",
)
_PATCH_HUNK_HEADER = re.compile(r"^(?:@@|@@(?: -(\d+)(?:,\d+)?)?(?: \+\d+(?:,\d+)?)? @@(?:.*))$")
MAX_APPLY_PATCH_BYTES = 2 * 1024 * 1024
MAX_APPLY_PATCH_FILE_BYTES = 8 * 1024 * 1024


def _patch_path(header: str, prefix: str) -> str:
    path = header[len(prefix) :].strip()
    if not path or "\x00" in path:
        raise ToolError("patch file path must be a non-empty text path")
    return path


def _parse_patch_hunk_header(header: str) -> int:
    match = _PATCH_HUNK_HEADER.match(header)
    if match is None:
        raise ToolError("malformed patch hunk header")
    return int(match.group(1) or "1")


def _parse_patch(patch: str) -> tuple[_PatchOperation, ...]:
    if len(patch.encode("utf-8")) > MAX_APPLY_PATCH_BYTES:
        raise ToolError(f"patch must be at most {MAX_APPLY_PATCH_BYTES} bytes")
    lines = patch.splitlines()
    if len(lines) < 2 or lines[0].strip() != "*** Begin Patch":
        raise ToolError("patch must start with *** Begin Patch")
    if "*** End Patch" not in lines:
        raise ToolError("patch must end with *** End Patch")
    operations: list[_PatchOperation] = []
    index = 1
    while index < len(lines):
        header = lines[index]
        if header == "*** End Patch":
            if any(line.strip() for line in lines[index + 1 :]):
                raise ToolError("unexpected content after *** End Patch")
            break
        prefix = next((value for value in _PATCH_HEADER_PREFIXES if header.startswith(value)), None)
        if prefix is None:
            raise ToolError(f"unexpected patch line {index + 1}")
        path = _patch_path(header, prefix)
        kind = prefix.removeprefix("*** ").removesuffix(" File: ").lower()
        index += 1
        if kind == "add":
            added: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                line = lines[index]
                if not line.startswith("+"):
                    raise ToolError(f"add-file content line {index + 1} must start with +")
                added.append(line[1:])
                index += 1
            operations.append(_PatchOperation("add", path, add_lines=tuple(added)))
            continue
        if kind == "delete":
            if index < len(lines) and not lines[index].startswith("*** "):
                raise ToolError(f"delete-file operation at line {index + 1} has unexpected content")
            operations.append(_PatchOperation("delete", path))
            continue

        move_to: str | None = None
        if index < len(lines) and lines[index].startswith("*** Move to: "):
            move_to = _patch_path(lines[index], "*** Move to: ")
            index += 1
        hunks: list[_PatchHunk] = []
        while index < len(lines) and not lines[index].startswith("*** "):
            header_line = lines[index]
            if not header_line.startswith("@@"):
                raise ToolError(f"update-file content at line {index + 1} must start with @@")
            old_start = _parse_patch_hunk_header(header_line)
            index += 1
            hunk_lines: list[str] = []
            while (
                index < len(lines)
                and not lines[index].startswith("@@")
                and not lines[index].startswith("*** ")
            ):
                hunk_line = lines[index]
                if hunk_line == "\\ No newline at end of file":
                    index += 1
                    continue
                if not hunk_line or hunk_line[0] not in " +-":
                    raise ToolError(f"malformed patch hunk line {index + 1}")
                hunk_lines.append(hunk_line)
                index += 1
            if not hunk_lines:
                raise ToolError(f"patch hunk at line {index + 1} must not be empty")
            hunks.append(_PatchHunk(old_start, tuple(hunk_lines)))
        if not hunks and move_to is None:
            raise ToolError(f"update operation for {path!r} has no hunks")
        operations.append(_PatchOperation("update", path, move_to=move_to, hunks=tuple(hunks)))
    if not operations:
        raise ToolError("patch must contain at least one file operation")
    return tuple(operations)


def _apply_patch_hunks(original: str, hunks: tuple[_PatchHunk, ...]) -> str:
    if len(original.encode("utf-8")) > MAX_APPLY_PATCH_FILE_BYTES:
        raise ToolError(f"target file exceeds {MAX_APPLY_PATCH_FILE_BYTES} bytes")
    newline = "\r\n" if "\r\n" in original else "\n"
    original_lines = original.splitlines()
    had_final_newline = original.endswith(("\n", "\r"))
    current = list(original_lines)
    for hunk in hunks:
        old_lines = tuple(line[1:] for line in hunk.lines if line[0] in " -")
        new_lines = tuple(line[1:] for line in hunk.lines if line[0] in " +")
        if not old_lines:
            # A hunk containing only ``+`` lines is a valid insertion.  The
            # location is still validated against the bounded old-line index,
            # but no existing text is required to match.
            expected = min(max(hunk.old_start, 0), len(current))
            if expected > len(current):
                raise ToolError("patch hunk insertion point is outside the current file")
            current[expected:expected] = new_lines
            continue
        expected = min(max(hunk.old_start - 1, 0), len(current))
        candidates = (
            [expected] if current[expected : expected + len(old_lines)] == list(old_lines) else []
        )
        if not candidates:
            candidates = [
                offset
                for offset in range(len(current) - len(old_lines) + 1)
                if tuple(current[offset : offset + len(old_lines)]) == old_lines
            ]
        if len(candidates) != 1:
            raise ToolError("patch hunk does not match the current file exactly")
        offset = candidates[0]
        current[offset : offset + len(old_lines)] = new_lines
    rendered = newline.join(current)
    return rendered + (newline if had_final_newline else "")


def _add_file_content(lines: tuple[str, ...]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


def _ensure_no_link_components(context: ToolContext, requested: str) -> None:
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = context.cwd / candidate
    for parent in reversed(candidate.parents):
        if _is_link_like(parent):
            raise ToolError("patch paths must not traverse symlinks or junctions")
    if _is_link_like(candidate):
        raise ToolError("patch paths must not target symlinks or junctions")


class ApplyPatchTool:
    """Apply a validated multi-file patch as one local filesystem mutation."""

    definition = ToolDefinition(
        name="apply_patch",
        description=(
            "Apply a structured workspace patch that may add, update, delete, or move files. "
            "Use it for structural, multi-hunk, or multi-file edits; use search_replace only "
            "for one known exact replacement. Patch content is validated before any write."
        ),
        input_schema={
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    def workspace_target_paths(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        """Return patch source and destination paths without touching the workspace."""

        patch = arguments.get("patch")
        if not isinstance(patch, str):
            return ()
        try:
            operations = _parse_patch(patch)
        except ToolError:
            return ()
        paths: list[str] = []
        for operation in operations:
            paths.append(operation.path)
            if operation.move_to is not None:
                paths.append(operation.move_to)
        return tuple(dict.fromkeys(paths))

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        patch = _require_string(arguments, "patch")
        operations = _parse_patch(patch)
        if not context.sandbox_profile.workspace_writable:
            raise ToolError(
                f"sandbox profile {context.sandbox_profile.value!r} prohibits workspace edits"
            )
        resolved = self._resolve_operations(operations, context)
        preflight = self._instruction_preflight(resolved, context)
        if preflight is not None:
            path, instructions_text = preflight
            return ToolResult(
                "I discovered project instructions in the target directory "
                f"that you haven't seen yet ({_display_path(context, path)}). "
                "Please review them before proceeding with the write. "
                "Re-issue the command if you wish to proceed.\n\n" + instructions_text,
                is_error=True,
                metadata={"preflight": "new_instructions", "path": str(path)},
            )
        client = context.client_file_system
        if client is not None:
            return await self._execute_client(resolved, client, context)
        return await run_blocking(self._execute_local, resolved, context)

    def _resolve_operations(
        self,
        operations: tuple[_PatchOperation, ...],
        context: ToolContext,
    ) -> tuple[tuple[_PatchOperation, Path, Path | None], ...]:
        resolved: list[tuple[_PatchOperation, Path, Path | None]] = []
        occupied: set[Path] = set()
        for operation in operations:
            _ensure_no_link_components(context, operation.path)
            source = _resolve_path(
                context,
                operation.path,
                must_exist=operation.kind != "add" and context.client_file_system is None,
            )
            destination: Path | None = None
            if operation.move_to is not None:
                _ensure_no_link_components(context, operation.move_to)
                destination = _resolve_path(context, operation.move_to, must_exist=False)
            targets = (source, destination) if destination is not None else (source,)
            for target in targets:
                if target is None or target in occupied:
                    raise ToolError("patch contains duplicate or overlapping file targets")
                occupied.add(target)
                if (
                    not _is_primary_workspace_path(context, target)
                    and context.sandbox_profile is not SandboxProfile.OFF
                ):
                    raise ToolError(
                        "sandboxed sessions permit only read access to additional workspace directories"
                    )
                if context.client_file_system is None and not target.parent.is_dir():
                    raise ToolError(f"patch target parent is not a directory: {target.parent}")
            if operation.kind == "add":
                if context.client_file_system is None and source.exists():
                    raise ToolError(f"cannot add existing file: {_display_path(context, source)}")
            elif context.client_file_system is None and not source.is_file():
                raise ToolError(f"patch source is not a file: {_display_path(context, source)}")
            if (
                destination is not None
                and context.client_file_system is None
                and destination.exists()
            ):
                raise ToolError(
                    f"move destination already exists: {_display_path(context, destination)}"
                )
            resolved.append((operation, source, destination))
        return tuple(resolved)

    @staticmethod
    def _instruction_preflight(
        operations: tuple[tuple[_PatchOperation, Path, Path | None], ...],
        context: ToolContext,
    ) -> tuple[Path, str] | None:
        if context.instruction_tracker is None:
            return None
        checked: set[Path] = set()
        for _operation, source, destination in operations:
            for path in (source, destination):
                if path is None or path in checked or not _is_primary_workspace_path(context, path):
                    continue
                checked.add(path)
                discovered = context.instruction_tracker.check_path_for_write(path)
                if discovered is not None:
                    return path, discovered.model_context_text()
        return None

    async def _execute_client(
        self,
        operations: tuple[tuple[_PatchOperation, Path, Path | None], ...],
        client: ClientFileSystem,
        context: ToolContext,
    ) -> ToolResult:
        if not (client.supports_read and client.supports_write):
            raise ToolError("ACP client does not support patch text-file reads and writes")
        if (
            len(operations) != 1
            or operations[0][0].kind != "update"
            or operations[0][2] is not None
        ):
            raise ToolError(
                "ACP delegated filesystem supports only one-file update patches; "
                "add/delete/move and multi-file transactions are unavailable"
            )
        operation, path, _destination = operations[0]
        try:
            original = await client.read_text_file(path)
        except Exception as error:
            raise ToolError("ACP client could not read the patch target") from error
        updated = _apply_patch_hunks(original, operation.hunks)
        try:
            await client.write_text_file(path, updated)
        except Exception as error:
            raise ToolError("ACP client could not write the patch target") from error
        return ToolResult(
            f"updated {_display_path(context, path)}",
            metadata={
                "changed_files": [_display_path(context, path)],
                "added_files": [],
                "deleted_files": [],
                "moved_files": [],
                "hunks_applied": len(operation.hunks),
                "client_delegated": True,
                "truncated": False,
            },
        )

    @staticmethod
    def _execute_local(
        operations: tuple[tuple[_PatchOperation, Path, Path | None], ...],
        context: ToolContext,
    ) -> ToolResult:
        originals: dict[Path, bytes | None] = {}
        modes: dict[Path, int] = {}
        prepared: dict[Path, tuple[bytes, int]] = {}
        changed: list[str] = []
        added: list[str] = []
        deleted: list[str] = []
        moved: list[dict[str, str]] = []
        hunks_applied = 0
        affected: set[Path] = set()
        for operation, source, destination in operations:
            affected.add(source)
            if destination is not None:
                affected.add(destination)
            if source.exists():
                originals[source] = source.read_bytes()
                modes[source] = source.stat().st_mode
            else:
                originals[source] = None
            if destination is not None:
                originals.setdefault(destination, None)
            if operation.kind == "add":
                added_content = _add_file_content(operation.add_lines)
                prepared[source] = (added_content.encode("utf-8"), 0o100644)
                added.append(_display_path(context, source))
                changed.append(_display_path(context, source))
                continue
            original_bytes = originals[source]
            if original_bytes is None:
                raise ToolError(f"patch source disappeared: {_display_path(context, source)}")
            try:
                original = original_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ToolError(
                    f"patch source is not UTF-8 text: {_display_path(context, source)}"
                ) from error
            if operation.kind == "delete":
                deleted.append(_display_path(context, source))
                changed.append(_display_path(context, source))
                continue
            updated = _apply_patch_hunks(original, operation.hunks)
            hunks_applied += len(operation.hunks)
            target = destination or source
            prepared[target] = (updated.encode("utf-8"), modes[source])
            if destination is None:
                changed.append(_display_path(context, source))
            else:
                moved.append(
                    {
                        "from": _display_path(context, source),
                        "to": _display_path(context, destination),
                    }
                )
                deleted.append(_display_path(context, source))
                changed.extend(
                    (_display_path(context, source), _display_path(context, destination))
                )

        temporary_paths: list[Path] = []

        def stage(path: Path, content: bytes, mode: int) -> Path:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".patch.tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.chmod(temporary_path, mode)
            temporary_paths.append(temporary_path)
            return temporary_path

        staged: dict[Path, Path] = {}
        try:
            for path, (content, mode) in prepared.items():
                staged[path] = stage(path, content, mode)
            for path, temporary in staged.items():
                os.replace(temporary, path)
            for operation, source, destination in operations:
                if operation.kind == "delete" or destination is not None:
                    source.unlink()
        except BaseException as error:
            for path in affected:
                original_bytes_for_restore = originals.get(path)
                try:
                    if original_bytes_for_restore is None:
                        if path.exists():
                            path.unlink()
                    else:
                        restore = stage(
                            path,
                            original_bytes_for_restore,
                            modes.get(path, 0o100644),
                        )
                        os.replace(restore, path)
                except OSError:
                    pass
            raise ToolError("patch transaction failed and was rolled back") from error
        finally:
            for temporary in temporary_paths:
                with suppress(OSError):
                    temporary.unlink()
        return ToolResult(
            f"applied patch to {len(changed)} file change(s)",
            metadata={
                "changed_files": list(dict.fromkeys(changed)),
                "added_files": added,
                "deleted_files": deleted,
                "moved_files": moved,
                "hunks_applied": hunks_applied,
                "truncated": False,
                "client_delegated": False,
            },
        )


class SearchReplaceTool:
    definition = ToolDefinition(
        name="search_replace",
        description=(
            "Replace a known exact text occurrence in one UTF-8 workspace file atomically. "
            "Use apply_patch for structural, multi-hunk, multi-file, add, delete, or move edits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    def workspace_target_paths(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        path = arguments.get("path")
        return (path,) if isinstance(path, str) and path else ()

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not context.sandbox_profile.workspace_writable:
            raise ToolError(
                f"sandbox profile {context.sandbox_profile.value!r} prohibits workspace edits"
            )
        client_file_system = context.client_file_system
        path = _resolve_path(
            context,
            _require_string(arguments, "path"),
            must_exist=client_file_system is None,
        )
        old = _require_string(arguments, "old")
        new = arguments.get("new")
        replace_all = arguments.get("replace_all", False)
        if not isinstance(new, str):
            raise ToolError("new must be a string")
        if not isinstance(replace_all, bool):
            raise ToolError("replace_all must be a boolean")
        if client_file_system is None and not path.is_file():
            raise ToolError(f"not a file: {path}")
        if (
            _is_primary_workspace_path(context, path) is False
            and context.sandbox_profile is not SandboxProfile.OFF
        ):
            raise ToolError(
                "sandboxed sessions permit only read access to additional workspace directories"
            )
        if context.instruction_tracker is not None and _is_primary_workspace_path(context, path):
            new_instructions = context.instruction_tracker.check_path_for_write(path)
            if new_instructions is not None:
                instructions_text = new_instructions.model_context_text()
                rel = _display_path(context, path)
                return ToolResult(
                    "I discovered project instructions in the target directory "
                    f"that you haven't seen yet ({rel}). "
                    "Please review them before proceeding with the write. "
                    "Re-issue the command if you wish to proceed.\n\n" + instructions_text,
                    is_error=True,
                    metadata={"path": str(path), "preflight": "new_instructions"},
                )

        async def replace_client_text(file_system: ClientFileSystem) -> int:
            if not (file_system.supports_read and file_system.supports_write):
                raise ToolError("ACP client does not support text-file replacement")
            original = await file_system.read_text_file(path)
            count = original.count(old)
            if count == 0:
                raise ToolError("old text was not found")
            if count > 1 and not replace_all:
                raise ToolError(f"old text is ambiguous: found {count} occurrences")
            updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
            await file_system.write_text_file(path, updated)
            return count if replace_all else 1

        def replace_text() -> int:
            original = path.read_text(encoding="utf-8")
            count = original.count(old)
            if count == 0:
                raise ToolError("old text was not found")
            if count > 1 and not replace_all:
                raise ToolError(f"old text is ambiguous: found {count} occurrences")
            updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
            mode = path.stat().st_mode
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary.write(updated)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_name = temporary.name
                os.chmod(temporary_name, mode)
                os.replace(temporary_name, path)
            finally:
                if temporary_name is not None and os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            return count if replace_all else 1

        if client_file_system is None:
            replaced = await run_blocking(replace_text)
        else:
            replaced = await replace_client_text(client_file_system)
        return ToolResult(
            f"replaced {replaced} occurrence(s) in {_display_path(context, path)}",
            metadata={
                "path": str(path),
                "replacements": replaced,
                "client_delegated": client_file_system is not None,
            },
        )


__all__ = [
    "ApplyPatchTool",
    "GlobTool",
    "GrepManyTool",
    "GrepTool",
    "ListDirTool",
    "ListTreeTool",
    "ReadFileTool",
    "ReadFilesTool",
    "SearchReplaceTool",
]
