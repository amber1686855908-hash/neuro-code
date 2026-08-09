from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    ContextCompactionPlanner,
    ContextCompactionPolicy,
    ProviderContextWindow,
)
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionBoundaryDecision,
    ContextCompactionCommandResult,
    ContextCompactionCommandStatus,
    ContextCompactionExecutionRecordPolicy,
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeBudget,
    ContextCompactionRuntimeFailureHandling,
    ContextCompactionRuntimeFailureKind,
    ContextCompactionRuntimeFailureProjection,
    ContextCompactionRuntimeGate,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeResult,
    ContextCompactionSafePoint,
    ContextCompactionTimeoutError,
    ContextCompactionTurnProjection,
    build_automatic_context_compaction_runtime_request,
    build_context_usage_snapshot,
    build_explicit_context_compaction_runtime_request,
    classify_context_compaction_failure,
    project_context_compaction_command_result,
    project_context_compaction_failure,
    project_context_compaction_result,
)
from neuro_code.application.memory.compaction_service import ContextCompactionApplicationService
from neuro_code.application.memory.compaction_trigger import (
    ContextCompactionTriggerAssessment,
    ContextCompactionTriggerMode,
    ContextCompactionTriggerRequest,
    ContextCompactionTriggerResult,
    ContextCompactionTriggerService,
)
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.compaction import (
    DurableCompactionItem,
    compute_compaction_source_fingerprint,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelCompleted, ModelEvent
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SupervisorReasonCode,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.shared.errors import ConfigurationError, ProviderError, SessionError


class ScriptedTriggerProvider:
    provider_name = "provider"
    model_name = "model"
    context_affinity = "profile"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent | BaseException]]) -> None:
        self._scripts = list(scripts)
        self.requests: list[tuple[ModelContext, tuple[ToolDefinition, ...], ModelToolPolicy]] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append((context, tuple(tools), tool_policy))
        script = self._scripts.pop(0)
        for event in script:
            if isinstance(event, BaseException):
                raise event
            yield event


class RecordingTriggerStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, DurableCompactionItem]] = []

    async def save_compaction_item(self, session_id: str, item: DurableCompactionItem) -> None:
        self.calls.append((session_id, item))


class DelayedTriggerProvider(ScriptedTriggerProvider):
    def __init__(self, delay_seconds: float) -> None:
        super().__init__(())
        self.delay_seconds = delay_seconds
        self.cancelled = False
        self.started = asyncio.Event()

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append((context, tuple(tools), tool_policy))
        self.started.set()
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield ModelCompleted("stop", response_text="summary")


class DelayedTriggerStore(RecordingTriggerStore):
    def __init__(self, delay_seconds: float) -> None:
        super().__init__()
        self.delay_seconds = delay_seconds
        self.cancelled = False

    async def save_compaction_item(self, session_id: str, item: DurableCompactionItem) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        await super().save_compaction_item(session_id, item)


def _context() -> ModelContext:
    return ModelContext(
        tuple(Message(Role.USER, f"conversation item {index}") for index in range(12))
    )


def _usage() -> CompactionContextUsage:
    return CompactionContextUsage.from_provider_window(
        950,
        ProviderContextWindow("provider", "model", 1_000, context_affinity="profile"),
        estimated=False,
    )


def _service(
    store: RecordingTriggerStore,
    provider: ScriptedTriggerProvider,
) -> ContextCompactionTriggerService:
    persistence = ContextCompactionApplicationService(
        cast(SessionStore, store),
        provider,
        redaction_values=("secret-value",),
        token_estimator=lambda text: max(1, len(text.split())),
    )
    return ContextCompactionTriggerService(
        persistence,
        planner=ContextCompactionPlanner(ContextCompactionPolicy(max_summary_tokens=100)),
    )


def test_context_usage_snapshot_prefers_reported_model_usage() -> None:
    context = _context()
    window = ProviderContextWindow("provider", "model", 1_000, context_affinity="profile")

    usage = build_context_usage_snapshot(
        context,
        window,
        reported_input_tokens=120,
        reported_output_tokens=30,
    )

    assert usage.used_tokens == 150
    assert usage.capacity_tokens == 1_000
    assert usage.estimated is False
    assert usage.provider_window == window

    partial = build_context_usage_snapshot(
        context,
        None,
        reported_input_tokens=120,
    )
    assert partial.used_tokens == 120
    assert partial.capacity_tokens is None
    assert partial.estimated is True


