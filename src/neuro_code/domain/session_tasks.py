"""Durable task lifecycle values shared by plan execution and future subagents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from neuro_code.domain.plans import SessionPlan

MAX_SESSION_TASK_ID_BYTES = 80


class SessionTaskKind(StrEnum):
    """The owned work category, independent of its eventual implementation."""

    PLAN_EXECUTION = "plan_execution"
    SUBAGENT = "subagent"


class SessionTaskStatus(StrEnum):
    """Lifecycle states with no implicit retry or automatic follow-up."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self is not self.RUNNING


@dataclass(frozen=True, slots=True)
class SessionTask:
    """A bounded durable record for one execution owned by a session."""

    task_id: str
    kind: SessionTaskKind
    status: SessionTaskStatus
    started_at: datetime
    finished_at: datetime | None = None
    plan_snapshot: SessionPlan | None = None

    def __post_init__(self) -> None:
        if (
            not self.task_id
            or "\x00" in self.task_id
            or len(self.task_id.encode("utf-8")) > MAX_SESSION_TASK_ID_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in self.task_id)
        ):
            raise ValueError("session task id is invalid")
        if not isinstance(self.kind, SessionTaskKind):
            raise ValueError("session task kind must be canonical")
        if not isinstance(self.status, SessionTaskStatus):
            raise ValueError("session task status must be canonical")
        if self.started_at.tzinfo is None:
            raise ValueError("session task start time must be timezone-aware")
        if self.finished_at is not None and self.finished_at.tzinfo is None:
            raise ValueError("session task finish time must be timezone-aware")
        if self.status.terminal != (self.finished_at is not None):
            raise ValueError("session task terminal state and finish time disagree")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("session task finish time must not precede its start")
        if self.plan_snapshot is not None and not isinstance(self.plan_snapshot, SessionPlan):
            raise ValueError("session task plan snapshot must be canonical")
        if self.plan_snapshot is not None and self.kind is not SessionTaskKind.PLAN_EXECUTION:
            raise ValueError("only a plan execution task may contain a plan snapshot")

    def finish(self, status: SessionTaskStatus, *, finished_at: datetime) -> SessionTask:
        """Return the one allowed terminal transition for this task."""

        if self.status is not SessionTaskStatus.RUNNING:
            raise ValueError("session task is already terminal")
        if not status.terminal:
            raise ValueError("session task finish status must be terminal")
        return SessionTask(
            self.task_id,
            self.kind,
            status,
            self.started_at,
            finished_at,
            self.plan_snapshot,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "plan": self.plan_snapshot.to_dict() if self.plan_snapshot is not None else None,
        }


__all__ = [
    "MAX_SESSION_TASK_ID_BYTES",
    "SessionTask",
    "SessionTaskKind",
    "SessionTaskStatus",
]
