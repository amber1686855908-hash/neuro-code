"""Provider adapters used by the benchmark boundary.

``ScriptedBenchmarkProvider`` is only a deterministic test/smoke double.  It
still emits ordinary model events and tool calls, so the production
ApplicationComposition, AgentLoop, ToolExecutor, Session, and sandbox path are
exercised.  Live runs use the normal routed provider factory instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from neuro_code.application.ports.model import (
    ModelCapability,
    ModelCapabilitySet,
    ModelToolPolicy,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelProviderSelected,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.tools import ToolDefinition


class ScriptedBenchmarkProvider:
    """A regular tool-calling provider double, not a benchmark execution path."""

    provider_name = "benchmark-fixture"
    model_name = "fixture-tool-caller"
    context_affinity = "benchmark-fixture-v1"
    capabilities = ModelCapabilitySet.from_supported(ModelCapability.FUNCTION_TOOLS)

    def __init__(self, commands: Sequence[str]) -> None:
        self._commands = list(commands)
        self.calls = 0
        self.tool_definitions: list[tuple[ToolDefinition, ...]] = []
        self.contexts: list[ModelContext] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        self.contexts.append(context)
        self.tool_definitions.append(tuple(tools))
        yield ModelProviderSelected(
            self.provider_name,
            self.model_name,
            self.context_affinity,
            False,
            131_072,
        )
        if tool_policy is ModelToolPolicy.DISABLED or not self._commands:
            yield ModelTextDelta("Fixture completed the requested work.")
            yield ModelCompleted("stop", input_tokens=64, output_tokens=12)
            return
        command = self._commands.pop(0)
        yield ModelToolCall(ToolCall(f"fixture-call-{self.calls}", "bash", {"command": command}))
        yield ModelCompleted("tool_calls", input_tokens=64, output_tokens=8)


__all__ = ["ScriptedBenchmarkProvider"]