def test_automatic_request_uses_projected_usage_but_guards_the_canonical_source() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    service = _service(store, provider)
    source = _context()
    projected = ModelContext(
        (*source.items, Message(Role.USER, "temporary runtime guidance " * 20))
    )
    window = ProviderContextWindow("provider", "model", 1_000, context_affinity="profile")

    request = build_automatic_context_compaction_runtime_request(
        service,
        source_context=source,
        usage_context=projected,
        boundary=ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            3,
        ),
        provider_window=window,
        protected_item_count=0,
        session_id="session-1",
        compaction_id="automatic-1",
        created_at=datetime(2026, 8, 9, tzinfo=UTC),
        token_estimator=lambda items: 950 if len(items) == len(projected.items) else 1,
    )

    assert request.trigger.mode is ContextCompactionTriggerMode.AUTOMATIC
    assert request.trigger.context is source
    assert request.trigger.usage.used_tokens == 950
    assert request.trigger.expected_source_fingerprint is not None
    assert service.assess(request.trigger).will_trigger is True


def test_automatic_trigger_uses_the_same_disabled_tool_policy_as_explicit_compaction() -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingTriggerStore()
    service = _service(store, provider)
    source = _context()
    request = build_automatic_context_compaction_runtime_request(
        service,
        source_context=source,
        usage_context=source,
        boundary=ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.AFTER_TOOL_BATCH,
            2,
        ),
        provider_window=ProviderContextWindow(
            "provider",
            "model",
            1_000,
            context_affinity="profile",
        ),
        session_id="session-1",
        compaction_id="automatic-2",
        created_at=datetime(2026, 8, 9, tzinfo=UTC),
        token_estimator=lambda _items: 950,
    )

    result = asyncio.run(ContextCompactionRuntimeGate(service).trigger(request))

    assert result.trigger_result.triggered is True
    assert provider.requests[0][1] == ()
    assert provider.requests[0][2] is ModelToolPolicy.DISABLED
    assert store.calls[0][0] == "session-1"


def test_context_usage_snapshot_falls_back_to_context_estimate_without_provider_usage() -> None:
    context = _context()
    window = ProviderContextWindow("provider", "model", 1_000)

    usage = build_context_usage_snapshot(
        context,
        window,
        token_estimator=lambda items: len(tuple(items)) * 3,
    )

    assert usage.used_tokens == len(context.items) * 3
    assert usage.capacity_tokens == 1_000
    assert usage.estimated is True
    assert "conversation item" not in repr(usage)


def test_context_usage_snapshot_rejects_incomplete_or_invalid_usage_inputs() -> None:
    context = _context()
    with pytest.raises(ValueError, match="reported_output_tokens requires"):
        build_context_usage_snapshot(context, None, reported_output_tokens=1)
    with pytest.raises(ValueError, match="reported_input_tokens"):
        build_context_usage_snapshot(context, None, reported_input_tokens=-1)
    with pytest.raises(ValueError, match="token_estimator"):
        build_context_usage_snapshot(context, None, token_estimator=lambda _: -1)


def test_explicit_request_builder_computes_stale_guard_for_actionable_snapshot() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    service = _service(store, provider)
    context = _context()
    boundary = ContextCompactionRuntimeBoundary(
        ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
        3,
    )

    request = build_explicit_context_compaction_runtime_request(
        service,
        context=context,
        boundary=boundary,
        provider_window=ProviderContextWindow(
            "provider",
            "model",
            1_000,
            context_affinity="profile",
        ),
        protected_item_count=1,
        reported_input_tokens=950,
        reported_output_tokens=0,
        session_id="session-1",
        compaction_id="compaction-1",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )

    assessment = service.assess(request.trigger)
    assert assessment.will_trigger is True
    candidate_range = assessment.plan.candidate_range
    assert candidate_range is not None
    assert request.trigger.expected_source_fingerprint == compute_compaction_source_fingerprint(
        context.items,
        candidate_range,
    )
    assert request.trigger.usage.used_tokens == 950
    assert request.boundary == boundary
    assert provider.requests == []
    assert store.calls == []


def test_explicit_request_builder_leaves_no_stale_guard_for_non_actionable_snapshot() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    service = _service(store, provider)
    request = build_explicit_context_compaction_runtime_request(
        service,
        context=_context(),
        boundary=ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.AFTER_TOOL_BATCH,
            1,
        ),
        provider_window=ProviderContextWindow("provider", "model", 1_000),
        reported_input_tokens=100,
    )

    assert request.trigger.expected_source_fingerprint is None
    assert request.trigger.session_id is None
    assert request.trigger.compaction_id is None
    assert request.trigger.created_at is None
    assert service.assess(request.trigger).will_trigger is False
    assert provider.requests == []
    assert store.calls == []


