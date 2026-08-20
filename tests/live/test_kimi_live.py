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


def _credential_env() -> str:
    for name in ("MOONSHOT_API_KEY", "KIMI_API_KEY"):
        if _env(name):
            return name
    pytest.skip("SKIPPED_NO_CREDENTIAL: MOONSHOT_API_KEY or KIMI_API_KEY is not configured")


def _profile() -> ProviderProfile:
    if _env("NEURO_CODE_RUN_LIVE_KIMI") != "1":
        pytest.skip("SKIPPED_NO_CREDENTIAL: set NEURO_CODE_RUN_LIVE_KIMI=1 for paid Kimi tests")
    credential_env = _credential_env()
    proxy_mode = _env("NEURO_CODE_LIVE_PROXY_MODE") or "environment"
    return ProviderProfile(
        name="live-kimi",
        service_id="kimi",
        protocol="openai-chat",
        dialect="kimi",
        model=_env("NEURO_CODE_LIVE_KIMI_MODEL") or "kimi-k2.6",
        base_url=_env("NEURO_CODE_LIVE_KIMI_BASE_URL") or "https://api.moonshot.ai/v1",
        api_key_env=credential_env,
        timeout_seconds=120,
        max_output_tokens=1_024,
        proxy_mode=proxy_mode,
        proxy_url_env="NEURO_CODE_LIVE_PROXY_URL" if proxy_mode == "explicit" else None,
    )


async def _events(
    provider: ModelProvider, context: ModelContext, tools: tuple[ToolDefinition, ...]
) -> list[ModelEvent]:
    try:
        return [event async for event in provider.stream(context, tools)]
    except Exception as error:
        pytest.fail(f"FAILED: {type(error).__name__}: Kimi live request failed")


@pytest.mark.asyncio
async def test_kimi_live_text_tool_roundtrip_and_usage() -> None:
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
    # Kimi thinking models currently allow only auto/none tool choice. Keep the
    # live smoke test on the compatible auto path rather than weakening thinking.
    provider = create_provider(profile, tool_choice="auto")
    initial = await _events(
        provider,
        ModelContext(
            (Message(Role.USER, "Call fixture_lookup with key p3a, then explain the result."),)
        ),
        (tool,),
    )
    calls = [event.call for event in initial if isinstance(event, ModelToolCall)]
    completion = next((event for event in initial if isinstance(event, ModelCompleted)), None)
    if not calls or completion is None:
        pytest.fail("FAILED: Kimi did not return a parseable tool call")
    assert all(isinstance(call.arguments, dict) for call in calls)
    assert completion.usage is not None

    reasoning = "".join(event.text for event in initial if isinstance(event, ModelReasoningDelta))
    assistant = Message(
        Role.ASSISTANT,
        "",
        tool_calls=tuple(calls),
        reasoning_content=reasoning or None,
    )
    tool_results = tuple(
        Message(
            Role.TOOL,
            '{"key":"p3a","value":"fixture-ok"}',
            name=call.name,
            tool_call_id=call.id,
        )
        for call in calls
    )
    final_events = await _events(
        provider,
        ModelContext(
            (
                Message(Role.USER, "Call fixture_lookup with key p3a, then explain the result."),
                assistant,
                *tool_results,
            )
        ),
        (tool,),
    )
    final = next((event for event in final_events if isinstance(event, ModelCompleted)), None)
    assert final is not None
    assert "".join(event.text for event in final_events if isinstance(event, ModelTextDelta))
    assert final.usage is not None
