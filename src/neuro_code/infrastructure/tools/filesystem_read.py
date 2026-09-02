"""Read-only file query tools.

This module owns targeted single-file and bounded batch UTF-8 reads.
只读文件查询工具. 本模块拥有单文件与有界批量 UTF-8 读取.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    _numbered_lines,
    _safe_bounded_output,
)
from neuro_code.infrastructure.tools.filesystem_security import (
    _display_path,
    _prepare_local_targets,
    _require_bounded_integer,
    _require_string,
    _resolve_path,
    _track_primary_workspace_path,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError

MAX_BATCH_READ_FILES = 16
MAX_BATCH_READ_LINES_PER_FILE = 5000


@dataclass(frozen=True, slots=True)
class _FileReadRequest:
    requested_path: str
    start_line: int
    max_lines: int


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

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        requested = _require_string(arguments, "path")
        return _prepare_local_targets(
            "read_file",
            context,
            (FilesystemTargetRequest(requested, FilesystemAccessOperation.READ, must_exist=True),),
        )

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
            operation=FilesystemAccessOperation.READ,
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

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        raw_files = arguments.get("files")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
            raise ToolError("files must be an array")
        files = tuple(raw_files)
        if not 1 <= len(files) <= MAX_BATCH_READ_FILES:
            raise ToolError(f"files must contain between 1 and {MAX_BATCH_READ_FILES} items")
        requests = tuple(
            FilesystemTargetRequest(
                _parse_file_read_request(value, index=index).requested_path,
                FilesystemAccessOperation.READ,
                must_exist=False,
            )
            for index, value in enumerate(files)
        )
        return _prepare_local_targets("read_files", context, requests)

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
                    operation=FilesystemAccessOperation.READ,
                    target_index=index,
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


__all__ = ["ReadFileTool", "ReadFilesTool"]