def test_explicit_request_builder_requires_metadata_only_when_actionable() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    service = _service(store, provider)

    with pytest.raises(ValueError, match="requires session"):
        build_explicit_context_compaction_runtime_request(
            service,
            context=_context(),
            boundary=ContextCompactionRuntimeBoundary(
                ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                1,
            ),
            provider_window=ProviderContextWindow("provider", "model", 1_000),
            reported_input_tokens=950,
        )

    assert provider.requests == []
    assert store.calls == []


def test_explicit_request_builder_preserves_stale_source_protection() -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingTriggerStore()
    service = _service(store, provider)
    original = _context()
    request = build_explicit_context_compaction_runtime_request(
        service,
        context=original,
        boundary=ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            1,
        ),
        provider_window=ProviderContextWindow("provider", "model", 1_000),
        protected_item_count=1,
        reported_input_tokens=950,
        session_id="session-1",
        compaction_id="compaction-1",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )
    changed_items = list(original.items)
    changed_items[1] = Message(Role.USER, "changed context item")
    changed = ModelContext(tuple(changed_items))
    stale = ContextCompactionTriggerRequest(
        context=changed,
        usage=request.trigger.usage,
        mode=ContextCompactionTriggerMode.EXPLICIT,
        protected_item_count=request.trigger.protected_item_count,
        session_id=request.trigger.session_id,
        compaction_id=request.trigger.compaction_id,
        expected_source_fingerprint=request.trigger.expected_source_fingerprint,
        created_at=request.trigger.created_at,
    )

    with pytest.raises(ValueError, match="source fingerprint is stale"):
        asyncio.run(service.trigger(stale))
    assert provider.requests == []
    assert store.calls == []


def _request(
    *,
    mode: ContextCompactionTriggerMode = ContextCompactionTriggerMode.DISABLED,
    context: ModelContext | None = None,
    expected_source_fingerprint: str | None = None,
    session_id: str | None = None,
    compaction_id: str | None = None,
    created_at: datetime | None = None,
) -> ContextCompactionTriggerRequest:
    return ContextCompactionTriggerRequest(
        context=context or _context(),
        usage=_usage(),
        mode=mode,
        protected_item_count=1,
        expected_source_fingerprint=expected_source_fingerprint,
        session_id=session_id,
        compaction_id=compaction_id,
        created_at=created_at,
    )


def test_disabled_trigger_only_assesses_and_does_not_call_provider_or_store() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()

    result = asyncio.run(_service(store, provider).trigger(_request()))

    assert result.assessment.plan.decision.value == "required"
    assert result.assessment.will_trigger is False
    assert result.triggered is False
    assert result.persistence is None
    assert provider.requests == []
    assert store.calls == []


def test_explicit_trigger_does_not_call_provider_when_plan_is_not_needed() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    service = _service(store, provider)
    request = ContextCompactionTriggerRequest(
        context=_context(),
        usage=CompactionContextUsage.from_provider_window(
            100,
            ProviderContextWindow("provider", "model", 1_000, context_affinity="profile"),
            estimated=True,
        ),
        mode=ContextCompactionTriggerMode.EXPLICIT,
        protected_item_count=1,
    )

    result = asyncio.run(service.trigger(request))

    assert result.assessment.plan.decision.value == "not_needed"
    assert result.triggered is False
    assert provider.requests == []
    assert store.calls == []


def test_explicit_trigger_fails_closed_when_summary_budget_leaves_no_input_capacity() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    request = ContextCompactionTriggerRequest(
        context=_context(),
        usage=CompactionContextUsage.from_provider_window(
            99,
            ProviderContextWindow("provider", "model", 100, context_affinity="profile"),
            estimated=False,
        ),
        mode=ContextCompactionTriggerMode.EXPLICIT,
        protected_item_count=1,
    )

    result = asyncio.run(_service(store, provider).trigger(request))

    assert result.assessment.plan.decision.value == "required"
    assert result.assessment.plan.max_summary_tokens == 100
    assert result.triggered is False
    assert provider.requests == []
    assert store.calls == []


def test_explicit_actionable_trigger_requires_persistence_metadata() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()

    with pytest.raises(ValueError, match="requires session"):
        asyncio.run(
            _service(store, provider).trigger(_request(mode=ContextCompactionTriggerMode.EXPLICIT))
        )

    assert provider.requests == []
    assert store.calls == []


def test_explicit_trigger_rejects_stale_source_before_provider() -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingTriggerStore()

    with pytest.raises(ValueError, match="source fingerprint is stale"):
        asyncio.run(
            _service(store, provider).trigger(
                _request(
                    mode=ContextCompactionTriggerMode.EXPLICIT,
                    expected_source_fingerprint="a" * 64,
                    session_id="session-1",
                    compaction_id="compaction-1",
                    created_at=datetime(2026, 8, 8, tzinfo=UTC),
                )
            )
        )

    assert provider.requests == []
    assert store.calls == []


