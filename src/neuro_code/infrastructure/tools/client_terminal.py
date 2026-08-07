"""Canonical ACP client terminal tools.

This module owns the bounded client-terminal execution and inspection tools:
``terminal_exec``, ``terminal_start``, ``terminal_output``, ``terminal_wait``,
and ``terminal_kill``. All operations delegate through the ``ClientTerminal``
port; this module does not own terminal sessions or process lifecycle. Shared
task-id/wait/rendering helpers and capability/sandbox gating are kept here so
the legacy module can remain a one-way compatibility facade.

定义规范的 ACP 客户端终端工具. 所有操作都委托给 ClientTerminal 端口,本模块不拥有终端会话或进程生命周期.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.client_terminal import (
    MAX_CLIENT_TERMINAL_OUTPUT_BYTES,
    ClientTerminal,
    ClientTerminalResult,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.background_tasks.models import (
    MAX_BACKGROUND_TASK_WAIT_IDS,
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import ToolError

MAX_TASK_WAIT_SECONDS = 30.0
MAX_COMMAND_BYTES = 4 * 1024
MAX_ARGUMENTS = 64
MAX_ARGUMENT_BYTES = 4 * 1024
MAX_ARGUMENT_TOTAL_BYTES = 32 * 1024


class ClientTerminalTool:
    """Execute one direct command in an ACP client-owned terminal.

    ACP does not define portable shell selection, so this deliberately accepts
    an executable and argument vector instead of changing the established
    ``bash`` tool's shell semantics.

    在 ACP 客户端拥有的终端中执行一个直接命令. 接受可执行文件和参数向量,不改变既有 bash 工具的 Shell 语义.
    """

    side_effecting = True
    definition = ToolDefinition(
        name="terminal_exec",
        description=(
            "Run one executable with arguments in the connected client's terminal. "
            "This is not a shell: provide the executable as command and separate args."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        terminal = context.client_terminal
        if terminal is None:
            raise ToolError("ACP client terminal execution is unavailable")
        if context.sandbox_profile.enabled:
            raise ToolError(
                "ACP client terminal execution is unavailable while sandboxing is enabled"
            )
        command = _command(arguments.get("command"))
        command_arguments = _arguments(arguments.get("args", []))
        timeout = _timeout(
            arguments.get("timeout_seconds", context.command_timeout_seconds),
        )
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")

        result = await terminal.run(
            command,
            command_arguments,
            cwd=context.cwd,
            output_byte_limit=min(context.output_byte_limit, MAX_CLIENT_TERMINAL_OUTPUT_BYTES),
            timeout_seconds=timeout,
        )
        if not isinstance(result, ClientTerminalResult):
            raise ToolError("ACP client terminal returned an invalid result")
        _result_status(result)
        content, truncated = _bounded_output(
            result.output,
            min(context.output_byte_limit, MAX_CLIENT_TERMINAL_OUTPUT_BYTES),
            result.truncated,
        )
        return ToolResult(
            content,
            is_error=result.exit_code != 0 or result.signal is not None,
            metadata={
                "exit_code": result.exit_code,
                "signal": result.signal,
                "truncated": truncated,
                "client_delegated": True,
            },
        )


def _command(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ToolError("command must be a non-empty string")
    if len(value.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise ToolError("command exceeds the size limit")
    return value


def _arguments(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ToolError("args must be an array of strings")
    if len(value) > MAX_ARGUMENTS:
        raise ToolError(f"args cannot contain more than {MAX_ARGUMENTS} items")
    arguments: list[str] = []
    total_bytes = 0
    for argument in value:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ToolError("args must be an array of strings")
        size = len(argument.encode("utf-8"))
        if size > MAX_ARGUMENT_BYTES:
            raise ToolError("an argument exceeds the size limit")
        total_bytes += size
        if total_bytes > MAX_ARGUMENT_TOTAL_BYTES:
            raise ToolError("args exceed the total size limit")
        arguments.append(argument)
    return tuple(arguments)


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ToolError("timeout_seconds must be positive")
    return float(value)


def _bounded_output(output: object, byte_limit: int, truncated: object) -> tuple[str, bool]:
    if not isinstance(output, str) or not isinstance(truncated, bool):
        raise ToolError("ACP client terminal returned an invalid result")
    encoded = output.encode("utf-8")
    was_truncated = truncated or len(encoded) > byte_limit
    if len(encoded) > byte_limit:
        output = encoded[:byte_limit].decode("utf-8", "ignore")
    if was_truncated:
        output += "\n[output truncated]"
    return output, was_truncated


def _result_status(result: ClientTerminalResult) -> None:
    if result.exit_code is not None and (
        isinstance(result.exit_code, bool)
        or not isinstance(result.exit_code, int)
        or result.exit_code < 0
    ):
        raise ToolError("ACP client terminal returned an invalid result")
    if result.signal is not None and (
        not isinstance(result.signal, str)
        or not result.signal
        or "\x00" in result.signal
        or any(ord(character) < 32 or ord(character) == 127 for character in result.signal)
        or len(result.signal.encode("utf-8")) > 128
    ):
        raise ToolError("ACP client terminal returned an invalid result")
    if result.exit_code is None and result.signal is None:
        raise ToolError("ACP client terminal returned an invalid result")


def _background_terminal(context: ToolContext) -> ClientTerminal:
    terminal = context.client_terminal
    if terminal is None:
        raise ToolError("ACP client background terminal execution is unavailable")
    if context.sandbox_profile.enabled:
        raise ToolError(
            "ACP client background terminal execution is unavailable while sandboxing is enabled"
        )
    return terminal


def _task_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 128
    ):
        raise ToolError("task_id must be a non-empty string of at most 128 characters")
    return value


def _task_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ToolError("task_ids must be a non-empty array")
    if len(value) > MAX_BACKGROUND_TASK_WAIT_IDS:
        raise ToolError(f"task_ids must contain at most {MAX_BACKGROUND_TASK_WAIT_IDS} entries")
    task_ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        task_id = _task_id(item)
        if task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)
    return tuple(task_ids)


def _wait_seconds(value: object, *, default: float = 0.0) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= MAX_TASK_WAIT_SECONDS
    ):
        raise ToolError(f"wait_seconds must be between 0 and {MAX_TASK_WAIT_SECONDS:g}")
    return float(value)


def _wait_mode(value: object) -> BackgroundTaskWaitMode:
    if not isinstance(value, str):
        raise ToolError("mode must be 'wait_any' or 'wait_all'")
    try:
        return BackgroundTaskWaitMode(value)
    except ValueError as error:
        raise ToolError("mode must be 'wait_any' or 'wait_all'") from error


def _render_snapshot(snapshot: BackgroundTaskSnapshot) -> str:
    if snapshot.exit_code is not None:
        exit_code = str(snapshot.exit_code)
    elif snapshot.status.terminal:
        exit_code = "(none)"
    else:
        exit_code = "(running)"
    output = snapshot.output or "(no output)"
    truncation = "\n[client terminal output is truncated]" if snapshot.truncated else ""
    return (
        f"task_id: {snapshot.task_id}\n"
        f"status: {snapshot.status.value}\n"
        f"command: {snapshot.command}\n"
        f"exit_code: {exit_code}\n"
        f"output:\n{output}{truncation}"
    )


def _render_wait_result(
    task_ids: tuple[str, ...],
    result: BackgroundTaskWaitResult,
    *,
    output_byte_limit: int,
) -> str:
    by_id = {snapshot.task_id: snapshot for snapshot in result.snapshots}
    missing = set(result.missing_task_ids)
    lines = [
        f"mode: {result.mode.value}",
        f"summary: {result.terminal_count}/{len(task_ids)} tasks reached a terminal state",
        f"timed_out: {str(result.timed_out).lower()}",
    ]
    for task_id in task_ids:
        lines.append("")
        if task_id in missing:
            lines.extend((f"task_id: {task_id}", "status: not_found"))
        else:
            lines.append(_render_snapshot(by_id[task_id]))
    content = "\n".join(lines)
    encoded = content.encode("utf-8")
    if len(encoded) <= output_byte_limit:
        return content
    return (
        f"{encoded[:output_byte_limit].decode('utf-8', 'ignore')}\n[terminal wait output truncated]"
    )


class ClientTerminalStartTool:
    definition = ToolDefinition(
        name="terminal_start",
        description=(
            "Start a managed direct executable in the connected ACP client's terminal. "
            "This is not a shell; use terminal_output, terminal_wait, or terminal_kill "
            "with the returned task_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        terminal = _background_terminal(context)
        command = _command(arguments.get("command"))
        command_arguments = _arguments(arguments.get("args", []))
        raw_timeout = arguments.get("timeout_seconds")
        timeout = _timeout(raw_timeout) if raw_timeout is not None else None
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        snapshot = await terminal.start_exec(
            command,
            command_arguments,
            cwd=context.cwd,
            output_byte_limit=min(context.output_byte_limit, MAX_CLIENT_TERMINAL_OUTPUT_BYTES),
            timeout_seconds=timeout,
        )
        metadata = snapshot.to_dict(include_output=False)
        metadata["client_delegated"] = True
        return ToolResult(
            (
                f"Client terminal task started: {snapshot.task_id}\n"
                "Use terminal_output, terminal_wait, or terminal_kill with this task_id."
            ),
            metadata=metadata,
        )


class ClientTerminalKillTool:
    definition = ToolDefinition(
        name="terminal_kill",
        description="Terminate one managed ACP client terminal task.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        terminal = _background_terminal(context)
        task_id = _task_id(arguments.get("task_id"))
        result = await terminal.kill(task_id)
        if result is None:
            raise ToolError("ACP client terminal task was not found")
        metadata = result.snapshot.to_dict(include_output=False)
        metadata.update({"client_delegated": True, "outcome": result.outcome.value})
        return ToolResult(
            f"task_id: {task_id}\noutcome: {result.outcome.value}",
            metadata=metadata,
        )


class ClientTerminalOutputTool:
    definition = ToolDefinition(
        name="terminal_output",
        description=(
            "Get bounded output and status for a managed ACP client terminal task. "
            "Optionally wait up to 30 seconds for completion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        terminal = _background_terminal(context)
        task_id = _task_id(arguments.get("task_id"))
        snapshot = await terminal.get(
            task_id,
            wait_seconds=_wait_seconds(arguments.get("wait_seconds")),
        )
        if snapshot is None:
            raise ToolError("ACP client terminal task was not found")
        metadata = snapshot.to_dict(include_output=False)
        metadata["client_delegated"] = True
        return ToolResult(
            _render_snapshot(snapshot),
            is_error=snapshot.status
            in {BackgroundTaskStatus.FAILED, BackgroundTaskStatus.TIMED_OUT},
            metadata=metadata,
        )


class ClientTerminalWaitTool:
    definition = ToolDefinition(
        name="terminal_wait",
        description=(
            "Wait for one or more managed ACP client terminal tasks. wait_any returns "
            "after one known task finishes; wait_all waits for every known task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_BACKGROUND_TASK_WAIT_IDS,
                },
                "mode": {"type": "string", "enum": ["wait_any", "wait_all"]},
                "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 30},
            },
            "required": ["task_ids", "mode"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        terminal = _background_terminal(context)
        task_ids = _task_ids(arguments.get("task_ids"))
        result = await terminal.wait(
            task_ids,
            mode=_wait_mode(arguments.get("mode")),
            timeout_seconds=_wait_seconds(
                arguments.get("timeout_seconds"), default=MAX_TASK_WAIT_SECONDS
            ),
        )
        metadata: dict[str, object] = {
            "client_delegated": True,
            "mode": result.mode.value,
            "timed_out": result.timed_out,
            "terminal_count": result.terminal_count,
            "requested_count": len(task_ids),
            "results": [snapshot.to_dict(include_output=False) for snapshot in result.snapshots],
        }
        if result.missing_task_ids:
            metadata["missing_task_ids"] = list(result.missing_task_ids)
        return ToolResult(
            _render_wait_result(task_ids, result, output_byte_limit=context.output_byte_limit),
            is_error=bool(result.missing_task_ids)
            or any(
                snapshot.status in {BackgroundTaskStatus.FAILED, BackgroundTaskStatus.TIMED_OUT}
                for snapshot in result.snapshots
            ),
            metadata=metadata,
        )


__all__ = [
    "ClientTerminalKillTool",
    "ClientTerminalOutputTool",
    "ClientTerminalStartTool",
    "ClientTerminalTool",
    "ClientTerminalWaitTool",
]
