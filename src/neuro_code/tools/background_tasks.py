from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.background_tasks import (
    MAX_BACKGROUND_TASK_WAIT_IDS,
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import ToolError

_MAX_TASK_WAIT_SECONDS = 30.0


def _task_id(arguments: Mapping[str, Any]) -> str:
    task_id = arguments.get("task_id")
    if (
        not isinstance(task_id, str)
        or not task_id.strip()
        or "\x00" in task_id
        or len(task_id) > 128
    ):
        raise ToolError("task_id must be a non-empty string of at most 128 characters")
    return task_id


async def _unknown_task(task_id: str, context: ToolContext) -> ToolError:
    assert context.background_tasks is not None
    known = await context.background_tasks.list()
    known_ids = ", ".join(snapshot.task_id for snapshot in known) or "(none)"
    return ToolError(f"background task not found: {task_id}; known task IDs: {known_ids}")


def _render_snapshot(snapshot: BackgroundTaskSnapshot) -> str:
    if snapshot.exit_code is not None:
        exit_code = str(snapshot.exit_code)
    elif snapshot.status.terminal:
        exit_code = "(none)"
    else:
        exit_code = "(running)"
    output = snapshot.output or "(no output)"
    truncation = "\n[background output preview is truncated]" if snapshot.truncated else ""
    return (
        f"task_id: {snapshot.task_id}\n"
        f"status: {snapshot.status.value}\n"
        f"command: {snapshot.command}\n"
        f"exit_code: {exit_code}\n"
        f"output:\n{output}{truncation}"
    )


def _task_ids(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    raw_ids = arguments.get("task_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ToolError("task_ids must be a non-empty array")
    if len(raw_ids) > MAX_BACKGROUND_TASK_WAIT_IDS:
        raise ToolError(f"task_ids must contain at most {MAX_BACKGROUND_TASK_WAIT_IDS} entries")
    task_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        if not isinstance(raw_id, str):
            raise ToolError("each task_id must be a string")
        task_id = raw_id.strip()
        if not task_id or "\x00" in task_id or len(task_id) > 128:
            raise ToolError("each task_id must contain 1 to 128 valid characters")
        if task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)
    return tuple(task_ids)


def _wait_mode(arguments: Mapping[str, Any]) -> BackgroundTaskWaitMode:
    raw_mode = arguments.get("mode")
    if not isinstance(raw_mode, str):
        raise ToolError("mode must be 'wait_any' or 'wait_all'")
    try:
        return BackgroundTaskWaitMode(raw_mode)
    except ValueError as error:
        raise ToolError("mode must be 'wait_any' or 'wait_all'") from error


def _wait_timeout(arguments: Mapping[str, Any]) -> float:
    timeout_seconds = arguments.get("timeout_seconds", _MAX_TASK_WAIT_SECONDS)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
        or timeout_seconds > _MAX_TASK_WAIT_SECONDS
    ):
        raise ToolError(f"timeout_seconds must be between 0 and {_MAX_TASK_WAIT_SECONDS:g}")
    return float(timeout_seconds or _MAX_TASK_WAIT_SECONDS)


def _render_wait_result(
    task_ids: tuple[str, ...],
    result: BackgroundTaskWaitResult,
    *,
    output_byte_limit: int,
) -> str:
    snapshot_by_id = {snapshot.task_id: snapshot for snapshot in result.snapshots}
    missing = set(result.missing_task_ids)
    summary = (
        f"{result.terminal_count}/{len(task_ids)} tasks reached a terminal state "
        f"({result.mode.value})"
    )
    lines = [
        f"mode: {result.mode.value}",
        f"summary: {summary}",
        f"timed_out: {str(result.timed_out).lower()}",
    ]
    for task_id in task_ids:
        lines.append("")
        if task_id in missing:
            lines.extend((f"task_id: {task_id}", "status: not_found"))
        else:
            lines.append(_render_snapshot(snapshot_by_id[task_id]))
    content = "\n".join(lines)
    encoded = content.encode()
    if len(encoded) <= output_byte_limit:
        return content
    bounded = encoded[:output_byte_limit].decode("utf-8", "ignore")
    return f"{bounded}\n[wait_tasks output truncated]"


class TaskOutputTool:
    definition = ToolDefinition(
        name="task_output",
        description=(
            "Get the current output and status of a managed background command. "
            "Omit wait_seconds for a non-blocking snapshot."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": _MAX_TASK_WAIT_SECONDS,
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        task_id = _task_id(arguments)
        wait_seconds = arguments.get("wait_seconds", 0.0)
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, int | float)
            or not math.isfinite(wait_seconds)
            or wait_seconds < 0
            or wait_seconds > _MAX_TASK_WAIT_SECONDS
        ):
            raise ToolError(f"wait_seconds must be between 0 and {_MAX_TASK_WAIT_SECONDS:g}")
        if context.background_tasks is None:
            raise ToolError("background task inspection is unavailable")
        snapshot = await context.background_tasks.get(
            task_id,
            wait_seconds=float(wait_seconds),
        )
        if snapshot is None:
            raise await _unknown_task(task_id, context)
        if snapshot.status.terminal:
            await context.background_tasks.mark_completions_reported((task_id,))
        return ToolResult(
            _render_snapshot(snapshot),
            is_error=snapshot.status.value in {"failed", "timed_out"},
            metadata=snapshot.to_dict(include_output=False),
        )


class WaitTasksTool:
    definition = ToolDefinition(
        name="wait_tasks",
        description=(
            "Wait for one or more managed background commands using completion events. "
            "wait_any returns after one known task finishes; wait_all waits for every "
            "known task. The default and maximum wait are 30 seconds."
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
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": _MAX_TASK_WAIT_SECONDS,
                },
            },
            "required": ["task_ids", "mode"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        task_ids = _task_ids(arguments)
        mode = _wait_mode(arguments)
        timeout_seconds = _wait_timeout(arguments)
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        if context.background_tasks is None:
            raise ToolError("background task waiting is unavailable")
        result = await context.background_tasks.wait(
            task_ids,
            mode=mode,
            timeout_seconds=timeout_seconds,
        )
        terminal_ids = tuple(
            snapshot.task_id for snapshot in result.snapshots if snapshot.status.terminal
        )
        if terminal_ids:
            await context.background_tasks.mark_completions_reported(terminal_ids)

        snapshot_by_id = {snapshot.task_id: snapshot for snapshot in result.snapshots}
        missing = set(result.missing_task_ids)
        results: list[dict[str, object]] = []
        for task_id in task_ids:
            if task_id in missing:
                results.append({"task_id": task_id, "status": "not_found"})
            else:
                results.append(snapshot_by_id[task_id].to_dict(include_output=False))
        metadata: dict[str, object] = {
            "mode": mode.value,
            "timed_out": result.timed_out,
            "terminal_count": result.terminal_count,
            "requested_count": len(task_ids),
            "results": results,
        }
        is_error = bool(result.missing_task_ids) or any(
            snapshot.status in {BackgroundTaskStatus.FAILED, BackgroundTaskStatus.TIMED_OUT}
            for snapshot in result.snapshots
        )
        return ToolResult(
            _render_wait_result(
                task_ids,
                result,
                output_byte_limit=context.output_byte_limit,
            ),
            is_error=is_error,
            metadata=metadata,
        )


class KillTaskTool:
    definition = ToolDefinition(
        name="kill_task",
        description=(
            "Terminate a managed background command and its owned process tree. "
            "A completed task is reported without being changed."
        ),
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        task_id = _task_id(arguments)
        if context.background_tasks is None:
            raise ToolError("background task termination is unavailable")
        result = await context.background_tasks.kill(task_id)
        if result is None:
            raise await _unknown_task(task_id, context)
        await context.background_tasks.mark_completions_reported((task_id,))
        metadata = result.snapshot.to_dict(include_output=False)
        metadata["outcome"] = result.outcome.value
        return ToolResult(
            f"task_id: {task_id}\noutcome: {result.outcome.value}",
            metadata=metadata,
        )
