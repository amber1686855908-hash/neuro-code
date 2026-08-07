"""Typed application seam for scheduling the current saved plan.

The existing conversation/controller remains the owner of plan validation,
turn serialization, queue limits, task creation, persistence, and error
semantics.  This facade only carries the scheduling command across the
application boundary.

定义调度当前已保存计划的类型化应用接缝. 现有控制器仍负责验证、串行化、队列限制、任务创建和持久化.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.domain.session_tasks import SessionTask


@dataclass(frozen=True, slots=True)
class SchedulePlanRequest:
    """Intent to queue the currently saved plan for explicit execution.

    表示将当前已保存计划排队等待明确执行的意图."""


class PlanSchedulingController(Protocol):
    """Existing owner consumed by the plan-scheduling application facade.

    表示计划调度应用门面使用的现有所有者契约."""

    async def schedule_plan(self) -> SessionTask: ...


class PlanSchedulingService:
    """Expose plan scheduling without owning task or storage lifecycle.

    暴露计划调度用例,但不拥有任务或存储生命周期."""

    __slots__ = ("_controller",)

    def __init__(self, controller: PlanSchedulingController) -> None:
        self._controller = controller

    async def schedule_plan(self, request: SchedulePlanRequest) -> SessionTask:
        """Delegate a canonical scheduling intent while preserving errors.

        委托规范的调度意图,同时保留错误语义."""

        if not isinstance(request, SchedulePlanRequest):
            raise ValueError("schedule plan request must be canonical")
        return await self._controller.schedule_plan()


__all__ = [
    "PlanSchedulingController",
    "PlanSchedulingService",
    "SchedulePlanRequest",
]
