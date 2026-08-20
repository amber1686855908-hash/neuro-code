from __future__ import annotations

import os

import pytest

from neuro_code.application.ports.model import ModelProvider
from neuro_code.configuration.app import ProviderProfile
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers import create_provider

pytestmark = pytest.mark.live


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _profile() -> ProviderProfile:
    if _env("NEURO_CODE_RUN_LIVE_MINIMAX") != "1":
        pytest.skip(
            "SKIPPED_NO_CREDENTIAL: set NEURO_CODE_RUN_LIVE_MINIMAX=1 for paid MiniMax tests"
        )
    if not _env("MINIMAX_API_KEY"):
        pytest.skip("SKIPPED_NO_CREDENTIAL: MINIMAX_API_KEY is not configured")
    proxy_mode = _env("NEURO_CODE_LIVE_PROXY_MODE") or "environment"
    return ProviderProfile(
        name="live-minimax",
        service_id="minimax",
        protocol="openai-chat",
        dialect="minimax",
        model=_env("NEURO_CODE_LIVE_MINIMAX_MODEL") or "MiniMax-M3",
        base_url=_env("NEURO_CODE_LIVE_MINIMAX_BASE_URL") or "https://api.minimaxi.com/v1",
        api_key_env="MINIMAX_API_KEY",
        timeout_seconds=120,
        max_output_tokens=1_024,
        native_context="profile",
        proxy_mode=proxy_mode,
        proxy_url_env="NEURO_CODE_LIVE_PROXY_URL" if proxy_mode == "explicit" else None,
    )


async def _events(
    provider: ModelProvider, context: ModelContext, tools: tuple[ToolDefinition, ...]
) -> list[ModelEvent]:
    try:
        return [event async for event in provider.stream(context, tools)]
    except Exception as error:
        pytest.fail(f"FAILED: {type(error).__name__}: MiniMax live request failed")


@pytest.mark.asyncio
async def test_minimax_live_text_tool_roundtrip_structured_reasoning_and_usage() -> None:
    profile = _profile()
    tool = ToolDefinition(
        "fixture_lookup",
        "Return the fixed value for the requested key.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    )
    provider = create_provider(profile)
    prompt = "Use fixture_lookup with key p3a, then explain its fixed result."
    initial = await _events(provider, ModelContext((Message(Role.USER, prompt),)), (tool,))
    calls = [event.call for event in initial if isinstance(event, ModelToolCall)]
    completion = next((event for event in initial if isinstance(event, ModelCompleted)), None)
    if not calls or completion is None:
        pytest.fail("FAILED: MiniMax did not return a parseable tool call")
    assert all(isinstance(call.arguments, dict) for call in calls)
    assert completion.usage is not None
    reasoning_events = [event for event in initial if isinstance(event, ModelReasoningDelta)]
    reasoning = "".join(event.text for event in reasoning_events)
    assistant = Message(
        Role.ASSISTANT,
        "",
        tool_calls=tuple(calls),
        reasoning_content=reasoning or None,
    )
    assert completion.context_items
    final_events = await _events(
        provider,
        ModelContext(
            (
                Message(Role.USER, prompt),
                *completion.context_items,
                assistant,
                *(
                    Message(Role.TOOL, '{"value":"fixture-ok"}', tool_call_id=call.id)
                    for call in calls
                ),
            ),
            source_provider=provider.provider_name,
            source_model=provider.model_name,
            source_context_affinity=provider.context_affinity,
        ),
        (tool,),
    )
    final = next((event for event in final_events if isinstance(event, ModelCompleted)), None)
    assert final is not None
    assert "".join(event.text for event in final_events if isinstance(event, ModelTextDelta))
    assert final.usage is not None
