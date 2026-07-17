from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from typing import Any

from neuro_code.async_utils import run_blocking
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.errors import ToolError
from neuro_code.ports.tools import ToolContext
from neuro_code.workspace import resolve_workspace_path


def _require_string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{key} must be a non-empty string")
    return value


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
        path = resolve_workspace_path(
            context.cwd, _require_string(arguments, "path"), must_exist=True
        )
        if not path.is_file():
            raise ToolError(f"not a file: {path}")
        start_line = arguments.get("start_line", 1)
        max_lines = arguments.get("max_lines", 500)
        if not isinstance(start_line, int) or start_line < 1:
            raise ToolError("start_line must be a positive integer")
        if not isinstance(max_lines, int) or not 1 <= max_lines <= 5000:
            raise ToolError("max_lines must be between 1 and 5000")

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
        path = resolve_workspace_path(context.cwd, raw_path, must_exist=True)
        if not path.is_dir():
            raise ToolError(f"not a directory: {path}")

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
        root = resolve_workspace_path(context.cwd, raw_path, must_exist=True)

        def search() -> list[str]:
            paths = (root,) if root.is_file() else root.rglob("*")
            matches: list[str] = []
            for path in paths:
                if not path.is_file() or ".git" in path.parts:
                    continue
                try:
                    with path.open("r", encoding="utf-8") as file:
                        for line_number, line in enumerate(file, start=1):
                            if pattern.search(line):
                                relative = path.relative_to(context.cwd.resolve())
                                matches.append(f"{relative}:{line_number}:{line.rstrip()}")
                                if len(matches) >= max_results:
                                    return matches
                except (OSError, UnicodeError):
                    continue
            return matches

        matches = await run_blocking(search)
        return ToolResult("\n".join(matches), metadata={"count": len(matches)})


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
        path = resolve_workspace_path(
            context.cwd, _require_string(arguments, "path"), must_exist=True
        )
        old = _require_string(arguments, "old")
        new = arguments.get("new")
        replace_all = arguments.get("replace_all", False)
        if not isinstance(new, str):
            raise ToolError("new must be a string")
        if not isinstance(replace_all, bool):
            raise ToolError("replace_all must be a boolean")
        if not path.is_file():
            raise ToolError(f"not a file: {path}")

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

        replaced = await run_blocking(replace_text)
        return ToolResult(
            f"replaced {replaced} occurrence(s) in {path.relative_to(context.cwd.resolve())}",
            metadata={"path": str(path), "replacements": replaced},
        )
