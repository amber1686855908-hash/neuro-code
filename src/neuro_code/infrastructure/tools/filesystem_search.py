"""Bounded workspace content search tools.

本模块拥有 grep/grep_many 的查询、匹配、上下文和结果限制语义.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
    FilesystemTargetRequest,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.tools.filesystem_discovery import _WorkspaceFileSelector
from neuro_code.infrastructure.tools.filesystem_output import _safe_bounded_output
from neuro_code.infrastructure.tools.filesystem_security import (
    _display_path,
    _ensure_no_link_components,
    _prepare_local_targets,
    _require_bool,
    _require_bounded_integer,
    _require_string,
    _require_string_sequence,
    _resolve_path,
    _track_primary_workspace_path,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError

MAX_GREP_QUERIES = 16
MAX_GREP_GLOBS = 32
MAX_GREP_RESULTS_PER_QUERY = 200
MAX_GREP_TOTAL_RESULTS = 1000
MAX_GREP_SCANNED_FILES = 20_000
MAX_GREP_CONTEXT_LINES = 20


def _optional_globs(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return _require_string_sequence(
        value,
        field_name=field_name,
        minimum_items=0,
        maximum_items=MAX_GREP_GLOBS,
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
            "grep",
            context,
            (FilesystemTargetRequest(raw_path, FilesystemAccessOperation.SEARCH, must_exist=True),),
        )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        query = _require_string(arguments, "query")
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ToolError("path must be a string")
        _ensure_no_link_components(context, raw_path)
        include_globs = _optional_globs(arguments.get("include_globs"), "include_globs")
        exclude_globs = _optional_globs(arguments.get("exclude_globs"), "exclude_globs")
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
        root = _resolve_path(
            context,
            raw_path,
            must_exist=True,
            operation=FilesystemAccessOperation.SEARCH,
        )
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
            "grep_many",
            context,
            (FilesystemTargetRequest(raw_path, FilesystemAccessOperation.SEARCH, must_exist=True),),
        )

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
        include_globs = _optional_globs(arguments.get("include_globs"), "include_globs")
        exclude_globs = _optional_globs(arguments.get("exclude_globs"), "exclude_globs")
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
        root = _resolve_path(
            context,
            raw_path,
            must_exist=True,
            operation=FilesystemAccessOperation.SEARCH,
        )
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


__all__ = ["GrepManyTool", "GrepTool"]
