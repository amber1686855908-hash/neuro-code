from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.web_search import (
    MAX_TOTAL_RESULT_BYTES,
    HostedWebSearchEvent,
    WebSearchRequest,
)
from neuro_code.configuration.app import ProviderProfile
from neuro_code.infrastructure.providers import create_provider
from neuro_code.infrastructure.providers.hosted_web_search import ResponsesHostedWebSearchBackend

pytestmark = pytest.mark.live


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _profile() -> ProviderProfile:
    if _env("NEURO_CODE_RUN_LIVE_WEB_SEARCH") != "1":
        pytest.skip("live web search requires NEURO_CODE_RUN_LIVE_WEB_SEARCH=1")
    if not _env("XAI_API_KEY"):
        pytest.skip("SKIPPED_NO_CREDENTIAL: XAI_API_KEY is not configured")
    model = _env("NEURO_CODE_LIVE_XAI_SEARCH_MODEL") or "grok-4.6"
    proxy_mode = _env("NEURO_CODE_LIVE_PROXY_MODE") or "environment"
    return ProviderProfile(
        name="live-xai-web-search",
        protocol="openai-responses",
        dialect="xai",
        model=model,
        base_url=_env("NEURO_CODE_LIVE_XAI_SEARCH_BASE_URL") or "https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        builtin_tools=("web_search",),
        timeout_seconds=120,
        max_output_tokens=1_024,
        proxy_mode=proxy_mode,
        proxy_url_env="NEURO_CODE_LIVE_PROXY_URL" if proxy_mode == "explicit" else None,
    )


@pytest.mark.asyncio
async def test_xai_hosted_web_search_executes_and_preserves_citations() -> None:
    profile = _profile()
    events: list[HostedWebSearchEvent] = []

    def factory(
        observer: Callable[[Mapping[str, Any]], None],
        request: WebSearchRequest,
    ) -> ModelProvider:
        options: dict[str, object] = {}
        if request.allowed_domains:
            options = {"filters": {"allowed_domains": list(request.allowed_domains)}}
        return create_provider(
            profile,
            response_observer=observer,
            builtin_tool_options={"web_search": options} if options else None,
            tool_choice="required",
        )

    async def sink(event: HostedWebSearchEvent) -> None:
        events.append(event)

    result = await ResponsesHostedWebSearchBackend(profile, factory).search(
        WebSearchRequest(
            "What is the official xAI web search documentation URL?",
            max_sources=4,
            allowed_domains=("docs.x.ai",),
        ),
        event_sink=sink,
    )

    assert result.sources
    assert result.evidence_text
    assert all(source.url.startswith(("http://", "https://")) for source in result.sources)
    assert any("docs.x.ai" in source.url for source in result.sources)
    assert result.citations or result.sources
    assert result.total_bytes <= MAX_TOTAL_RESULT_BYTES
    assert result.metadata is not None
    assert result.metadata["auxiliary"] is True
    credential = _env("XAI_API_KEY")
    rendered_values = " ".join(
        (
            result.query,
            result.evidence_text,
            *(source.url for source in result.sources),
            *(citation.url for citation in result.citations),
        )
    )
    assert credential not in rendered_values
    assert any(event.name == "web_search" and not event.completed for event in events)
    assert any(event.name == "web_search" and event.completed for event in events)
