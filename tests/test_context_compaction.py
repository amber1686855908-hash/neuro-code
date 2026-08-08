from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    CompactionResumeRebuilder,
    ContextCompactionDecision,
    ContextCompactionPlan,
    ContextCompactionPlanner,
    ContextCompactionPolicy,
    ContextSummaryGenerationResult,
    ContextSummaryInput,
    ContextSummaryInputBuilder,
    ContextSummaryItem,
    ContextSummaryRequest,
    ContextSummarySourceKind,
    DurableCompactionItem,
    ProviderContextSummaryGenerator,
    ProviderContextWindow,
    build_durable_compaction_item,
)
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.domain.conversation.compaction import compute_compaction_source_fingerprint
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import (
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SyntheticReason,
    ToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.shared.errors import ProviderError


class ScriptedSummaryProvider:
    provider_name = "deepseek"
    model_name = "deepseek-v4-flash"
    context_affinity = "deepseek-profile"

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


def _items(count: int) -> tuple[Message, ...]:
    return tuple(Message(Role.USER, f"item-{index}") for index in range(count))


def test_unknown_capacity_is_explicitly_unavailable_without_candidate() -> None:
    plan = ContextCompactionPlanner().plan(
        _items(20),
        CompactionContextUsage(used_tokens=100_000, capacity_tokens=None, estimated=True),
    )

    assert plan.decision is ContextCompactionDecision.UNAVAILABLE
    assert plan.candidate_range is None
    assert plan.candidate_item_count == 0
    assert plan.soft_limit_tokens is None
    assert plan.hard_limit_tokens is None


def test_below_soft_threshold_does_not_propose_compaction() -> None:
    plan = ContextCompactionPlanner().plan(
        _items(20),
        CompactionContextUsage(used_tokens=79, capacity_tokens=100, estimated=False),
    )

    assert plan.decision is ContextCompactionDecision.NOT_NEEDED
    assert plan.candidate_range is None
    assert plan.target_tokens is None
    assert plan.soft_limit_tokens == 80
    assert plan.hard_limit_tokens == 95


def test_recommended_plan_preserves_protected_prefix_and_recent_suffix() -> None:
    items = _items(20)
    plan = ContextCompactionPlanner(ContextCompactionPolicy(minimum_recent_items=4)).plan(
        items,
        CompactionContextUsage(used_tokens=82, capacity_tokens=100, estimated=True),
        protected_item_count=3,
    )

    assert plan.decision is ContextCompactionDecision.RECOMMENDED
    assert plan.candidate_range == (3, 16)
    assert plan.candidate_item_count == 13
    assert plan.protected_item_count == 3
    assert plan.recent_item_count == 4
    assert items[0].content == "item-0"
    assert "item-0" not in repr(plan)


def test_required_plan_uses_the_same_bounded_candidate_shape() -> None:
    plan = ContextCompactionPlanner(ContextCompactionPolicy(minimum_recent_items=2)).plan(
        _items(10),
        CompactionContextUsage(used_tokens=95, capacity_tokens=100, estimated=False),
        protected_item_count=2,
    )

    assert plan.decision is ContextCompactionDecision.REQUIRED
    assert plan.candidate_range == (2, 8)
    assert plan.target_tokens == 80


def test_provider_window_binds_usage_and_summary_request_without_source_items() -> None:
    window = ProviderContextWindow(
        provider_name="deepseek",
        model_name="deepseek-v4-flash",
        capacity_tokens=1_000,
        context_affinity="deepseek-profile",
    )
    usage = CompactionContextUsage.from_provider_window(850, window, estimated=True)
    plan = ContextCompactionPlanner(
        ContextCompactionPolicy(minimum_recent_items=2, max_summary_tokens=300)
    ).plan(_items(10), usage, protected_item_count=1)

    request = ContextSummaryRequest.from_plan(plan)

    assert plan.decision is ContextCompactionDecision.RECOMMENDED
    assert plan.max_summary_tokens == 300
    assert request.provider_window is window
    assert request.candidate_range == (1, 8)
    assert request.candidate_item_count == 7
    assert request.target_tokens == 800
    assert request.max_summary_tokens == 300
    assert "item-0" not in repr(request)


def test_provider_window_clamps_summary_budget_to_small_capacity() -> None:
    window = ProviderContextWindow("provider", "model", capacity_tokens=100)
    usage = CompactionContextUsage.from_provider_window(95, window, estimated=False)
    plan = ContextCompactionPlanner().plan(_items(12), usage)

    assert plan.max_summary_tokens == 100
    assert ContextSummaryRequest.from_plan(plan).max_summary_tokens == 100


def test_summary_request_requires_provider_bound_actionable_plan() -> None:
    unknown = ContextCompactionPlanner().plan(
        _items(20),
        CompactionContextUsage(used_tokens=90, capacity_tokens=None, estimated=True),
    )
    with pytest.raises(ValueError, match="actionable compaction"):
        ContextSummaryRequest.from_plan(unknown)

    no_candidate = ContextCompactionPlanner(ContextCompactionPolicy(minimum_recent_items=3)).plan(
        _items(5),
        CompactionContextUsage(
            used_tokens=99,
            capacity_tokens=100,
            estimated=False,
            provider_window=ProviderContextWindow("provider", "model", 100),
        ),
        protected_item_count=2,
    )
    with pytest.raises(ValueError, match="non-empty candidate"):
        ContextSummaryRequest.from_plan(no_candidate)


@pytest.mark.parametrize(
    ("provider_name", "model_name", "capacity_tokens"),
    [
        ("", "model", 100),
        ("provider", "", 100),
        ("provider\n", "model", 100),
        ("https://provider", "model", 100),
        ("p" * 129, "model", 100),
        ("provider", "model", 0),
    ],
)
def test_provider_window_rejects_unbounded_or_unsafe_identity(
    provider_name: str, model_name: str, capacity_tokens: int
) -> None:
    with pytest.raises(ValueError, match=r"must be|must not|opaque"):
        ProviderContextWindow(provider_name, model_name, capacity_tokens)


def test_usage_rejects_provider_capacity_mismatch() -> None:
    window = ProviderContextWindow("provider", "model", 100)
    with pytest.raises(ValueError, match="match provider_window"):
        CompactionContextUsage(
            used_tokens=10,
            capacity_tokens=101,
            estimated=False,
            provider_window=window,
        )


def test_usage_rejects_wrong_provider_window_type() -> None:
    with pytest.raises(TypeError, match="ProviderContextWindow"):
        CompactionContextUsage(
            used_tokens=10,
            capacity_tokens=100,
            estimated=False,
            provider_window="not-a-window",  # type: ignore[arg-type]
        )


def test_policy_rejects_unbounded_summary_budget() -> None:
    with pytest.raises(ValueError, match="max_summary_tokens"):
        ContextCompactionPolicy(max_summary_tokens=4_097)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"soft_limit_ratio": True},
        {"hard_limit_ratio": "0.95"},
        {"max_summary_tokens": 0},
        {"max_summary_tokens": True},
    ],
)
def test_policy_rejects_invalid_types_and_summary_bounds(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ContextCompactionPolicy(**kwargs)  # type: ignore[arg-type]


def test_actionable_decision_can_have_no_candidate_when_all_items_are_retained() -> None:
    plan = ContextCompactionPlanner(ContextCompactionPolicy(minimum_recent_items=3)).plan(
        _items(5),
        CompactionContextUsage(used_tokens=99, capacity_tokens=100, estimated=False),
        protected_item_count=2,
    )

    assert plan.decision is ContextCompactionDecision.REQUIRED
    assert plan.candidate_range is None
    assert plan.candidate_item_count == 0
    assert plan.recent_item_count == 3


@pytest.mark.parametrize(
    ("soft", "hard"),
    [(0.0, 0.9), (0.9, 0.9), (0.95, 0.9), (0.8, 1.1)],
)
def test_policy_rejects_invalid_threshold_order(soft: float, hard: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        ContextCompactionPolicy(soft_limit_ratio=soft, hard_limit_ratio=hard)


def test_policy_rejects_non_finite_thresholds_and_negative_retention() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        ContextCompactionPolicy(soft_limit_ratio=float("nan"))
    with pytest.raises(ValueError, match="must be finite"):
        ContextCompactionPolicy(hard_limit_ratio=float("inf"))
    with pytest.raises(ValueError, match="minimum_recent_items"):
        ContextCompactionPolicy(minimum_recent_items=-1)


@pytest.mark.parametrize(
    ("used_tokens", "capacity_tokens", "message"),
    [
        (-1, 100, "used_tokens must be an integer"),
        (1, 0, "capacity_tokens must be an integer"),
    ],
)
def test_usage_rejects_invalid_token_bounds(
    used_tokens: int, capacity_tokens: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CompactionContextUsage(
            used_tokens=used_tokens,
            capacity_tokens=capacity_tokens,
            estimated=False,
        )


def test_usage_rejects_non_boolean_estimated_flag() -> None:
    with pytest.raises(TypeError):
        CompactionContextUsage(used_tokens=1, capacity_tokens=10, estimated=1)  # type: ignore[arg-type]


def test_plan_rejects_invalid_protected_boundary() -> None:
    usage = CompactionContextUsage(used_tokens=90, capacity_tokens=100, estimated=False)
    with pytest.raises(ValueError, match="protected_item_count"):
        ContextCompactionPlanner().plan(_items(3), usage, protected_item_count=4)


def test_plan_rejects_invalid_usage_type_and_protected_count() -> None:
    planner = ContextCompactionPlanner()
    with pytest.raises(TypeError, match="CompactionContextUsage"):
        planner.plan(_items(2), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        planner.plan(
            _items(2),
            CompactionContextUsage(used_tokens=1, capacity_tokens=10, estimated=False),
            protected_item_count=-1,
        )


def test_plan_rejects_inconsistent_direct_boundaries() -> None:
    known = CompactionContextUsage(used_tokens=90, capacity_tokens=100, estimated=False)
    unknown = CompactionContextUsage(used_tokens=90, capacity_tokens=None, estimated=True)

    with pytest.raises(ValueError, match="protected_item_count"):
        ContextCompactionPlan(
            ContextCompactionDecision.REQUIRED,
            known,
            source_item_count=2,
            protected_item_count=3,
            recent_item_count=0,
            candidate_range=None,
            soft_limit_tokens=80,
            hard_limit_tokens=95,
            target_tokens=80,
            max_summary_tokens=100,
        )
    with pytest.raises(ValueError, match="recent_item_count"):
        ContextCompactionPlan(
            ContextCompactionDecision.REQUIRED,
            known,
            source_item_count=2,
            protected_item_count=1,
            recent_item_count=2,
            candidate_range=None,
            soft_limit_tokens=80,
            hard_limit_tokens=95,
            target_tokens=80,
            max_summary_tokens=100,
        )
    with pytest.raises(ValueError, match="token limits"):
        ContextCompactionPlan(
            ContextCompactionDecision.UNAVAILABLE,
            unknown,
            source_item_count=2,
            protected_item_count=0,
            recent_item_count=0,
            candidate_range=None,
            soft_limit_tokens=1,
            hard_limit_tokens=None,
            target_tokens=None,
            max_summary_tokens=2_048,
        )
    with pytest.raises(ValueError, match="ordered soft"):
        ContextCompactionPlan(
            ContextCompactionDecision.REQUIRED,
            known,
            source_item_count=2,
            protected_item_count=0,
            recent_item_count=0,
            candidate_range=None,
            soft_limit_tokens=None,
            hard_limit_tokens=95,
            target_tokens=80,
            max_summary_tokens=100,
        )


def test_plan_rejects_decision_and_candidate_mismatches() -> None:
    known = CompactionContextUsage(used_tokens=90, capacity_tokens=100, estimated=False)
    common = {
        "usage": known,
        "source_item_count": 4,
        "protected_item_count": 0,
        "recent_item_count": 1,
        "soft_limit_tokens": 80,
        "hard_limit_tokens": 95,
        "max_summary_tokens": 100,
    }
    with pytest.raises(ValueError, match="unavailable"):
        ContextCompactionPlan(
            decision=ContextCompactionDecision.UNAVAILABLE,
            candidate_range=(0, 1),
            target_tokens=None,
            **common,
        )
    with pytest.raises(ValueError, match="not-needed"):
        ContextCompactionPlan(
            decision=ContextCompactionDecision.NOT_NEEDED,
            candidate_range=None,
            target_tokens=80,
            **common,
        )
    with pytest.raises(ValueError, match="target token"):
        ContextCompactionPlan(
            decision=ContextCompactionDecision.REQUIRED,
            candidate_range=None,
            target_tokens=None,
            **common,
        )
    with pytest.raises(TypeError, match="two-item tuple"):
        ContextCompactionPlan(
            decision=ContextCompactionDecision.REQUIRED,
            candidate_range=[0, 2],  # type: ignore[arg-type]
            target_tokens=80,
            **common,
        )
    with pytest.raises(ValueError, match="exclude"):
        ContextCompactionPlan(
            decision=ContextCompactionDecision.REQUIRED,
            candidate_range=(0, 4),
            target_tokens=80,
            **common,
        )


def test_summary_request_rejects_invalid_window_and_ranges() -> None:
    window = ProviderContextWindow("provider", "model", 100)
    valid = {
        "provider_window": window,
        "source_item_count": 5,
        "protected_item_count": 1,
        "recent_item_count": 1,
        "candidate_range": (1, 4),
        "target_tokens": 80,
        "max_summary_tokens": 50,
    }
    with pytest.raises(TypeError, match="ProviderContextWindow"):
        ContextSummaryRequest(
            provider_window="bad",
            **{k: v for k, v in valid.items() if k != "provider_window"},  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="target_tokens"):
        ContextSummaryRequest(**{**valid, "target_tokens": 101})
    with pytest.raises(ValueError, match="max_summary_tokens"):
        ContextSummaryRequest(**{**valid, "max_summary_tokens": 101})
    with pytest.raises(ValueError, match="exclude"):
        ContextSummaryRequest(**{**valid, "candidate_range": (0, 4)})


def test_plan_validates_injected_plan_invariants() -> None:
    usage = CompactionContextUsage(used_tokens=90, capacity_tokens=100, estimated=False)
    with pytest.raises(ValueError, match="not-needed"):
        ContextCompactionPlan(
            decision=ContextCompactionDecision.NOT_NEEDED,
            usage=usage,
            source_item_count=3,
            protected_item_count=0,
            recent_item_count=0,
            candidate_range=(0, 1),
            soft_limit_tokens=80,
            hard_limit_tokens=95,
            target_tokens=None,
            max_summary_tokens=100,
        )


def _summary_request(
    *,
    source_item_count: int,
    capacity_tokens: int = 1_000,
    protected_item_count: int = 1,
    recent_item_count: int = 1,
    max_summary_tokens: int = 100,
) -> ContextSummaryRequest:
    window = ProviderContextWindow("deepseek", "deepseek-v4-flash", capacity_tokens)
    return ContextSummaryRequest(
        provider_window=window,
        source_item_count=source_item_count,
        protected_item_count=protected_item_count,
        recent_item_count=recent_item_count,
        candidate_range=(
            protected_item_count,
            source_item_count - recent_item_count,
        ),
        target_tokens=min(capacity_tokens, 800),
        max_summary_tokens=max_summary_tokens,
    )


def _summary_context() -> ModelContext:
    secret = "summary-secret-value"
    return ModelContext(
        (
            Message(Role.SYSTEM, "protected instructions"),
            Message(Role.USER, f"api_key={secret}; inspect src/neuro_code/domain"),
            Message(
                Role.ASSISTANT,
                "tool-backed answer",
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "secret.txt", "token": secret},
                    ),
                ),
                reasoning_content="private reasoning must not be summarized",
            ),
            PreservedContextItem(
                ContextItemKind.BACKEND_TOOL_CALL,
                {
                    "type": "backend_tool_call",
                    "kind": {"tool_type": "web_search", "query": secret},
                },
            ),
            Message(Role.USER, "recent user input"),
        )
    )


def test_summary_input_builder_redacts_and_projects_without_mutating_context() -> None:
    context = _summary_context()
    original_items = context.items
    request = _summary_request(source_item_count=len(context.items))

    summary = ContextSummaryInputBuilder(
        redaction_values=("summary-secret-value",),
        token_estimator=len,
    ).build(context, request)

    assert context.items == original_items
    assert summary.request is request
    assert summary.estimated_input_tokens <= summary.input_budget_tokens
    assert summary.omitted_item_count == 0
    assert summary.redacted_item_count == 3
    assert summary.truncated_item_count == 0
    assert tuple(item.source_index for item in summary.items) == (1, 2, 3)
    assert summary.items[0].source_kind is ContextSummarySourceKind.MESSAGE
    assert summary.items[0].role is Role.USER
    assert "[REDACTED]" in summary.items[0].text
    assert "summary-secret-value" not in repr(summary)
    assert "arguments omitted" in summary.items[1].text
    assert "token" not in summary.items[1].text
    assert "reasoning omitted" in summary.items[1].text
    assert summary.items[2].source_kind is ContextSummarySourceKind.PRESERVED_CONTEXT
    assert "payload omitted" in summary.items[2].text


def test_summary_input_builder_bounds_item_bytes_and_input_tokens() -> None:
    context = ModelContext(
        (
            Message(Role.SYSTEM, "protected"),
            Message(Role.USER, "x" * 100),
            Message(Role.ASSISTANT, "y" * 100),
            Message(Role.USER, "recent"),
        )
    )
    request = _summary_request(
        source_item_count=4,
        capacity_tokens=40,
        max_summary_tokens=10,
    )
    summary = ContextSummaryInputBuilder(
        max_item_bytes=16,
        token_estimator=len,
    ).build(context, request)

    assert summary.input_budget_tokens == 30
    assert summary.estimated_input_tokens <= 30
    assert summary.truncated_item_count >= 1
    assert all(len(item.text.encode("utf-8")) <= 16 for item in summary.items)
    assert summary.omitted_item_count == 0


def test_summary_input_builder_omits_items_after_input_budget_is_exhausted() -> None:
    context = ModelContext(
        (
            Message(Role.SYSTEM, "protected"),
            Message(Role.USER, "first"),
            Message(Role.ASSISTANT, "second"),
            Message(Role.USER, "recent"),
        )
    )
    request = _summary_request(
        source_item_count=4,
        capacity_tokens=12,
        max_summary_tokens=2,
    )
    summary = ContextSummaryInputBuilder(token_estimator=lambda _text: 6).build(context, request)

    assert summary.input_budget_tokens == 10
    assert summary.estimated_input_tokens == 6
    assert summary.omitted_item_count == 1
    assert tuple(item.source_index for item in summary.items) == (1,)


def test_summary_input_builder_fails_closed_for_empty_or_unusable_input() -> None:
    context = ModelContext(
        (
            Message(Role.SYSTEM, "protected"),
            Message(Role.USER, "candidate"),
            Message(Role.USER, "recent"),
        )
    )
    with pytest.raises(ValueError, match="no input token budget"):
        ContextSummaryInputBuilder().build(
            context,
            _summary_request(
                source_item_count=3,
                capacity_tokens=10,
                max_summary_tokens=10,
                recent_item_count=1,
            ),
        )
    with pytest.raises(ValueError, match="no representable content"):
        ContextSummaryInputBuilder(token_estimator=lambda _text: 3).build(
            context,
            _summary_request(
                source_item_count=3,
                capacity_tokens=3,
                max_summary_tokens=1,
                recent_item_count=1,
            ),
        )


def test_summary_input_builder_rejects_context_shape_and_estimator_contract_errors() -> None:
    request = _summary_request(source_item_count=3)
    with pytest.raises(ValueError, match="item count"):
        ContextSummaryInputBuilder().build(ModelContext((Message(Role.USER, "one"),)), request)
    with pytest.raises(ValueError, match="integer >= 0"):
        ContextSummaryInputBuilder(token_estimator=lambda _text: -1).build(
            ModelContext(
                (
                    Message(Role.SYSTEM, "protected"),
                    Message(Role.USER, "candidate"),
                    Message(Role.USER, "recent"),
                )
            ),
            request,
        )
    with pytest.raises(ValueError, match="integer >= 0"):
        ContextSummaryInputBuilder(token_estimator=lambda _text: True).build(
            ModelContext(
                (
                    Message(Role.SYSTEM, "protected"),
                    Message(Role.USER, "candidate"),
                    Message(Role.USER, "recent"),
                )
            ),
            request,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_item_bytes": 0}, "max_item_bytes"),
        ({"max_item_bytes": 4_097}, "max_item_bytes"),
        ({"max_items": 0}, "max_items"),
        ({"max_items": 129}, "max_items"),
    ],
)
def test_summary_input_builder_rejects_unbounded_limits(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ContextSummaryInputBuilder(**kwargs)


def test_summary_item_and_input_reject_unsafe_or_untyped_values() -> None:
    with pytest.raises(ValueError, match="unsafe control"):
        ContextSummaryItem(
            source_index=1,
            source_kind=ContextSummarySourceKind.MESSAGE,
            role=Role.USER,
            text="unsafe\x00text",
            estimated_tokens=2,
            redacted=True,
            truncated=False,
        )


def _durable_context() -> ModelContext:
    return ModelContext(
        (
            Message(Role.SYSTEM, "rules"),
            Message(Role.USER, "first source"),
            Message(Role.ASSISTANT, "second source"),
            Message(Role.USER, "recent"),
        ),
        source_provider="deepseek",
        source_model="deepseek-v4-flash",
        source_context_affinity="profile-a",
    )


def _durable_request() -> ContextSummaryRequest:
    return ContextSummaryRequest(
        provider_window=ProviderContextWindow(
            "deepseek",
            "deepseek-v4-flash",
            1_000,
            "profile-a",
        ),
        source_item_count=4,
        protected_item_count=1,
        recent_item_count=1,
        candidate_range=(1, 3),
        target_tokens=800,
        max_summary_tokens=100,
    )


def test_build_durable_compaction_item_redacts_and_binds_source_range() -> None:
    context = _durable_context()
    record = build_durable_compaction_item(
        context,
        _durable_request(),
        compaction_id="compact-1",
        summary="Summary api_key=durable-secret",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        redaction_values=("durable-secret",),
        token_estimator=len,
    )

    assert record.summary_redacted is True
    assert "durable-secret" not in record.summary
    assert record.source_fingerprint == compute_compaction_source_fingerprint(
        context.items,
        (1, 3),
    )
    assert "Summary" not in repr(record)


def test_durable_compaction_item_rejects_unsafe_or_unredacted_values() -> None:
    with pytest.raises(ValueError, match="redacted"):
        DurableCompactionItem(
            compaction_id="compact-1",
            provider_name="deepseek",
            model_name="deepseek-v4-flash",
            capacity_tokens=1_000,
            context_affinity=None,
            source_item_count=3,
            protected_item_count=0,
            recent_item_count=1,
            candidate_range=(0, 2),
            target_tokens=800,
            summary_tokens=4,
            source_fingerprint="0" * 64,
            summary="summary",
            summary_redacted=False,
            summary_truncated=False,
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
        )


def test_durable_compaction_record_validates_every_persistence_boundary() -> None:
    context = _durable_context()
    request = _durable_request()
    valid = build_durable_compaction_item(
        context,
        request,
        compaction_id="compact-valid",
        summary="safe summary",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        token_estimator=len,
    )

    invalid_values = (
        ("compaction_id", "", "non-empty"),
        ("provider_name", "p" * 129, "byte limit"),
        ("model_name", "model\n", "opaque"),
        ("capacity_tokens", 0, "positive"),
        ("source_item_count", 0, "positive"),
        ("protected_item_count", 5, "must not exceed"),
        ("recent_item_count", 4, "unprotected"),
        ("candidate_range", [1, 3], "two-item tuple"),
        ("candidate_range", (0, 3), "exclude"),
        ("target_tokens", 1_001, "capacity"),
        ("summary_tokens", 1_001, "capacity"),
        ("source_fingerprint", "not-a-digest", "SHA-256"),
        ("summary", "", "must not be empty"),
        ("summary", "unsafe\x00summary", "control"),
        ("summary_truncated", 1, "bool"),
        ("created_at", datetime(2026, 8, 8, tzinfo=UTC).replace(tzinfo=None), "timezone-aware"),
    )
    for field_name, value, message in invalid_values:
        with pytest.raises((TypeError, ValueError), match=message):
            replace(valid, **{field_name: value})

    with pytest.raises(ValueError, match="byte limit"):
        replace(valid, summary="x" * 8_193)

    with pytest.raises(ValueError, match="opaque"):
        replace(valid, context_affinity="https://profile")


def test_compaction_fingerprint_rejects_invalid_source_ranges() -> None:
    items = _durable_context().items
    with pytest.raises(TypeError, match="two-item tuple"):
        compute_compaction_source_fingerprint(items, [1, 3])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        compute_compaction_source_fingerprint(items, (-1, 3))
    with pytest.raises(ValueError, match="non-empty"):
        compute_compaction_source_fingerprint(items, (2, 2))
    with pytest.raises(ValueError, match="in-bounds"):
        compute_compaction_source_fingerprint(items, (1, 99))


def test_durable_builder_bounds_summary_and_estimator_contract() -> None:
    context = _durable_context()
    request = _durable_request()
    with pytest.raises(ValueError, match="item count"):
        build_durable_compaction_item(
            ModelContext(context.items[:-1]),
            request,
            compaction_id="compact-invalid",
            summary="summary",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="integer >= 0"):
        build_durable_compaction_item(
            context,
            request,
            compaction_id="compact-invalid",
            summary="summary",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
            token_estimator=lambda _text: -1,
        )
    with pytest.raises(ValueError, match="at least one"):
        build_durable_compaction_item(
            context,
            request,
            compaction_id="compact-invalid",
            summary="summary",
            created_at=datetime(2026, 8, 8, tzinfo=UTC),
            token_estimator=lambda _text: 0,
        )
    record = build_durable_compaction_item(
        context,
        replace(request, max_summary_tokens=5),
        compaction_id="compact-truncated",
        summary="a very long summary that must be bounded",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        token_estimator=len,
    )
    assert record.summary_truncated is True
    assert record.summary_tokens <= 5


def test_compaction_resume_rebuilder_handles_empty_and_invalid_records() -> None:
    context = _durable_context()
    empty = CompactionResumeRebuilder().rebuild(context, ())
    assert empty.context is context
    assert empty.applied_compaction_ids == ()
    assert empty.omitted_item_count == 0
    with pytest.raises(TypeError, match="DurableCompactionItem"):
        CompactionResumeRebuilder().rebuild(context, (object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ModelContext"):
        CompactionResumeRebuilder().rebuild(object(), ())  # type: ignore[arg-type]
    record = build_durable_compaction_item(
        context,
        _durable_request(),
        compaction_id="compact-stale",
        summary="safe summary",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        token_estimator=len,
    )
    with pytest.raises(ValueError, match="stale"):
        CompactionResumeRebuilder().rebuild(
            context,
            (replace(record, source_item_count=5),),
        )


def test_compaction_resume_rebuilder_replaces_only_validated_middle_range() -> None:
    context = _durable_context()
    record = build_durable_compaction_item(
        context,
        _durable_request(),
        compaction_id="compact-1",
        summary="safe summary",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        token_estimator=len,
    )

    result = CompactionResumeRebuilder().rebuild(context, (record,))

    assert context.items == _durable_context().items
    assert result.applied_compaction_ids == ("compact-1",)
    assert result.omitted_item_count == 2
    assert len(result.context.items) == 3
    summary = result.context.items[1]
    assert isinstance(summary, Message)
    assert summary.synthetic_reason is SyntheticReason.COMPACTION_SUMMARY
    assert "safe summary" in summary.content
    assert result.context.items[0] == context.items[0]
    assert result.context.items[2] == context.items[3]


def test_compaction_resume_rebuilder_rejects_stale_overlap_and_provider_records() -> None:
    context = _durable_context()
    first = build_durable_compaction_item(
        context,
        _durable_request(),
        compaction_id="compact-1",
        summary="safe summary",
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        token_estimator=len,
    )
    changed = ModelContext(
        (*context.items[:2], Message(Role.ASSISTANT, "changed"), context.items[-1]),
        source_provider=context.source_provider,
        source_model=context.source_model,
        source_context_affinity=context.source_context_affinity,
    )
    with pytest.raises(ValueError, match="fingerprint"):
        CompactionResumeRebuilder().rebuild(changed, (first,))

    overlapping = replace(
        first,
        compaction_id="compact-2",
        candidate_range=(2, 3),
        source_fingerprint=compute_compaction_source_fingerprint(context.items, (2, 3)),
    )
    with pytest.raises(ValueError, match="overlap"):
        CompactionResumeRebuilder().rebuild(context, (first, overlapping))

    wrong_origin = ModelContext(
        context.items,
        source_provider="other-provider",
        source_model=context.source_model,
        source_context_affinity=context.source_context_affinity,
    )
    with pytest.raises(ValueError, match="provider"):
        CompactionResumeRebuilder().rebuild(wrong_origin, (first,))

    request = _summary_request(source_item_count=3)
    with pytest.raises(TypeError, match="ContextSummaryItem"):
        ContextSummaryInput(
            request=request,
            items=("raw item",),  # type: ignore[arg-type]
            input_budget_tokens=10,
            estimated_input_tokens=0,
            omitted_item_count=2,
            redacted_item_count=0,
            truncated_item_count=0,
        )


def test_provider_summary_generator_uses_disabled_policy_and_redacted_prompt() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items))
    summary_input = ContextSummaryInputBuilder(
        redaction_values=("summary-secret-value",),
        token_estimator=len,
    ).build(source, request)
    provider = ScriptedSummaryProvider(
        ((ModelTextDelta("streamed "), ModelCompleted("stop", 12, 5, response_text="canonical")),)
    )

    result = asyncio.run(
        ProviderContextSummaryGenerator(
            provider,
            redaction_values=("summary-secret-value",),
        ).generate(summary_input)
    )

    assert isinstance(result, ContextSummaryGenerationResult)
    assert result.summary == "canonical"
    assert (result.input_tokens, result.output_tokens) == (12, 5)
    context, tools, policy = provider.requests[0]
    assert tools == ()
    assert policy is ModelToolPolicy.DISABLED
    assert context.source_provider == request.provider_window.provider_name
    assert context.source_model == request.provider_window.model_name
    assert context.messages[0].role is Role.SYSTEM
    assert context.messages[0].content.startswith("You are a bounded context-compaction summarizer")
    assert all("summary-secret-value" not in message.content for message in context.messages)
    assert "summary-secret-value" not in repr(result)
    assert source == _summary_context()


def test_provider_summary_generator_prefers_completion_text_and_bounds_output() -> None:
    source = _summary_context()
    request = replace(_summary_request(source_item_count=len(source.items)), max_summary_tokens=3)
    summary_input = ContextSummaryInputBuilder(token_estimator=len).build(source, request)
    provider = ScriptedSummaryProvider(
        (
            (
                ModelTextDelta("delta should not be duplicated"),
                ModelCompleted("stop", response_text="one two three four"),
            ),
        )
    )

    result = asyncio.run(
        ProviderContextSummaryGenerator(
            provider,
            token_estimator=lambda text: len(text.split()),
        ).generate(summary_input)
    )

    assert result.summary == "one two three"
    assert result.summary_tokens == 3
    assert result.summary_truncated is True


def test_provider_summary_generator_rejects_tool_calls_without_execution() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items))
    summary_input = ContextSummaryInputBuilder().build(source, request)
    provider = ScriptedSummaryProvider(
        (
            (
                ModelToolCall(ToolCall("remote", "read_file", {"path": "secret.txt"})),
                ModelCompleted("tool_calls"),
            ),
        )
    )

    with pytest.raises(ProviderError, match="unexpected tool call"):
        asyncio.run(ProviderContextSummaryGenerator(provider).generate(summary_input))

    assert provider.requests[0][1] == ()
    assert provider.requests[0][2] is ModelToolPolicy.DISABLED


