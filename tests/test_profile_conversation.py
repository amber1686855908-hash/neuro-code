from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator, Sequence

from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelEvent
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ConfigurationError
from neuro_code.runtime import (
    AgentRunResult,
    ConversationBinding,
    ProfileConversationController,
    ProviderOption,
)
from neuro_code.runtime.agent import EventSink


class FixtureProvider:
    context_affinity = None

    def __init__(self, name: str, model: str) -> None:
        self.provider_name = name
        self.model_name = model

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        if False:
            yield


class FixtureConversation:
    def __init__(self, session_id: str | None = None, *, blocked: bool = False) -> None:
        self._session_id = session_id
        self.prompts: list[str] = []
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del sink
        self.prompts.append(prompt)
        self.started.set()
        if self.blocked:
            await self.release.wait()
        self._session_id = self._session_id or "new-session"
        return AgentRunResult(self._session_id, "ok", (), (), (), 1)


def option(
    name: str,
    *,
    available: bool = True,
    credential_configured: bool = True,
) -> ProviderOption:
    return ProviderOption(
        name,
        "openai-chat",
        f"{name}-model",
        available,
        credential_configured,
        default=name == "first",
    )


class ProfileConversationControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_preserves_old_session_and_uses_a_fresh_binding(self) -> None:
        first = FixtureConversation("old-session")
        second = FixtureConversation()
        requested: list[str] = []

        async def bind(name: str) -> ConversationBinding:
            requested.append(name)
            return ConversationBinding(second, FixtureProvider(name, f"{name}-model"))

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(first, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        selection = await controller.select_profile("second")
        result = await controller.run("use the second profile")

        self.assertTrue(selection.changed)
        self.assertEqual(selection.previous_session_id, "old-session")
        self.assertEqual(requested, ["second"])
        self.assertEqual(controller.selected_profile, "second")
        self.assertEqual([profile.selected for profile in controller.profiles], [False, True])
        self.assertEqual(first.prompts, [])
        self.assertEqual(second.prompts, ["use the second profile"])
        self.assertEqual(result.session_id, "new-session")

    async def test_reselecting_current_profile_is_a_noop(self) -> None:
        runner = FixtureConversation("existing")
        factory_calls = 0

        async def bind(_: str) -> ConversationBinding:
            nonlocal factory_calls
            factory_calls += 1
            return ConversationBinding(FixtureConversation(), FixtureProvider("first", "model"))

        controller = ProfileConversationController(
            options=(option("first"),),
            selected_profile="first",
            binding=ConversationBinding(runner, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        selection = await controller.select_profile("first")

        self.assertFalse(selection.changed)
        self.assertEqual(factory_calls, 0)
        self.assertEqual(controller.session_id, "existing")

    async def test_unready_unknown_and_nonfresh_profiles_fail_closed(self) -> None:
        runner = FixtureConversation("existing")

        async def bind(name: str) -> ConversationBinding:
            if name == "broken":
                raise ConfigurationError("fixture provider construction failed")
            return ConversationBinding(
                FixtureConversation("unexpected-resume"),
                FixtureProvider(name, f"{name}-model"),
            )

        controller = ProfileConversationController(
            options=(
                option("first"),
                option("unavailable", available=False),
                option("missing-key", credential_configured=False),
                option("resumed"),
                option("broken"),
            ),
            selected_profile="first",
            binding=ConversationBinding(runner, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        for name, message in (
            ("unknown", "does not exist"),
            ("unavailable", "unavailable"),
            ("missing-key", "credential is not configured"),
            ("resumed", "fresh conversation"),
            ("broken", "provider construction failed"),
        ):
            with self.assertRaisesRegex(ConfigurationError, message):
                await controller.select_profile(name)

        self.assertEqual(controller.selected_profile, "first")
        self.assertEqual(controller.session_id, "existing")

    async def test_switch_is_rejected_while_a_turn_is_running(self) -> None:
        runner = FixtureConversation(blocked=True)

        async def bind(name: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation(), FixtureProvider(name, f"{name}-model")
            )

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(runner, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        turn = asyncio.create_task(controller.run("blocked"))
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        with self.assertRaisesRegex(ConfigurationError, "while a turn is running"):
            await controller.select_profile("second")
        runner.release.set()
        await turn

        self.assertEqual(controller.selected_profile, "first")
