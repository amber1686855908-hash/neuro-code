"""Local attached interactive-terminal tools.

This module is the infrastructure owner of the model-visible local terminal
tool family.  It validates bounded model arguments and delegates every
operation to the application-owned ``InteractiveTerminalManager`` port; it
does not own process lifecycle, workspace security, or permission policy.

本模块负责模型可见的本地附加终端工具.它只校验有界参数并委托应用端口,不拥有进程
生命周期、工作区安全或权限策略.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.terminal import (
    InteractiveTerminalSession,
    TerminalCreationAuthorization,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.terminal.models import (
    DEFAULT_TERMINAL_OUTPUT_CAPACITY,
    MAX_TERMINAL_DIMENSION,
    MAX_TERMINAL_OUTPUT_BYTES,
    MAX_TERMINAL_READ_BYTES,
    MAX_TERMINAL_WRITE_BYTES,
    TerminalSize,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import TerminalError, ToolError

_MAX_ARGUMENTS = 64
_MAX_ARGUMENT_BYTES = 4 * 1024
_MAX_ARGUMENT_TOTAL_BYTES = 32 * 1024
_MAX_CWD_BYTES = 4 * 1024
_MAX_ENVIRONMENT_VARIABLES = 64
_MAX_ENVIRONMENT_NAME_BYTES = 256
_MAX_ENVIRONMENT_VALUE_BYTES = 4 * 1024
_MAX_ENVIRONMENT_TOTAL_BYTES = 64 * 1024
_MAX_TERMINAL_ID_BYTES = 128
_MAX_TERMINAL_WAIT_SECONDS = 60.0
_MAX_TERMINAL_OFFSET = (1 << 63) - 1


def _ensure_keys(arguments: Mapping[str, Any], allowed: frozenset[str]) -> None:
    if not isinstance(arguments, Mapping):
        raise ToolError("terminal arguments must be an object")
    unknown = [key for key in arguments if not isinstance(key, str) or key not in allowed]
    if unknown:
        raise ToolError(f"unknown terminal argument: {unknown[0]!r}")


def _bounded_text(value: object, *, name: str, max_bytes: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or "\x00" in value:
        raise ToolError(f"{name} must be a non-empty string without null bytes")
    if len(value.encode("utf-8")) > max_bytes:
        raise ToolError(f"{name} exceeds the size limit")
    return value


def _command(value: object) -> str:
    return _bounded_text(value, name="command", max_bytes=_MAX_ARGUMENT_BYTES)


def _arguments(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ToolError("args must be an array of strings")
    if len(value) > _MAX_ARGUMENTS:
        raise ToolError(f"args cannot contain more than {_MAX_ARGUMENTS} items")
    result: list[str] = []
    total = 0
    for item in value:
        argument = _bounded_text(item, name="argument", max_bytes=_MAX_ARGUMENT_BYTES)
        total += len(argument.encode("utf-8"))
        if total > _MAX_ARGUMENT_TOTAL_BYTES:
            raise ToolError("args exceed the total size limit")
        result.append(argument)
    return tuple(result)


def _cwd(value: object) -> str:
    return _bounded_text(value, name="cwd", max_bytes=_MAX_CWD_BYTES)


def _environment(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ToolError("env must be an object of string values")
    if len(value) > _MAX_ENVIRONMENT_VARIABLES:
        raise ToolError(f"env cannot contain more than {_MAX_ENVIRONMENT_VARIABLES} variables")
    result: dict[str, str] = {}
    total = 0
    for name, raw_value in value.items():
        variable_name = _bounded_text(
            name,
            name="environment variable name",
            max_bytes=_MAX_ENVIRONMENT_NAME_BYTES,
        )
        if "=" in variable_name:
            raise ToolError("environment variable name must not contain '='")
        variable_value = _bounded_text(
            raw_value,
            name="environment variable value",
            max_bytes=_MAX_ENVIRONMENT_VALUE_BYTES,
            allow_empty=True,
        )
        total += len(variable_name.encode("utf-8")) + len(variable_value.encode("utf-8"))
        if total > _MAX_ENVIRONMENT_TOTAL_BYTES:
            raise ToolError("env exceeds the total size limit")
        result[variable_name] = variable_value
    return result


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _wait_seconds(value: object, *, default: float) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= _MAX_TERMINAL_WAIT_SECONDS
    ):
        raise ToolError(f"wait_seconds must be between 0 and {_MAX_TERMINAL_WAIT_SECONDS:g}")
    return float(value)


def _terminal_id(value: object) -> str:
    return _bounded_text(value, name="terminal_id", max_bytes=_MAX_TERMINAL_ID_BYTES)


def _terminal_tool_error(error: TerminalError) -> ToolError:
    return ToolError(f"terminal operation failed: {error}")


async def _session(context: ToolContext, raw_id: object) -> InteractiveTerminalSession:
    manager = context.interactive_terminals
    if manager is None:
        raise ToolError("attached terminal sessions are unavailable")
    session_id = _terminal_id(raw_id)
    try:
        session = await manager.get_session(session_id)
    except TerminalError as error:
        raise _terminal_tool_error(error) from error
    if session is None:
        raise ToolError(f"attached terminal session not found: {session_id}")
    return session


async def _status(session: InteractiveTerminalSession) -> tuple[str, int | None]:
    exit_code = await session.wait(timeout_seconds=0)
    return ("exited" if exit_code is not None else "running", exit_code)


def _result(
    payload: Mapping[str, object],
    *,
    metadata: Mapping[str, object] | None = None,
) -> ToolResult:
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(content, metadata=dict(payload if metadata is None else metadata))


def _output_metadata(payload: Mapping[str, object]) -> dict[str, object]:
    """Keep terminal output in the bounded tool content, not metadata."""

    return {key: value for key, value in payload.items() if key != "data"}


class CreateTerminalTool:
    side_effecting = True
    definition = ToolDefinition(
        name="create_terminal",
        description=(
            "Create one bounded attached interactive terminal. Provide an executable and "
            "separate arguments; this is not a shell command."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 4096},
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "cwd": {"type": "string", "default": ".", "maxLength": 4096},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "rows": {"type": "integer", "minimum": 1, "maximum": MAX_TERMINAL_DIMENSION},
                "columns": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TERMINAL_DIMENSION,
                },
                "output_capacity": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TERMINAL_OUTPUT_BYTES,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        _ensure_keys(
            arguments,
            frozenset({"command", "args", "cwd", "env", "rows", "columns", "output_capacity"}),
        )
        manager = context.interactive_terminals
        authorization = context.terminal_creation_authorization
        if manager is None:
            raise ToolError("attached terminal sessions are unavailable")
        if not isinstance(authorization, TerminalCreationAuthorization):
            raise ToolError("terminal creation must be authorized by the tool pipeline")
        command = _command(arguments.get("command"))
        command_arguments = _arguments(arguments.get("args", []))
        size = TerminalSize(
            _integer(
                arguments.get("columns", 80),
                name="columns",
                minimum=1,
                maximum=MAX_TERMINAL_DIMENSION,
            ),
            _integer(
                arguments.get("rows", 24),
                name="rows",
                minimum=1,
                maximum=MAX_TERMINAL_DIMENSION,
            ),
        )
        output_capacity = _integer(
            arguments.get("output_capacity", DEFAULT_TERMINAL_OUTPUT_CAPACITY),
            name="output_capacity",
            minimum=1,
            maximum=MAX_TERMINAL_OUTPUT_BYTES,
        )
        try:
            session = await manager.create_exec(
                authorization.call_id,
                command,
                command_arguments,
                cwd=_cwd(arguments.get("cwd", ".")),
                env=_environment(arguments.get("env", {})),
                size=size,
                output_capacity=output_capacity,
                authorization=authorization,
            )
            status, exit_code = await _status(session)
        except TerminalError as error:
            raise _terminal_tool_error(error) from error
        return _result(
            {
                "terminal_id": session.session_id,
                "process_id": session.process_id,
                "size": {"columns": session.size.columns, "rows": session.size.rows},
                "next_offset": 0,
                "status": status,
                "exit_code": exit_code,
                "eof": False,
            }
        )


class TerminalOutputTool:
    side_effecting = False
    definition = ToolDefinition(
        name="terminal_output",
        description="Read a bounded incremental output chunk from an attached terminal.",
        input_schema={
            "type": "object",
            "properties": {
                "terminal_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "after_offset": {"type": "integer", "minimum": 0},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_TERMINAL_READ_BYTES},
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": _MAX_TERMINAL_WAIT_SECONDS,
                },
            },
            "required": ["terminal_id"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        _ensure_keys(
            arguments, frozenset({"terminal_id", "after_offset", "max_bytes", "wait_seconds"})
        )
        session = await _session(context, arguments.get("terminal_id"))
        after_offset = _integer(
            arguments.get("after_offset", 0),
            name="after_offset",
            minimum=0,
            maximum=_MAX_TERMINAL_OFFSET,
        )
        max_bytes = _integer(
            arguments.get("max_bytes", 65_536),
            name="max_bytes",
            minimum=1,
            maximum=MAX_TERMINAL_READ_BYTES,
        )
        try:
            chunk = await session.read(
                after_offset=after_offset,
                max_bytes=max_bytes,
                wait_seconds=_wait_seconds(arguments.get("wait_seconds"), default=0.0),
            )
            status, exit_code = await _status(session)
        except TerminalError as error:
            raise _terminal_tool_error(error) from error
        payload = {
            "terminal_id": session.session_id,
            "data": chunk.data.decode("utf-8", "replace"),
            "next_offset": chunk.next_offset,
            "dropped_bytes": chunk.dropped_bytes,
            "eof": chunk.eof,
            "status": status,
            "exit_code": exit_code,
        }
        return _result(payload, metadata=_output_metadata(payload))


class TerminalWriteTool:
    side_effecting = True
    definition = ToolDefinition(
        name="terminal_write",
        description="Write bounded text input to an attached terminal.",
        input_schema={
            "type": "object",
            "properties": {
                "terminal_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "text": {"type": "string", "maxLength": MAX_TERMINAL_WRITE_BYTES},
                "newline": {"type": "boolean", "default": False},
            },
            "required": ["terminal_id", "text"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        _ensure_keys(arguments, frozenset({"terminal_id", "text", "newline"}))
        session = await _session(context, arguments.get("terminal_id"))
        text = _bounded_text(
            arguments.get("text"),
            name="text",
            max_bytes=MAX_TERMINAL_WRITE_BYTES,
            allow_empty=True,
        )
        newline = arguments.get("newline", False)
        if not isinstance(newline, bool):
            raise ToolError("newline must be a boolean")
        data = text.encode("utf-8") + (b"\n" if newline else b"")
        if len(data) > MAX_TERMINAL_WRITE_BYTES:
            raise ToolError("text exceeds the size limit after newline is added")
        try:
            await session.write(data)
        except TerminalError as error:
            raise _terminal_tool_error(error) from error
        return _result({"terminal_id": session.session_id, "bytes_written": len(data)})


class TerminalResizeTool:
    side_effecting = True
    definition = ToolDefinition(
        name="terminal_resize",
        description="Resize an attached terminal within the platform dimension limit.",
        input_schema={
            "type": "object",
            "properties": {
                "terminal_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "rows": {"type": "integer", "minimum": 1, "maximum": MAX_TERMINAL_DIMENSION},
                "columns": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TERMINAL_DIMENSION,
                },
            },
            "required": ["terminal_id", "rows", "columns"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        _ensure_keys(arguments, frozenset({"terminal_id", "rows", "columns"}))
        session = await _session(context, arguments.get("terminal_id"))
        size = TerminalSize(
            _integer(
                arguments.get("columns"), name="columns", minimum=1, maximum=MAX_TERMINAL_DIMENSION
            ),
            _integer(arguments.get("rows"), name="rows", minimum=1, maximum=MAX_TERMINAL_DIMENSION),
        )
        try:
            await session.resize(size)
        except TerminalError as error:
            raise _terminal_tool_error(error) from error
        return _result(
            {
                "terminal_id": session.session_id,
                "size": {"columns": size.columns, "rows": size.rows},
            }
        )


class TerminalWaitTool:
    side_effecting = False
    definition = ToolDefinition(
        name="terminal_wait",
        description="Wait for an attached terminal to exit for a bounded duration.",
        input_schema={
            "type": "object",
            "properties": {
                "terminal_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": _MAX_TERMINAL_WAIT_SECONDS,
                },
            },
            "required": ["terminal_id"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        _ensure_keys(arguments, frozenset({"terminal_id", "timeout_seconds"}))
        session = await _session(context, arguments.get("terminal_id"))
        try:
            exit_code = await session.wait(
                timeout_seconds=_wait_seconds(
                    arguments.get("timeout_seconds"),
                    default=_MAX_TERMINAL_WAIT_SECONDS,
                )
            )
        except TerminalError as error:
            raise _terminal_tool_error(error) from error
        status = "exited" if exit_code is not None else "running"
        return _result(
            {"terminal_id": session.session_id, "status": status, "exit_code": exit_code}
        )


class TerminalKillTool:
    side_effecting = True
    definition = ToolDefinition(
        name="terminal_kill",
        description="Stop an attached terminal and return its bounded final output state.",
        input_schema={
            "type": "object",
            "properties": {"terminal_id": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["terminal_id"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        _ensure_keys(arguments, frozenset({"terminal_id"}))
        session = await _session(context, arguments.get("terminal_id"))
        try:
            await session.close()
            chunk = await session.read(after_offset=0, max_bytes=MAX_TERMINAL_READ_BYTES)
            status, exit_code = await _status(session)
        except TerminalError as error:
            raise _terminal_tool_error(error) from error
        payload = {
            "terminal_id": session.session_id,
            "data": chunk.data.decode("utf-8", "replace"),
            "next_offset": chunk.next_offset,
            "dropped_bytes": chunk.dropped_bytes,
            "eof": chunk.eof,
            "status": status,
            "exit_code": exit_code,
        }
        return _result(payload, metadata=_output_metadata(payload))


__all__ = [
    "CreateTerminalTool",
    "TerminalKillTool",
    "TerminalOutputTool",
    "TerminalResizeTool",
    "TerminalWaitTool",
    "TerminalWriteTool",
]
