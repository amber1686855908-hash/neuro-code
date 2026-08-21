from __future__ import annotations

import unittest
from collections.abc import AsyncIterator, Sequence
from unittest.mock import AsyncMock, patch

from neuro_code.application.ports.model import ModelCapabilitySet, ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelProviderAttemptFailed,
    ModelTextDelta,
)
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.infrastructure.providers.failure_policy import ProviderFailurePolicy
from neuro_code.infrastructure.providers.resilience import ResilientModelProvider
from neuro_code.shared.errors import (
    ConfigurationError,
    ProviderError,
    ProviderFailure,
    ProviderFailureKind,
    ProviderFailurePhase,
)


class _FailingProvider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = None
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self, error: BaseException, *, partial: bool = False) -> None:
        self.error = error
        self.partial = partial
        self.calls = 0

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        self.calls += 1
        if self.partial:
            yield ModelTextDelta("partial")
        raise self.error
        yield ModelCompleted("stop")


class _SuccessProvider:
    provider_name = "fallback"
    model_name = "fallback-model"
    context_affinity = None
    capabilities = ModelCapabilitySet.all_unknown()

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        yield ModelTextDelta("ok")
        yield ModelCompleted("stop")


def _context() -> ModelContext:
    return ModelContext((Message(Role.USER, "hello"),))


class ProviderFailureFactTests(unittest.TestCase):
    def test_http_statuses_and_structured_details_are_typed(self) -> None:
        cases = (
            (401, "denied", ProviderFailureKind.AUTHENTICATION),
            (403, "denied", ProviderFailureKind.AUTHORIZATION),
            (404, "missing", ProviderFailureKind.MODEL_NOT_FOUND),
            (408, "slow", ProviderFailureKind.TIMEOUT),
            (409, "conflict", ProviderFailureKind.INVALID_REQUEST),
            (413, "payload too large", ProviderFailureKind.CONTEXT_OVERFLOW),
            (422, "invalid", ProviderFailureKind.INVALID_REQUEST),
            (429, "busy", ProviderFailureKind.RATE_LIMIT),
            (500, "upstream", ProviderFailureKind.SERVER),
            (
                400,
                '{"error": {"code": "context_length_exceeded"}}',
                ProviderFailureKind.CONTEXT_OVERFLOW,
            ),
        )
        for status, detail, expected in cases:
            with self.subTest(status=status):
                failure = ProviderError.from_http(status, detail).failure
                self.assertEqual(failure.kind, expected)
                self.assertEqual(failure.status_code, status)

    def test_retry_after_is_bounded_and_invalid_values_are_ignored(self) -> None:
        bounded = ProviderError.from_http(
            429,
            "api_key=secret-key; busy",
            headers={"Retry-After": "999999"},
            redaction_values=("secret-key",),
        )
        self.assertEqual(bounded.failure.retry_after_seconds, 3_600.0)
        self.assertNotIn("secret-key", bounded.failure.detail)
        self.assertIsNone(
            ProviderError.from_http(
                429, "busy", headers={"Retry-After": "-1"}
            ).failure.retry_after_seconds
        )
        self.assertIsNone(
            ProviderError.from_http(
                429, "busy", headers={"Retry-After": "never"}
            ).failure.retry_after_seconds
        )

    def test_facts_are_bounded_redacted_and_do_not_contain_policy(self) -> None:
        failure = ProviderFailure(
            ProviderFailureKind.UNKNOWN,
            "secret=" + "x" * 2_000,
            provider="provider",
            model="model",
            phase=ProviderFailurePhase.STREAM,
        )
        self.assertLessEqual(len(failure.detail), 1_000)
        self.assertNotIn("retryable", vars(failure) if hasattr(failure, "__dict__") else {})
        self.assertFalse(hasattr(failure, "failover_allowed"))
        self.assertEqual(failure.phase, ProviderFailurePhase.STREAM)


class ProviderFailurePolicyTests(unittest.TestCase):
    def test_every_kind_has_independent_explicit_decisions(self) -> None:
        expected = {
            ProviderFailureKind.AUTHENTICATION: (False, False, True),
            ProviderFailureKind.AUTHORIZATION: (False, False, True),
            ProviderFailureKind.RATE_LIMIT: (True, False, True),
            ProviderFailureKind.INVALID_REQUEST: (False, False, False),
            ProviderFailureKind.MODEL_NOT_FOUND: (False, False, True),
            ProviderFailureKind.CONTEXT_OVERFLOW: (False, False, True),
            ProviderFailureKind.SERVER: (True, True, True),
            ProviderFailureKind.TIMEOUT: (True, True, True),
            ProviderFailureKind.NETWORK: (True, True, True),
            ProviderFailureKind.PROTOCOL: (False, False, True),
            ProviderFailureKind.UNKNOWN: (False, True, True),
        }
        for kind, values in expected.items():
            with self.subTest(kind=kind):
                decision = ProviderFailurePolicy.decide(ProviderFailure(kind, "fixture"))
                self.assertEqual(
                    (decision.retry, decision.counts_toward_circuit, decision.failover),
                    values,
                )

    def test_output_observed_disables_retry_and_failover_for_every_kind(self) -> None:
        for kind in ProviderFailureKind:
            with self.subTest(kind=kind):
                decision = ProviderFailurePolicy.decide(
                    ProviderFailure(kind, "fixture"), output_observed=True
                )
                self.assertEqual(
                    (decision.retry, decision.counts_toward_circuit, decision.failover),
                    (False, False, False),
                )

    def test_configuration_stays_outside_provider_taxonomy(self) -> None:
        decision = ProviderFailurePolicy.decide_error(ConfigurationError("missing"))
        self.assertEqual(
            (decision.retry, decision.counts_toward_circuit, decision.failover),
            (False, False, True),
        )


class ProviderFailureRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_permanent_failure_does_not_open_transient_circuit(self) -> None:
        provider = _FailingProvider(
            ProviderError.classified(ProviderFailureKind.INVALID_REQUEST, "bad request")
        )
        resilient = ResilientModelProvider(
            provider,
            max_attempts=1,
            failure_threshold=1,
            backoff_seconds=0,
        )
        for _ in range(3):
            with self.assertRaises(ProviderError):
                await anext(resilient.stream(_context(), ()))
        self.assertEqual(provider.calls, 3)
        self.assertFalse(resilient.health.circuit_open)
        self.assertEqual(resilient.health.consecutive_failures, 0)
        self.assertEqual(resilient.health.last_failure_kind, "invalid_request")

    async def test_server_failure_opens_circuit_and_timeout_is_typed(self) -> None:
        provider = _FailingProvider(
            ProviderError.classified(ProviderFailureKind.SERVER, "upstream")
        )
        resilient = ResilientModelProvider(
            provider,
            max_attempts=1,
            failure_threshold=2,
            cooldown_seconds=10,
            backoff_seconds=0,
        )
        for _ in range(2):
            with self.assertRaises(ProviderError):
                await anext(resilient.stream(_context(), ()))
        self.assertTrue(resilient.health.circuit_open)
        self.assertEqual(resilient.health.last_failure_kind, "server")

    async def test_rate_limit_retries_without_poisoning_circuit_and_honors_bound(self) -> None:
        error = ProviderError.from_http(429, "busy", headers={"Retry-After": "999999"})

        class RetryOnce(_FailingProvider):
            async def stream(
                self,
                context: ModelContext,
                tools: Sequence[ToolDefinition],
                *,
                tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
            ) -> AsyncIterator[ModelEvent]:
                del context, tools, tool_policy
                self.calls += 1
                if self.calls == 1:
                    raise self.error
                yield ModelTextDelta("ok")

        provider = RetryOnce(error)
        resilient = ResilientModelProvider(provider, max_attempts=2, backoff_seconds=0)
        sleep = AsyncMock()
        with patch("neuro_code.infrastructure.providers.resilience.asyncio.sleep", sleep):
            events = [event async for event in resilient.stream(_context(), ())]
        self.assertEqual(provider.calls, 2)
        self.assertEqual(
            [event.text for event in events if isinstance(event, ModelTextDelta)], ["ok"]
        )
        sleep.assert_awaited_once_with(10.0)
        self.assertFalse(resilient.health.circuit_open)

    async def test_partial_output_never_retries_or_opens_circuit(self) -> None:
        provider = _FailingProvider(
            ProviderError.classified(ProviderFailureKind.NETWORK, "interrupted"),
            partial=True,
        )
        resilient = ResilientModelProvider(
            provider,
            max_attempts=3,
            failure_threshold=1,
            backoff_seconds=0,
        )
        with self.assertRaises(ProviderError):
            _ = [event async for event in resilient.stream(_context(), ())]
        self.assertEqual(provider.calls, 1)
        self.assertFalse(resilient.health.circuit_open)

    async def test_pre_output_failover_uses_typed_event_and_invalid_request_stops(self) -> None:
        primary = _FailingProvider(ProviderError.classified(ProviderFailureKind.NETWORK, "offline"))
        router = FailoverModelProvider(
            (
                ProviderCandidate("primary", "primary-model", None, lambda: primary),
                ProviderCandidate("fallback", "fallback-model", None, _SuccessProvider),
            )
        )
        events = [event async for event in router.stream(_context(), ())]
        attempt = next(event for event in events if isinstance(event, ModelProviderAttemptFailed))
        self.assertEqual(attempt.failure_kind, "network")

        invalid = _FailingProvider(
            ProviderError.classified(ProviderFailureKind.INVALID_REQUEST, "bad request")
        )
        invalid_router = FailoverModelProvider(
            (
                ProviderCandidate("primary", "primary-model", None, lambda: invalid),
                ProviderCandidate("fallback", "fallback-model", None, _SuccessProvider),
            )
        )
        with self.assertRaises(ProviderError):
            _ = [event async for event in invalid_router.stream(_context(), ())]
        self.assertEqual(invalid.calls, 1)


if __name__ == "__main__":
    unittest.main()
