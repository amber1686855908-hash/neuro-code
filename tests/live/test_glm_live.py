from __future__ import annotations

import os

import pytest

from neuro_code.application.ports.configuration import ProviderProfile
from neuro_code.application.ports.model import ModelProvider
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
    for name in ("ZHIPU_API_KEY", "GLM_API_KEY", "ZAI_API_KEY"):
        if _env(name):
            return name
    pytest.skip(
        "SKIPPED_NO_CREDENTIAL: ZHIPU_API_KEY, GLM_API_KEY, or ZAI_API_KEY is not configured"
    )


def _profile() -> ProviderProfile:
    if _env("NEURO_CODE_RUN_LIVE_GLM") != "1":
        pytest.skip("SKIPPED_NO_CREDENTIAL: set NEURO_CODE_RUN_LIVE_GLM=1 for paid GLM tests")
    credential_env = _credential_env()
    proxy_mode = _env("NEURO_CODE_LIVE_PROXY_MODE") or "environment"
    return ProviderProfile(
        name="live-glm",
        service_id="glm",
        protocol="openai-chat",
        dialect="glm",
        model=_env("NEURO_CODE_LIVE_GLM_MODEL") or "glm-5.3",
        base_url=_env("NEURO_CODE_LIVE_GLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4",
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
        pytest.fail(f"FAILED: {type(error).__name__}: GLM live request failed")


@pytest.mark.asyncio
async def test_glm_live_text_tool_roundtrip_and_usage() -> None:
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
        pytest.fail("FAILED: GLM did not return a parseable tool call")
    assert all(isinstance(call.arguments, dict) for call in calls)
    assert completion.usage is not None
    reasoning = "".join(event.text for event in initial if isinstance(event, ModelReasoningDelta))
    assistant = Message(
        Role.ASSISTANT,
        "",
        tool_calls=tuple(calls),
        reasoning_content=reasoning or None,
    )
    final_events = await _events(
        provider,
        ModelContext(
            (
                Message(Role.USER, prompt),
                assistant,
                *(
                    Message(Role.TOOL, '{"value":"fixture-ok"}', tool_call_id=call.id)
                    for call in calls
                ),
            )
        ),
        (tool,),
    )
    final = next((event for event in final_events if isinstance(event, ModelCompleted)), None)
    assert final is not None
    assert "".join(event.text for event in final_events if isinstance(event, ModelTextDelta))
    assert final.usage is not None
