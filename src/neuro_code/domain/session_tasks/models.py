"""Canonical durable session-task value objects.

定义规范的持久化会话任务值对象."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from neuro_code.domain.plans import SessionPlan

MAX_SESSION_TASK_ID_BYTES = 80
MAX_QUEUED_SESSION_TASKS = 4
MAX_SUBAGENT_LINK_ID_BYTES = 512


class SessionTaskKind(StrEnum):
    """The owned work category, independent of its eventual implementation.

    表示任务所属的工作类别,与最终实现方式无关."""

    PLAN_EXECUTION = "plan_execution"
    SUBAGENT = "subagent"


class SessionTaskStatus(StrEnum):
    """Lifecycle states with no implicit retry or automatic follow-up.

    定义没有隐式重试或自动后续动作的生命周期状态."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            SessionTaskStatus.COMPLETED,
            SessionTaskStatus.FAILED,
            SessionTaskStatus.CANCELLED,
        }

    @property
    def active(self) -> bool:
        """Whether the task is waiting for or currently receiving execution.

        表示任务正在等待执行还是正在接收执行."""

        return self in {SessionTaskStatus.QUEUED, SessionTaskStatus.RUNNING}


@dataclass(frozen=True, slots=True)
class SubagentLink:
    """Durable ownership link between one parent task and one child session.

    表示一个父任务与一个子会话之间的持久归属关系.

    The link stores identifiers and creation time only.  Prompts, credentials,
    tool arguments, and model output remain in their own bounded owners.
    该链接只保存标识符和创建时间. 提示词、凭据、工具参数和模型输出仍由各自有界的所有者管理.
    """

    parent_session_id: str
    parent_task_id: str
    child_session_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.parent_session_id, "parent_session_id"),
            (self.parent_task_id, "parent_task_id"),
            (self.child_session_id, "child_session_id"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\x00" in value
                or len(value.encode("utf-8")) > MAX_SUBAGENT_LINK_ID_BYTES
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError(f"{field_name} must be a bounded safe identifier")
        if self.parent_session_id == self.child_session_id:
            raise ValueError("subagent child session must differ from its parent session")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("subagent link creation time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SessionTask:
    """A bounded durable record for one execution owned by a session.

    表示会话拥有的一次执行的有界持久化记录."""

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

    def start(self, *, started_at: datetime) -> SessionTask:
        """Return the one allowed queued-to-running transition.

        返回唯一允许的 queued 到 running 转换."""

        if self.status is not SessionTaskStatus.QUEUED:
            raise ValueError("session task is not queued")
        return SessionTask(
            self.task_id,
            self.kind,
            SessionTaskStatus.RUNNING,
            started_at,
            plan_snapshot=self.plan_snapshot,
        )

    def finish(self, status: SessionTaskStatus, *, finished_at: datetime) -> SessionTask:
        """Return the one allowed terminal transition for this task.

        返回当前任务唯一允许的终态转换."""

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
    "MAX_QUEUED_SESSION_TASKS",
    "MAX_SESSION_TASK_ID_BYTES",
    "MAX_SUBAGENT_LINK_ID_BYTES",
    "SessionTask",
    "SessionTaskKind",
    "SessionTaskStatus",
    "SubagentLink",
]
