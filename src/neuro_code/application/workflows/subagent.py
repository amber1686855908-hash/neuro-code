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
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.workflows.subagent_capabilities import MAX_SUBAGENT_STEPS
from neuro_code.domain.execution import AgentExecutionOutcome
from neuro_code.domain.session_tasks import (
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
    SubagentLink,
)
from neuro_code.shared.errors import ConfigurationError, SubagentTimeoutError
from neuro_code.shared.redaction import redact_sensitive_text

MAX_SUBAGENT_PROMPT_BYTES = 16 * 1024
MAX_SUBAGENT_SESSION_ID_BYTES = 512
MAX_SUBAGENT_TIMEOUT_SECONDS = 300.0
MAX_SUBAGENT_RESULT_BYTES = 32 * 1024


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


@runtime_checkable
class IsolatedSubagentRuntime(Protocol):
    """Fresh child runtime capability used by the isolated workflow.

    由隔离工作流使用的全新子运行时能力.

    The runtime owns its child conversation and resources.  It must never
    receive the parent's messages or mutable context.
    运行时拥有自己的子会话和资源,绝不能接收父会话消息或可变上下文.
    """

    @property
    def child_session_id(self) -> str: ...

    @property
    def capability_fingerprint(self) -> str: ...

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...

    async def close(self) -> None: ...


@runtime_checkable
class IsolatedSubagentRuntimeFactory(Protocol):
    """Create one fresh, capability-restricted child runtime."""

    async def create(
        self,
        request: RunSubagentRequest,
        *,
        parent_task_id: str,
    ) -> IsolatedSubagentRuntime: ...


@dataclass(frozen=True, slots=True)
class SubagentRunResult:
    """Result paired with the durable terminal task metadata.

    将模型运行结果与持久化终态任务元数据配对.
    """

    task: SessionTask
    result: AgentRunResult
    link: SubagentLink | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task, SessionTask):
            raise ValueError("subagent result task must be canonical")
        if self.task.kind is not SessionTaskKind.SUBAGENT:
            raise ValueError("subagent result task must have subagent kind")
        if not self.task.status.terminal:
            raise ValueError("subagent result task must be terminal")
        if not isinstance(self.result, AgentRunResult):
            raise ValueError("subagent result run result must be canonical")
        if self.link is not None:
            if self.link.parent_session_id == self.link.child_session_id:
                raise ValueError("subagent result link cannot self-reference")
            if self.link.parent_task_id != self.task.task_id:
                raise ValueError("subagent result link must reference its task")


def _bounded_utf8_text(value: str, *, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    suffix = "..."
    suffix_bytes = len(suffix.encode("utf-8"))
    prefix = encoded[: limit - suffix_bytes].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}", True


def _validate_result_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 4 <= value <= MAX_SUBAGENT_RESULT_BYTES
    ):
        raise ValueError(
            f"subagent result limit must be between 4 and {MAX_SUBAGENT_RESULT_BYTES} bytes"
        )
    return value


@dataclass(frozen=True, slots=True)
class SubagentResultProjection:
    """Bounded caller-facing result without child transcript details.

    The projection contains no messages, events, tool arguments, or raw model
    context.  Its response is redacted and bounded before it reaches callers.

    面向调用方的有界结果,不包含子会话 transcript 细节.
    """

    parent_session_id: str
    task_id: str
    child_session_id: str
    status: SessionTaskStatus
    response: str
    steps: int
    truncated: bool
    outcome: AgentExecutionOutcome | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.parent_session_id, field_name="parent_session_id")
        _validate_identifier(self.task_id, field_name="task_id")
        _validate_identifier(self.child_session_id, field_name="child_session_id")
        if not isinstance(self.status, SessionTaskStatus) or not self.status.terminal:
            raise ValueError("subagent result projection status must be terminal")
        if not isinstance(self.response, str):
            raise ValueError("subagent result projection response must be a string")
        if len(self.response.encode("utf-8")) > MAX_SUBAGENT_RESULT_BYTES:
            raise ValueError("subagent result projection response is too large")
        if isinstance(self.steps, bool) or not isinstance(self.steps, int) or self.steps < 0:
            raise ValueError("subagent result projection steps must be non-negative")
        if not isinstance(self.truncated, bool):
            raise ValueError("subagent result projection truncated must be a bool")
        if self.outcome is not None and not isinstance(self.outcome, AgentExecutionOutcome):
            raise ValueError("subagent result projection outcome must be canonical")