def test_explicit_trigger_generates_and_persists_one_compaction_without_turn_budget_state() -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", 12, 6, response_text="summary"),),))
    store = RecordingTriggerStore()
    service = _service(store, provider)
    context = _context()
    assessment = service.assess(
        _request(mode=ContextCompactionTriggerMode.EXPLICIT, context=context)
    )
    plan = assessment.plan
    assert plan.candidate_range is not None
    fingerprint = compute_compaction_source_fingerprint(context.items, plan.candidate_range)

    result = asyncio.run(
        service.trigger(
            _request(
                mode=ContextCompactionTriggerMode.EXPLICIT,
                context=context,
                expected_source_fingerprint=fingerprint,
                session_id="session-1",
                compaction_id="compaction-1",
                created_at=datetime(2026, 8, 8, tzinfo=UTC),
            )
        )
    )

    assert result.triggered is True
    assert result.persistence is not None
    assert result.persistence.item.summary == "summary"
    assert len(store.calls) == 1
    assert provider.requests[0][1] == ()
    assert provider.requests[0][2] is ModelToolPolicy.DISABLED
    assert context == _context()
    assert "summary='summary'" not in repr(result)


def test_trigger_provider_failure_and_cancellation_are_not_converted_to_noop() -> None:
    context = _context()
    planner = ContextCompactionPlanner()
    plan = planner.plan(context.items, _usage(), protected_item_count=1)
    assert plan.candidate_range is not None
    fingerprint = compute_compaction_source_fingerprint(context.items, plan.candidate_range)

    for failure in (ProviderError("summary provider failed"), asyncio.CancelledError()):
        provider = ScriptedTriggerProvider(((failure,),))
        store = RecordingTriggerStore()
        request = _request(
            mode=ContextCompactionTriggerMode.EXPLICIT,
            context=context,
            expected_source_fingerprint=fingerprint,
            session_id="session-1",
            compaction_id="compaction-1",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
        )

        with pytest.raises(type(failure)):
            asyncio.run(_service(store, provider).trigger(request))
        assert store.calls == []


def test_trigger_service_has_no_sticky_state_between_explicit_calls() -> None:
    provider = ScriptedTriggerProvider(
        (
            (ModelCompleted("stop", response_text="first summary"),),
            (ModelCompleted("stop", response_text="second summary"),),
        )
    )
    store = RecordingTriggerStore()
    service = _service(store, provider)
    context = _context()
    plan = service.assess(_request(mode=ContextCompactionTriggerMode.EXPLICIT)).plan
    assert plan.candidate_range is not None
    fingerprint = compute_compaction_source_fingerprint(context.items, plan.candidate_range)

    for index in (1, 2):
        result = asyncio.run(
            service.trigger(
                _request(
                    mode=ContextCompactionTriggerMode.EXPLICIT,
                    context=context,
                    expected_source_fingerprint=fingerprint,
                    session_id="session-1",
                    compaction_id=f"compaction-{index}",
                    created_at=datetime(2026, 8, 8, tzinfo=UTC),
                )
            )
        )
        assert result.triggered is True

    assert [item.summary for _, item in store.calls] == ["first summary", "second summary"]
    assert [request[2] for request in provider.requests] == [
        ModelToolPolicy.DISABLED,
        ModelToolPolicy.DISABLED,
    ]


def _runtime_request(
    service: ContextCompactionTriggerService,
    context: ModelContext,
    boundary: ContextCompactionRuntimeBoundary,
) -> ContextCompactionRuntimeRequest:
    base = _request(mode=ContextCompactionTriggerMode.EXPLICIT, context=context)
    plan = service.assess(base).plan
    assert plan.candidate_range is not None
    return ContextCompactionRuntimeRequest(
        trigger=_request(
            mode=ContextCompactionTriggerMode.EXPLICIT,
            context=context,
            expected_source_fingerprint=compute_compaction_source_fingerprint(
                context.items,
                plan.candidate_range,
            ),
            session_id="session-1",
            compaction_id="compaction-runtime-1",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
        ),
        boundary=boundary,
    )


def test_runtime_gate_disabled_mode_is_a_pure_no_op() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    gate = ContextCompactionRuntimeGate(_service(store, provider))
    request = ContextCompactionRuntimeRequest(
        _request(),
        ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.BEFORE_MODEL_REQUEST, 0),
    )

    result = asyncio.run(gate.trigger(request))

    assert result.assessment.decision is ContextCompactionBoundaryDecision.DISABLED
    assert result.trigger_result.triggered is False
    assert provider.requests == []
    assert store.calls == []


