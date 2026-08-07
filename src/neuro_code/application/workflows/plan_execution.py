"""Typed application seam for executing a saved plan.

The profile conversation controller remains the owner of the plan runner,
turn serialization, SessionTask lifecycle, permissions, event delivery, and
cancellation.  This service only exposes the inbound ExecutePlan intent; it
does not copy or manage any of those resources.

定义执行已保存计划的类型化应用接缝. 计划运行器、回合串行化、任务生命周期、权限和取消仍由会话控制器负责.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.runtime.agent import AgentRunResult, EventSink


@dataclass(frozen=True, slots=True)
class ExecutePlanRequest:
    """Validated intent to execute the current saved plan.

    ``task_id`` is optional because the interactive plan entry executes the
    current plan directly.  Queued ``SessionTask`` start/finish transitions
    remain owned by the existing session-task controller and are deliberately
    outside this first application slice.

    表示执行当前已保存计划的已验证意图. task_id 可选,因为交互式入口直接执行当前计划.
    """

    task_id: str | None = None

    def __post_init__(self) -> None:
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id.strip()
        ):
            raise ValueError("task_id must be non-empty when provided")


class PlanExecutionController(Protocol):
    """Minimal existing owner consumed by the application workflow facade.

    表示应用工作流门面使用的最小现有所有者契约."""

    async def execute_plan(
        self,
        *,
        sink: EventSink | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult: ...


class PlanExecutionService:
    """Expose ExecutePlan without owning runtime or task lifecycle.

    暴露 ExecutePlan 用例,但不拥有运行时或任务生命周期."""

    __slots__ = ("_controller",)

    def __init__(self, controller: PlanExecutionController) -> None:
        self._controller = controller

    async def execute_plan(
        self,
        request: ExecutePlanRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Delegate a typed request while preserving cancellation and errors.

        委托类型化请求,同时保留取消和错误语义."""

        if not isinstance(request, ExecutePlanRequest):
            raise ValueError("execute plan request must be canonical")
        return await self._controller.execute_plan(
            sink=sink,
            task_id=request.task_id,
        )


__all__ = [
    "ExecutePlanRequest",
    "PlanExecutionController",
    "PlanExecutionService",
]
