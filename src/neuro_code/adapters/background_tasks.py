"""Compatibility facade for the canonical background-task manager.

提供后台任务管理器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.background_tasks import LocalBackgroundTaskManager

__all__ = ["LocalBackgroundTaskManager"]
