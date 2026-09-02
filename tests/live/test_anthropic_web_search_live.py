from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from neuro_code.application.ports.configuration import ProviderProfile
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.web_search import (
    MAX_TOTAL_RESULT_BYTES,
    HostedWebSearchEvent,
    WebSearchRequest,
)
from neuro_code.infrastructure.providers import create_provider
from neuro_code.infrastructure.providers.hosted_web_search import (
    AnthropicHostedWebSearchBackend,
)

pytestmark = pytest.mark.live


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _profile() -> ProviderProfile:
    if _env("NEURO_CODE_RUN_LIVE_WEB_SEARCH") != "1":
        pytest.skip("live web search requires NEURO_CODE_RUN_LIVE_WEB_SEARCH=1")
    if _env("NEURO_CODE_RUN_LIVE_ANTHROPIC_WEB_SEARCH") != "1":
        pytest.skip("Anthropic live web search requires NEURO_CODE_RUN_LIVE_ANTHROPIC_WEB_SEARCH=1")
    if not _env("ANTHROPIC_API_KEY"):
        pytest.skip("SKIPPED_NO_CREDENTIAL: ANTHROPIC_API_KEY is not configured")
    return ProviderProfile(
        name="live-anthropic-web-search",
        service_id="anthropic",
        protocol="anthropic-messages",
        model=_env("NEURO_CODE_LIVE_ANTHROPIC_SEARCH_MODEL") or "claude-sonnet-4-6",
        base_url=_env("NEURO_CODE_LIVE_ANTHROPIC_SEARCH_BASE_URL") or "https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        builtin_tools=("web_search", "web_fetch"),
        timeout_seconds=120,
        max_output_tokens=1_024,
        proxy_mode=_env("NEURO_CODE_LIVE_PROXY_MODE") or "environment",
        proxy_url_env=(
            "NEURO_CODE_LIVE_PROXY_URL"
            if _env("NEURO_CODE_LIVE_PROXY_MODE") == "explicit"
            else None
        ),
    )


@pytest.mark.asyncio
async def test_anthropic_hosted_web_search_executes_and_returns_sources() -> None:
    profile = _profile()
    events: list[HostedWebSearchEvent] = []

    def factory(
        observer: Callable[[Mapping[str, Any]], None],
        request: WebSearchRequest,
    ) -> ModelProvider:
        options: dict[str, object] = {"max_uses": 1}
        if request.allowed_domains:
            options["allowed_domains"] = list(request.allowed_domains)
        return create_provider(
            profile,
            response_observer=observer,
            builtin_tool_options={"web_search": options, "web_fetch": {"max_uses": 1}},
            tool_choice={"type": "tool", "name": "web_search"},
        )

    async def sink(event: HostedWebSearchEvent) -> None:
        events.append(event)

    result = await AnthropicHostedWebSearchBackend(profile, factory).search(
        WebSearchRequest(
            "What is the official Anthropic web search tool documentation URL?",
            max_sources=4,
            allowed_domains=("platform.claude.com",),
        ),
        event_sink=sink,
    )

    assert result.sources
    assert result.evidence_text
    assert result.total_bytes <= MAX_TOTAL_RESULT_BYTES
    assert any("platform.claude.com" in source.url for source in result.sources)
    credential = _env("ANTHROPIC_API_KEY")
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
