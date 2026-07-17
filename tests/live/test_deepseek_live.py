from __future__ import annotations

from pathlib import Path

import pytest

from neuro_code.domain.events import AgentEventKind
from neuro_code.domain.messages import Message, Role
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import (
    ModelCompleted,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelTextDelta,
)
from neuro_code.errors import ConfigurationError
from neuro_code.permissions import PermissionManager
from neuro_code.ports.model import ModelProvider
from neuro_code.ports.tools import ToolContext
from neuro_code.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.runtime import AgentRuntime
from neuro_code.tools.filesystem import ReadFileTool
from neuro_code.tools.registry import ToolRegistry

pytestmark = pytest.mark.live

_STREAM_MARKER = "NEURO_CODE_DEEPSEEK_STREAM_OK"
_TOOL_MARKER = "NEURO_CODE_DEEPSEEK_TOOL_ROUNDTRIP_7F3A91"


@pytest.mark.asyncio
async def test_deepseek_stream_recovers_through_safe_failover(
    deepseek_provider: ModelProvider,
) -> None:
    def unavailable_primary() -> ModelProvider:
        raise ConfigurationError("intentional live-test primary failure")

    provider = FailoverModelProvider(
        (
            ProviderCandidate(
                "unavailable-live-primary",
                "unavailable-model",
                None,
                unavailable_primary,
            ),
            ProviderCandidate(
                deepseek_provider.provider_name,
                deepseek_provider.model_name,
                deepseek_provider.context_affinity,
                lambda: deepseek_provider,
            ),
        )
    )
    context = ModelContext(
        (
            Message(Role.SYSTEM, "Follow the user's output format exactly."),
            Message(Role.USER, f"Reply with exactly {_STREAM_MARKER} and nothing else."),
        )
    )

    events = [event async for event in provider.stream(context, ())]

    assert isinstance(events[0], ModelProviderAttemptFailed)
    selected = next(event for event in events if isinstance(event, ModelProviderSelected))
    assert selected.provider == "live-deepseek"
    assert selected.failover
    assert any(isinstance(event, ModelCompleted) for event in events)
    response = "".join(event.text for event in events if isinstance(event, ModelTextDelta))
    assert _STREAM_MARKER in response


@pytest.mark.asyncio
async def test_deepseek_performs_a_read_only_local_tool_roundtrip(
    deepseek_provider: ModelProvider,
    tmp_path: Path,
) -> None:
    (tmp_path / "live_probe.txt").write_text(f"{_TOOL_MARKER}\n", encoding="utf-8")
    runtime = AgentRuntime(
        provider=deepseek_provider,
        tools=ToolRegistry((ReadFileTool(),)),
        permissions=PermissionManager(),
        tool_context=ToolContext(tmp_path),
        system_prompt=(
            "You are a deterministic integration-test agent. When instructed to read a file, "
            "you must call the supplied read_file tool before answering."
        ),
        max_steps=3,
    )

    result = await runtime.run(
        "Use read_file to inspect live_probe.txt. Then reply with the marker found in the file."
    )

    assert any(
        event.kind is AgentEventKind.TOOL_COMPLETED and event.data.get("name") == "read_file"
        for event in result.events
    )
    assert result.steps >= 2
    assert _TOOL_MARKER in result.response