@pytest.mark.parametrize(
    "safe_point",
    [
        ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
        ContextCompactionSafePoint.AFTER_TOOL_BATCH,
    ],
)
def test_runtime_gate_allows_only_completed_safe_boundaries(
    safe_point: ContextCompactionSafePoint,
) -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingTriggerStore()
    service = _service(store, provider)
    context = _context()
    request = _runtime_request(
        service,
        context,
        ContextCompactionRuntimeBoundary(safe_point, 2),
    )

    result = asyncio.run(ContextCompactionRuntimeGate(service).trigger(request))

    assert result.assessment.decision is ContextCompactionBoundaryDecision.ALLOW
    assert result.trigger_result.triggered is True
    assert len(provider.requests) == 1
    assert provider.requests[0][1] == ()
    assert provider.requests[0][2] is ModelToolPolicy.DISABLED
    assert len(store.calls) == 1


@pytest.mark.parametrize(
    "boundary",
    [
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            2,
            model_request_active=True,
        ),
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.AFTER_TOOL_BATCH,
            2,
            tool_batch_active=True,
        ),
    ],
)
def test_runtime_gate_rejects_active_model_or_tool_operations(
    boundary: ContextCompactionRuntimeBoundary,
) -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    request = ContextCompactionRuntimeRequest(
        _request(mode=ContextCompactionTriggerMode.EXPLICIT),
        boundary,
    )

    result = asyncio.run(ContextCompactionRuntimeGate(_service(store, provider)).trigger(request))

    assert result.assessment.decision is ContextCompactionBoundaryDecision.UNSAFE_BOUNDARY
    assert result.trigger_result.triggered is False
    assert provider.requests == []
    assert store.calls == []


def test_runtime_gate_rejects_cancellation_before_provider_or_storage() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    request = ContextCompactionRuntimeRequest(
        _request(mode=ContextCompactionTriggerMode.EXPLICIT),
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            2,
            cancellation_requested=True,
        ),
    )

    result = asyncio.run(ContextCompactionRuntimeGate(_service(store, provider)).trigger(request))

    assert result.assessment.decision is ContextCompactionBoundaryDecision.CANCELLED
    assert result.trigger_result.triggered is False
    assert provider.requests == []
    assert store.calls == []


def test_runtime_gate_reports_non_actionable_plan_without_side_effects() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    service = _service(store, provider)
    request = ContextCompactionRuntimeRequest(
        ContextCompactionTriggerRequest(
            context=_context(),
            usage=CompactionContextUsage.from_provider_window(
                100,
                ProviderContextWindow("provider", "model", 1_000, context_affinity="profile"),
                estimated=True,
            ),
            mode=ContextCompactionTriggerMode.EXPLICIT,
            protected_item_count=1,
        ),
        ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.BEFORE_MODEL_REQUEST, 0),
    )

    result = asyncio.run(ContextCompactionRuntimeGate(service).trigger(request))

    assert result.assessment.decision is ContextCompactionBoundaryDecision.NOT_ACTIONABLE
    assert result.trigger_result.triggered is False
    assert provider.requests == []
    assert store.calls == []


def test_runtime_budget_cannot_expand_into_turn_or_tool_budget() -> None:
    with pytest.raises(ValueError, match="exactly one model request"):
        ContextCompactionRuntimeBudget(max_model_requests=2)
    with pytest.raises(ValueError, match="zero tool calls"):
        ContextCompactionRuntimeBudget(max_tool_calls=1)
    with pytest.raises(ValueError, match="ordinary turn budget"):
        ContextCompactionRuntimeBudget(inherits_turn_budget=True)


@pytest.mark.parametrize(
    "timeout",
    [0, -0.1, math.nan, math.inf, 300.1],
)
def test_runtime_budget_rejects_unbounded_or_non_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="max_wall_time_seconds"):
        ContextCompactionRuntimeBudget(max_wall_time_seconds=timeout)


def test_runtime_budget_rejects_boolean_timeout() -> None:
    with pytest.raises(TypeError, match="max_wall_time_seconds"):
        ContextCompactionRuntimeBudget(max_wall_time_seconds=True)


def test_runtime_budget_rejects_non_numeric_timeout() -> None:
    with pytest.raises(TypeError, match="max_wall_time_seconds"):
        ContextCompactionRuntimeBudget(max_wall_time_seconds=cast(float, "slow"))


