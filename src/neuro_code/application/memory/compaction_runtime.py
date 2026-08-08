"""Guard explicit context compaction at a safe Runtime boundary.

This module does not integrate compaction into ``AgentRuntime``.  It defines
the boundary a future caller must prove before it may invoke the existing
default-disabled trigger: no model request or tool batch may be active, the
operation must not run after cancellation, and compaction must use its own
bounded one-request/no-tool budget.

在安全的 Runtime 边界保护显式上下文压缩。

本模块不会把压缩接入 ``AgentRuntime``, 而是定义未来调用方在调用现有默认关闭触发器前必须证明的边界: 不能有正在进行的模型请求或工具批次, 取消后不得运行, 且压缩必须使用独立的单次请求/无工具有界预算.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    ProviderContextWindow,
)
from neuro_code.application.memory.compaction_trigger import (
    ContextCompactionTriggerAssessment,
    ContextCompactionTriggerMode,
    ContextCompactionTriggerRequest,
    ContextCompactionTriggerResult,
    ContextCompactionTriggerService,
)
from neuro_code.domain.conversation.compaction import (
    MAX_DURABLE_COMPACTION_ID_BYTES,
    DurableCompactionItem,
    compute_compaction_source_fingerprint,
)
from neuro_code.domain.conversation.context import ModelContext, estimate_context_tokens
from neuro_code.domain.conversation.messages import SessionItem
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SupervisorReasonCode,
)
from neuro_code.shared.errors import ConfigurationError, NeuroCodeError, ProviderError, SessionError

DEFAULT_CONTEXT_COMPACTION_TIMEOUT_SECONDS = 30.0
MAX_CONTEXT_COMPACTION_TIMEOUT_SECONDS = 300.0


def _validate_reported_tokens(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer when provided")


def _validate_estimated_tokens(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("token_estimator must return an integer >= 0")
    return value


def build_context_usage_snapshot(
    context: ModelContext,
    provider_window: ProviderContextWindow | None,
    *,
    reported_input_tokens: int | None = None,
    reported_output_tokens: int | None = None,
    token_estimator: Callable[[Sequence[SessionItem]], int] = estimate_context_tokens,
) -> CompactionContextUsage:
    """Build one bounded usage snapshot for a concrete immutable context.

    When both model usage values are present, the snapshot follows the existing
    ``CONTEXT_USAGE_UPDATED`` contract and records input plus output tokens as
    reported.  A missing output value keeps the input value but marks the
    snapshot estimated.  When input usage is unavailable, the frozen context
    is estimated without exposing its contents.  The provider window is
    metadata only; this helper never discovers or changes provider state.

    为一个具体的不可变上下文构建有界用量快照。

    当模型输入和输出用量都存在时, 快照遵循现有
    ``CONTEXT_USAGE_UPDATED`` 契约, 记录已报告的输入加输出 token。输出缺失时保留输入值但将快照标记为估算。
    当输入用量不可用时, 根据冻结上下文估算且不暴露其内容。Provider 窗口只是元数据, 本函数不会发现或修改 Provider 状态。
    """

    if not isinstance(context, ModelContext):
        raise TypeError("context must be a ModelContext")
    if provider_window is not None and not isinstance(provider_window, ProviderContextWindow):
        raise TypeError("provider_window must be a ProviderContextWindow or None")
    if not callable(token_estimator):
        raise TypeError("token_estimator must be callable")
    _validate_reported_tokens("reported_input_tokens", reported_input_tokens)
    _validate_reported_tokens("reported_output_tokens", reported_output_tokens)
    if reported_input_tokens is None and reported_output_tokens is not None:
        raise ValueError("reported_output_tokens requires reported_input_tokens")

    if reported_input_tokens is None:
        used_tokens = _validate_estimated_tokens(token_estimator(context.items))
        estimated = True
    else:
        used_tokens = reported_input_tokens + (reported_output_tokens or 0)
        estimated = reported_output_tokens is None

    if provider_window is None:
        return CompactionContextUsage(
            used_tokens=used_tokens,
            capacity_tokens=None,
            estimated=estimated,
        )
    return CompactionContextUsage.from_provider_window(
        used_tokens,
        provider_window,
        estimated=estimated,
    )


def build_explicit_context_compaction_runtime_request(
    trigger_service: ContextCompactionTriggerService,
    *,
    context: ModelContext,
    boundary: ContextCompactionRuntimeBoundary,
    provider_window: ProviderContextWindow | None,
    protected_item_count: int = 0,
    reported_input_tokens: int | None = None,
    reported_output_tokens: int | None = None,
    session_id: str | None = None,
    compaction_id: str | None = None,
    created_at: datetime | None = None,
    token_estimator: Callable[[Sequence[SessionItem]], int] = estimate_context_tokens,
) -> ContextCompactionRuntimeRequest:
    """Build an explicit request and its stale-source guard without side effects.

    The trigger service is used only for deterministic assessment.  A source
    fingerprint is computed from this exact context snapshot only when the
    plan is actionable; no caller-supplied digest is accepted.  Persistence,
    Provider calls, session locking, and stale validation at execution remain
    owned by the existing Runtime/application boundaries.

    构建显式请求及其过期源保护值, 且不产生副作用。

    本函数只使用触发服务进行确定性评估。仅当计划可执行时才根据这份精确上下文快照计算源指纹, 不接受调用方传入的摘要。
    持久化、Provider 调用、会话加锁以及执行时的过期校验仍由现有 Runtime/应用边界负责。
    """

    if not isinstance(trigger_service, ContextCompactionTriggerService):
        raise TypeError("trigger_service must be a ContextCompactionTriggerService")
    if not isinstance(context, ModelContext):
        raise TypeError("context must be a ModelContext")
    if not isinstance(boundary, ContextCompactionRuntimeBoundary):
        raise TypeError("boundary must be a ContextCompactionRuntimeBoundary")
    usage = build_context_usage_snapshot(
        context,
        provider_window,
        reported_input_tokens=reported_input_tokens,
        reported_output_tokens=reported_output_tokens,
        token_estimator=token_estimator,
    )
    base = ContextCompactionTriggerRequest(
        context=context,
        usage=usage,
        mode=ContextCompactionTriggerMode.EXPLICIT,
        protected_item_count=protected_item_count,
        session_id=session_id,
        compaction_id=compaction_id,
        created_at=created_at,
    )
    assessment = trigger_service.assess(base)
    if not assessment.will_trigger:
        return ContextCompactionRuntimeRequest(base, boundary)
    if session_id is None or compaction_id is None or created_at is None:
        raise ValueError(
            "actionable explicit compaction requires session, compaction, and timestamp"
        )
    candidate_range = assessment.plan.candidate_range
    if candidate_range is None:
        raise ValueError("actionable compaction requires a candidate range")
    guarded = ContextCompactionTriggerRequest(
        context=context,
        usage=usage,
        mode=ContextCompactionTriggerMode.EXPLICIT,
        protected_item_count=protected_item_count,
        session_id=session_id,
        compaction_id=compaction_id,
        expected_source_fingerprint=compute_compaction_source_fingerprint(
            context.items,
            candidate_range,
        ),
        created_at=created_at,
    )
    return ContextCompactionRuntimeRequest(guarded, boundary)


class ContextCompactionTimeoutError(NeuroCodeError):
    """A bounded compaction operation exceeded its wall-clock limit.

    有界上下文压缩操作超过了自身的墙钟时间限制。
    """


class ContextCompactionRuntimeFailureKind(StrEnum):
    """Classify a known failure from an explicitly gated compaction call.

    对显式门控压缩调用产生的已知失败进行分类。
    """

    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROVIDER_FAILURE = "provider_failure"
    STORAGE_FAILURE = "storage_failure"


class ContextCompactionRuntimeFailureHandling(StrEnum):
    """Describe whether a future Runtime may project a failure to a terminal outcome.

    描述未来 Runtime 是否可以将失败投影为终态结果。
    """

    CONTROLLED_TERMINAL = "controlled_terminal"
    PROPAGATE = "propagate"


class ContextCompactionExecutionRecordPolicy(StrEnum):
    """Define who may persist an execution record after compaction failure.

    定义上下文压缩失败后由谁可以持久化执行记录。
    """

    NONE = "none"
    TURN_FINALIZATION = "turn_finalization"


@dataclass(frozen=True, slots=True)
class ContextCompactionRuntimeFailureProjection:
    """Project a known compaction failure without retaining exception details.

    A timeout has a controlled terminal projection for a future Runtime.  The
    projection is not a persisted record: only the caller that owns turn
    finalization may persist it together with that turn's completion event.
    Cancellation, Provider failures, and storage failures remain propagation
    cases and have no execution-record projection.

    投影已知的压缩失败,但不保留异常详情。

    超时为未来 Runtime 提供受控终态投影,但该投影不是持久化记录: 只有拥有回合最终化事务的调用方才可以将它与该回合完成事件一起保存. 取消、Provider 失败和存储失败仍然必须传播,且没有执行记录投影.
    """

    kind: ContextCompactionRuntimeFailureKind
    handling: ContextCompactionRuntimeFailureHandling
    outcome: AgentExecutionOutcome | None
    execution_record_policy: ContextCompactionExecutionRecordPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ContextCompactionRuntimeFailureKind):
            raise TypeError("kind must be a ContextCompactionRuntimeFailureKind")
        if not isinstance(self.handling, ContextCompactionRuntimeFailureHandling):
            raise TypeError("handling must be a ContextCompactionRuntimeFailureHandling")
        if not isinstance(self.execution_record_policy, ContextCompactionExecutionRecordPolicy):
            raise TypeError(
                "execution_record_policy must be a ContextCompactionExecutionRecordPolicy"
            )
        if self.kind is ContextCompactionRuntimeFailureKind.TIMEOUT:
            expected_outcome = AgentExecutionOutcome(
                status=AgentExecutionStatus.BUDGET_LIMITED,
                reason_code=SupervisorReasonCode.WALL_TIME_BUDGET,
                finalized=False,
                recoverable=True,
            )
            if self.handling is not ContextCompactionRuntimeFailureHandling.CONTROLLED_TERMINAL:
                raise ValueError("timeout failures require controlled terminal handling")
            if self.outcome != expected_outcome:
                raise ValueError("timeout failures require a budget-limited outcome")
            if (
                self.execution_record_policy
                is not ContextCompactionExecutionRecordPolicy.TURN_FINALIZATION
            ):
                raise ValueError("timeout outcomes require turn-finalization record ownership")
            return
        if self.handling is not ContextCompactionRuntimeFailureHandling.PROPAGATE:
            raise ValueError("non-timeout compaction failures must propagate")
        if self.outcome is not None:
            raise ValueError("propagated compaction failures must not carry an outcome")
        if self.execution_record_policy is not ContextCompactionExecutionRecordPolicy.NONE:
            raise ValueError("propagated compaction failures must not request a record")


def classify_context_compaction_failure(
    error: BaseException,
) -> ContextCompactionRuntimeFailureProjection | None:
    """Return a bounded policy projection for a known compaction failure.

    The function does not catch, replace, log, or persist the supplied error.
    A future Runtime may call it inside an exception handler and either
    preserve the original exception or explicitly consume the timeout using
    the returned terminal projection.

    为已知的压缩失败返回有界策略投影。

    本函数不会捕获、替换、记录或持久化传入异常. 未来 Runtime 可以在异常处理器中调用它,然后保留原异常,或使用返回的终态投影显式消费超时.
    """

    if isinstance(error, ContextCompactionTimeoutError):
        return ContextCompactionRuntimeFailureProjection(
            kind=ContextCompactionRuntimeFailureKind.TIMEOUT,
            handling=ContextCompactionRuntimeFailureHandling.CONTROLLED_TERMINAL,
            outcome=AgentExecutionOutcome(
                status=AgentExecutionStatus.BUDGET_LIMITED,
                reason_code=SupervisorReasonCode.WALL_TIME_BUDGET,
                finalized=False,
                recoverable=True,
            ),
            execution_record_policy=ContextCompactionExecutionRecordPolicy.TURN_FINALIZATION,
        )
    if isinstance(error, asyncio.CancelledError):
        return ContextCompactionRuntimeFailureProjection(
            kind=ContextCompactionRuntimeFailureKind.CANCELLED,
            handling=ContextCompactionRuntimeFailureHandling.PROPAGATE,
            outcome=None,
            execution_record_policy=ContextCompactionExecutionRecordPolicy.NONE,
        )
    if isinstance(error, ProviderError):
        return ContextCompactionRuntimeFailureProjection(
            kind=ContextCompactionRuntimeFailureKind.PROVIDER_FAILURE,
            handling=ContextCompactionRuntimeFailureHandling.PROPAGATE,
            outcome=None,
            execution_record_policy=ContextCompactionExecutionRecordPolicy.NONE,
        )
    if isinstance(error, SessionError):
        return ContextCompactionRuntimeFailureProjection(
            kind=ContextCompactionRuntimeFailureKind.STORAGE_FAILURE,
            handling=ContextCompactionRuntimeFailureHandling.PROPAGATE,
            outcome=None,
            execution_record_policy=ContextCompactionExecutionRecordPolicy.NONE,
        )
    return None


class ContextCompactionSafePoint(StrEnum):
    """Identify a Runtime point at which a compaction request may be considered.

    标识可以评估压缩请求的 Runtime 安全位置。
    """

    BEFORE_MODEL_REQUEST = "before_model_request"
    AFTER_TOOL_BATCH = "after_tool_batch"


class ContextCompactionBoundaryDecision(StrEnum):
    """Explain why a Runtime compaction request may or may not run.

    说明 Runtime 压缩请求为何可以或不可以执行。
    """

    ALLOW = "allow"
    DISABLED = "disabled"
    NOT_ACTIONABLE = "not_actionable"
    UNSAFE_BOUNDARY = "unsafe_boundary"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ContextCompactionRuntimeBudget:
    """Keep compaction accounting separate from the ordinary turn budget.

    The current summary generator performs exactly one Provider request and no
    tool calls.  Those operation counts are fixed contract values rather than
    caller-tunable limits.  The wall-clock limit is finite, bounded, enforced
    by the runtime gate, and independent from the ordinary turn budget.

    将压缩计量与普通回合预算隔离。

    当前摘要生成器恰好执行一次 Provider 请求且不执行工具, 因此这两个操作次数是固定契约值, 而不是调用方可调的限制. 墙钟限制是有限且有上限的, 由 Runtime 门控真正执行, 并且与普通回合预算相互独立.
    """

    max_model_requests: int = 1
    max_tool_calls: int = 0
    inherits_turn_budget: bool = False
    max_wall_time_seconds: float = DEFAULT_CONTEXT_COMPACTION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.max_model_requests, bool) or self.max_model_requests != 1:
            raise ValueError("context compaction must use exactly one model request")
        if isinstance(self.max_tool_calls, bool) or self.max_tool_calls != 0:
            raise ValueError("context compaction must use zero tool calls")
        if not isinstance(self.inherits_turn_budget, bool):
            raise TypeError("inherits_turn_budget must be a bool")
        if self.inherits_turn_budget:
            raise ValueError("context compaction must not inherit the ordinary turn budget")
        if isinstance(self.max_wall_time_seconds, bool) or not isinstance(
            self.max_wall_time_seconds, (int, float)
        ):
            raise TypeError("max_wall_time_seconds must be a finite positive number")
        if not math.isfinite(float(self.max_wall_time_seconds)):
            raise ValueError("max_wall_time_seconds must be finite")
        if not 0 < self.max_wall_time_seconds <= MAX_CONTEXT_COMPACTION_TIMEOUT_SECONDS:
            raise ValueError(
                "max_wall_time_seconds must be greater than zero and at most "
                f"{MAX_CONTEXT_COMPACTION_TIMEOUT_SECONDS:g}"
            )


@dataclass(frozen=True, slots=True)
class ContextCompactionRuntimeBoundary:
    """Describe the observable state around one possible compaction call.

        A boundary can be reported as unsafe instead of raising, so a future
        Runtime can fail closed without manufacturing a Provider request.  The
        model-step number is diagnostic only and is never consumed as a budget.

        描述一次潜在压缩调用周围可观察的状态。

    边界可以被报告为不安全而不是抛出异常, 以便未来 Runtime 安全地关闭请求而不伪造 Provider 调用. 模型步骤编号仅用于诊断, 绝不会被当作预算消耗.
    """

    safe_point: ContextCompactionSafePoint
    model_step: int
    model_request_active: bool = False
    tool_batch_active: bool = False
    cancellation_requested: bool = False
    budget: ContextCompactionRuntimeBudget = field(
        default_factory=ContextCompactionRuntimeBudget,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.safe_point, ContextCompactionSafePoint):
            raise TypeError("safe_point must be a ContextCompactionSafePoint")
        if isinstance(self.model_step, bool) or not isinstance(self.model_step, int):
            raise TypeError("model_step must be a non-negative integer")
        if self.model_step < 0:
            raise ValueError("model_step must be a non-negative integer")
        for name, value in (
            ("model_request_active", self.model_request_active),
            ("tool_batch_active", self.tool_batch_active),
            ("cancellation_requested", self.cancellation_requested),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")
        if not isinstance(self.budget, ContextCompactionRuntimeBudget):
            raise TypeError("budget must be a ContextCompactionRuntimeBudget")

    @property
    def safe(self) -> bool:
        """Return whether no active operation or cancellation blocks the boundary.

        返回当前边界是否没有活动操作或取消请求阻塞。
        """

        return not (
            self.model_request_active or self.tool_batch_active or self.cancellation_requested
        )


@dataclass(frozen=True, slots=True)
class ContextCompactionRuntimeRequest:
    """Pair a trigger request with the Runtime boundary that guards it.

    将压缩触发请求与保护它的 Runtime 边界配对。
    """

    trigger: ContextCompactionTriggerRequest = field(repr=False)
    boundary: ContextCompactionRuntimeBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, ContextCompactionTriggerRequest):
            raise TypeError("trigger must be a ContextCompactionTriggerRequest")
        if not isinstance(self.boundary, ContextCompactionRuntimeBoundary):
            raise TypeError("boundary must be a ContextCompactionRuntimeBoundary")


@dataclass(frozen=True, slots=True)
class ContextCompactionRuntimeAssessment:
    """Expose the trigger plan and the safe-boundary decision.

    暴露压缩计划和安全边界决定。
    """

    boundary: ContextCompactionRuntimeBoundary
    trigger: ContextCompactionTriggerAssessment
    decision: ContextCompactionBoundaryDecision

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, ContextCompactionRuntimeBoundary):
            raise TypeError("boundary must be a ContextCompactionRuntimeBoundary")
        if not isinstance(self.trigger, ContextCompactionTriggerAssessment):
            raise TypeError("trigger must be a ContextCompactionTriggerAssessment")
        if not isinstance(self.decision, ContextCompactionBoundaryDecision):
            raise TypeError("decision must be a ContextCompactionBoundaryDecision")
        if self.decision is ContextCompactionBoundaryDecision.ALLOW:
            if not self.boundary.safe or not self.trigger.will_trigger:
                raise ValueError("ALLOW requires a safe actionable trigger")
        elif self.decision is ContextCompactionBoundaryDecision.DISABLED:
            if self.trigger.mode is not ContextCompactionTriggerMode.DISABLED:
                raise ValueError("DISABLED requires a disabled trigger mode")
        elif self.decision is ContextCompactionBoundaryDecision.NOT_ACTIONABLE:
            if (
                self.trigger.mode is ContextCompactionTriggerMode.DISABLED
                or self.trigger.will_trigger
            ):
                raise ValueError("NOT_ACTIONABLE requires an enabled non-actionable trigger")
        elif self.decision is ContextCompactionBoundaryDecision.UNSAFE_BOUNDARY:
            if self.boundary.safe or self.trigger.mode is ContextCompactionTriggerMode.DISABLED:
                raise ValueError("UNSAFE_BOUNDARY requires an unsafe enabled boundary")
        elif self.decision is ContextCompactionBoundaryDecision.CANCELLED and (
            not self.boundary.cancellation_requested
            or self.trigger.mode is ContextCompactionTriggerMode.DISABLED
        ):
            raise ValueError("CANCELLED requires an enabled cancelled boundary")

    @property
    def will_trigger(self) -> bool:
        """Return whether the guarded operation may call Provider/storage.

        返回受保护操作是否可以调用 Provider/存储。
        """

        return self.decision is ContextCompactionBoundaryDecision.ALLOW


@dataclass(frozen=True, slots=True)
class ContextCompactionRuntimeResult:
    """Return a boundary assessment and the underlying trigger result.

    返回边界评估和底层触发结果。
    """

    assessment: ContextCompactionRuntimeAssessment
    trigger_result: ContextCompactionTriggerResult

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, ContextCompactionRuntimeAssessment):
            raise TypeError("assessment must be a ContextCompactionRuntimeAssessment")
        if not isinstance(self.trigger_result, ContextCompactionTriggerResult):
            raise TypeError("trigger_result must be a ContextCompactionTriggerResult")
        if self.trigger_result.assessment != self.assessment.trigger:
            raise ValueError("trigger result must retain the boundary assessment's trigger plan")
        if self.trigger_result.triggered != self.assessment.will_trigger:
            raise ValueError("trigger result must match the boundary decision")


@dataclass(frozen=True, slots=True)
class ContextCompactionTurnProjection:
    """Transfer only safe compaction data to an owner of turn finalization.

    A successful explicit trigger transfers its already persisted, validated
    durable item.  A classified failure transfers only the bounded failure
    projection; it never retains the original exception or generated summary.
    The projection itself performs no persistence, event emission, or error
    handling, so a caller can preserve propagation semantics explicitly.

    将安全的压缩数据传递给回合最终化所有者。

    成功的显式触发只传递已经持久化且校验过的持久化条目。已分类的失败只传递有界失败投影,
    绝不保留原始异常或生成的摘要。本投影不执行持久化、事件发出或错误处理,调用方可以显式保持异常传播语义。
    """

    triggered: bool
    compaction_item: DurableCompactionItem | None = field(default=None, repr=False)
    failure: ContextCompactionRuntimeFailureProjection | None = field(
        default=None,
        repr=False,
    )
    outcome: AgentExecutionOutcome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.triggered, bool):
            raise TypeError("triggered must be a bool")
        if self.compaction_item is not None and not isinstance(
            self.compaction_item,
            DurableCompactionItem,
        ):
            raise TypeError("compaction_item must be a DurableCompactionItem or None")
        if self.failure is not None and not isinstance(
            self.failure,
            ContextCompactionRuntimeFailureProjection,
        ):
            raise TypeError("failure must be a ContextCompactionRuntimeFailureProjection or None")
        if self.outcome is not None and not isinstance(self.outcome, AgentExecutionOutcome):
            raise TypeError("outcome must be an AgentExecutionOutcome or None")
        if self.triggered:
            if self.compaction_item is None or self.failure is not None or self.outcome is not None:
                raise ValueError("a triggered compaction projection requires only a durable item")
            return
        if self.compaction_item is not None:
            raise ValueError("an untriggered compaction projection must not carry an item")
        if self.failure is None:
            if self.outcome is not None:
                raise ValueError("a no-op compaction projection must not carry an outcome")
            return
        if self.outcome != self.failure.outcome:
            raise ValueError("compaction failure outcome must match its failure projection")

    @property
    def ready_for_turn_finalization(self) -> bool:
        """Return whether a caller has a bounded value to pass to its recorder.

        返回调用方是否拥有可以传递给记录器的有界值。
        """

        return self.triggered or self.outcome is not None

    @property
    def must_propagate(self) -> bool:
        """Return whether the classified failure remains propagation-only.

        返回已分类失败是否仍然只能继续传播。
        """

        return self.failure is not None and self.failure.handling is (
            ContextCompactionRuntimeFailureHandling.PROPAGATE
        )


class ContextCompactionCommandStatus(StrEnum):
    """Describe the bounded public result of one explicit compaction command.

    描述一次显式上下文压缩命令的有界公开结果。

    ``BUDGET_LIMITED`` is used only for the existing controlled timeout
    projection.  Provider, cancellation, storage, and unknown failures remain
    exceptions and are never converted into this result.
    ``BUDGET_LIMITED`` 仅用于现有的受控超时投影。Provider、取消、存储和未知失败仍然是异常,
    绝不会被转换为此结果。
    """

    COMPLETED = "completed"
    NOT_NEEDED = "not_needed"
    BUDGET_LIMITED = "budget_limited"


@dataclass(frozen=True, slots=True)
class ContextCompactionCommandResult:
    """Expose safe compaction metadata without summary or source contents.

    The result is the application/interface projection for an explicit command.
    It contains only bounded counts and the opaque compaction identifier; the
    generated summary, source fingerprint, prompt, messages, and exception
    details remain outside the projection.

    以不包含摘要或源内容的方式暴露安全压缩元数据。

    该结果是显式命令的应用层/接口层投影,只包含有界计数和不透明压缩 ID;生成的摘要、源指纹,
    提示词、消息及异常详情都不会进入投影。
    """

    status: ContextCompactionCommandStatus
    triggered: bool
    outcome: AgentExecutionOutcome | None = None
    compaction_id: str | None = None
    source_item_count: int | None = None
    candidate_item_count: int | None = None
    summary_tokens: int | None = None
    summary_truncated: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ContextCompactionCommandStatus):
            raise TypeError("status must be a ContextCompactionCommandStatus")
        if not isinstance(self.triggered, bool):
            raise TypeError("triggered must be a bool")
        if self.outcome is not None and not isinstance(self.outcome, AgentExecutionOutcome):
            raise TypeError("outcome must be an AgentExecutionOutcome or None")
        if self.compaction_id is not None:
            if not isinstance(self.compaction_id, str) or not self.compaction_id:
                raise ValueError("compaction_id must be a non-empty string when provided")
            if len(self.compaction_id.encode("utf-8")) > MAX_DURABLE_COMPACTION_ID_BYTES:
                raise ValueError("compaction_id exceeds its bounded identifier size")
            if any(
                ord(character) < 32 or ord(character) == 127 for character in self.compaction_id
            ):
                raise ValueError("compaction_id must not contain control characters")
        for name, value in (
            ("source_item_count", self.source_item_count),
            ("candidate_item_count", self.candidate_item_count),
            ("summary_tokens", self.summary_tokens),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer when provided")
        if self.summary_truncated is not None and not isinstance(self.summary_truncated, bool):
            raise TypeError("summary_truncated must be a bool when provided")

        has_item_metadata = all(
            value is not None
            for value in (
                self.compaction_id,
                self.source_item_count,
                self.candidate_item_count,
                self.summary_tokens,
                self.summary_truncated,
            )
        )
        if self.status is ContextCompactionCommandStatus.COMPLETED:
            if not self.triggered or self.outcome is not None or not has_item_metadata:
                raise ValueError("completed compaction results require a persisted item")
            return
        if self.status is ContextCompactionCommandStatus.NOT_NEEDED:
            if self.triggered or self.outcome is not None or has_item_metadata:
                raise ValueError("not-needed compaction results must be empty")
            return
        if self.status is ContextCompactionCommandStatus.BUDGET_LIMITED:
            expected = AgentExecutionOutcome(
                status=AgentExecutionStatus.BUDGET_LIMITED,
                reason_code=SupervisorReasonCode.WALL_TIME_BUDGET,
                finalized=False,
                recoverable=True,
            )
            if self.triggered or self.outcome != expected or has_item_metadata:
                raise ValueError("budget-limited compaction results require the timeout outcome")
            return
        raise ValueError("unsupported context compaction command status")


def project_context_compaction_command_result(
    projection: ContextCompactionTurnProjection,
) -> ContextCompactionCommandResult:
    """Convert an internal turn projection to a safe command result.

    Propagation-only failures fail closed here; the caller must keep the
    original Provider, cancellation, storage, or unknown exception instead of
    manufacturing a result.  No-op, successful, and controlled-timeout paths
    are the only representable command results.

    将内部回合投影转换为安全的命令结果。

    只能传播的失败在此安全失败;调用方必须保留原始 Provider、取消、存储或未知异常,不能伪造结果。
    只有无操作、成功和受控超时路径可以表示为命令结果。
    """

    if not isinstance(projection, ContextCompactionTurnProjection):
        raise TypeError("projection must be a ContextCompactionTurnProjection")
    if projection.must_propagate:
        raise ConfigurationError("propagation-only compaction failures must remain exceptions")
    if projection.triggered:
        item = projection.compaction_item
        if item is None:
            raise ConfigurationError("completed compaction projection is missing its item")
        return ContextCompactionCommandResult(
            status=ContextCompactionCommandStatus.COMPLETED,
            triggered=True,
            compaction_id=item.compaction_id,
            source_item_count=item.source_item_count,
            candidate_item_count=item.candidate_range[1] - item.candidate_range[0],
            summary_tokens=item.summary_tokens,
            summary_truncated=item.summary_truncated,
        )
    if projection.outcome is None:
        return ContextCompactionCommandResult(
            status=ContextCompactionCommandStatus.NOT_NEEDED,
            triggered=False,
        )
    if projection.failure is None or projection.failure.kind is not (
        ContextCompactionRuntimeFailureKind.TIMEOUT
    ):
        raise ConfigurationError("only controlled compaction timeouts may produce a result")
    return ContextCompactionCommandResult(
        status=ContextCompactionCommandStatus.BUDGET_LIMITED,
        triggered=False,
        outcome=projection.outcome,
    )


def project_context_compaction_result(
    result: ContextCompactionRuntimeResult,
) -> ContextCompactionTurnProjection:
    """Project a completed gate result without changing its persistence state.

    将已完成的门控结果投影为不改变持久化状态的回合值。
    """

    if not isinstance(result, ContextCompactionRuntimeResult):
        raise TypeError("result must be a ContextCompactionRuntimeResult")
    persistence = result.trigger_result.persistence
    if result.trigger_result.triggered:
        if persistence is None:
            raise ValueError("a triggered compaction result requires persistence")
        return ContextCompactionTurnProjection(True, persistence.item)
    if persistence is not None:
        raise ValueError("an untriggered compaction result must not carry persistence")
    return ContextCompactionTurnProjection(False)


def project_context_compaction_failure(
    error: BaseException,
) -> ContextCompactionTurnProjection | None:
    """Project a known failure while leaving exception propagation to the caller.

    投影已知失败,并将异常传播责任留给调用方。
    """

    projection = classify_context_compaction_failure(error)
    if projection is None:
        return None
    return ContextCompactionTurnProjection(
        triggered=False,
        failure=projection,
        outcome=projection.outcome,
    )


class ContextCompactionRuntimeGate:
    """Gate explicit compaction without integrating it into the main loop.

    保护显式压缩, 但不把它接入主循环.
    """

    __slots__ = ("_trigger_service",)

    def __init__(self, trigger_service: ContextCompactionTriggerService) -> None:
        if not isinstance(trigger_service, ContextCompactionTriggerService):
            raise TypeError("trigger_service must be a ContextCompactionTriggerService")
        self._trigger_service = trigger_service

    def assess(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeAssessment:
        """Assess the plan and safe boundary without Provider/storage work.

        评估压缩计划和安全边界, 不执行 Provider/存储操作.
        """

        if not isinstance(request, ContextCompactionRuntimeRequest):
            raise TypeError("request must be a ContextCompactionRuntimeRequest")
        trigger = self._trigger_service.assess(request.trigger)
        if request.trigger.mode is ContextCompactionTriggerMode.DISABLED:
            decision = ContextCompactionBoundaryDecision.DISABLED
        elif request.boundary.cancellation_requested:
            decision = ContextCompactionBoundaryDecision.CANCELLED
        elif not request.boundary.safe:
            decision = ContextCompactionBoundaryDecision.UNSAFE_BOUNDARY
        elif not trigger.will_trigger:
            decision = ContextCompactionBoundaryDecision.NOT_ACTIONABLE
        else:
            decision = ContextCompactionBoundaryDecision.ALLOW
        return ContextCompactionRuntimeAssessment(request.boundary, trigger, decision)

    def build_explicit_request(
        self,
        *,
        context: ModelContext,
        boundary: ContextCompactionRuntimeBoundary,
        provider_window: ProviderContextWindow | None,
        protected_item_count: int = 0,
        reported_input_tokens: int | None = None,
        reported_output_tokens: int | None = None,
        session_id: str | None = None,
        compaction_id: str | None = None,
        created_at: datetime | None = None,
        token_estimator: Callable[[Sequence[SessionItem]], int] = estimate_context_tokens,
    ) -> ContextCompactionRuntimeRequest:
        """Build one explicit request through this gate's assessment service.

        The gate remains the owner of the trigger service, while the builder
        remains side-effect free.  This method only assembles a request; it
        never calls a Provider, storage adapter, or ``trigger``.

        通过当前门控持有的评估服务构建一次显式请求。
        门控继续拥有触发服务,构建器仍保持无副作用。本方法只组装请求,不会调用 Provider、存储适配器或 ``trigger``.
        """

        return build_explicit_context_compaction_runtime_request(
            self._trigger_service,
            context=context,
            boundary=boundary,
            provider_window=provider_window,
            protected_item_count=protected_item_count,
            reported_input_tokens=reported_input_tokens,
            reported_output_tokens=reported_output_tokens,
            session_id=session_id,
            compaction_id=compaction_id,
            created_at=created_at,
            token_estimator=token_estimator,
        )

    async def trigger(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult:
        """Run only an explicitly enabled, actionable request at a safe boundary.

        仅在安全边界执行显式启用且可操作的请求。
        """

        assessment = self.assess(request)
        if not assessment.will_trigger:
            return ContextCompactionRuntimeResult(
                assessment,
                ContextCompactionTriggerResult(assessment.trigger, False, None),
            )
        try:
            async with asyncio.timeout(request.boundary.budget.max_wall_time_seconds):
                trigger_result = await self._trigger_service.trigger(request.trigger)
        except TimeoutError as error:
            raise ContextCompactionTimeoutError(
                "context compaction exceeded its wall-clock budget"
            ) from error
        return ContextCompactionRuntimeResult(assessment, trigger_result)


__all__ = [
    "DEFAULT_CONTEXT_COMPACTION_TIMEOUT_SECONDS",
    "MAX_CONTEXT_COMPACTION_TIMEOUT_SECONDS",
    "ContextCompactionBoundaryDecision",
    "ContextCompactionCommandResult",
    "ContextCompactionCommandStatus",
    "ContextCompactionExecutionRecordPolicy",
    "ContextCompactionRuntimeAssessment",
    "ContextCompactionRuntimeBoundary",
    "ContextCompactionRuntimeBudget",
    "ContextCompactionRuntimeFailureHandling",
    "ContextCompactionRuntimeFailureKind",
    "ContextCompactionRuntimeFailureProjection",
    "ContextCompactionRuntimeGate",
    "ContextCompactionRuntimeRequest",
    "ContextCompactionRuntimeResult",
    "ContextCompactionSafePoint",
    "ContextCompactionTimeoutError",
    "ContextCompactionTurnProjection",
    "build_context_usage_snapshot",
    "build_explicit_context_compaction_runtime_request",
    "classify_context_compaction_failure",
    "project_context_compaction_command_result",
    "project_context_compaction_failure",
    "project_context_compaction_result",
]
