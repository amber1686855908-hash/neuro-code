"""Typed application seam for explicitly running a queued plan task.

The existing conversation/runtime owners continue to claim, execute, finish,
persist, and publish the task.  This facade only carries the bounded task
identity from an inbound interface to that owner.

定义明确运行排队计划任务的类型化应用接缝. 任务的认领、执行、完成、持久化和发布仍由现有控制器负责.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.runtime.agent import AgentRunResult, EventSink


@dataclass(frozen=True, slots=True)
class RunSessionTaskRequest:
    """Validated intent to run one explicitly selected queued task.

    表示运行一个明确选中的排队任务的已验证意图."""

    task_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must not be empty")


class QueuedPlanExecutionController(Protocol):
    """Existing owner consumed by the queued-plan application facade.

    表示排队计划应用门面使用的现有所有者契约."""

    async def run_session_task(
        self,
        task_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...


class QueuedPlanExecutionService:
    """Expose queued plan execution without owning task lifecycle.

    暴露排队计划执行用例,但不拥有任务生命周期."""

    __slots__ = ("_controller",)

    def __init__(self, controller: QueuedPlanExecutionController) -> None:
        self._controller = controller

    async def run_session_task(
        self,
        request: RunSessionTaskRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Delegate the typed task intent while preserving cancellation/errors.

        委托类型化任务意图,同时保留取消和错误语义."""

        if not isinstance(request, RunSessionTaskRequest):
            raise ValueError("run session task request must be canonical")
        return await self._controller.run_session_task(request.task_id, sink=sink)


__all__ = [
    "QueuedPlanExecutionController",
    "QueuedPlanExecutionService",
    "RunSessionTaskRequest",
]