def test_runtime_boundary_rejects_invalid_state() -> None:
    with pytest.raises(TypeError, match="safe_point"):
        ContextCompactionRuntimeBoundary(cast(ContextCompactionSafePoint, "before"), 0)
    with pytest.raises(TypeError, match="model_step"):
        ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.BEFORE_MODEL_REQUEST, True)
    with pytest.raises(ValueError, match="model_step"):
        ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.BEFORE_MODEL_REQUEST, -1)
    with pytest.raises(TypeError, match="model_request_active"):
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            0,
            model_request_active=cast(bool, "yes"),
        )
    with pytest.raises(TypeError, match="budget"):
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            0,
            budget=cast(ContextCompactionRuntimeBudget, object()),
        )


def test_runtime_request_rejects_non_contract_members() -> None:
    boundary = ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.BEFORE_MODEL_REQUEST, 0)
    with pytest.raises(TypeError, match="trigger"):
        ContextCompactionRuntimeRequest(cast(ContextCompactionTriggerRequest, object()), boundary)
    with pytest.raises(TypeError, match="boundary"):
        ContextCompactionRuntimeRequest(
            _request(),
            cast(ContextCompactionRuntimeBoundary, object()),
        )


def test_runtime_gate_rejects_invalid_service_and_request() -> None:
    with pytest.raises(TypeError, match="trigger_service"):
        ContextCompactionRuntimeGate(cast(ContextCompactionTriggerService, object()))
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    gate = ContextCompactionRuntimeGate(_service(store, provider))
    with pytest.raises(TypeError, match="request"):
        gate.assess(cast(ContextCompactionRuntimeRequest, object()))


def test_runtime_result_rejects_mismatched_trigger_accounting() -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingTriggerStore()
    service = _service(store, provider)
    request = _runtime_request(
        service,
        _context(),
        ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.BEFORE_MODEL_REQUEST, 2),
    )
    result = asyncio.run(ContextCompactionRuntimeGate(service).trigger(request))
    assert result.trigger_result.triggered is True
    with pytest.raises(ValueError, match="boundary assessment's trigger plan"):
        ContextCompactionRuntimeResult(
            result.assessment,
            ContextCompactionTriggerResult(
                ContextCompactionTriggerAssessment(
                    ContextCompactionTriggerMode.DISABLED,
                    result.assessment.trigger.plan,
                    False,
                ),
                False,
                None,
            ),
        )
    with pytest.raises(ValueError, match="match the boundary decision"):
        ContextCompactionRuntimeResult(
            result.assessment,
            ContextCompactionTriggerResult(result.assessment.trigger, False, None),
        )


def test_runtime_gate_enforces_provider_wall_clock_timeout() -> None:
    provider = DelayedTriggerProvider(0.05)
    store = RecordingTriggerStore()
    service = _service(store, cast(ScriptedTriggerProvider, provider))
    request = _runtime_request(
        service,
        _context(),
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            2,
            budget=ContextCompactionRuntimeBudget(max_wall_time_seconds=0.005),
        ),
    )

    with pytest.raises(ContextCompactionTimeoutError, match="wall-clock budget"):
        asyncio.run(ContextCompactionRuntimeGate(service).trigger(request))

    assert len(provider.requests) == 1
    assert provider.cancelled is True
    assert store.calls == []


def test_runtime_gate_enforces_storage_wall_clock_timeout_without_success() -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = DelayedTriggerStore(0.05)
    service = _service(store, provider)
    request = _runtime_request(
        service,
        _context(),
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.AFTER_TOOL_BATCH,
            2,
            budget=ContextCompactionRuntimeBudget(max_wall_time_seconds=0.005),
        ),
    )

    with pytest.raises(ContextCompactionTimeoutError, match="wall-clock budget"):
        asyncio.run(ContextCompactionRuntimeGate(service).trigger(request))

    assert store.cancelled is True
    assert store.calls == []


def test_runtime_gate_preserves_explicit_cancellation_during_provider_call() -> None:
    provider = DelayedTriggerProvider(0.05)
    store = RecordingTriggerStore()
    service = _service(store, cast(ScriptedTriggerProvider, provider))
    request = _runtime_request(
        service,
        _context(),
        ContextCompactionRuntimeBoundary(
            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            2,
            budget=ContextCompactionRuntimeBudget(max_wall_time_seconds=0.5),
        ),
    )

    async def cancel_running_request() -> None:
        task = asyncio.create_task(ContextCompactionRuntimeGate(service).trigger(request))
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_running_request())
    assert provider.cancelled is True
    assert store.calls == []


