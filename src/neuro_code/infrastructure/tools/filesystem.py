"""Canonical filesystem tool infrastructure adapters.

This module owns the bounded readers and the atomic ``search_replace`` writer.
The legacy ``neuro_code.tools.filesystem`` path remains a compatibility facade;
all path, instruction-tracker, sandbox, client-filesystem, and write semantics
have one implementation owner here.

定义规范的文件系统工具基础设施适配器. 本模块拥有有界读取器和原子 search_replace 写入器.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
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


__all__ = ["GrepTool", "ListDirTool", "ReadFileTool", "SearchReplaceTool"]
