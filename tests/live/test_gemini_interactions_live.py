from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import pytest

from neuro_code.application.ports.model import ModelCapability, ModelProvider
from neuro_code.application.ports.web_search import (
    MAX_TOTAL_RESULT_BYTES,
    HostedWebSearchEvent,
    WebSearchRequest,
)
from neuro_code.configuration.app import ProviderProfile
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelCompleted, ModelEvent
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.infrastructure.providers import create_provider
from neuro_code.infrastructure.providers.gemini_interactions import (
    GeminiInteractionsProvider,
)
from neuro_code.infrastructure.providers.hosted_web_search import (
    GeminiHostedWebSearchBackend,
)

pytestmark = pytest.mark.live


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _profile() -> ProviderProfile:
    if _env("NEURO_CODE_RUN_LIVE_GEMINI_INTERACTIONS") != "1":
        pytest.skip(
            "Gemini Interactions live tests require NEURO_CODE_RUN_LIVE_GEMINI_INTERACTIONS=1"
        )
    if not _env("GEMINI_API_KEY"):
        pytest.skip("SKIPPED_NO_CREDENTIAL: GEMINI_API_KEY is not configured")
    model = _env("NEURO_CODE_LIVE_GEMINI_MODEL") or "gemini-3.6-flash"
    proxy_mode = _env("NEURO_CODE_LIVE_PROXY_MODE") or "environment"
    return ProviderProfile(
        name="live-gemini-interactions",
        protocol="gemini-interactions",
        service_id="google-ai-studio",
        model=model,
        base_url=(
            _env("NEURO_CODE_LIVE_GEMINI_BASE_URL")
            or "https://generativelanguage.googleapis.com/v1"
        ),
        api_key_env="GEMINI_API_KEY",
        builtin_tools=("google_search", "url_context"),
        timeout_seconds=120,
        max_output_tokens=1_024,
        proxy_mode=proxy_mode,
        proxy_url_env="NEURO_CODE_LIVE_PROXY_URL" if proxy_mode == "explicit" else None,
    )


def _rendered_result(result: object) -> str:
    return repr(result)


@pytest.mark.asyncio
async def test_gemini_interactions_live_basic_text_and_stateless_request() -> None:
    profile = _profile()
    provider = create_provider(replace(profile, builtin_tools=()))
    events: list[ModelEvent] = [
        event
        async for event in provider.stream(
            ModelContext((Message(Role.USER, "Reply with the single word ready."),)),
            (),
        )
    ]
    completion = next(event for event in events if isinstance(event, ModelCompleted))
    assert completion.response_text
    assert completion.context_items
    assert completion.usage is not None


@pytest.mark.asyncio
async def test_gemini_interactions_live_google_search_sidecar() -> None:
    profile = _profile()
    implementation = GeminiInteractionsProvider.implementation_capabilities(
        model=profile.model,
        builtin_tools=("google_search",),
    )
    if not implementation.supports(ModelCapability.HOSTED_WEB_SEARCH):
        pytest.skip(f"Gemini model {profile.model!r} has no documented Google Search capability")

    events: list[HostedWebSearchEvent] = []

    def factory(
        observer: Callable[[Mapping[str, Any]], None],
        request: WebSearchRequest,
    ) -> ModelProvider:
        del request
        return create_provider(
            replace(profile, builtin_tools=("google_search",)),
            response_observer=observer,
            tool_choice={"allowed_tools": {"mode": "any", "tools": ["google_search"]}},
        )

    async def sink(event: HostedWebSearchEvent) -> None:
        events.append(event)

    result = await GeminiHostedWebSearchBackend(profile, factory).search(
        WebSearchRequest(
            "What is the official Google Gemini Interactions API documentation URL?",
            max_sources=4,
        ),
        event_sink=sink,
    )

    assert result.evidence_text
    assert result.sources or result.citations
    assert result.total_bytes <= MAX_TOTAL_RESULT_BYTES
    assert any(event.name == "google_search" and not event.completed for event in events)
    assert any(event.name == "google_search" and event.completed for event in events)
    assert "search_suggestions" not in result.evidence_text
    assert _env("GEMINI_API_KEY") not in _rendered_result(result)


@pytest.mark.asyncio
async def test_gemini_interactions_live_url_context_when_enabled() -> None:
    if _env("NEURO_CODE_RUN_LIVE_GEMINI_URL_CONTEXT") != "1":
        pytest.skip("URL Context live smoke requires NEURO_CODE_RUN_LIVE_GEMINI_URL_CONTEXT=1")
    profile = _profile()
    implementation = GeminiInteractionsProvider.implementation_capabilities(
        model=profile.model,
        builtin_tools=("url_context",),
    )
    if not implementation.supports(ModelCapability.HOSTED_WEB_FETCH):
        pytest.skip(f"Gemini model {profile.model!r} has no documented URL Context capability")
    provider = create_provider(replace(profile, builtin_tools=("url_context",)))
    events: list[ModelEvent] = [
        event
        async for event in provider.stream(
            ModelContext(
                (
                    Message(
                        Role.USER,
                        "Read https://ai.google.dev/gemini-api/docs/interactions-overview "
                        "and summarize its purpose in one sentence.",
                    ),
                )
            ),
            (),
        )
    ]
    completion = next(event for event in events if isinstance(event, ModelCompleted))
    assert completion.response_text
    assert any(
        getattr(event, "name", None) == "url_context" and getattr(event, "completed", False)
        for event in events
    )