def test_runtime_gate_provider_failure_is_not_converted_to_noop() -> None:
    provider = ScriptedTriggerProvider(((ProviderError("summary failed"),),))
    store = RecordingTriggerStore()
    service = _service(store, provider)
    request = _runtime_request(
        service,
        _context(),
        ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.AFTER_TOOL_BATCH, 2),
    )

    with pytest.raises(ProviderError, match="summary failed"):
        asyncio.run(ContextCompactionRuntimeGate(service).trigger(request))

    assert store.calls == []


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (
            ContextCompactionTimeoutError("secret timeout detail"),
            ContextCompactionRuntimeFailureKind.TIMEOUT,
        ),
        (asyncio.CancelledError(), ContextCompactionRuntimeFailureKind.CANCELLED),
        (
            ProviderError("provider secret detail"),
            ContextCompactionRuntimeFailureKind.PROVIDER_FAILURE,
        ),
        (
            SessionError("storage secret detail"),
            ContextCompactionRuntimeFailureKind.STORAGE_FAILURE,
        ),
    ],
)
def test_runtime_failure_projection_is_typed_and_bounded(
    error: BaseException,
    kind: ContextCompactionRuntimeFailureKind,
) -> None:
    projection = classify_context_compaction_failure(error)

    assert projection is not None
    assert projection.kind is kind
    assert projection.handling is (
        ContextCompactionRuntimeFailureHandling.CONTROLLED_TERMINAL
        if kind is ContextCompactionRuntimeFailureKind.TIMEOUT
        else ContextCompactionRuntimeFailureHandling.PROPAGATE
    )
    if kind is ContextCompactionRuntimeFailureKind.TIMEOUT:
        assert projection.outcome is not None
        assert projection.outcome.status.value == "budget_limited"
        assert projection.outcome.reason_code.value == "wall_time_budget"
        assert projection.outcome.finalized is False
        assert projection.outcome.recoverable is True
        assert (
            projection.execution_record_policy
            is ContextCompactionExecutionRecordPolicy.TURN_FINALIZATION
        )
    else:
        assert projection.outcome is None
        assert projection.execution_record_policy is ContextCompactionExecutionRecordPolicy.NONE
    assert "secret" not in repr(projection)


def test_runtime_failure_projection_leaves_unknown_errors_unclassified() -> None:
    assert classify_context_compaction_failure(RuntimeError("unclassified detail")) is None


def test_runtime_failure_projection_rejects_inconsistent_terminal_policy() -> None:
    with pytest.raises(ValueError, match="controlled terminal"):
        ContextCompactionRuntimeFailureProjection(
            kind=ContextCompactionRuntimeFailureKind.TIMEOUT,
            handling=ContextCompactionRuntimeFailureHandling.PROPAGATE,
            outcome=None,
            execution_record_policy=ContextCompactionExecutionRecordPolicy.NONE,
        )
    with pytest.raises(ValueError, match="must propagate"):
        ContextCompactionRuntimeFailureProjection(
            kind=ContextCompactionRuntimeFailureKind.PROVIDER_FAILURE,
            handling=ContextCompactionRuntimeFailureHandling.CONTROLLED_TERMINAL,
            outcome=None,
            execution_record_policy=ContextCompactionExecutionRecordPolicy.NONE,
        )


def test_runtime_failure_projection_does_not_claim_gate_persistence() -> None:
    projection = classify_context_compaction_failure(ContextCompactionTimeoutError("timeout"))

    assert projection is not None
    assert (
        projection.execution_record_policy
        is ContextCompactionExecutionRecordPolicy.TURN_FINALIZATION
    )
    assert projection.outcome is not None
    assert projection.outcome.finalized is False


def test_compaction_success_projects_only_the_validated_item_for_turn_finalization() -> None:
    provider = ScriptedTriggerProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingTriggerStore()
    service = _service(store, provider)
    result = asyncio.run(
        ContextCompactionRuntimeGate(service).trigger(
            _runtime_request(
                service,
                _context(),
                ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.AFTER_TOOL_BATCH, 3),
            )
        )
    )

    projection = project_context_compaction_result(result)

    assert projection.triggered is True
    assert projection.ready_for_turn_finalization is True
    assert projection.must_propagate is False
    assert projection.compaction_item is store.calls[0][1]
    assert projection.outcome is None
    assert projection.failure is None
    assert "summary" not in repr(projection)


def test_compaction_noop_projects_no_turn_finalization_value() -> None:
    provider = ScriptedTriggerProvider(())
    store = RecordingTriggerStore()
    service = _service(store, provider)
    result = asyncio.run(
        ContextCompactionRuntimeGate(service).trigger(
            ContextCompactionRuntimeRequest(
                _request(),
                ContextCompactionRuntimeBoundary(
                    ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                    0,
                ),
            )
        )
    )

    projection = project_context_compaction_result(result)

    assert projection == ContextCompactionTurnProjection(False)
    assert projection.ready_for_turn_finalization is False
    assert projection.must_propagate is False


