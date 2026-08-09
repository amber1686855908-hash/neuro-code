"""Canonical filesystem tool infrastructure adapters.

This module owns the bounded readers and the atomic ``search_replace`` writer.
The retired ``neuro_code.tools.filesystem`` facade has been removed. Path,
instruction-tracker, sandbox, client-filesystem, and write semantics have one
implementation owner here.

定义规范的文件系统工具基础设施适配器. 本模块拥有有界读取器和原子 search_replace 写入器.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
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

_DEFAULT_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
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
        description="Read a UTF-8 text file from the current workspace.",
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
            "Read several UTF-8 workspace files in one bounded request. "
            "Each file reports success or error independently."
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
        description="List files and directories directly beneath a workspace path.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
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
            "dependency, cache, and build directories."
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
            },
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
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
        root = _resolve_path(context, raw_path, must_exist=True)
        if not root.is_dir():
            raise ToolError(f"not a directory: {root}")
        _track_primary_workspace_path(context, root)

        def build_tree() -> tuple[list[str], bool, int, int]:
            entries: list[str] = []
            entry_limited = False
            ignored_directories = 0
            ignored_links = 0

            def visit(directory: Path, depth: int) -> None:
                nonlocal entry_limited, ignored_directories, ignored_links
                try:
                    children = sorted(
                        directory.iterdir(),
                        key=lambda item: (item.name.casefold(), item.name),
                    )
                except OSError:
                    return
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
                    suffix = "/" if is_directory else ""
                    entries.append(f"{'  ' * (depth - 1)}{child.name}{suffix}")
                    if is_directory and depth < max_depth:
                        visit(child, depth + 1)
                        if entry_limited:
                            return

            visit(root, 1)
            return entries, entry_limited, ignored_directories, ignored_links

        entries, entry_limited, ignored_directories, ignored_links = await run_blocking(build_tree)
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
            },
        )


class GrepTool:
    definition = ToolDefinition(
        name="grep",
        description="Search UTF-8 workspace files with a regular expression.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
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
        max_results = arguments.get("max_results", 200)
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        if not isinstance(max_results, int) or not 1 <= max_results <= 1000:
            raise ToolError("max_results must be between 1 and 1000")
        try:
            pattern = re.compile(query)
        except re.error as error:
            raise ToolError(f"invalid regular expression: {error}") from error
        root = _resolve_path(context, raw_path, must_exist=True)
        _track_primary_workspace_path(context, root)

        def search() -> tuple[list[str], Path | None]:
            paths = (root,) if root.is_file() else root.rglob("*")
            matches: list[str] = []
            last_matched_path: Path | None = None
            for path in paths:
                if not path.is_file() or ".git" in path.parts:
                    continue
                try:
                    with path.open("r", encoding="utf-8") as file:
                        for line_number, line in enumerate(file, start=1):
                            if pattern.search(line):
                                display_path = _display_path(context, path)
                                matches.append(f"{display_path}:{line_number}:{line.rstrip()}")
                                last_matched_path = path
                                if len(matches) >= max_results:
                                    return matches, last_matched_path
                except (OSError, UnicodeError):
                    continue
            return matches, last_matched_path

        matches, last_matched_path = await run_blocking(search)
        # Trackers are binding-local mutable state. Update them on the event
        # loop after the blocking filesystem walk rather than from a worker
        # thread. The last match retains the documented single-target policy.
        if last_matched_path is not None:
            _track_primary_workspace_path(context, last_matched_path)
        return ToolResult("\n".join(matches), metadata={"count": len(matches)})


class GrepManyTool:
    """Search several expressions over one deterministic bounded file set.

    在一个确定且有界的文件集合中搜索多个表达式。
    """

    definition = ToolDefinition(
        name="grep_many",
        description=(
            "Search workspace files for several regular expressions in one bounded request. "
            "Results are grouped in query order."
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
        include_globs = self._optional_globs(arguments.get("include_globs"), "include_globs")
        exclude_globs = self._optional_globs(arguments.get("exclude_globs"), "exclude_globs")
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

        compiled: list[re.Pattern[str] | None] = []
        query_errors: list[str | None] = []
        for query in queries:
            try:
                compiled.append(re.compile(query))
                query_errors.append(None)
            except re.error as error:
                compiled.append(None)
                query_errors.append(f"invalid regular expression: {error}")

        def search() -> tuple[list[list[str]], int, int, bool, bool, Path | None]:
            paths, scan_limited = self._search_paths(
                root,
                include_globs=include_globs,
                exclude_globs=exclude_globs,
            )
            matches: list[list[str]] = [[] for _query in queries]
            total_matches = 0
            unreadable_files = 0
            total_limited = False
            last_matched_path: Path | None = None
            for path in paths:
                try:
                    with path.open("r", encoding="utf-8") as file:
                        for line_number, line in enumerate(file, start=1):
                            for query_index, pattern in enumerate(compiled):
                                if (
                                    pattern is None
                                    or len(matches[query_index]) >= max_results_per_query
                                ):
                                    continue
                                if pattern.search(line) is None:
                                    continue
                                display_path = _display_path(context, path)
                                matches[query_index].append(
                                    f"{display_path}:{line_number}:{line.rstrip()}"
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
                len(paths),
                unreadable_files,
                scan_limited,
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

    @staticmethod
    def _search_paths(
        root: Path,
        *,
        include_globs: tuple[str, ...],
        exclude_globs: tuple[str, ...],
    ) -> tuple[list[Path], bool]:
        base = root if root.is_dir() else root.parent
        paths: list[Path] = []
        limited = False

        def included(path: Path) -> bool:
            relative = path.relative_to(base)
            if include_globs and not any(relative.match(pattern) for pattern in include_globs):
                return False
            return not any(relative.match(pattern) for pattern in exclude_globs)

        def add(path: Path) -> bool:
            nonlocal limited
            if not included(path):
                return True
            if len(paths) >= MAX_GREP_SCANNED_FILES:
                limited = True
                return False
            paths.append(path)
            return True

        def visit(directory: Path) -> bool:
            try:
                children = sorted(
                    directory.iterdir(),
                    key=lambda item: (item.name.casefold(), item.name),
                )
            except OSError:
                return True
            for child in children:
                if _is_link_like(child):
                    continue
                try:
                    is_directory = child.is_dir()
                except OSError:
                    continue
                if is_directory:
                    if child.name.casefold() in _DEFAULT_IGNORED_DIRECTORY_NAMES:
                        continue
                    if not visit(child):
                        return False
                elif child.is_file() and not add(child):
                    return False
            return True

        if root.is_file():
            if not _is_link_like(root):
                add(root)
        elif root.is_dir():
            visit(root)
        else:
            raise ToolError(f"not a file or directory: {root}")
        return paths, limited


class SearchReplaceTool:
    definition = ToolDefinition(
        name="search_replace",
        description="Replace an exact text occurrence in a UTF-8 workspace file atomically.",
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
    "GrepManyTool",
    "GrepTool",
    "ListDirTool",
    "ListTreeTool",
    "ReadFileTool",
    "ReadFilesTool",
    "SearchReplaceTool",
]
