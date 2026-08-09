"""Explicit and automatic context-compaction trigger boundary.

This module is the application seam Runtime calls after it has reached a safe
model-turn boundary. It assesses the immutable context first, performs no
Provider or storage work while disabled, and only delegates an actionable
explicit or automatic request to the existing persistence service.
The compaction operation has no AgentRuntime step counter or retry state of
its own; normal-turn budgets therefore remain separate until a later Runtime
integration defines that boundary explicitly.

显式和自动上下文压缩的触发边界。

本模块是 Runtime 在安全模型回合边界调用的应用层接口。它先评估不可变上下文,关闭时不调用
Provider 或存储,只有显式或自动请求且计划可执行时才委托现有持久化服务。压缩操作不拥有
AgentRuntime 步骤计数器或重试状态,因此在未来 Runtime 明确定义边界前,普通回合预算保持隔离。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    ContextCompactionDecision,
    ContextCompactionPlan,
    ContextCompactionPlanner,
    ContextSummaryRequest,
)
from neuro_code.application.memory.compaction_service import (
    ContextCompactionApplicationService,
    ContextCompactionPersistenceResult,
    PersistContextCompactionRequest,
)
from neuro_code.domain.conversation.context import ModelContext


class ContextCompactionTriggerMode(StrEnum):
    """Select whether a caller may perform a compaction operation.

    选择调用方是否可以执行显式压缩操作。
    """

    DISABLED = "disabled"
    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"


def _mode_allows_trigger(mode: ContextCompactionTriggerMode) -> bool:
    return mode in {
        ContextCompactionTriggerMode.EXPLICIT,
        ContextCompactionTriggerMode.AUTOMATIC,
    }


def _require_non_negative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_optional_identifier(name: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when provided")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _is_actionable_plan(plan: ContextCompactionPlan) -> bool:
    capacity = plan.usage.capacity_tokens
    return (
        plan.decision in {ContextCompactionDecision.RECOMMENDED, ContextCompactionDecision.REQUIRED}
        and plan.candidate_range is not None
        and capacity is not None
        and plan.max_summary_tokens < capacity
    )


@dataclass(frozen=True, slots=True)
class ContextCompactionTriggerRequest:
    """Describe one explicit or disabled compaction trigger attempt.

        The optional persistence fields are required only when an explicit request
        produces an actionable plan.  Keeping them out of the disabled path lets
        Runtime callers assess every turn without inventing IDs or timestamps.
        The context and stale-source digest are hidden from ``repr``.

        描述一次显式或关闭状态的上下文压缩触发尝试。

    只有显式请求生成可执行计划时才要求可选持久化字段。将它们排除在关闭路径之外,允许 Runtime
        在每个回合评估而无需伪造 ID 或时间戳。上下文和过期源摘要不会出现在 ``repr`` 中。
    """

    context: ModelContext = field(repr=False)
    usage: CompactionContextUsage
    mode: ContextCompactionTriggerMode = ContextCompactionTriggerMode.DISABLED
    protected_item_count: int = 0
    session_id: str | None = field(default=None, repr=False)
    compaction_id: str | None = field(default=None, repr=False)
    expected_source_fingerprint: str | None = field(default=None, repr=False)
    created_at: datetime | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context, ModelContext):
            raise TypeError("context must be a ModelContext")
        if not isinstance(self.usage, CompactionContextUsage):
            raise TypeError("usage must be a CompactionContextUsage")
        if not isinstance(self.mode, ContextCompactionTriggerMode):
            raise TypeError("mode must be a ContextCompactionTriggerMode")
        _require_non_negative_int("protected_item_count", self.protected_item_count)
        _require_optional_identifier("session_id", self.session_id)
        _require_optional_identifier("compaction_id", self.compaction_id)
        if self.expected_source_fingerprint is not None:
            if (
                not isinstance(self.expected_source_fingerprint, str)
                or not self.expected_source_fingerprint
            ):
                raise ValueError("expected_source_fingerprint must be non-empty when provided")
            if any(
                ord(character) < 32 or ord(character) == 127
                for character in self.expected_source_fingerprint
            ):
                raise ValueError("expected_source_fingerprint must not contain control characters")
        if self.created_at is not None and (
            not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None
        ):
            raise ValueError("created_at must be timezone-aware when provided")


@dataclass(frozen=True, slots=True)
class ContextCompactionTriggerAssessment:
    """Expose a safe plan and whether this request may call persistence.

    暴露安全计划以及本次请求是否可以调用持久化服务。
    """

    mode: ContextCompactionTriggerMode
    plan: ContextCompactionPlan
    will_trigger: bool

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ContextCompactionTriggerMode):
            raise TypeError("mode must be a ContextCompactionTriggerMode")
        if not isinstance(self.plan, ContextCompactionPlan):
            raise TypeError("plan must be a ContextCompactionPlan")
        if not isinstance(self.will_trigger, bool):
            raise TypeError("will_trigger must be a bool")
        expected = _mode_allows_trigger(self.mode) and _is_actionable_plan(self.plan)
        if self.will_trigger is not expected:
            raise ValueError("will_trigger does not match mode and plan")


@dataclass(frozen=True, slots=True)
class ContextCompactionTriggerResult:
    """Return a bounded assessment and optional persisted compaction result.

        The persisted result is hidden from ``repr`` so a generated summary cannot
        be logged accidentally.  A result is returned only after storage confirms
        the write; Provider, cancellation, and storage failures propagate.

        返回有界评估和可选的已持久化压缩结果。

    已持久化结果不会出现在 ``repr`` 中,以免生成摘要被意外记录。只有存储确认写入后才返回成功结果;
        Provider、取消和存储失败会继续传播。
    """

    assessment: ContextCompactionTriggerAssessment
    triggered: bool
    persistence: ContextCompactionPersistenceResult | None = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, ContextCompactionTriggerAssessment):
            raise TypeError("assessment must be a ContextCompactionTriggerAssessment")
        if not isinstance(self.triggered, bool):
            raise TypeError("triggered must be a bool")
        if self.triggered is not (self.persistence is not None):
            raise ValueError("triggered must match persistence presence")
        if self.triggered and not self.assessment.will_trigger:
            raise ValueError("a non-actionable assessment cannot be triggered")


class ContextCompactionTriggerService:
    """Assess and, only when explicitly enabled, persist one compaction.

        This service is deliberately stateless.  Each invocation receives a fresh
        immutable context and caller-owned stale-source guard.  It does not start a
        model turn, alter ``AgentRunResult.steps``, emit an event, or reuse a
        previous turn's budget or retry state.

        评估并仅在显式启用时持久化一次上下文压缩。

    本服务刻意无状态。每次调用接收新的不可变上下文和调用方持有的过期源保护值,不启动模型回合,
    不修改 ``AgentRunResult.steps``,不发出事件,也不复用上一回合的预算或重试状态。
    """

    __slots__ = ("_persistence_service", "_planner")

    def __init__(
        self,
        persistence_service: ContextCompactionApplicationService,
        *,
        planner: ContextCompactionPlanner | None = None,
    ) -> None:
        if not isinstance(persistence_service, ContextCompactionApplicationService):
            raise TypeError("persistence_service must be a ContextCompactionApplicationService")
        if planner is not None and not isinstance(planner, ContextCompactionPlanner):
            raise TypeError("planner must be a ContextCompactionPlanner or None")
        self._persistence_service = persistence_service
        self._planner = planner or ContextCompactionPlanner()

    def assess(
        self,
        request: ContextCompactionTriggerRequest,
    ) -> ContextCompactionTriggerAssessment:
        """Plan without contacting a Provider or storage adapter.

        只进行计划评估,不联系 Provider 或存储适配器。
        """

        if not isinstance(request, ContextCompactionTriggerRequest):
            raise TypeError("request must be a ContextCompactionTriggerRequest")
        plan = self._planner.plan(
            request.context.items,
            request.usage,
            protected_item_count=request.protected_item_count,
        )
        return ContextCompactionTriggerAssessment(
            mode=request.mode,
            plan=plan,
            will_trigger=_mode_allows_trigger(request.mode) and _is_actionable_plan(plan),
        )

    async def trigger(
        self,
        request: ContextCompactionTriggerRequest,
    ) -> ContextCompactionTriggerResult:
        """Execute one actionable explicit request, otherwise return a no-op.

        执行一次可操作的显式请求,否则返回不调用 Provider/存储的空操作结果。
        """

        if not isinstance(request, ContextCompactionTriggerRequest):
            raise TypeError("request must be a ContextCompactionTriggerRequest")
        assessment = self.assess(request)
        if not assessment.will_trigger:
            return ContextCompactionTriggerResult(assessment, False, None)

        if (
            request.session_id is None
            or request.compaction_id is None
            or request.expected_source_fingerprint is None
            or request.created_at is None
        ):
            raise ValueError(
                "actionable compaction requires session, compaction, source fingerprint, and timestamp"
            )
        summary_request = ContextSummaryRequest.from_plan(assessment.plan)
        persistence = await self._persistence_service.generate_and_save(
            PersistContextCompactionRequest(
                session_id=request.session_id,
                compaction_id=request.compaction_id,
                context=request.context,
                summary_request=summary_request,
                expected_source_fingerprint=request.expected_source_fingerprint,
                created_at=request.created_at,
            )
        )
        return ContextCompactionTriggerResult(assessment, True, persistence)


__all__ = [
    "ContextCompactionTriggerAssessment",
    "ContextCompactionTriggerMode",
    "ContextCompactionTriggerRequest",
    "ContextCompactionTriggerResult",
    "ContextCompactionTriggerService",
]
