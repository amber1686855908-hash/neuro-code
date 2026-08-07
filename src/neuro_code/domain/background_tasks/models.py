from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from pathlib import Path

MAX_BACKGROUND_TASK_WAIT_IDS = 20
MAX_BACKGROUND_WAKE_TASK_IDS = 64
MAX_BACKGROUND_WAKE_COUNT = 64
DEFAULT_BACKGROUND_WAKE_MAX_PER_SESSION = 8
DEFAULT_BACKGROUND_WAKE_COOLDOWN_SECONDS = 5.0


class BackgroundTaskStatus(StrEnum):
    """Lifecycle states exposed by an owned background command.

    定义由后台命令所有者暴露的生命周期状态."""

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
    """Completion condition for an event-driven multi-task wait.

    表示事件驱动多任务等待的完成条件."""

    WAIT_ANY = "wait_any"
    WAIT_ALL = "wait_all"


class BackgroundTaskWakePolicy(StrEnum):
    """Controls whether an idle TUI may start a bounded model wake turn.

    控制空闲 TUI 是否可以启动有界模型唤醒回合."""

    DISABLED = "disabled"
    ENABLED = "enabled"


class BackgroundWakeDecision(StrEnum):
    """Why a persisted wake ledger may or may not start a model wake.

    说明持久化唤醒账本可以或不可以启动模型唤醒的原因."""

    ALLOW = "allow"
    NO_PENDING_TASKS = "no_pending_tasks"
    IN_FLIGHT = "in_flight"
    COOLDOWN = "cooldown"
    BUDGET_LIMITED = "budget_limited"


@dataclass(frozen=True, slots=True)
class BackgroundWakeLimits:
    """Bound autonomous wakes for one durable conversation session.

    限制一个持久化会话可以执行的自主唤醒次数."""

    max_wakes_per_session: int = DEFAULT_BACKGROUND_WAKE_MAX_PER_SESSION
    cooldown_seconds: float = DEFAULT_BACKGROUND_WAKE_COOLDOWN_SECONDS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_wakes_per_session, int)
            or isinstance(self.max_wakes_per_session, bool)
            or self.max_wakes_per_session <= 0
        ):
            raise ValueError("max_wakes_per_session must be a positive integer")
        if (
            not isinstance(self.cooldown_seconds, int | float)
            or isinstance(self.cooldown_seconds, bool)
            or self.cooldown_seconds <= 0
            or not isfinite(self.cooldown_seconds)
        ):
            raise ValueError("cooldown_seconds must be a positive number")


