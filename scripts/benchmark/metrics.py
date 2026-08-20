"""Bounded metrics and event-trace projections for benchmark attempts."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind

TOOL_NAMES = (
    "bash",
    "read_file",
    "read_files",
    "grep",
    "grep_many",
    "apply_patch",
    "search_replace",
    "web_search",
    "web_fetch",
    "list_dir",
    "list_tree",
)


def event_dicts(events: Iterable[AgentEvent]) -> list[dict[str, object]]:
    return [event.to_dict() for event in events]


def event_jsonl(events: Iterable[AgentEvent]) -> str:
    return "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in event_dicts(events)
    )


def _tool_counts(events: list[dict[str, object]]) -> dict[str, int]:
    counts = Counter(dict.fromkeys(TOOL_NAMES, 0))
    failures = Counter()
    for event in events:
        if event.get("kind") not in {
            AgentEventKind.TOOL_REQUESTED.value,
            AgentEventKind.TOOL_FAILED.value,
        }:
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        name = data.get("name")
        if isinstance(name, str):
            if event.get("kind") == AgentEventKind.TOOL_REQUESTED.value:
                counts[name] += 1
            else:
                failures[name] += 1
    return {
        **{f"{name}_count": counts[name] for name in TOOL_NAMES},
        "tool_failures": dict(sorted(failures.items())),
    }


def _agent_final_verification(events: list[dict[str, object]]) -> bool:
    for event in events:
        if event.get("kind") != AgentEventKind.TOOL_REQUESTED.value:
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        arguments = data.get("arguments")
        if isinstance(arguments, Mapping):
            command = arguments.get("command")
            if isinstance(command, str) and any(
                token in command for token in ("pytest", "unittest", "compileall", "ruff", "mypy")
            ):
                return True
    return False


def project_metrics(
    events: Iterable[AgentEvent],
    *,
    wall_time_seconds: float,
    outcome: str,
    stop_reason: str | None = None,
) -> dict[str, object]:
    """Project only observable bounded execution facts; never infer reasoning quality."""

    items = event_dicts(events)
    tool_counts = _tool_counts(items)
    provider_selected = [
        event for event in items if event.get("kind") == AgentEventKind.PROVIDER_SELECTED.value
    ]
    failovers = 0
    for event in provider_selected:
        data = event.get("data")
        if isinstance(data, Mapping) and data.get("failover") is True:
            failovers += 1
    model_steps = sum(
        event.get("kind") == AgentEventKind.MODEL_STEP_STARTED.value for event in items
    )
    compactions = sum(
        event.get("kind") == AgentEventKind.CONTEXT_COMPACTION_COMPLETED.value for event in items
    )
    permission_events = sum(
        event.get("kind")
        in {
            AgentEventKind.TOOL_PERMISSION.value,
            AgentEventKind.TOOL_APPROVAL_REQUESTED.value,
            AgentEventKind.TOOL_APPROVAL_RESOLVED.value,
        }
        for event in items
    )
    background_events = sum(
        event.get("kind")
        in {
            AgentEventKind.BACKGROUND_TASK_AUTO_WAKE_STARTED.value,
            AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER.value,
        }
        for event in items
    )
    token_data: list[Mapping[str, object]] = []
    for event in items:
        data = event.get("data")
        if isinstance(data, Mapping) and ("input_tokens" in data or "output_tokens" in data):
            token_data.append(data)
    input_tokens = next(
        (
            data.get("input_tokens")
            for data in reversed(token_data)
            if isinstance(data.get("input_tokens"), int)
        ),
        None,
    )
    output_tokens = next(
        (
            data.get("output_tokens")
            for data in reversed(token_data)
            if isinstance(data.get("output_tokens"), int)
        ),
        None,
    )
    cache_read_tokens = next(
        (
            data.get("cache_read_tokens")
            for data in reversed(token_data)
            if isinstance(data.get("cache_read_tokens"), int)
        ),
        None,
    )
    cache_write_tokens = next(
        (
            data.get("cache_write_tokens")
            for data in reversed(token_data)
            if isinstance(data.get("cache_write_tokens"), int)
        ),
        None,
    )
    return {
        "outcome": outcome,
        "wall_time_seconds": round(max(0.0, wall_time_seconds), 6),
        "model_steps": model_steps,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "tool_counts": tool_counts,
        "compactions": compactions,
        "provider_failovers": failovers,
        "permission_events": permission_events,
        "background_tasks": background_events,
        "agent_invoked_final_verification": _agent_final_verification(items),
        "provider_selections": len(provider_selected),
        "stop_reason": stop_reason,
    }


__all__ = ["TOOL_NAMES", "event_dicts", "event_jsonl", "project_metrics"]