def test_command_projection_distinguishes_success_and_noop_without_summary_data() -> None:
    provider = ScriptedTriggerProvider(
        ((ModelCompleted("stop", response_text="generated summary text"),),)
    )
    store = RecordingTriggerStore()
    service = _service(store, provider)
    success = asyncio.run(
        ContextCompactionRuntimeGate(service).trigger(
            _runtime_request(
                service,
                _context(),
                ContextCompactionRuntimeBoundary(ContextCompactionSafePoint.AFTER_TOOL_BATCH, 3),
            )
        )
    )

    completed = project_context_compaction_command_result(
        project_context_compaction_result(success)
    )
    assert completed.status is ContextCompactionCommandStatus.COMPLETED
    assert completed.triggered is True
    assert completed.compaction_id == "compaction-runtime-1"
    assert completed.source_item_count == 12
    assert completed.candidate_item_count is not None
    assert completed.summary_tokens is not None
    assert completed.summary_truncated is False
    assert completed.outcome is None
    assert "generated summary text" not in repr(completed)

    no_op = project_context_compaction_command_result(ContextCompactionTurnProjection(False))
    assert no_op == ContextCompactionCommandResult(
        status=ContextCompactionCommandStatus.NOT_NEEDED,
        triggered=False,
    )


def test_command_projection_maps_only_controlled_timeout_to_budget_limited() -> None:
    projection = project_context_compaction_failure(
        ContextCompactionTimeoutError("secret timeout detail")
    )
    assert projection is not None

    result = project_context_compaction_command_result(projection)

    assert result.status is ContextCompactionCommandStatus.BUDGET_LIMITED
    assert result.triggered is False
    assert result.outcome is not None
    assert result.outcome.reason_code is SupervisorReasonCode.WALL_TIME_BUDGET
    assert result.outcome.recoverable is True
    assert result.compaction_id is None
    assert "secret" not in repr(result)


def test_command_projection_keeps_propagation_failures_out_of_result() -> None:
    projection = project_context_compaction_failure(ProviderError("secret provider failure"))
    assert projection is not None

    with pytest.raises(ConfigurationError, match="must remain exceptions"):
        project_context_compaction_command_result(projection)


def test_command_projection_rejects_invalid_result_combinations() -> None:
    with pytest.raises(ValueError, match="completed compaction"):
        ContextCompactionCommandResult(
            status=ContextCompactionCommandStatus.COMPLETED,
            triggered=False,
        )
    with pytest.raises(ValueError, match="budget-limited"):
        ContextCompactionCommandResult(
            status=ContextCompactionCommandStatus.BUDGET_LIMITED,
            triggered=False,
        )
    with pytest.raises(ValueError, match="bounded identifier"):
        ContextCompactionCommandResult(
            status=ContextCompactionCommandStatus.COMPLETED,
            triggered=True,
            compaction_id="x" * 257,
            source_item_count=1,
            candidate_item_count=1,
            summary_tokens=1,
            summary_truncated=False,
        )


@pytest.mark.parametrize(
    "error",
    [
        asyncio.CancelledError(),
        ProviderError("provider failure"),
        SessionError("storage failure"),
    ],
)
def test_propagated_compaction_failure_projection_has_no_outcome_or_item(
    error: BaseException,
) -> None:
    projection = project_context_compaction_failure(error)

    assert projection is not None
    assert projection.ready_for_turn_finalization is False
    assert projection.must_propagate is True
    assert projection.compaction_item is None
    assert projection.outcome is None
    assert "failure" not in repr(projection)


def test_timeout_compaction_failure_projects_a_recoverable_terminal_outcome() -> None:
    projection = project_context_compaction_failure(
        ContextCompactionTimeoutError("secret timeout detail")
    )

    assert projection is not None
    assert projection.ready_for_turn_finalization is True
    assert projection.must_propagate is False
    assert projection.compaction_item is None
    assert projection.outcome is not None
    assert projection.outcome.status.value == "budget_limited"
    assert projection.outcome.reason_code is SupervisorReasonCode.WALL_TIME_BUDGET
    assert projection.outcome.recoverable is True
    assert projection.failure is not None
    assert "secret" not in repr(projection)


def test_compaction_turn_projection_rejects_mismatched_failure_outcome() -> None:
    failure = classify_context_compaction_failure(ContextCompactionTimeoutError("timeout"))
    assert failure is not None

    with pytest.raises(ValueError, match="must match"):
        ContextCompactionTurnProjection(
            triggered=False,
            failure=failure,
            outcome=AgentExecutionOutcome(
                AgentExecutionStatus.COMPLETED,
                None,
                finalized=False,
                recoverable=False,
            ),
        )


def test_unknown_compaction_failure_is_left_for_normal_exception_handling() -> None:
    assert project_context_compaction_failure(RuntimeError("unknown")) is None
