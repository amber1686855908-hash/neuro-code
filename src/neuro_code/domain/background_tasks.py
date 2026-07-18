from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_BACKGROUND_TASK_WAIT_IDS = 20


class BackgroundTaskStatus(StrEnum):
    """Lifecycle states exposed by an owned background command."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self is not self.RUNNING


class BackgroundTaskKillOutcome(StrEnum):
    KILLED = "killed"
    ALREADY_EXITED = "already_exited"


class BackgroundTaskWaitMode(StrEnum):
    """Completion condition for an event-driven multi-task wait."""

    WAIT_ANY = "wait_any"
    WAIT_ALL = "wait_all"


@dataclass(frozen=True, slots=True)
class BackgroundTaskSnapshot:
    task_id: str
    command: str
    cwd: str
    status: BackgroundTaskStatus
    output: str
    total_output_bytes: int
    truncated: bool
    exit_code: int | None
    started_at: datetime
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.task_id or not self.command or not self.cwd:
            raise ValueError("background task identity fields must not be empty")
        if self.total_output_bytes < 0:
            raise ValueError("background task output size must not be negative")
        if self.started_at.tzinfo is None:
            raise ValueError("background task start time must be timezone-aware")
        if self.finished_at is not None and self.finished_at.tzinfo is None:
            raise ValueError("background task finish time must be timezone-aware")
        if self.status.terminal != (self.finished_at is not None):
            raise ValueError("background task terminal state and finish time disagree")

    def to_dict(self, *, include_output: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": self.task_id,
            "command": self.command,
            "cwd": self.cwd,
            "status": self.status.value,
            "total_output_bytes": self.total_output_bytes,
            "truncated": self.truncated,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        if include_output:
            payload["output"] = self.output
        return payload


@dataclass(frozen=True, slots=True)
class BackgroundTaskKillResult:
    outcome: BackgroundTaskKillOutcome
    snapshot: BackgroundTaskSnapshot


@dataclass(frozen=True, slots=True)
class BackgroundTaskWaitResult:
    """Ordered known snapshots plus IDs hidden from or absent in this scope."""

    mode: BackgroundTaskWaitMode
    snapshots: tuple[BackgroundTaskSnapshot, ...]
    missing_task_ids: tuple[str, ...]
    timed_out: bool

    def __post_init__(self) -> None:
        known_ids = tuple(snapshot.task_id for snapshot in self.snapshots)
        if not known_ids and not self.missing_task_ids:
            raise ValueError("background task wait result must not be empty")
        if len(set(known_ids)) != len(known_ids):
            raise ValueError("background task wait snapshots must be unique")
        if len(set(self.missing_task_ids)) != len(self.missing_task_ids):
            raise ValueError("missing background task IDs must be unique")
        if set(known_ids).intersection(self.missing_task_ids):
            raise ValueError("known and missing background task IDs must not overlap")

    @property
    def terminal_count(self) -> int:
        return sum(snapshot.status.terminal for snapshot in self.snapshots)
