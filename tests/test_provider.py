from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import httpx

from neuro_code.config import AppConfig, ProviderProfile
from neuro_code.domain.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.model_context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.model_events import (
    ModelCompleted,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ConfigurationError, ProviderError
from neuro_code.providers import create_provider, create_routed_provider
from neuro_code.providers.anthropic import AnthropicProvider
from neuro_code.providers.failover import FailoverModelProvider
from neuro_code.providers.gemini import GeminiProvider
from neuro_code.providers.openai_compatible import OpenAICompatibleProvider, _ToolCallBuffer
from neuro_code.providers.openai_responses import OpenAIResponsesProvider


def _sse(*chunks: object) -> str:
    lines = [f"data: {json.dumps(chunk)}" for chunk in chunks]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


class OpenAICompatibleProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_official_import_affinity_is_independent_of_model_spelling(self) -> None:
        provider = OpenAICompatibleProvider(
            model="future-xai-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
        )
        context = ModelContext(
            (Message(Role.USER, "continue"),),
            source_provider=UPSTREAM_IMPORT_PROVIDER,
            source_model="legacy-xai-model",
        )

        self.assertTrue(provider._has_xai_import_affinity(context))

    def test_structured_user_images_use_native_content_blocks(self) -> None:
        provider = OpenAICompatibleProvider(
            model="fixture-model",
            base_url="https://provider.invalid/v1",
            api_key="fixture",
        )
        message = Message(
            Role.USER,
            content_parts=(
                ContentPart.from_text("before"),
                ContentPart.from_image("data:image/png;base64,aW1hZ2U="),
                ContentPart.from_image("https://example.com/screenshot.png"),
                ContentPart.from_text("after"),
            ),
        )

        self.assertEqual(
            provider._message_payload(message)["content"],
            [
                {"type": "text", "text": "before"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/screenshot.png"},
                },
                {"type": "text", "text": "after"},
            ],
        )

    def test_images_fall_back_for_invalid_input_and_non_user_roles(self) -> None:
        provider = OpenAICompatibleProvider(
            model="fixture-model",
            base_url="https://provider.invalid/v1",
            api_key="fixture",
        )
        invalid = Message(
            Role.USER,
            content_parts=(ContentPart.from_image("data:image/png;base64,not-base64"),),
        )
        assistant = Message(
            Role.ASSISTANT,
            content_parts=(ContentPart.from_image("data:image/png;base64,aW1hZ2U="),),
        )

        self.assertEqual(
            provider._message_payload(invalid)["content"],
            [{"type": "text", "text": IMAGE_MODEL_PLACEHOLDER}],
        )
        self.assertEqual(
            provider._message_payload(assistant)["content"],
            IMAGE_MODEL_PLACEHOLDER,
        )

    def test_reasoning_is_replayed_only_for_assistant_tool_calls(self) -> None:
        provider = OpenAICompatibleProvider(
            model="fixture-model",
            base_url="https://provider.invalid/v1",
            api_key="fixture",
        )
        tool_turn = Message(
            Role.ASSISTANT,
            tool_calls=(ToolCall("call-1", "read_file", {"path": "a.py"}),),
            reasoning_content="Need to inspect a.py.",
        )
        completed_turn = Message(
            Role.ASSISTANT,
            "Done.",
            reasoning_content="The tool result is sufficient.",
        )

        self.assertEqual(
            provider._message_payload(tool_turn)["reasoning_content"],
            "Need to inspect a.py.",
        )
        self.assertNotIn("reasoning_content", provider._message_payload(completed_turn))

    def test_affine_xai_import_replays_visible_ordered_context(self) -> None:
        provider = OpenAICompatibleProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
        )
        reasoning = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "reasoning-1",
                "summary": [{"type": "summary_text", "text": "summary fallback"}],
                "content": [{"type": "reasoning_text", "text": "full visible reasoning"}],
                "encrypted_content": "opaque-must-not-enter-chat-completions",
            },
        )
        backend_items = (
            PreservedContextItem(
                ContextItemKind.BACKEND_TOOL_CALL,
                {
                    "type": "backend_tool_call",
                    "kind": {
                        "tool_type": "web_search",
                        "id": "web-1",
                        "action": {"type": "search", "query": "provider context"},
                    },
                },
            ),
            PreservedContextItem(
                ContextItemKind.BACKEND_TOOL_CALL,
                {
                    "type": "backend_tool_call",
                    "kind": {
                        "tool_type": "x_search",
                        "id": "x-1",
                        "name": "x_keyword_search",
                        "input": '{"query":"fixture"}',
                    },
                },
            ),
            PreservedContextItem(
                ContextItemKind.BACKEND_TOOL_CALL,
                {
                    "type": "backend_tool_call",
                    "kind": {
                        "tool_type": "code_interpreter",
                        "id": "code-1",
                        "code": "x" * 101,
                    },
                },
            ),
        )
        context = ModelContext(
            (
                Message(Role.SYSTEM, "system"),
                Message(Role.USER, "question"),
                reasoning,
                *backend_items,
                Message(Role.ASSISTANT, "source answer"),
                Message(Role.USER, "continue"),
            ),
            source_provider=UPSTREAM_IMPORT_PROVIDER,
            source_model="xai-test-model",
        )

        payloads = provider._message_payloads(context)

        self.assertEqual(
            [payload["content"] for payload in payloads[2:5]],
            [
                "[backend web_search] search: provider context",
                '[backend x_search] x_keyword_search({"query":"fixture"})',
                f"[backend code_interpreter] {'x' * 100}...",
            ],
        )
        self.assertEqual(payloads[5]["reasoning_content"], "full visible reasoning")
        self.assertNotIn("opaque-must-not-enter-chat-completions", json.dumps(payloads))

    def test_non_affine_import_drops_opaque_items(self) -> None:
        preserved = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "reasoning-1",
                "summary": [{"type": "summary_text", "text": "must be filtered"}],
                "encrypted_content": "opaque-must-be-filtered",
            },
        )
        items = (
            Message(Role.USER, "question"),
            preserved,
            Message(Role.ASSISTANT, "answer"),
        )
        cases = (
            (
                "https://api.deepseek.com",
                "deepseek-v4-flash",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            (
                "https://api.x.ai.evil/v1",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            ("https://[broken", "xai-test-model", UPSTREAM_IMPORT_PROVIDER, "xai-test-model"),
            ("http://api.x.ai/v1", "xai-test-model", UPSTREAM_IMPORT_PROVIDER, "xai-test-model"),
            (
                "https://user@api.x.ai/v1",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            (
                "https://api.x.ai:8443/v1",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            (
                "https://api.x.ai:invalid/v1",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            (
                "https://api.x.ai/v1?proxy=1",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            (
                "https://api.x.ai/v1#proxy",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            ("https://api.x.ai/v1", "xai-test-model", "openai-compatible", "xai-test-model"),
        )
        for base_url, model, source_provider, source_model in cases:
            with self.subTest(
                base_url=base_url,
                model=model,
                source_provider=source_provider,
                source_model=source_model,
            ):
                provider = OpenAICompatibleProvider(
                    model=model,
                    base_url=base_url,
                    api_key="fixture",
                )
                context = ModelContext(
                    items,
                    source_provider=source_provider,
                    source_model=source_model,
                )

                payloads = provider._message_payloads(context)

                self.assertEqual(
                    payloads,
                    [
                        {"role": "user", "content": "question"},
                        {"role": "assistant", "content": "answer"},
                    ],
                )

    def test_preserved_context_summaries_are_validated_and_bounded(self) -> None:
        def backend(kind: dict[str, object]) -> PreservedContextItem:
            return PreservedContextItem(
                ContextItemKind.BACKEND_TOOL_CALL,
                {"type": "backend_tool_call", "kind": kind},
            )

        open_page = backend(
            {
                "tool_type": "web_search",
                "action": {"type": "open_page", "url": "https://example.invalid"},
            }
        )
        find = backend(
            {
                "tool_type": "web_search",
                "action": {
                    "type": "find_in_page",
                    "pattern": "needle",
                    "url": "https://example.invalid/page",
                },
            }
        )
        long_x_search = backend(
            {
                "tool_type": "x_search",
                "name": "x_keyword_search",
                "input": "x" * 2000,
            }
        )
        unknown = backend({"tool_type": "future_tool", "payload": "opaque"})
        malformed_reasoning = PreservedContextItem(
            ContextItemKind.REASONING,
            {"type": "reasoning", "content": [{"type": "wrong", "text": "hidden"}]},
        )
        summary_reasoning = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "visible summary"}],
            },
        )

        self.assertEqual(
            OpenAICompatibleProvider._backend_tool_summary(open_page),
            "[backend web_search] open: https://example.invalid",
        )
        self.assertEqual(
            OpenAICompatibleProvider._backend_tool_summary(find),
            '[backend web_search] find "needle" in https://example.invalid/page',
        )
        x_summary = OpenAICompatibleProvider._backend_tool_summary(long_x_search)
        assert x_summary is not None
        self.assertLess(len(x_summary), 1100)
        self.assertTrue(x_summary.endswith("...)"))
        self.assertIsNone(OpenAICompatibleProvider._backend_tool_summary(unknown))
        self.assertEqual(OpenAICompatibleProvider._reasoning_text(malformed_reasoning), "")
        self.assertEqual(
            OpenAICompatibleProvider._reasoning_text(summary_reasoning),
            "visible summary",
        )

    def test_affine_reasoning_does_not_duplicate_matching_tool_reasoning(self) -> None:
        provider = OpenAICompatibleProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
        )
        reasoning = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "reasoning-1",
                "summary": [{"type": "summary_text", "text": "same reasoning"}],
            },
        )
        assistant = Message(
            Role.ASSISTANT,
            tool_calls=(ToolCall("call-1", "read_file", {"path": "a.py"}),),
            reasoning_content="same reasoning",
        )
        context = ModelContext(
            (reasoning, assistant),
            source_provider=UPSTREAM_IMPORT_PROVIDER,
            source_model="xai-test-model",
        )

        payloads = provider._message_payloads(context)

        self.assertEqual(payloads[0]["reasoning_content"], "same reasoning")

    async def test_stream_normalizes_text_reasoning_tool_calls_and_usage(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            body = _sse(
                {"choices": []},
                {"choices": [None]},
                {"choices": [{"delta": None}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "hello ",
                                "reasoning_content": "checking",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-",
                                        "function": {"name": "read_", "arguments": '{"path":'},
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "delta": {
                                "content": "world",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "1",
                                        "function": {"name": "file", "arguments": '"a.py"}'},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 7},
                },
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        provider = OpenAICompatibleProvider(
            model="fixture-model",
            base_url="https://provider.invalid/v1/",
            api_key="secret-key",
            max_output_tokens=512,
            transport=httpx.MockTransport(handler),
        )
        messages = (
            Message(Role.USER, "hello"),
            Message(
                Role.ASSISTANT,
                tool_calls=(ToolCall("old", "old_tool", {"value": 1}),),
                reasoning_content="prior tool reasoning",
            ),
            Message(Role.TOOL, "done", tool_call_id="old"),
        )
        tools = (
            ToolDefinition(
                "read_file",
                "read",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        )

        events = [event async for event in provider.stream(ModelContext(tuple(messages)), tools)]

        self.assertEqual(provider.provider_name, "openai-compatible")
        self.assertEqual(provider.model_name, "fixture-model")
        self.assertEqual(
            [type(event) for event in events],
            [
                ModelTextDelta,
                ModelReasoningDelta,
                ModelTextDelta,
                ModelToolCall,
                ModelCompleted,
            ],
        )
        tool_event = events[3]
        assert isinstance(tool_event, ModelToolCall)
        self.assertEqual(tool_event.call, ToolCall("call-1", "read_file", {"path": "a.py"}))
        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(
            (completed.stop_reason, completed.input_tokens, completed.output_tokens),
            (
                "tool_calls",
                4,
                7,
            ),
        )
        self.assertEqual(captured["authorization"], "Bearer secret-key")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "fixture-model")
        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(body["messages"][1]["reasoning_content"], "prior tool reasoning")
        self.assertEqual(body["messages"][2]["tool_call_id"], "old")
        self.assertEqual(body["tools"][0]["function"]["name"], "read_file")

    async def test_http_error_is_bounded_and_does_not_echo_api_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized must-not-leak")

        provider = OpenAICompatibleProvider(
            model="model",
            base_url="https://provider.invalid/v1",
            api_key="must-not-leak",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ProviderError) as raised:
            [
                event
                async for event in provider.stream(ModelContext((Message(Role.USER, "hello"),)), ())
            ]
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertNotIn("must-not-leak", str(raised.exception))

    async def test_malformed_stream_json_is_rejected(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text="data: {broken}\n\n")
        )
        provider = OpenAICompatibleProvider(
            model="model",
            base_url="https://provider.invalid/v1",
            api_key="key",
            transport=transport,
        )
        with self.assertRaisesRegex(ProviderError, "malformed streaming JSON"):
            [
                event
                async for event in provider.stream(ModelContext((Message(Role.USER, "hello"),)), ())
            ]

    async def test_invalid_or_incomplete_tool_arguments_are_rejected(self) -> None:
        for function, expected in (
            ({"name": "tool", "arguments": "[1]"}, "JSON object"),
            ({"name": "tool", "arguments": "{"}, "invalid JSON"),
            ({"name": "", "arguments": "{}"}, "incomplete tool call"),
        ):
            chunk = {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "id", "function": function}]}}
                ]
            }
            transport = httpx.MockTransport(
                lambda request, value=chunk: httpx.Response(200, text=_sse(value))
            )
            provider = OpenAICompatibleProvider(
                model="model",
                base_url="https://provider.invalid/v1",
                api_key="key",
                transport=transport,
            )
            with self.assertRaisesRegex(ProviderError, expected):
                [
                    event
                    async for event in provider.stream(
                        ModelContext((Message(Role.USER, "hello"),)), ()
                    )
                ]

    async def test_transport_failure_becomes_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        provider = OpenAICompatibleProvider(
            model="model",
            base_url="https://provider.invalid/v1",
            api_key="key",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderError, "provider stream failed"):
            [
                event
                async for event in provider.stream(ModelContext((Message(Role.USER, "hello"),)), ())
            ]

    def test_tool_call_accumulator_ignores_invalid_fragments(self) -> None:
        buffers: dict[int, _ToolCallBuffer] = {}
        OpenAICompatibleProvider._accumulate_tool_calls(
            [None, {"index": "bad"}, {"index": 1, "id": 3}, {"index": 1, "function": None}],
            buffers,
        )
        self.assertIn(1, buffers)
        self.assertEqual(buffers[1], _ToolCallBuffer())

    def test_provider_factory_and_unknown_protocol(self) -> None:
        expected_types = {
            "openai-chat": OpenAICompatibleProvider,
            "openai-responses": OpenAIResponsesProvider,
            "anthropic-messages": AnthropicProvider,
            "gemini-generate-content": GeminiProvider,
        }
        with mock.patch.dict("os.environ", {"TOKEN": "value"}, clear=True):
            for protocol, expected_type in expected_types.items():
                with self.subTest(protocol=protocol):
                    provider = create_provider(
                        ProviderProfile(
                            name=f"fixture-{protocol}",
                            protocol=protocol,
                            model="m",
                            base_url="https://example.invalid/v1",
                            api_key_env="TOKEN",
                            proxy_mode="direct",
                        )
                    )
                    self.assertIsInstance(provider, expected_type)
                    self.assertFalse(provider._http_policy.trust_env)
            xai_provider = create_provider(
                ProviderProfile(
                    name="xai",
                    protocol="openai-responses",
                    dialect="xai",
                    model="fixture-model",
                    base_url="https://api.x.ai/v1",
                    api_key_env="TOKEN",
                    builtin_tools=("web_search", "code_interpreter"),
                    proxy_mode="direct",
                )
            )
            assert isinstance(xai_provider, OpenAIResponsesProvider)
            self.assertEqual(
                xai_provider._builtin_tools,
                ("web_search", "code_interpreter"),
            )
            self.assertEqual(xai_provider.provider_name, "xai")
            with self.assertRaisesRegex(ConfigurationError, "require dialect"):
                ProviderProfile(
                    name="invalid-tools",
                    protocol="openai-chat",
                    model="m",
                    base_url="https://example.invalid/v1",
                    api_key_env="TOKEN",
                    builtin_tools=("web_search",),
                )
        with self.assertRaises(ConfigurationError):
            ProviderProfile(
                name="unknown",
                protocol="unknown",
                model="m",
                base_url="https://example.invalid/v1",
                api_key_env="TOKEN",
            )

    def test_routed_provider_is_lazy_and_can_be_disabled(self) -> None:
        primary = ProviderProfile(
            name="primary",
            protocol="openai-chat",
            model="primary-model",
            base_url="https://primary.invalid/v1",
            api_key_env="PRIMARY_KEY",
        )
        fallback = ProviderProfile(
            name="fallback",
            protocol="openai-chat",
            model="fallback-model",
            base_url="https://fallback.invalid/v1",
            api_key_env="MISSING_FALLBACK_KEY",
        )
        config = AppConfig(
            cwd=Path("/workspace"),
            state_dir=Path("/state"),
            providers={"primary": primary, "fallback": fallback},
            default_provider="primary",
            selected_provider="primary",
            fallback_providers=("fallback",),
        )

        with mock.patch.dict("os.environ", {"PRIMARY_KEY": "value"}, clear=True):
            routed = create_routed_provider(config)
            direct = create_routed_provider(config, failover=False)

        self.assertIsInstance(routed, FailoverModelProvider)
        self.assertIsInstance(direct, OpenAICompatibleProvider)


if __name__ == "__main__":
    unittest.main()