def _validate_wake_task_ids(
    values: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if len(values) > MAX_BACKGROUND_WAKE_TASK_IDS:
        raise ValueError(f"{field_name} exceeds the bounded task limit")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must contain unique task IDs")
    for task_id in values:
        if (
            not isinstance(task_id, str)
            or not task_id
            or len(task_id) > 128
            or "\x00" in task_id
            or any(ord(character) < 32 or ord(character) == 127 for character in task_id)
        ):
            raise ValueError(f"{field_name} contains an invalid task ID")
    return values


@dataclass(frozen=True, slots=True)
class BackgroundWakeState:
    """Minimal durable ledger for restart-aware background wakes.

    The ledger intentionally contains task IDs and scheduling metadata only.
    It never stores command text, working-directory paths, output, credentials,
    or a synthetic task result.

    定义可感知重启的后台唤醒最小持久化账本. 只保存任务 ID 和调度元数据,不保存命令、路径、输出或凭据.
    """

    announced_task_ids: tuple[str, ...] = ()
    pending_task_ids: tuple[str, ...] = ()
    wake_count: int = 0
    last_wake_at: datetime | None = None
    wake_in_flight: bool = False

    def __post_init__(self) -> None:
        announced = _validate_wake_task_ids(
            tuple(self.announced_task_ids),
            field_name="announced_task_ids",
        )
        pending = _validate_wake_task_ids(
            tuple(self.pending_task_ids),
            field_name="pending_task_ids",
        )
        if not set(pending).issubset(announced):
            raise ValueError("pending_task_ids must be a subset of announced_task_ids")
        if (
            not isinstance(self.wake_count, int)
            or isinstance(self.wake_count, bool)
            or self.wake_count < 0
            or self.wake_count > MAX_BACKGROUND_WAKE_COUNT
        ):
            raise ValueError("wake_count is outside the bounded range")
        if self.last_wake_at is not None:
            if self.last_wake_at.tzinfo is None:
                raise ValueError("last_wake_at must be timezone-aware")
            object.__setattr__(self, "last_wake_at", self.last_wake_at.astimezone(UTC))
        if not isinstance(self.wake_in_flight, bool):
            raise ValueError("wake_in_flight must be a bool")
        object.__setattr__(self, "announced_task_ids", announced)
        object.__setattr__(self, "pending_task_ids", pending)

    def decision(
        self,
        now: datetime,
        *,
        limits: BackgroundWakeLimits,
    ) -> BackgroundWakeDecision:
        """Return a deterministic scheduling decision without changing state.

        返回确定性的调度决策,不改变当前状态."""

        if now.tzinfo is None:
            raise ValueError("wake decision time must be timezone-aware")
        if not isinstance(limits, BackgroundWakeLimits):
            raise TypeError("limits must be BackgroundWakeLimits")
        if not self.pending_task_ids:
            return BackgroundWakeDecision.NO_PENDING_TASKS
        if self.wake_in_flight:
            return BackgroundWakeDecision.IN_FLIGHT
        if self.wake_count >= limits.max_wakes_per_session:
            return BackgroundWakeDecision.BUDGET_LIMITED
        if self.last_wake_at is not None:
            cooldown_until = self.last_wake_at + timedelta(seconds=limits.cooldown_seconds)
            if now.astimezone(UTC) < cooldown_until:
                return BackgroundWakeDecision.COOLDOWN
        return BackgroundWakeDecision.ALLOW

    def record_terminal_task(self, task_id: str, *, enqueue: bool) -> BackgroundWakeState:
        """Record one observed terminal task without retaining sensitive details.

        记录一个已观察到的终端任务,不保留敏感细节."""

        _validate_wake_task_ids((task_id,), field_name="task_id")
        announced = self._append_bounded(self.announced_task_ids, task_id)
        pending = self.pending_task_ids
        if enqueue:
            pending = self._append_bounded(pending, task_id)
        pending = tuple(candidate for candidate in pending if candidate in announced)
        return BackgroundWakeState(
            announced_task_ids=announced,
            pending_task_ids=pending,
            wake_count=self.wake_count,
            last_wake_at=self.last_wake_at,
            wake_in_flight=self.wake_in_flight,
        )

    def reconcile_visible_tasks(self, task_ids: set[str]) -> BackgroundWakeState:
        """Drop pending IDs no longer owned by the current task supervisor.

        丢弃当前任务监督器已不再拥有的待处理任务 ID."""

        if any(not isinstance(task_id, str) for task_id in task_ids):
            raise TypeError("visible task IDs must be strings")
        pending = tuple(task_id for task_id in self.pending_task_ids if task_id in task_ids)
        if pending == self.pending_task_ids:
            return self
        return BackgroundWakeState(
            announced_task_ids=self.announced_task_ids,
            pending_task_ids=pending,
            wake_count=self.wake_count,
            last_wake_at=self.last_wake_at,
            wake_in_flight=self.wake_in_flight,
        )

    def begin_wake(self, now: datetime, *, limits: BackgroundWakeLimits) -> BackgroundWakeState:
        """Persist an in-flight marker before starting one allowed wake.

        在启动一次允许的唤醒前持久化进行中标记."""

        if now.tzinfo is None:
            raise ValueError("wake start time must be timezone-aware")
        if self.decision(now, limits=limits) is not BackgroundWakeDecision.ALLOW:
            raise ValueError("background wake is not currently allowed")
        return BackgroundWakeState(
            announced_task_ids=self.announced_task_ids,
            pending_task_ids=self.pending_task_ids,
            wake_count=self.wake_count,
            last_wake_at=self.last_wake_at,
            wake_in_flight=True,
        )

    def complete_wake(
        self,
        task_ids: tuple[str, ...],
        *,
        completed_at: datetime,
    ) -> BackgroundWakeState:
        """Consume one completed wake only after its turn completed successfully.

        仅在唤醒回合成功完成后消费一条已完成唤醒记录."""

        _validate_wake_task_ids(tuple(task_ids), field_name="task_ids")
        if completed_at.tzinfo is None:
            raise ValueError("wake completion time must be timezone-aware")
        if not self.wake_in_flight:
            raise ValueError("background wake is not in flight")
        consumed = set(task_ids)
        return BackgroundWakeState(
            announced_task_ids=self.announced_task_ids,
            pending_task_ids=tuple(
                task_id for task_id in self.pending_task_ids if task_id not in consumed
            ),
            wake_count=self.wake_count + 1,
            last_wake_at=completed_at.astimezone(UTC),
            wake_in_flight=False,
        )

    def abandon_wake(self, *, failed_at: datetime) -> BackgroundWakeState:
        """Clear a failed wake while retaining pending work and applying cooldown.

        清除失败的唤醒,同时保留待处理工作并应用冷却时间."""

        if failed_at.tzinfo is None:
            raise ValueError("wake failure time must be timezone-aware")
        if not self.wake_in_flight:
            return self
        return BackgroundWakeState(
            announced_task_ids=self.announced_task_ids,
            pending_task_ids=self.pending_task_ids,
            wake_count=self.wake_count,
            last_wake_at=failed_at.astimezone(UTC),
            wake_in_flight=False,
        )

    def recover_after_restart(self) -> BackgroundWakeState:
        """Make a crashed/interrupted wake retryable without replaying it twice.

        使崩溃或中断的唤醒可以重试,同时避免重复重放."""

        if not self.wake_in_flight:
            return self
        return BackgroundWakeState(
            announced_task_ids=self.announced_task_ids,
            pending_task_ids=self.pending_task_ids,
            wake_count=self.wake_count,
            last_wake_at=self.last_wake_at,
            wake_in_flight=False,
        )

    @staticmethod
    def _append_bounded(values: tuple[str, ...], task_id: str) -> tuple[str, ...]:
        if task_id in values:
            return values
        combined = (*values, task_id)
        return combined[-MAX_BACKGROUND_WAKE_TASK_IDS:]


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
    completion_reported: bool = False
    output_artifact_id: str | None = None
    output_artifact_path: str | None = None
    output_artifact_bytes: int | None = None
    output_artifact_truncated: bool = False

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
        if not isinstance(self.completion_reported, bool):
            raise ValueError("background task completion_reported must be a bool")
        if self.completion_reported and not self.status.terminal:
            raise ValueError("only terminal background tasks can be reported")
        if self.output_artifact_id is None:
            if (
                self.output_artifact_path is not None
                or self.output_artifact_bytes is not None
                or self.output_artifact_truncated
            ):
                raise ValueError("artifact metadata requires an artifact ID")
        elif not self.output_artifact_id or "\x00" in self.output_artifact_id:
            raise ValueError("background task artifact ID must be non-empty")
        elif self.output_artifact_path is None:
            raise ValueError("background task artifact path is required with an ID")
        if self.output_artifact_path is not None:
            if self.output_artifact_path.startswith(("/", "\\")):
                raise ValueError("background task artifact path must be relative")
            if "\x00" in self.output_artifact_path or ".." in Path(self.output_artifact_path).parts:
                raise ValueError("background task artifact path must not escape its root")
        if self.output_artifact_bytes is not None and self.output_artifact_bytes < 0:
            raise ValueError("background task artifact size must not be negative")

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
        if self.output_artifact_id is not None:
            payload.update(
                {
                    "output_artifact_id": self.output_artifact_id,
                    "output_artifact_path": self.output_artifact_path,
                    "output_artifact_bytes": self.output_artifact_bytes,
                    "output_artifact_truncated": self.output_artifact_truncated,
                }
            )
        if include_output:
            payload["output"] = self.output
        return payload


@dataclass(frozen=True, slots=True)
class BackgroundTaskKillResult:
    outcome: BackgroundTaskKillOutcome
    snapshot: BackgroundTaskSnapshot


@dataclass(frozen=True, slots=True)
class BackgroundTaskWaitResult:
    """Ordered known snapshots plus IDs hidden from or absent in this scope.

    表示按顺序排列的已知快照,以及当前范围内隐藏或缺失的任务 ID."""

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