def test_provider_summary_generator_rejects_provider_affinity_mismatch() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items))
    summary_input = ContextSummaryInputBuilder().build(source, request)
    provider = ScriptedSummaryProvider(((ModelCompleted("stop", response_text="summary"),),))
    mismatched = replace(
        request,
        provider_window=ProviderContextWindow(
            "other-provider",
            request.provider_window.model_name,
            request.provider_window.capacity_tokens,
            request.provider_window.context_affinity,
        ),
    )
    mismatched_input = replace(summary_input, request=mismatched)

    with pytest.raises(ValueError, match="provider"):
        asyncio.run(ProviderContextSummaryGenerator(provider).generate(mismatched_input))
    assert provider.requests == []


def test_provider_summary_generator_propagates_provider_error_and_requires_completion() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items))
    summary_input = ContextSummaryInputBuilder().build(source, request)
    failed = ScriptedSummaryProvider(((ProviderError("summary provider unavailable"),),))
    with pytest.raises(ProviderError, match="summary provider unavailable"):
        asyncio.run(ProviderContextSummaryGenerator(failed).generate(summary_input))

    incomplete = ScriptedSummaryProvider(((ModelTextDelta("partial"),),))
    with pytest.raises(ProviderError, match="without a completion"):
        asyncio.run(ProviderContextSummaryGenerator(incomplete).generate(summary_input))