@runtime_checkable
class SubagentExecutionController(Protocol):
    """Minimal runner consumed by the safe result projection facade."""

    async def run_subagent(
        self,
        request: RunSubagentRequest,
        *,
        sink: EventSink | None = None,
    ) -> SubagentRunResult: ...


class ReadOnlySubagentApplicationService:
    """Expose one explicit read-only subagent run as a safe projection.

    This facade never appends to the parent session and never returns the
    child's messages, events, tool arguments, or model context.

    以安全投影暴露一次明确的只读子代理运行.
    该 facade 不会追加父会话,也不会返回子会话消息、事件、工具参数或模型上下文.
    """

    __slots__ = ("_controller", "_max_result_bytes", "_redaction_values")

    def __init__(
        self,
        controller: SubagentExecutionController,
        *,
        redaction_values: tuple[str, ...] = (),
        max_result_bytes: int = MAX_SUBAGENT_RESULT_BYTES,
    ) -> None:
        if not isinstance(controller, SubagentExecutionController):
            raise ConfigurationError("subagent execution controller is invalid")
        if not isinstance(redaction_values, tuple) or not all(
            isinstance(value, str) for value in redaction_values
        ):
            raise TypeError("redaction_values must be a tuple of strings")
        self._controller = controller
        self._redaction_values = tuple(value for value in redaction_values if value)
        self._max_result_bytes = _validate_result_limit(max_result_bytes)

    async def run_subagent(self, request: RunSubagentRequest) -> SubagentResultProjection:
        """Run once and return only the bounded, redacted child result."""

        if not isinstance(request, RunSubagentRequest):
            raise ValueError("run subagent request must be canonical")
        run_result = await self._controller.run_subagent(request)
        link = run_result.link
        if link is None or link.parent_session_id != request.parent_session_id:
            raise ConfigurationError("subagent result does not contain its parent link")
        child_session_id = link.child_session_id
        if run_result.result.session_id != child_session_id:
            raise ConfigurationError("subagent result link does not match child result")
        safe_response = redact_sensitive_text(
            run_result.result.response,
            explicit_values=self._redaction_values,
        )
        safe_response, truncated = _bounded_utf8_text(
            safe_response,
            limit=self._max_result_bytes,
        )
        return SubagentResultProjection(
            parent_session_id=request.parent_session_id,
            task_id=run_result.task.task_id,
            child_session_id=child_session_id,
            status=run_result.task.status,
            response=safe_response,
            steps=run_result.result.steps,
            truncated=truncated,
            outcome=run_result.result.outcome,
        )


