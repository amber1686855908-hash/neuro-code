from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class AgentEventKind(StrEnum):
    SESSION_STARTED = "session_started"
    USER_MESSAGE = "user_message"
    MODEL_STEP_STARTED = "model_step_started"
    MODEL_THINKING_COMPLETED = "model_thinking_completed"
    CONTEXT_USAGE_UPDATED = "context_usage_updated"
    BACKGROUND_TASK_COMPLETION_REMINDER = "background_task_completion_reminder"
    PROVIDER_ATTEMPT_FAILED = "provider_attempt_failed"
    PROVIDER_SELECTED = "provider_selected"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    BACKEND_TOOL_STARTED = "backend_tool_started"
    BACKEND_TOOL_COMPLETED = "backend_tool_completed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_PERMISSION = "tool_permission"
    TOOL_APPROVAL_REQUESTED = "tool_approval_requested"
    TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    sequence: int
    kind: AgentEventKind
    data: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")

    @classmethod
    def create(
        cls,
        sequence: int,
        kind: AgentEventKind,
        data: Mapping[str, Any] | None = None,
    ) -> AgentEvent:
        return cls(sequence, kind, data or {}, datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind.value,
            "data": dict(self.data),
            "created_at": self.created_at.isoformat(),
        }