def test_provider_summary_generator_redacts_provider_output_and_keeps_request_bounded() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items), max_summary_tokens=100)
    summary_input = ContextSummaryInputBuilder().build(source, request)
    secret = "summary-output-secret"
    provider = ScriptedSummaryProvider(
        ((ModelCompleted("stop", response_text=f"confirmed {secret}"),),)
    )

    result = asyncio.run(
        ProviderContextSummaryGenerator(provider, redaction_values=(secret,)).generate(
            summary_input
        )
    )

    assert "[REDACTED]" in result.summary
    assert secret not in result.summary
    assert len(provider.requests[0][0].items) == 2
    assert (
        len("\n".join(message.content for message in provider.requests[0][0].messages).encode())
        <= 32 * 1024
    )


def test_provider_summary_generator_rejects_invalid_configuration() -> None:
    with pytest.raises(TypeError, match="stream protocol"):
        ProviderContextSummaryGenerator(None)  # type: ignore[arg-type]
    provider = ScriptedSummaryProvider(())
    with pytest.raises(TypeError, match="redaction_values"):
        ProviderContextSummaryGenerator(provider, redaction_values=(1,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_prompt_bytes"):
        ProviderContextSummaryGenerator(provider, max_prompt_bytes=0)
    with pytest.raises(ValueError, match="summary prompt limit"):
        ProviderContextSummaryGenerator(provider, max_prompt_bytes=32 * 1024 + 1)
    with pytest.raises(ValueError, match="max_summary_bytes"):
        ProviderContextSummaryGenerator(provider, max_summary_bytes=0)
    with pytest.raises(ValueError, match="durable summary limit"):
        ProviderContextSummaryGenerator(provider, max_summary_bytes=8 * 1024 + 1)
    with pytest.raises(TypeError, match="token_estimator"):
        ProviderContextSummaryGenerator(provider, token_estimator=None)  # type: ignore[arg-type]


def test_provider_summary_generator_bounds_the_temporary_prompt() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items))
    summary_input = ContextSummaryInputBuilder().build(source, request)
    provider = ScriptedSummaryProvider(((ModelCompleted("stop", response_text="summary"),),))

    result = asyncio.run(
        ProviderContextSummaryGenerator(provider, max_prompt_bytes=200).generate(summary_input)
    )

    assert result.summary == "summary"
    prompt = provider.requests[0][0].messages[1].content
    assert len(prompt.encode("utf-8")) <= 200


