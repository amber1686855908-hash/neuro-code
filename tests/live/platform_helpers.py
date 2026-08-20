from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

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


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def platform_profile(
    *,
    service_id: str,
    opt_in: str,
    api_key_env: str,
    protocol_env: str,
    default_protocol: str,
    base_url_env: str,
    base_urls_by_protocol: Mapping[str, str],
    model_env: str,
    default_model: str,
) -> ProviderProfile:
    if env("NEURO_CODE_RUN_LIVE_PLATFORM_TESTS") != "1":
        pytest.skip(
            "SKIPPED_NO_CREDENTIAL: set NEURO_CODE_RUN_LIVE_PLATFORM_TESTS=1 "
            "for paid platform tests"
        )
    if env(opt_in) != "1":
        pytest.skip(f"SKIPPED_NO_CREDENTIAL: set {opt_in}=1 for paid platform tests")
    if not env(api_key_env):
        pytest.skip(f"SKIPPED_NO_CREDENTIAL: {api_key_env} is not configured")
    protocol = env(protocol_env, default_protocol)
    try:
        default_base_url = base_urls_by_protocol[protocol]
    except KeyError as error:
        pytest.fail(f"FAILED: unsupported live protocol selection {protocol!r}")
        raise AssertionError from error
    proxy_mode = env("NEURO_CODE_LIVE_PROXY_MODE", "environment")
    return ProviderProfile(
        name=f"live-{service_id}",
        service_id=service_id,
        protocol=protocol,
        model=env(model_env, default_model),
        base_url=env(base_url_env, default_base_url),
        api_key_env=api_key_env,
        timeout_seconds=120,
        max_output_tokens=1_024,
        native_context="profile",
        proxy_mode=proxy_mode,
        proxy_url_env="NEURO_CODE_LIVE_PROXY_URL" if proxy_mode == "explicit" else None,
    )


async def collect_events(
    provider: ModelProvider,
    context: ModelContext,
    tools: Sequence[ToolDefinition],
    provider_label: str,
) -> list[ModelEvent]:
    try:
        return [event async for event in provider.stream(context, tuple(tools))]
    except Exception as error:
        pytest.fail(f"FAILED: {type(error).__name__}: {provider_label} live request failed")
        return []


def fixture_tool() -> ToolDefinition:
    return ToolDefinition(
        "fixture_lookup",
        "Return the fixed value for the requested key.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    )


async def run_tool_roundtrip(
    provider: ModelProvider,
    provider_label: str,
) -> None:
    tool = fixture_tool()
    prompt = "Use fixture_lookup with key p3b, then explain its fixed result."
    initial = await collect_events(
        provider,
        ModelContext((Message(Role.USER, prompt),)),
        (tool,),
        provider_label,
    )
    calls = [event.call for event in initial if isinstance(event, ModelToolCall)]
    completion = next((event for event in initial if isinstance(event, ModelCompleted)), None)
    if not calls or completion is None:
        pytest.fail(f"FAILED: {provider_label} did not return a parseable tool call")
    assert all(isinstance(call.arguments, Mapping) for call in calls)
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
            '{"key":"p3b","value":"fixture-ok"}',
            name=call.name,
            tool_call_id=call.id,
        )
        for call in calls
    )
    final_events = await collect_events(
        provider,
        ModelContext(
            (
                Message(Role.USER, prompt),
                *completion.context_items,
                assistant,
                *tool_results,
            ),
            source_provider=provider.provider_name,
            source_model=provider.model_name,
            source_context_affinity=provider.context_affinity,
        ),
        (tool,),
        provider_label,
    )
    final = next((event for event in final_events if isinstance(event, ModelCompleted)), None)
    assert final is not None
    assert "".join(event.text for event in final_events if isinstance(event, ModelTextDelta))


async def run_text_probe(provider: ModelProvider, provider_label: str) -> None:
    marker = f"NEURO_CODE_{provider_label.upper()}_P3B_TEXT_OK"
    events = await collect_events(
        provider,
        ModelContext((Message(Role.USER, f"Reply with exactly {marker} and nothing else."),)),
        (),
        provider_label,
    )
    assert marker in "".join(event.text for event in events if isinstance(event, ModelTextDelta))
