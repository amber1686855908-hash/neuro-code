from __future__ import annotations

import json
import unittest
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
from neuro_code.infrastructure.providers.failure_conformance import (
    ProviderFailureProtocol,
    classify_provider_failure,
)
from neuro_code.infrastructure.providers.failure_policy import ProviderFailurePolicy
from neuro_code.infrastructure.providers.gemini import GeminiProvider
from neuro_code.infrastructure.providers.gemini_interactions import GeminiInteractionsProvider
from neuro_code.infrastructure.providers.openai_compatible import OpenAICompatibleProvider
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.shared.errors import (
    ProviderError,
    ProviderFailure,
    ProviderFailureKind,
    ProviderFailureOrigin,
)


def _context() -> ModelContext:
    return ModelContext((Message(Role.USER, "fixture request"),))


def _json_response(status: int, payload: Mapping[str, object]) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers={"content-type": "application/json"},
    )


class ProviderStructuredEnvelopeTests(unittest.TestCase):
    def test_official_envelope_fields_map_to_facts_without_reading_messages(self) -> None:
        fixtures = (
            (
                "openai auth",
                ProviderFailureProtocol.OPENAI_RESPONSES,
                '{"error":{"type":"invalid_request_error","code":"invalid_api_key","message":"ignored"}}',
                ProviderFailureKind.AUTHENTICATION,
            ),
            (
                "openai permission",
                ProviderFailureProtocol.OPENAI_RESPONSES,
                '{"error":{"type":"permission_error","code":"forbidden","message":"ignored"}}',
                ProviderFailureKind.AUTHORIZATION,
            ),
            (
                "openai rate",
                ProviderFailureProtocol.OPENAI_RESPONSES,
                '{"error":{"code":"rate_limit_exceeded","message":"ignored"}}',
                ProviderFailureKind.RATE_LIMIT,
            ),
            (
                "openai quota",
                ProviderFailureProtocol.OPENAI_RESPONSES,
                '{"error":{"code":"credit_balance_exhausted","message":"ignored"}}',
                ProviderFailureKind.AUTHORIZATION,
            ),
            (
                "openai model",
                ProviderFailureProtocol.OPENAI_COMPATIBLE,
                '{"error":{"code":"model_not_found","message":"ignored"}}',
                ProviderFailureKind.MODEL_NOT_FOUND,
            ),
            (
                "openai context",
                ProviderFailureProtocol.OPENAI_COMPATIBLE,
                '{"error":{"code":"context_length_exceeded","message":"ignored"}}',
                ProviderFailureKind.CONTEXT_OVERFLOW,
            ),
            (
                "openai server",
                ProviderFailureProtocol.OPENAI_RESPONSES,
                '{"type":"response.failed","response":{"error":{"code":"server_error","message":"ignored"}}}',
                ProviderFailureKind.SERVER,
            ),
            (
                "anthropic auth",
                ProviderFailureProtocol.ANTHROPIC,
                '{"type":"error","error":{"type":"authentication_error","message":"ignored"}}',
                ProviderFailureKind.AUTHENTICATION,
            ),
            (
                "anthropic billing",
                ProviderFailureProtocol.ANTHROPIC,
                '{"type":"error","error":{"type":"billing_error","message":"ignored"}}',
                ProviderFailureKind.AUTHORIZATION,
            ),
            (
                "anthropic ambiguous rate",
                ProviderFailureProtocol.ANTHROPIC,
                '{"type":"error","error":{"type":"rate_limit_error","message":"ignored"}}',
                ProviderFailureKind.UNKNOWN,
            ),
            (
                "anthropic request too large",
                ProviderFailureProtocol.ANTHROPIC,
                '{"type":"error","error":{"type":"request_too_large","message":"ignored"}}',
                ProviderFailureKind.INVALID_REQUEST,
            ),
            (
                "gemini permission",
                ProviderFailureProtocol.GEMINI_GENERATE_CONTENT,
                '{"error":{"status":"PERMISSION_DENIED","message":"ignored"}}',
                ProviderFailureKind.AUTHORIZATION,
            ),
            (
                "gemini quota or rate ambiguous",
                ProviderFailureProtocol.GEMINI_GENERATE_CONTENT,
                '{"error":{"status":"RESOURCE_EXHAUSTED","message":"ignored"}}',
                ProviderFailureKind.UNKNOWN,
            ),
            (
                "gemini server",
                ProviderFailureProtocol.GEMINI_GENERATE_CONTENT,
                '{"error":{"status":"INTERNAL","message":"ignored"}}',
                ProviderFailureKind.SERVER,
            ),
            (
                "interactions model",
                ProviderFailureProtocol.GEMINI_INTERACTIONS,
                '{"error":{"code":"model_not_found","message":"ignored"}}',
                ProviderFailureKind.MODEL_NOT_FOUND,
            ),
            (
                "interactions not found",
                ProviderFailureProtocol.GEMINI_INTERACTIONS,
                '{"error":{"code":"not_found","message":"ignored"}}',
                ProviderFailureKind.INVALID_REQUEST,
            ),
            (
                "interactions rate",
                ProviderFailureProtocol.GEMINI_INTERACTIONS,
                '{"error":{"code":"rate_limit_exceeded","message":"ignored"}}',
                ProviderFailureKind.RATE_LIMIT,
            ),
            (
                "interactions quota",
                ProviderFailureProtocol.GEMINI_INTERACTIONS,
                '{"error":{"code":"quota_exceeded","message":"ignored"}}',
                ProviderFailureKind.AUTHORIZATION,
            ),
            (
                "interactions protocol unknown",
                ProviderFailureProtocol.GEMINI_INTERACTIONS,
                '{"error":{"code":"future_provider_code","message":"server unavailable"}}',
                None,
            ),
        )
        for name, protocol, detail, expected in fixtures:
            with self.subTest(name=name):
                self.assertEqual(classify_provider_failure(protocol, detail), expected)

    def test_unknown_and_protocol_policy_is_safe(self) -> None:
        structured_unknown = ProviderError.from_http(
            429,
            '{"error":{"code":"future_rate_or_quota_code"}}',
        ).failure
        self.assertEqual(structured_unknown.kind, ProviderFailureKind.UNKNOWN)
        self.assertEqual(structured_unknown.origin, ProviderFailureOrigin.PROVIDER)
        unknown = ProviderFailure(
            ProviderFailureKind.UNKNOWN,
            "future provider envelope",
            origin=ProviderFailureOrigin.PROVIDER,
        )
        unknown_decision = ProviderFailurePolicy.decide(unknown)
        self.assertEqual(
            (
                unknown_decision.retry,
                unknown_decision.counts_toward_circuit,
                unknown_decision.failover,
            ),
            (False, False, True),
        )
        protocol = ProviderError.protocol("malformed provider envelope").failure
        protocol_decision = ProviderFailurePolicy.decide(protocol)
        self.assertEqual(
            (
                protocol_decision.retry,
                protocol_decision.counts_toward_circuit,
                protocol_decision.failover,
            ),
            (False, False, True),
        )


class ProviderOfflineFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_http_fixture(
        self,
        factory: Callable[[httpx.AsyncBaseTransport], Any],
        *,
        status: int,
        payload: Mapping[str, object],
        expected_kind: ProviderFailureKind,
        expected_policy: tuple[bool, bool, bool],
    ) -> None:
        transport = httpx.MockTransport(lambda request: _json_response(status, payload))
        provider = factory(transport)
        with self.assertRaises(ProviderError) as raised:
            _ = [
                event
                async for event in provider.stream(
                    _context(), (), tool_policy=ModelToolPolicy.ALLOWED
                )
            ]
        failure = raised.exception.failure
        self.assertEqual(failure.kind, expected_kind)
        self.assertEqual(failure.origin, ProviderFailureOrigin.PROVIDER)
        decision = ProviderFailurePolicy.decide(failure)
        self.assertEqual(
            (decision.retry, decision.counts_toward_circuit, decision.failover),
            expected_policy,
        )

    async def test_each_adapter_uses_its_protocol_envelope_classifier_offline(self) -> None:
        cases = (
            (
                "openai-compatible auth",
                lambda transport: OpenAICompatibleProvider(
                    model="fixture-model",
                    base_url="https://provider.invalid/v1",
                    api_key="fixture-secret",
                    transport=transport,
                ),
                401,
                {
                    "error": {
                        "message": "Incorrect API key provided",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
                ProviderFailureKind.AUTHENTICATION,
                (False, False, True),
            ),
            (
                "openai responses quota",
                lambda transport: OpenAIResponsesProvider(
                    model="fixture-model",
                    base_url="https://provider.invalid/v1",
                    api_key="fixture-secret",
                    transport=transport,
                ),
                429,
                {
                    "error": {
                        "code": "credit_balance_exhausted",
                        "message": "Credit balance exhausted",
                    }
                },
                ProviderFailureKind.AUTHORIZATION,
                (False, False, True),
            ),
            (
                "anthropic not found resource",
                lambda transport: AnthropicProvider(
                    model="fixture-model",
                    base_url="https://provider.invalid",
                    api_key="fixture-secret",
                    transport=transport,
                ),
                404,
                {
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": "Resource not found",
                    },
                },
                ProviderFailureKind.INVALID_REQUEST,
                (False, False, False),
            ),
            (
                "gemini generate quota ambiguous",
                lambda transport: GeminiProvider(
                    model="fixture-model",
                    base_url="https://provider.invalid/v1beta",
                    api_key="fixture-secret",
                    transport=transport,
                ),
                429,
                {
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Resource exhausted",
                    }
                },
                ProviderFailureKind.UNKNOWN,
                (False, False, True),
            ),
            (
                "gemini interactions model",
                lambda transport: GeminiInteractionsProvider(
                    model="gemini-3.6-flash",
                    base_url="https://provider.invalid/v1beta",
                    api_key="fixture-secret",
                    provider_name="gemini-profile",
                    service_id="fixture-service",
                    transport=transport,
                ),
                404,
                {
                    "error": {
                        "code": "model_not_found",
                        "message": "Model not found",
                    }
                },
                ProviderFailureKind.MODEL_NOT_FOUND,
                (False, False, True),
            ),
        )
        for name, factory, status, payload, expected_kind, expected_policy in cases:
            with self.subTest(name=name):
                await self._assert_http_fixture(
                    factory,
                    status=status,
                    payload=payload,
                    expected_kind=expected_kind,
                    expected_policy=expected_policy,
                )

    async def test_responses_stream_failure_classifies_response_failed_event(self) -> None:
        event = {
            "type": "response.failed",
            "response": {"error": {"code": "server_error", "message": "model failed to generate"}},
        }
        provider = OpenAIResponsesProvider(
            model="fixture-model",
            base_url="https://provider.invalid/v1",
            api_key="fixture-secret",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
                    headers={"content-type": "text/event-stream"},
                )
            ),
        )
        with self.assertRaises(ProviderError) as raised:
            _ = [event async for event in provider.stream(_context(), ())]
        self.assertEqual(raised.exception.failure.kind, ProviderFailureKind.SERVER)
        self.assertEqual(raised.exception.failure.origin, ProviderFailureOrigin.PROVIDER)


if __name__ == "__main__":
    unittest.main()