def test_provider_summary_generator_handles_stream_completion_edges() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items))
    summary_input = ContextSummaryInputBuilder().build(source, request)

    streamed = ScriptedSummaryProvider(
        ((ModelTextDelta("streamed summary"), ModelCompleted("stop", response_text=None)),)
    )
    result = asyncio.run(ProviderContextSummaryGenerator(streamed).generate(summary_input))
    assert result.summary == "streamed summary"

    multiple = ScriptedSummaryProvider(
        ((ModelCompleted("stop", response_text="one"), ModelCompleted("stop")),)
    )
    with pytest.raises(ProviderError, match="multiple completions"):
        asyncio.run(ProviderContextSummaryGenerator(multiple).generate(summary_input))

    empty = ScriptedSummaryProvider(((ModelCompleted("stop", response_text="  "),),))
    with pytest.raises(ProviderError, match="empty response"):
        asyncio.run(ProviderContextSummaryGenerator(empty).generate(summary_input))

    no_reason = ScriptedSummaryProvider(((ModelCompleted("", response_text="summary"),),))
    with pytest.raises(ProviderError, match="empty stop reason"):
        asyncio.run(ProviderContextSummaryGenerator(no_reason).generate(summary_input))

    invalid_response = ScriptedSummaryProvider(
        ((ModelCompleted("stop", response_text=123),),)  # type: ignore[arg-type]
    )
    with pytest.raises(ProviderError, match="invalid response"):
        asyncio.run(ProviderContextSummaryGenerator(invalid_response).generate(summary_input))


