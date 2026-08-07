from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator, Sequence

from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelTextDelta,
)
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.shared.errors import ConfigurationError, ProviderError


class ScriptedModelProvider:
    def __init__(
        self,
        name: str,
        scripts: Sequence[Sequence[ModelEvent | BaseException]],
    ) -> None:
        self.provider_name = name
        self.model_name = f"{name}-model"
        self.context_affinity = f"profile-v1:{name}"
        self._scripts = [tuple(script) for script in scripts]
        self.calls = 0
        self.tool_policies: list[ModelToolPolicy] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        self.tool_policies.append(tool_policy)
        script = self._scripts[self.calls]
        self.calls += 1
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item


def _candidate(provider: ScriptedModelProvider) -> ProviderCandidate:
    return ProviderCandidate(
        provider.provider_name,
        provider.model_name,
        provider.context_affinity,
        lambda: provider,
        context_window_tokens=128_000,
    )


class FailoverModelProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_before_output_selects_the_next_provider(self) -> None:
        primary = ScriptedModelProvider(
            "primary",
            ((ProviderError("temporary upstream failure"),),),
        )
        fallback = ScriptedModelProvider(
            "fallback",
            ((ModelTextDelta("ok"), ModelCompleted("stop")),),
        )
        router = FailoverModelProvider((_candidate(primary), _candidate(fallback)))

        events = [
            event
            async for event in router.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
            )
        ]

        self.assertEqual(
            [type(event) for event in events],
            [
                ModelProviderAttemptFailed,
                ModelProviderSelected,
                ModelTextDelta,
                ModelCompleted,
            ],
        )
        selected = events[1]
        assert isinstance(selected, ModelProviderSelected)
        self.assertTrue(selected.failover)
        self.assertEqual(selected.context_window_tokens, 128_000)
        self.assertEqual(router.provider_name, "fallback")
        self.assertEqual(router.context_affinity, "profile-v1:fallback")
        self.assertEqual(primary.tool_policies, [ModelToolPolicy.ALLOWED])
        self.assertEqual(fallback.tool_policies, [ModelToolPolicy.ALLOWED])

    async def test_disabled_policy_is_forwarded_to_every_failover_candidate(self) -> None:
        primary = ScriptedModelProvider(
            "primary",
            ((ProviderError("temporary upstream failure"),),),
        )
        fallback = ScriptedModelProvider(
            "fallback",
            ((ModelTextDelta("ok"), ModelCompleted("stop")),),
        )
        router = FailoverModelProvider((_candidate(primary), _candidate(fallback)))

        events = [
            event
            async for event in router.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
                tool_policy=ModelToolPolicy.DISABLED,
            )
        ]

        self.assertEqual(
            [type(event) for event in events],
            [
                ModelProviderAttemptFailed,
                ModelProviderSelected,
                ModelTextDelta,
                ModelCompleted,
            ],
        )
        self.assertEqual(primary.tool_policies, [ModelToolPolicy.DISABLED])
        self.assertEqual(fallback.tool_policies, [ModelToolPolicy.DISABLED])

    async def test_cancellation_keeps_the_requested_policy_and_does_not_fail_over(self) -> None:
        primary = ScriptedModelProvider("primary", ((asyncio.CancelledError(),),))
        fallback = ScriptedModelProvider(
            "fallback",
            ((ModelTextDelta("unexpected"), ModelCompleted("stop")),),
        )
        router = FailoverModelProvider((_candidate(primary), _candidate(fallback)))

        with self.assertRaises(asyncio.CancelledError):
            async for _ in router.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
                tool_policy=ModelToolPolicy.DISABLED,
            ):
                pass

        self.assertEqual(primary.tool_policies, [ModelToolPolicy.DISABLED])
        self.assertEqual(fallback.calls, 0)

    async def test_failure_after_first_model_event_never_fails_over(self) -> None:
        primary = ScriptedModelProvider(
            "primary",
            ((ModelTextDelta("partial"), ProviderError("stream interrupted")),),
        )
        fallback_created = False

        def create_fallback() -> ScriptedModelProvider:
            nonlocal fallback_created
            fallback_created = True
            return ScriptedModelProvider(
                "fallback",
                ((ModelTextDelta("duplicate"), ModelCompleted("stop")),),
            )

        router = FailoverModelProvider(
            (
                _candidate(primary),
                ProviderCandidate(
                    "fallback",
                    "fallback-model",
                    "profile-v1:fallback",
                    create_fallback,
                ),
            )
        )
        emitted: list[ModelEvent] = []

        with self.assertRaisesRegex(ProviderError, "stream interrupted"):
            async for event in router.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
            ):
                emitted.append(event)

        self.assertEqual(
            [type(event) for event in emitted],
            [ModelProviderSelected, ModelTextDelta],
        )
        self.assertFalse(fallback_created)

    async def test_successful_failover_is_monotonic_across_model_steps(self) -> None:
        primary = ScriptedModelProvider("primary", ((ProviderError("offline"),),))
        fallback = ScriptedModelProvider(
            "fallback",
            (
                (ModelTextDelta("first"), ModelCompleted("tool_calls")),
                (ModelTextDelta("second"), ModelCompleted("stop")),
            ),
        )
        router = FailoverModelProvider((_candidate(primary), _candidate(fallback)))
        context = ModelContext((Message(Role.USER, "hello"),))

        first = [event async for event in router.stream(context, ())]
        second = [event async for event in router.stream(context, ())]

        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 2)
        self.assertTrue(any(isinstance(event, ModelProviderSelected) for event in first))
        self.assertFalse(any(isinstance(event, ModelProviderSelected) for event in second))

    async def test_configuration_failures_and_empty_streams_are_audited(self) -> None:
        async def empty_stream(
            context: ModelContext,
            tools: Sequence[ToolDefinition],
            *,
            tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
        ) -> AsyncIterator[ModelEvent]:
            del context, tools, tool_policy
            if False:
                yield ModelCompleted("stop")

        class EmptyProvider:
            provider_name = "empty"
            model_name = "empty-model"
            context_affinity = None
            stream = staticmethod(empty_stream)

        def missing_credential() -> ScriptedModelProvider:
            raise ConfigurationError("credential is missing")

        router = FailoverModelProvider(
            (
                ProviderCandidate("missing", "missing-model", None, missing_credential),
                ProviderCandidate("empty", "empty-model", None, EmptyProvider),
            )
        )
        emitted: list[ModelEvent] = []

        with self.assertRaisesRegex(ProviderError, "all configured model providers failed"):
            async for event in router.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
            ):
                emitted.append(event)

        self.assertEqual(len(emitted), 2)
        self.assertTrue(all(isinstance(event, ModelProviderAttemptFailed) for event in emitted))

    async def test_aggregate_failure_detail_is_bounded(self) -> None:
        providers = tuple(
            ScriptedModelProvider(
                f"provider-{index}",
                ((ProviderError("x" * 1_000),),),
            )
            for index in range(5)
        )
        router = FailoverModelProvider(tuple(_candidate(provider) for provider in providers))

        with self.assertRaises(ProviderError) as raised:
            async for _ in router.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
            ):
                pass

        self.assertLessEqual(len(str(raised.exception)), 2_060)
        self.assertTrue(str(raised.exception).endswith("..."))


if __name__ == "__main__":
    unittest.main()
