from __future__ import annotations

import json
from collections.abc import Sequence

from neuro_code.domain.background_tasks import BackgroundTaskSnapshot

BACKGROUND_TASK_COMPLETION_BATCH_LIMIT = 20


def format_background_task_completion_reminder(
    snapshots: Sequence[BackgroundTaskSnapshot],
    *,
    remaining_count: int = 0,
    task_output_tool: str | None = "task_output",
) -> str:
    """Render bounded model-only status without command or output content."""
    if not snapshots:
        raise ValueError("completion reminder requires at least one task")
    if any(not snapshot.status.terminal for snapshot in snapshots):
        raise ValueError("completion reminder accepts only terminal tasks")
    if remaining_count < 0:
        raise ValueError("remaining completion count must not be negative")

    count = len(snapshots)
    noun = "task" if count == 1 else "tasks"
    lines = [
        "<background-task-completions>",
        "Runtime-generated status (not user-authored content): "
        f"{count} managed background {noun} reached a terminal state.",
    ]
    for snapshot in snapshots:
        payload = {
            "task_id": snapshot.task_id,
            "status": snapshot.status.value,
            "exit_code": snapshot.exit_code,
            "output_bytes": snapshot.total_output_bytes,
            "output_preview_truncated": snapshot.truncated,
        }
        lines.append(f"- {json.dumps(payload, ensure_ascii=True, separators=(',', ':'))}")
    if task_output_tool is not None:
        lines.append(
            f"Use {task_output_tool} with an exact task_id only if the bounded output is needed."
        )
    if remaining_count:
        lines.append(
            f"{remaining_count} additional completion(s) remain queued for a later model boundary."
        )
    lines.append("</background-task-completions>")
    return "\n".join(lines)


__all__ = [
    "BACKGROUND_TASK_COMPLETION_BATCH_LIMIT",
    "format_background_task_completion_reminder",
]
