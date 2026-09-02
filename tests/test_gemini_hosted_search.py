from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from neuro_code.application.ports.configuration import AppConfig, ProviderProfile
from neuro_code.application.ports.model import ModelCapability
from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_search import (
    HostedWebSearchEvent,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.infrastructure.providers.gemini_interactions import (
    GeminiInteractionsProvider,
)
from neuro_code.infrastructure.providers.hosted_web_search import (
    GeminiHostedWebSearchBackend,
    RoutedWebSearchBackendResolver,
    _has_completed_gemini_google_search_execution,
    _provider_tool_options,
    extract_gemini_web_search_evidence,
)


def _sse(*events: Mapping[str, object]) -> str:
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events) + "\n\n"


def _profile(*, builtin_tools: tuple[str, ...] = ("google_search",)) -> ProviderProfile:
    return ProviderProfile(
        name="gemini-search",
        protocol="gemini-interactions",
        service_id="google-ai-studio",
        model="gemini-3.6-flash",
        base_url="https://generativelanguage.googleapis.com/v1",
        api_key_env="GEMINI_KEY",
        builtin_tools=builtin_tools,
        proxy_mode="direct",
    )


class GeminiHostedSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_requires_google_search_lifecycle_and_extracts_citations(self) -> None:
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {"type": "google_search_call", "id": "search-1"},
                    },
                    {"event_type": "step.stop", "index": 0},
                    {
                        "event_type": "step.start",
                        "index": 1,
                        "step": {
                            "type": "google_search_result",
                            "call_id": "search-1",
                            "result": [{"search_suggestions": "<html>not evidence</html>"}],
                        },
                    },
                    {"event_type": "step.stop", "index": 1},
                    {
                        "event_type": "step.start",
                        "index": 2,
                        "step": {"type": "model_output"},
                    },
                    {
                        "event_type": "step.delta",
                        "index": 2,
                        "delta": {
                            "type": "text",
                            "text": "Primary evidence.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://docs.example.com/guide",
                                    "title": "Official guide",
                                    "start_index": 0,
                                    "end_index": 17,
                                }
                            ],
                        },
                    },
                    {"event_type": "step.stop", "index": 2},
                    {
                        "event_type": "interaction.completed",
                        "interaction": {
                            "id": "interaction-1",
                            "status": "completed",
                            "usage": {"total_input_tokens": 11, "total_output_tokens": 7},
                        },
                    },
                ),
                headers={"content-type": "text/event-stream"},
            )

        profile = _profile()
        lifecycle: list[HostedWebSearchEvent] = []

        def factory(observer: Any, request: WebSearchRequest) -> GeminiInteractionsProvider:
            del request
            return GeminiInteractionsProvider(
                model=profile.model,
                base_url=profile.base_url,
                api_key="fixture-key",
                provider_name=profile.name,
                service_id=profile.service_id,
                context_affinity=profile.context_affinity,
                builtin_tools=("google_search",),
                tool_choice={"allowed_tools": {"mode": "any", "tools": ["google_search"]}},
                response_observer=observer,
                transport=httpx.MockTransport(handler),
                capabilities=profile.upstream_capabilities(),
            )

        backend = GeminiHostedWebSearchBackend(profile, factory)

        async def sink(event: HostedWebSearchEvent) -> None:
            lifecycle.append(event)

        result = await backend.search(WebSearchRequest("official guide"), event_sink=sink)

        self.assertEqual(result.evidence_text, "Primary evidence.")
        self.assertEqual(
            tuple(source.url for source in result.sources), ("https://docs.example.com/guide",)
        )
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(
            [(event.name, event.completed) for event in lifecycle],
            [("google_search", False), ("google_search", True)],
        )
        self.assertEqual(
            captured[0]["generation_config"]["tool_choice"]["allowed_tools"]["tools"],
            ["google_search"],
        )
        self.assertNotIn("search_suggestions", result.evidence_text)

    async def test_backend_rejects_a_plain_answer_without_search_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "interaction.completed",
                        "interaction": {
                            "id": "interaction-2",
                            "status": "completed",
                            "steps": [
                                {
                                    "type": "model_output",
                                    "content": [{"type": "text", "text": "Prior knowledge."}],
                                }
                            ],
                        },
                    }
                ),
            )

        profile = _profile()

        def factory(observer: Any, request: WebSearchRequest) -> GeminiInteractionsProvider:
            del request
            return GeminiInteractionsProvider(
                model=profile.model,
                base_url=profile.base_url,
                api_key="fixture-key",
                provider_name=profile.name,
                service_id=profile.service_id,
                builtin_tools=("google_search",),
                response_observer=observer,
                transport=httpx.MockTransport(handler),
                capabilities=profile.upstream_capabilities(),
            )

        backend = GeminiHostedWebSearchBackend(profile, factory)
        with self.assertRaises(WebSearchError) as raised:
            await backend.search(WebSearchRequest("query"))
        self.assertIs(raised.exception.code, WebSearchErrorCode.SEARCH_PROVIDER_DID_NOT_SEARCH)