def test_provider_summary_generator_applies_utf8_output_bound() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items), max_summary_tokens=100)
    summary_input = ContextSummaryInputBuilder().build(source, request)
    provider = ScriptedSummaryProvider(((ModelCompleted("stop", response_text="1234567890"),),))

    result = asyncio.run(
        ProviderContextSummaryGenerator(
            provider,
            max_summary_bytes=8,
            token_estimator=len,
        ).generate(summary_input)
    )

    assert result.summary == "12345678"
    assert result.summary_truncated is True


def test_provider_summary_generator_preserves_cancellation() -> None:
    source = _summary_context()
    request = _summary_request(source_item_count=len(source.items))
    summary_input = ContextSummaryInputBuilder().build(source, request)
    provider = ScriptedSummaryProvider(((asyncio.CancelledError(),),))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ProviderContextSummaryGenerator(provider).generate(summary_input))


def test_context_summary_generation_result_rejects_invalid_invariants() -> None:
    valid = {
        "summary": "safe summary",
        "summary_tokens": 2,
        "input_tokens": None,
        "output_tokens": 2,
        "stop_reason": "stop",
        "source_item_count": 3,
        "omitted_item_count": 1,
        "redacted_item_count": 1,
        "truncated_item_count": 0,
        "summary_truncated": False,
    }
    with pytest.raises(ValueError, match="summary must be non-empty"):
        ContextSummaryGenerationResult(**{**valid, "summary": "  "})
    with pytest.raises(ValueError, match="control"):
        ContextSummaryGenerationResult(**{**valid, "summary": "unsafe\x00"})
    with pytest.raises(ValueError, match="summary_tokens"):
        ContextSummaryGenerationResult(**{**valid, "summary_tokens": 0})
    with pytest.raises(ValueError, match="stop_reason"):
        ContextSummaryGenerationResult(**{**valid, "stop_reason": ""})
    with pytest.raises(ValueError, match="omitted_item_count"):
        ContextSummaryGenerationResult(**{**valid, "omitted_item_count": 4})
    with pytest.raises(ValueError, match="redacted_item_count"):
        ContextSummaryGenerationResult(**{**valid, "redacted_item_count": 4})
    with pytest.raises(ValueError, match="truncated_item_count"):
        ContextSummaryGenerationResult(**{**valid, "truncated_item_count": 4})
    with pytest.raises(ValueError, match="stop_reason is too long"):
        ContextSummaryGenerationResult(**{**valid, "stop_reason": "x" * 129})
    with pytest.raises(TypeError, match="summary_truncated"):
        ContextSummaryGenerationResult(**{**valid, "summary_truncated": 1})  # type: ignore[arg-type]