class IsolatedSubagentExecutionService:
    """Run one fresh read-only child runtime with bounded cleanup.

    运行一次全新的只读子运行时并执行有界清理.

    This service has no queue, retry, automatic scheduling, recursive spawn,
    or parent-context projection.  A durable link is written before the child
    starts so restart can discover the child session even when execution later
    fails or is cancelled.
    本服务没有队列、重试、自动调度、递归创建或父上下文投影. 子运行开始前先写入持久链接,
    因此即使后续失败或取消,重启后仍能找到子会话.
    """

    __slots__ = ("_lock", "_runtime_factory", "_store", "_timeout_seconds")

    def __init__(
        self,
        store: SessionStore,
        runtime_factory: IsolatedSubagentRuntimeFactory,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0 < timeout_seconds <= MAX_SUBAGENT_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "subagent timeout_seconds must be finite and between 0 and "
                f"{MAX_SUBAGENT_TIMEOUT_SECONDS}"
            )
        if not isinstance(runtime_factory, IsolatedSubagentRuntimeFactory):
            raise ConfigurationError("isolated subagent runtime factory is invalid")
        self._store = store
        self._runtime_factory = runtime_factory
        self._timeout_seconds = float(timeout_seconds)
        self._lock = asyncio.Lock()

    async def run_subagent(
        self,
        request: RunSubagentRequest,
        *,
        sink: EventSink | None = None,
    ) -> SubagentRunResult:
        """Create ownership, run once, and preserve failure/cancellation semantics."""

        if not isinstance(request, RunSubagentRequest):
            raise ValueError("run subagent request must be canonical")
        async with self._lock:
            task = SessionTask(
                f"subagent-{uuid.uuid4().hex}",
                SessionTaskKind.SUBAGENT,
                SessionTaskStatus.RUNNING,
                datetime.now(UTC),
            )
            await self._store.create_session_task(request.parent_session_id, task)
            runtime: IsolatedSubagentRuntime | None = None
            link: SubagentLink | None = None
            try:
                runtime = await self._runtime_factory.create(
                    request,
                    parent_task_id=task.task_id,
                )
                if not isinstance(runtime, IsolatedSubagentRuntime):
                    raise ConfigurationError(
                        "isolated subagent factory returned an invalid runtime"
                    )
                link = SubagentLink(
                    request.parent_session_id,
                    task.task_id,
                    runtime.child_session_id,
                    datetime.now(UTC),
                )
                await self._store.save_subagent_link(link)
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._cleanup_unlinked_runtime(runtime, request.parent_session_id)
                )
                await asyncio.shield(
                    self._finish_task(request.parent_session_id, task, SessionTaskStatus.CANCELLED)
                )
                raise
            except Exception:
                await self._cleanup_unlinked_runtime(runtime, request.parent_session_id)
                await self._finish_task(request.parent_session_id, task, SessionTaskStatus.FAILED)
                raise

            try:
                async with asyncio.timeout(self._timeout_seconds):
                    result = await runtime.run(request.prompt, sink=sink)
                if result.session_id != runtime.child_session_id:
                    raise ConfigurationError(
                        "isolated subagent runtime returned a different child session"
                    )
            except asyncio.CancelledError:
                await asyncio.shield(self._discard_runtime(runtime))
                await asyncio.shield(
                    self._finish_task(request.parent_session_id, task, SessionTaskStatus.CANCELLED)
                )
                raise
            except TimeoutError as error:
                await self._discard_runtime(runtime)
                await self._finish_task(request.parent_session_id, task, SessionTaskStatus.FAILED)
                raise SubagentTimeoutError(
                    f"subagent exceeded its {self._timeout_seconds:g}-second wall-clock limit"
                ) from error
            except Exception:
                await self._discard_runtime(runtime)
                await self._finish_task(request.parent_session_id, task, SessionTaskStatus.FAILED)
                raise
            cleanup_error = await self._discard_runtime(runtime)
            if cleanup_error is not None:
                await self._finish_task(request.parent_session_id, task, SessionTaskStatus.FAILED)
                raise cleanup_error
            completed = await self._finish_task(
                request.parent_session_id,
                task,
                SessionTaskStatus.COMPLETED,
            )
            return SubagentRunResult(completed, result, link)

    async def _discard_runtime(
        self,
        runtime: IsolatedSubagentRuntime | None,
    ) -> BaseException | None:
        if runtime is None:
            return None
        try:
            await asyncio.shield(runtime.close())
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return error
        return None

    async def _cleanup_unlinked_runtime(
        self,
        runtime: IsolatedSubagentRuntime | None,
        parent_session_id: str,
    ) -> None:
        await self._discard_runtime(runtime)
        if runtime is None or runtime.child_session_id == parent_session_id:
            return
        try:
            await self._store.delete_session(runtime.child_session_id)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise

    async def _finish_task(
        self,
        session_id: str,
        task: SessionTask,
        status: SessionTaskStatus,
    ) -> SessionTask:
        finished = task.finish(status, finished_at=datetime.now(UTC))
        await self._store.update_session_task(session_id, finished)
        return finished


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
    "MAX_SUBAGENT_RESULT_BYTES",
    "MAX_SUBAGENT_SESSION_ID_BYTES",
    "MAX_SUBAGENT_STEPS",
    "MAX_SUBAGENT_TIMEOUT_SECONDS",
    "IsolatedSubagentExecutionService",
    "IsolatedSubagentRuntime",
    "IsolatedSubagentRuntimeFactory",
    "ReadOnlySubagentApplicationService",
    "RunSubagentRequest",
    "SubagentExecutionController",
    "SubagentExecutionService",
    "SubagentExecutor",
    "SubagentExecutorFactory",
    "SubagentResultProjection",
    "SubagentRunResult",
]