class GeminiHostedSearchContractTests(unittest.TestCase):
    def test_extractor_uses_only_model_output_url_citations(self) -> None:
        response = {
            "steps": [
                {
                    "type": "google_search_result",
                    "result": [{"search_suggestions": "<html>should not surface</html>"}],
                },
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": "Answer from docs.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://docs.example.com/a",
                                    "title": "Docs A",
                                    "start_index": 0,
                                    "end_index": 16,
                                }
                            ],
                        }
                    ],
                },
            ]
        }
        text, sources, citations, truncated = extract_gemini_web_search_evidence(
            response,
            provider="gemini-search",
            request=WebSearchRequest("query"),
        )
        self.assertEqual(text, "Answer from docs.")
        self.assertEqual(tuple(source.url for source in sources), ("https://docs.example.com/a",))
        self.assertEqual(citations[0].cited_text, "Answer from docs")
        self.assertFalse(truncated)

    def test_google_search_filters_are_not_faked_into_the_wire_request(self) -> None:
        with self.assertRaises(WebSearchError) as raised:
            _provider_tool_options(
                _profile(),
                WebSearchRequest("query", allowed_domains=("docs.example.com",)),
            )
        self.assertIs(raised.exception.code, WebSearchErrorCode.SEARCH_UNSUPPORTED)
        self.assertTrue(
            _has_completed_gemini_google_search_execution(
                {
                    "steps": [
                        {"type": "google_search_call", "id": "call-1"},
                        {
                            "type": "google_search_result",
                            "call_id": "call-1",
                            "result": [{"search_suggestions": "html"}],
                        },
                    ]
                }
            )
        )
        self.assertFalse(
            _has_completed_gemini_google_search_execution(
                {
                    "steps": [
                        {"type": "google_search_call", "id": "call-1"},
                        {
                            "type": "google_search_result",
                            "call_id": "call-1",
                            "status": "error",
                        },
                    ]
                }
            )
        )

    def test_resolver_selects_gemini_interactions_only_for_explicit_search_capability(self) -> None:
        profile = _profile(builtin_tools=("google_search", "url_context"))
        config = AppConfig(
            cwd=Path("/workspace"),
            state_dir=Path("/state"),
            providers={profile.name: profile},
            default_provider=profile.name,
            selected_provider=profile.name,
        )
        backend = RoutedWebSearchBackendResolver(config).resolve(
            ModelRoute(RuntimeRole.WEB_SEARCH, profile.name, profile.model)
        )
        self.assertIsInstance(backend, GeminiHostedWebSearchBackend)
        assert isinstance(backend, GeminiHostedWebSearchBackend)
        self.assertEqual(backend._profile.builtin_tools, ("google_search", "url_context"))
        self.assertTrue(backend.capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH))

        with patch.dict("os.environ", {"GEMINI_KEY": "fixture-key"}, clear=True):
            provider = backend._provider_factory(lambda response: None, WebSearchRequest("query"))
        assert isinstance(provider, GeminiInteractionsProvider)
        request_body = provider._request_body(
            ModelContext((Message(Role.USER, "query"),)),
            (),
        )
        self.assertEqual(
            request_body["generation_config"]["tool_choice"],
            {
                "allowed_tools": {
                    "mode": "any",
                    "tools": ["google_search", "url_context"],
                }
            },
        )

        unknown = ProviderProfile(
            name="gemini-unknown",
            protocol="gemini-interactions",
            service_id="google-ai-studio",
            model="gemini-future-unknown",
            base_url=profile.base_url,
            api_key_env="GEMINI_KEY",
            builtin_tools=("google_search",),
            proxy_mode="direct",
        )
        unknown_config = AppConfig(
            cwd=Path("/workspace"),
            state_dir=Path("/state"),
            providers={unknown.name: unknown},
            default_provider=unknown.name,
            selected_provider=unknown.name,
        )
        self.assertIsNone(
            RoutedWebSearchBackendResolver(unknown_config).resolve(
                ModelRoute(RuntimeRole.WEB_SEARCH, unknown.name, unknown.model)
            )
        )


if __name__ == "__main__":
    unittest.main()
