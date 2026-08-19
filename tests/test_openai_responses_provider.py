from __future__ import annotations

import json
import unittest

import httpx

from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelCompleted, ModelTextDelta
from neuro_code.domain.conversation.messages import (
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.shared.errors import ConfigurationError


def _reasoning() -> PreservedContextItem:
    return PreservedContextItem(
        ContextItemKind.REASONING,
        {
            "type": "reasoning",
            "id": "reasoning-1",
            "summary": [{"type": "summary_text", "text": "summary"}],
            "encrypted_content": "opaque-provider-state",
        },
    )


class OpenAIResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_standard_dialect_uses_the_portable_responses_subset(self) -> None:
        provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="https://gateway.invalid/v1",
            api_key="fixture",
            provider_name="gateway",
        )

        body = provider._request_body(
            ModelContext((Message(Role.USER, "hello"),)),
            (),
        )

        self.assertEqual(provider.provider_name, "gateway")
        self.assertIsNone(provider.context_affinity)
        self.assertNotIn("reasoning", body)
        self.assertNotIn("include", body)
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])

    def test_standard_hosted_search_requires_the_explicit_builtin_tool(self) -> None:
        provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="https://api.openai.com/v1",
            api_key="fixture",
            provider_name="openai",
            builtin_tools=("web_search",),
            builtin_tool_options={
                "web_search": {"filters": {"allowed_domains": ["docs.openai.com"]}}
            },
            tool_choice="required",
        )

        body = provider._request_body(
            ModelContext((Message(Role.USER, "current docs"),)),
            (),
        )

        self.assertEqual(
            body["tools"],
            [
                {
                    "type": "web_search",
                    "filters": {"allowed_domains": ["docs.openai.com"]},
                }
            ],
        )
        self.assertEqual(body["include"], ["web_search_call.action.sources"])
        self.assertEqual(body["tool_choice"], "required")
        inline_provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="https://api.openai.com/v1",
            api_key="fixture",
            provider_name="openai-inline",
            builtin_tools=("web_search",),
        )
        inline_body = inline_provider._request_body(
            ModelContext((Message(Role.USER, "current docs"),)),
            (),
        )
        self.assertNotIn("tool_choice", inline_body)
        with self.assertRaisesRegex(ConfigurationError, "unsupported OpenAI Responses"):
            OpenAIResponsesProvider(
                model="response-model",
                base_url="https://api.openai.com/v1",
                api_key="fixture",
                builtin_tools=("x_search",),
            )

    async def test_hosted_search_keeps_structured_sources_visible_in_assistant_text(self) -> None:
        body = "\n\n".join(
            (
                "data: "
                + json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "citations": ["https://docs.x.ai/developers/tools/citations"],
                            "output": [
                                {
                                    "type": "web_search_call",
                                    "id": "search-1",
                                    "status": "completed",
                                    "action": {
                                        "sources": [
                                            {
                                                "title": "OpenAI guide",
                                                "url": "https://platform.openai.com/docs/quickstart",
                                            }
                                        ]
                                    },
                                },
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": "The guide is authoritative.",
                                            "annotations": [
                                                {
                                                    "type": "url_citation",
                                                    "url_citation": {
                                                        "title": "OpenAI guide",
                                                        "url": "https://platform.openai.com/docs/quickstart",
                                                        "start_index": 4,
                                                        "end_index": 9,
                                                    },
                                                }
                                            ],
                                        }
                                    ],
                                },
                            ],
                        },
                    }
                ),
                "data: [DONE]",
                "",
            )
        )
        provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="http://127.0.0.1:15721/provider/v1",
            api_key="fixture",
            provider_name="openai-search",
            builtin_tools=("web_search",),
            tool_choice="required",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
        )

        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "official docs"),)),
                (),
            )
        ]

        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertIn("Sources:", completed.response_text)
        self.assertIn("https://platform.openai.com/docs/quickstart", completed.response_text)
        self.assertIn("https://docs.x.ai/developers/tools/citations", completed.response_text)
        self.assertTrue(
            any(
                isinstance(event, ModelTextDelta)
                and "https://platform.openai.com/docs/quickstart" in event.text
                for event in events
            )
        )

    def test_opaque_context_requires_an_exact_profile_affinity(self) -> None:
        provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="https://gateway.invalid/v1",
            api_key="fixture",
            provider_name="gateway",
            context_affinity="profile-v1:matching",
        )
        items = (Message(Role.USER, "hello"), _reasoning())

        matching = provider._input_items(
            ModelContext(
                items,
                source_provider="gateway",
                source_model="response-model",
                source_context_affinity="profile-v1:matching",
            )
        )
        foreign = provider._input_items(
            ModelContext(
                items,
                source_provider="gateway",
                source_model="response-model",
                source_context_affinity="profile-v1:foreign",
            )
        )

        self.assertEqual([item["type"] for item in matching], ["message", "reasoning"])
        self.assertEqual([item["type"] for item in foreign], ["message"])
        self.assertNotIn("opaque-provider-state", json.dumps(foreign))

    async def test_affine_generic_responses_output_is_preserved(self) -> None:
        body = "\n\n".join(
            (
                "data: "
                + json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "reasoning",
                                    "id": "reasoning-2",
                                    "summary": [],
                                    "encrypted_content": "encrypted",
                                },
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": "done"}],
                                },
                            ],
                        },
                    }
                ),
                "data: [DONE]",
                "",
            )
        )
        provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="http://127.0.0.1:15721/provider/v1",
            api_key="PROXY_MANAGED",
            provider_name="cc-switch:stable",
            context_affinity="profile-v1:stable",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
        )

        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
            )
        ]

        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(len(completed.context_items), 1)
        self.assertEqual(
            completed.context_items[0].to_dict()["encrypted_content"],
            "encrypted",
        )

    async def test_disabled_tool_policy_omits_local_functions_without_sticky_state(self) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            body = "\n\n".join(
                (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "output": [
                                    {
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [{"type": "output_text", "text": "done"}],
                                    }
                                ],
                                "usage": {
                                    "input_tokens": 2,
                                    "output_tokens": 1,
                                    "input_tokens_details": {
                                        "cached_tokens": 1,
                                        "cache_write_tokens": 2,
                                        "cache_miss_tokens": 1,
                                    },
                                },
                            },
                        }
                    ),
                    "data: [DONE]",
                    "",
                )
            )
            return httpx.Response(200, text=body)

        provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="https://gateway.invalid/v1",
            api_key="fixture",
            transport=httpx.MockTransport(handler),
        )
        context = ModelContext((Message(Role.USER, "hello"),))
        tools = (ToolDefinition("read_file", "Read", {"type": "object"}),)

        first = [event async for event in provider.stream(context, tools)]
        disabled = [
            event
            async for event in provider.stream(
                context,
                tools,
                tool_policy=ModelToolPolicy.DISABLED,
            )
        ]
        second = [event async for event in provider.stream(context, tools)]

        self.assertIsInstance(first[-1], ModelCompleted)
        self.assertIsInstance(disabled[-1], ModelCompleted)
        self.assertIsInstance(second[-1], ModelCompleted)
        completed = first[-1]
        assert isinstance(completed, ModelCompleted)
        assert completed.usage is not None
        self.assertEqual(completed.usage.cache_read_tokens, 1)
        self.assertEqual(completed.usage.cache_write_tokens, 2)
        self.assertEqual(completed.usage.cache_miss_tokens, 1)
        self.assertEqual(captured[0]["tools"], captured[2]["tools"])
        self.assertNotIn("tools", captured[1])
        self.assertNotIn("tool_choice", captured[1])
        self.assertNotIn("parallel_tool_calls", captured[1])
        self.assertEqual(tools[0].name, "read_file")


if __name__ == "__main__":
    unittest.main()
