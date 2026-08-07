"""Compatibility facade for the canonical background-task package.

提供后台任务领域包的兼容门面,并重新导出规范实现."""

from neuro_code.domain.background_tasks.models import (
    DEFAULT_BACKGROUND_WAKE_COOLDOWN_SECONDS,
    DEFAULT_BACKGROUND_WAKE_MAX_PER_SESSION,
    MAX_BACKGROUND_TASK_WAIT_IDS,
    MAX_BACKGROUND_WAKE_COUNT,
    MAX_BACKGROUND_WAKE_TASK_IDS,
    BackgroundTaskKillOutcome,
    BackgroundTaskKillResult,
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
    BackgroundTaskWakePolicy,
    BackgroundWakeDecision,
    BackgroundWakeLimits,
    BackgroundWakeState,
)

__all__ = [
    "DEFAULT_BACKGROUND_WAKE_COOLDOWN_SECONDS",
    "DEFAULT_BACKGROUND_WAKE_MAX_PER_SESSION",
    "MAX_BACKGROUND_TASK_WAIT_IDS",
    "MAX_BACKGROUND_WAKE_COUNT",
    "MAX_BACKGROUND_WAKE_TASK_IDS",
    "BackgroundTaskKillOutcome",
    "BackgroundTaskKillResult",
    "BackgroundTaskSnapshot",
    "BackgroundTaskStatus",
    "BackgroundTaskWaitMode",
    "BackgroundTaskWaitResult",
    "BackgroundTaskWakePolicy",
    "BackgroundWakeDecision",
    "BackgroundWakeLimits",
    "BackgroundWakeState",
]
