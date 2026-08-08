from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest

from neuro_code.application.memory.compaction import (
    ContextSummaryGenerationResult,
    ContextSummaryRequest,
    ProviderContextWindow,
)
from neuro_code.application.memory.compaction_service import (
    ContextCompactionApplicationService,
    ContextCompactionPersistenceResult,
    PersistContextCompactionRequest,
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
from neuro_code.domain.tools import ToolDefinition
from neuro_code.shared.errors import ProviderError


class ScriptedCompactionProvider:
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


class RecordingCompactionStore:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, object]] = []

    async def save_compaction_item(
        self,
        session_id: str,
        item: DurableCompactionItem,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((session_id, item))


class IdempotentCompactionStore(RecordingCompactionStore):
    def __init__(self) -> None:
        super().__init__()
        self.items: dict[str, object] = {}

    async def save_compaction_item(
        self,
        session_id: str,
        item: DurableCompactionItem,
    ) -> None:
        compaction_id = item.compaction_id
        previous = self.items.get(compaction_id)
        if previous is not None and previous != item:
            raise RuntimeError("conflicting compaction record")
        self.items[compaction_id] = item
        await super().save_compaction_item(session_id, item)


def _context() -> ModelContext:
    return ModelContext(
        (
            Message(Role.SYSTEM, "system instructions"),
            Message(Role.USER, "inspect the workspace secret-value"),
            Message(Role.ASSISTANT, "confirmed the relevant files"),
            Message(Role.USER, "keep the recent request"),
        )
    )


def _summary_request() -> ContextSummaryRequest:
    return ContextSummaryRequest(
        provider_window=ProviderContextWindow(
            "provider",
            "model",
            capacity_tokens=1_000,
            context_affinity="profile",
        ),
        source_item_count=4,
        protected_item_count=1,
        recent_item_count=1,
        candidate_range=(1, 3),
        target_tokens=800,
        max_summary_tokens=100,
    )


def _request(
    context: ModelContext, *, expected: str | None = None
) -> PersistContextCompactionRequest:
    summary_request = _summary_request()
    fingerprint = expected or compute_compaction_source_fingerprint(
        context.items,
        summary_request.candidate_range,
    )
    return PersistContextCompactionRequest(
        session_id="session-1",
        compaction_id="compaction-1",
        context=context,
        summary_request=summary_request,
        expected_source_fingerprint=fingerprint,
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


def _service(
    store: RecordingCompactionStore,
    provider: ScriptedCompactionProvider,
) -> ContextCompactionApplicationService:
    return ContextCompactionApplicationService(
        cast(SessionStore, store),
        provider,
        redaction_values=("secret-value",),
        token_estimator=lambda text: max(1, len(text.split())),
    )


def test_application_service_generates_and_saves_one_bounded_item() -> None:
    context = _context()
    provider = ScriptedCompactionProvider(
        ((ModelCompleted("stop", 12, 6, response_text="confirmed secret-value"),),)
    )
    store = RecordingCompactionStore()

    result = asyncio.run(_service(store, provider).generate_and_save(_request(context)))

    assert isinstance(result, ContextCompactionPersistenceResult)
    assert len(store.calls) == 1
    assert store.calls[0][0] == "session-1"
    item = result.item
    assert item.compaction_id == "compaction-1"
    assert item.summary == "confirmed [REDACTED]"
    assert item.source_fingerprint == compute_compaction_source_fingerprint(
        context.items,
        (1, 3),
    )
    assert result.generation.input_tokens == 12
    assert result.generation.output_tokens == 6
    assert provider.requests[0][1] == ()
    assert provider.requests[0][2] is ModelToolPolicy.DISABLED
    assert all(
        "secret-value" not in message.content for message in provider.requests[0][0].messages
    )
    assert context == _context()
    assert "secret-value" not in repr(result)


def test_application_service_rejects_stale_source_before_model_call() -> None:
    provider = ScriptedCompactionProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingCompactionStore()

    with pytest.raises(ValueError, match="source fingerprint is stale"):
        asyncio.run(
            _service(store, provider).generate_and_save(_request(_context(), expected="a" * 64))
        )

    assert provider.requests == []
    assert store.calls == []


def test_application_service_rejects_source_count_drift_before_model_call() -> None:
    context = _context()
    provider = ScriptedCompactionProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingCompactionStore()
    request = _request(context)
    drifted = PersistContextCompactionRequest(
        session_id=request.session_id,
        compaction_id=request.compaction_id,
        context=request.context,
        summary_request=ContextSummaryRequest(
            provider_window=request.summary_request.provider_window,
            source_item_count=5,
            protected_item_count=1,
            recent_item_count=1,
            candidate_range=(1, 3),
            target_tokens=800,
            max_summary_tokens=100,
        ),
        expected_source_fingerprint=request.expected_source_fingerprint,
        created_at=request.created_at,
    )

    with pytest.raises(ValueError, match="item count"):
        asyncio.run(_service(store, provider).generate_and_save(drifted))

    assert provider.requests == []
    assert store.calls == []


def test_application_service_does_not_persist_provider_failure_or_cancellation() -> None:
    for failure in (
        ProviderError("provider failed"),
        asyncio.CancelledError(),
    ):
        provider = ScriptedCompactionProvider(((failure,),))
        store = RecordingCompactionStore()
        with pytest.raises(type(failure)):
            asyncio.run(_service(store, provider).generate_and_save(_request(_context())))
        assert store.calls == []


def test_application_service_propagates_storage_failure_after_generation() -> None:
    provider = ScriptedCompactionProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingCompactionStore(RuntimeError("storage unavailable"))

    with pytest.raises(RuntimeError, match="storage unavailable"):
        asyncio.run(_service(store, provider).generate_and_save(_request(_context())))

    assert len(provider.requests) == 1
    assert store.calls == []


def test_application_service_delegates_duplicate_idempotency_to_store() -> None:
    provider = ScriptedCompactionProvider(
        (
            (ModelCompleted("stop", response_text="same summary"),),
            (ModelCompleted("stop", response_text="same summary"),),
        )
    )
    store = IdempotentCompactionStore()
    service = _service(store, provider)
    request = _request(_context())

    first = asyncio.run(service.generate_and_save(request))
    second = asyncio.run(service.generate_and_save(request))

    assert first.item == second.item
    assert len(store.items) == 1
    assert len(store.calls) == 2
    assert len(provider.requests) == 2


def test_persist_request_hides_context_and_digest_and_validates_boundaries() -> None:
    request = _request(_context())
    assert "secret-value" not in repr(request)
    assert request.expected_source_fingerprint not in repr(request)

    with pytest.raises(ValueError, match="created_at"):
        PersistContextCompactionRequest(
            session_id=request.session_id,
            compaction_id=request.compaction_id,
            context=request.context,
            summary_request=request.summary_request,
            expected_source_fingerprint=request.expected_source_fingerprint,
            created_at=datetime(2026, 8, 8, tzinfo=UTC).replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="SHA-256"):
        _request(_context(), expected="not-a-digest")


def test_persist_request_rejects_invalid_identifiers_and_types() -> None:
    request = _request(_context())
    fields = {
        "session_id": request.session_id,
        "compaction_id": request.compaction_id,
        "context": request.context,
        "summary_request": request.summary_request,
        "expected_source_fingerprint": request.expected_source_fingerprint,
        "created_at": request.created_at,
    }

    with pytest.raises(ValueError, match="session_id"):
        PersistContextCompactionRequest(**{**fields, "session_id": ""})
    with pytest.raises(ValueError, match="control"):
        PersistContextCompactionRequest(**{**fields, "compaction_id": "bad\nvalue"})
    with pytest.raises(TypeError, match="ModelContext"):
        PersistContextCompactionRequest(**{**fields, "context": object()})
    with pytest.raises(TypeError, match="ContextSummaryRequest"):
        PersistContextCompactionRequest(**{**fields, "summary_request": object()})
    with pytest.raises(ValueError, match="SHA-256"):
        PersistContextCompactionRequest(**{**fields, "expected_source_fingerprint": "A" * 64})


def test_application_service_rejects_invalid_request_and_result_accounting() -> None:
    service = _service(RecordingCompactionStore(), ScriptedCompactionProvider(()))

    with pytest.raises(TypeError, match="PersistContextCompactionRequest"):
        asyncio.run(service.generate_and_save(cast(PersistContextCompactionRequest, object())))

    provider = ScriptedCompactionProvider(((ModelCompleted("stop", response_text="summary"),),))
    store = RecordingCompactionStore()
    result = asyncio.run(_service(store, provider).generate_and_save(_request(_context())))
    mismatched_generation = replace(
        result.generation,
        summary_tokens=result.generation.summary_tokens + 1,
    )
    with pytest.raises(ValueError, match="token accounting"):
        ContextCompactionPersistenceResult(result.item, mismatched_generation)

    mismatched_truncation = replace(
        result.generation, summary_truncated=not result.generation.summary_truncated
    )
    with pytest.raises(ValueError, match="truncation"):
        ContextCompactionPersistenceResult(result.item, mismatched_truncation)

    with pytest.raises(TypeError, match="DurableCompactionItem"):
        ContextCompactionPersistenceResult(cast(object, object()), result.generation)
    with pytest.raises(TypeError, match="ContextSummaryGenerationResult"):
        ContextCompactionPersistenceResult(
            result.item, cast(ContextSummaryGenerationResult, object())
        )
