"""Explicit, bounded subagent task lifecycle.

This module owns the application boundary for one caller-requested subagent
run.  The executor is injected so that the lifecycle does not silently create
another provider, inherit the parent conversation, or become an automatic
scheduler.  The durable ``SessionTask`` stores metadata only; prompts,
tool arguments, and model output remain outside the task record.

显式且有界的子代理任务生命周期.

本模块负责一次由调用方明确请求的子代理运行应用边界. 执行器通过注入提供,
因此生命周期不会偷偷创建另一个 Provider、继承父会话或变成自动调度器.
持久化 ``SessionTask`` 只保存元数据;提示词、工具参数和模型输出不会进入任务记录.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.shared.errors import ConfigurationError

MAX_SUBAGENT_PROMPT_BYTES = 16 * 1024
MAX_SUBAGENT_STEPS = 12
MAX_SUBAGENT_SESSION_ID_BYTES = 512


@dataclass(frozen=True, slots=True)
class RunSubagentRequest:
    """Validated intent for one explicitly requested subagent run.

    The request carries only bounded input needed by the executor.  It does
    not carry parent messages, tool definitions, credentials, or raw output.

    表示一次明确请求的子代理运行意图. 请求只携带执行器所需的有界输入,
    不携带父消息、工具定义、凭据或原始输出.
    """

    parent_session_id: str
    prompt: str
    max_steps: int = 8

    def __post_init__(self) -> None:
        _validate_identifier(self.parent_session_id, field_name="parent_session_id")
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or "\x00" in self.prompt
            or len(self.prompt.encode("utf-8")) > MAX_SUBAGENT_PROMPT_BYTES
        ):
            raise ValueError("subagent prompt must be non-empty and bounded")
        if any(ord(character) < 32 and character not in "\n\t\r" for character in self.prompt):
            raise ValueError("subagent prompt contains an unsafe control character")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or not 1 <= self.max_steps <= MAX_SUBAGENT_STEPS
        ):
            raise ValueError(f"subagent max_steps must be between 1 and {MAX_SUBAGENT_STEPS}")


@runtime_checkable
class SubagentExecutor(Protocol):
    """Execute one isolated request without owning task persistence.

    The implementation must create a fresh child runtime/context and must
    not reuse the parent conversation.  Tool and permission capabilities are
    selected by that implementation, not by this lifecycle service.

    执行一次隔离请求但不负责任务持久化. 实现必须创建新的子运行时/上下文,
    不得复用父会话;工具和权限能力由实现选择,而非由本生命周期服务决定.
    """

    async def run(
        self,
        request: RunSubagentRequest,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...


SubagentExecutorFactory = Callable[[], SubagentExecutor]


@dataclass(frozen=True, slots=True)
class SubagentRunResult:
    """Result paired with the durable terminal task metadata.

    将模型运行结果与持久化终态任务元数据配对.
    """

    task: SessionTask
    result: AgentRunResult

    def __post_init__(self) -> None:
        if not isinstance(self.task, SessionTask):
            raise ValueError("subagent result task must be canonical")
        if self.task.kind is not SessionTaskKind.SUBAGENT:
            raise ValueError("subagent result task must have subagent kind")
        if not self.task.status.terminal:
            raise ValueError("subagent result task must be terminal")
        if not isinstance(self.result, AgentRunResult):
            raise ValueError("subagent result run result must be canonical")


class SubagentExecutionService:
    """Run one explicit subagent and persist only its bounded lifecycle.

    This service deliberately has no queue, retry policy, parent-context
    projection, or automatic scheduling.  A caller must provide the executor
    explicitly for every service instance.  A process-local lock prevents two
    requests through the same service from overlapping; cross-process
    coordination remains a later storage contract.

    运行一次明确的子代理并只持久化其有界生命周期.
    本服务刻意没有队列、重试策略、父上下文投影或自动调度;每个服务实例都必须显式提供执行器.
    同一个服务实例使用进程内锁避免请求重叠;跨进程协调留待后续存储契约.
    """

    __slots__ = ("_executor_factory", "_lock", "_store")

    def __init__(
        self,
        store: SessionStore,
        executor_factory: SubagentExecutorFactory,
    ) -> None:
        self._store = store
        self._executor_factory = executor_factory
        self._lock = asyncio.Lock()

    async def run_subagent(
        self,
        request: RunSubagentRequest,
        *,
        sink: EventSink | None = None,
    ) -> SubagentRunResult:
        """Run the injected executor and persist exactly one terminal task.

        The task is created before the executor starts.  Normal failures and
        cancellation update the task before the original exception is
        propagated; no error is converted into a successful result.

        运行注入的执行器并恰好持久化一个终态任务.
        任务在执行器启动前创建;普通失败和取消会先更新任务再传播原始异常,
        不会把错误转换为成功结果.
        """

        if not isinstance(request, RunSubagentRequest):
            raise ValueError("run subagent request must be canonical")
        async with self._lock:
            executor = self._executor_factory()
            if not isinstance(executor, SubagentExecutor):
                raise ConfigurationError("subagent executor factory returned an invalid executor")
            task = SessionTask(
                f"subagent-{uuid.uuid4().hex}",
                SessionTaskKind.SUBAGENT,
                SessionTaskStatus.RUNNING,
                datetime.now(UTC),
            )
            await self._store.create_session_task(request.parent_session_id, task)
            try:
                result = await executor.run(request, sink=sink)
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._finish_task(request.parent_session_id, task, SessionTaskStatus.CANCELLED)
                )
                raise
            except Exception:
                await self._finish_task(request.parent_session_id, task, SessionTaskStatus.FAILED)
                raise
            completed = await self._finish_task(
                request.parent_session_id,
                task,
                SessionTaskStatus.COMPLETED,
            )
            return SubagentRunResult(completed, result)

    async def _finish_task(
        self,
        session_id: str,
        task: SessionTask,
        status: SessionTaskStatus,
    ) -> SessionTask:
        finished = task.finish(status, finished_at=datetime.now(UTC))
        await self._store.update_session_task(session_id, finished)
        return finished


def _validate_identifier(value: str, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_SUBAGENT_SESSION_ID_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")


__all__ = [
    "MAX_SUBAGENT_PROMPT_BYTES",
    "MAX_SUBAGENT_SESSION_ID_BYTES",
    "MAX_SUBAGENT_STEPS",
    "RunSubagentRequest",
    "SubagentExecutionService",
    "SubagentExecutor",
    "SubagentExecutorFactory",
    "SubagentRunResult",
]
