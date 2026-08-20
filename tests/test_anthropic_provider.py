from __future__ import annotations

import json
import unittest
from collections.abc import Mapping

import httpx

from neuro_code.application.ports.model import ModelCapability, ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelInputTokenSemantics,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
from neuro_code.shared.errors import ConfigurationError, ProviderError


def _sse(*events: object) -> str:
    return "".join(
        f"event: fixture\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
    )


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_server_tool_definitions_are_direct_and_do_not_duplicate_local_tools(self) -> None:
        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            builtin_tools=("web_search", "web_fetch"),
            builtin_tool_options={
                "web_search": {"allowed_domains": ["docs.example.com"], "max_uses": 2},
                "web_fetch": {"max_content_tokens": 4_000},
            },
            tool_choice={"type": "tool", "name": "web_search"},
        )
        tools = (
            ToolDefinition("web_search", "local collision", {"type": "object"}),
            ToolDefinition("read_file", "Read", {"type": "object"}),
        )

        body = provider._request_body(
            (Message(Role.USER, "search"),),
            tools,
        )

        assert body["tools"]
        self.assertEqual(
            [tool.get("type", "custom") for tool in body["tools"]],
            ["web_search_20260318", "web_fetch_20260318", "custom"],
        )
        self.assertEqual(body["tools"][0]["allowed_callers"], ["direct"])
        self.assertEqual(body["tools"][0]["allowed_domains"], ["docs.example.com"])
        self.assertEqual(body["tools"][0]["max_uses"], 2)
        self.assertEqual(body["tools"][1]["allowed_callers"], ["direct"])
        self.assertEqual(body["tools"][1]["citations"], {"enabled": True})
        self.assertEqual(body["tools"][1]["max_content_tokens"], 4_000)
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "web_search"})
        self.assertEqual(body["tools"][2]["name"], "read_file")
        self.assertNotIn("response_inclusion", body["tools"][0])
        self.assertNotIn("use_cache", body["tools"][1])
        self.assertIsNone(provider.context_affinity)
        self.assertIsNotNone(provider.capabilities)

        disabled = provider._request_body(
            (Message(Role.USER, "search"),),
            tools,
            tool_policy=ModelToolPolicy.DISABLED,
        )
        self.assertNotIn("tools", disabled)
        self.assertNotIn("tool_choice", disabled)

    def test_builtin_capability_and_constructor_validation_is_fail_closed(self) -> None:
        capabilities = AnthropicProvider.implementation_capabilities(
            model=" claude-sonnet-4-6 ".strip(),
            builtin_tools=("web_search", "web_fetch"),
            prompt_caching=False,
        )
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH))
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_WEB_FETCH))
        self.assertFalse(capabilities.supports(ModelCapability.PROMPT_CACHE))

        capability_cases = (
            ("web_search", "must be a sequence"),
            (("web_search", "web_search"), "must not contain duplicates"),
            (("not_a_server_tool",), "unsupported Anthropic builtin_tools"),
        )
        for builtin_tools, expected in capability_cases:
            with (
                self.subTest(builtin_tools=builtin_tools),
                self.assertRaisesRegex(ConfigurationError, expected),
            ):
                AnthropicProvider.implementation_capabilities(builtin_tools=builtin_tools)

        constructor_cases = (
            ({"prompt_caching": 1}, TypeError, "prompt_caching must be a bool"),
            ({"builtin_tools": "web_search"}, ConfigurationError, "must be a sequence"),
            ({"builtin_tools": (1,)}, ConfigurationError, "non-empty strings"),
            (
                {"builtin_tools": ("web_search", "web_search")},
                ConfigurationError,
                "must not contain duplicates",
            ),
            (
                {"builtin_tools": ("not_a_server_tool",)},
                ConfigurationError,
                "unsupported Anthropic builtin_tools",
            ),
            (
                {"builtin_tool_options": "invalid"},
                ConfigurationError,
                "must be a mapping",
            ),
            (
                {"builtin_tool_options": {"web_search": {"max_uses": 1}}},
                ConfigurationError,
                "disabled Anthropic tool",
            ),
            (
                {
                    "builtin_tools": ("web_search",),
                    "builtin_tool_options": {"web_search": "invalid"},
                },
                ConfigurationError,
                "must be a mapping",
            ),
            (
                {"tool_choice": "auto"},
                ConfigurationError,
                "tool_choice must be a mapping",
            ),
            (
                {"tool_choice": {"type": "required"}},
                ConfigurationError,
                "unsupported type",
            ),
            (
                {"tool_choice": {"type": "tool"}},
                ConfigurationError,
                "requires a name",
            ),
        )
        for kwargs, error_type, expected in constructor_cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(error_type, expected):
                AnthropicProvider(
                    model="claude-fixture",
                    base_url="https://api.anthropic.invalid",
                    api_key="fixture",
                    **kwargs,
                )

    def test_native_context_replay_rejects_opaque_or_malformed_items(self) -> None:
        reasoning = PreservedContextItem(
            ContextItemKind.REASONING,
            {"type": "reasoning", "text": "opaque"},
        )
        wrong_kind = PreservedContextItem(
            ContextItemKind.BACKEND_TOOL_CALL,
            {"type": "backend_tool_call", "kind": "not-a-mapping"},
        )
        wrong_provider = PreservedContextItem(
            ContextItemKind.BACKEND_TOOL_CALL,
            {
                "type": "backend_tool_call",
                "kind": {"provider": "other", "native_type": "anthropic_message_content"},
            },
        )
        malformed_content = PreservedContextItem(
            ContextItemKind.BACKEND_TOOL_CALL,
            {
                "type": "backend_tool_call",
                "kind": {
                    "provider": "anthropic-messages",
                    "native_type": "anthropic_message_content",
                    "content": ["not-a-block"],
                },
            },
        )

        self.assertIsNone(AnthropicProvider._native_content(reasoning))
        self.assertIsNone(AnthropicProvider._native_content(wrong_kind))
        self.assertIsNone(AnthropicProvider._native_content(wrong_provider))
        self.assertIsNone(AnthropicProvider._native_content(malformed_content))

    def test_structured_user_and_tool_images_use_native_content_blocks(self) -> None:
        user = Message(
            Role.USER,
            content_parts=(
                ContentPart.from_text("before"),
                ContentPart.from_image("data:image/png;base64,aW1hZ2U="),
                ContentPart.from_image("https://example.com/screenshot.webp"),
            ),
        )
        tool = Message(
            Role.TOOL,
            tool_call_id="call-1",
            content_parts=(
                ContentPart.from_text("result"),
                ContentPart.from_image("data:image/jpeg;base64,aW1hZ2U="),
            ),
        )

        assistant = Message(
            Role.ASSISTANT,
            tool_calls=(ToolCall("call-1", "read_file", {"path": "screenshot.png"}),),
        )

        _, converted = AnthropicProvider._convert_messages((user, assistant, tool))

        self.assertEqual(
            converted[0]["content"],
            [
                {"type": "text", "text": "before"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "aW1hZ2U=",
                    },
                },
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": "https://example.com/screenshot.webp",
                    },
                },
            ],
        )
        self.assertEqual(
            converted[2]["content"][0]["content"],
            [
                {"type": "text", "text": "result"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": "aW1hZ2U=",
                    },
                },
            ],
        )

    def test_invalid_and_assistant_images_use_the_safe_text_projection(self) -> None:
        user = Message(
            Role.USER,
            content_parts=(ContentPart.from_image("data:image/heic;base64,aW1hZ2U="),),
        )
        assistant = Message(
            Role.ASSISTANT,
            content_parts=(ContentPart.from_image("data:image/png;base64,aW1hZ2U="),),
        )

        _, converted = AnthropicProvider._convert_messages((user, assistant))

        self.assertEqual(
            converted[0]["content"][0],
            {"type": "text", "text": IMAGE_MODEL_PLACEHOLDER},
        )
        self.assertEqual(
            converted[1]["content"][0],
            {"type": "text", "text": IMAGE_MODEL_PLACEHOLDER},
        )

    async def test_stream_converts_messages_and_normalizes_all_core_events(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["api_key"] = request.headers.get("x-api-key")
            captured["version"] = request.headers.get("anthropic-version")
            captured["body"] = json.loads(request.content)
            body = _sse(
                [],
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 8,
                            "cache_creation_input_tokens": 4,
                        }
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": "Hello "},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "thinking", "thinking": "First "},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "world"},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "thinking_delta", "thinking": "check"},
                },
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "read_file",
                        "input": {"encoding": "utf-8"},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
                },
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "input_json_delta", "partial_json": '"a.py"}'},
                },
                {"type": "content_block_stop", "index": 2},
                {"type": "ping"},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 9},
                },
                {"type": "message_stop"},
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        provider = AnthropicProvider(
            model="claude-fixture",
            base_url="https://api.anthropic.invalid",
            api_key="secret-key",
            max_output_tokens=4096,
            transport=httpx.MockTransport(handler),
        )
        old_call = ToolCall("old-1", "read_file", {"path": "old.py"})
        messages = (
            Message(Role.SYSTEM, "Be precise."),
            Message(Role.USER, "Inspect it."),
            Message(Role.ASSISTANT, "I will.", tool_calls=(old_call,)),
            Message(Role.TOOL, "old contents", name="read_file", tool_call_id="old-1"),
            Message(Role.USER, "Continue."),
        )
        tools = (
            ToolDefinition(
                "read_file",
                "Read a file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        )

        events = [event async for event in provider.stream(ModelContext(tuple(messages)), tools)]

        self.assertEqual(provider.provider_name, "anthropic")
        self.assertEqual(provider.model_name, "claude-fixture")
        self.assertEqual(
            [type(event) for event in events],
            [
                ModelTextDelta,
                ModelReasoningDelta,
                ModelTextDelta,
                ModelReasoningDelta,
                ModelToolCall,
                ModelCompleted,
            ],
        )
        tool_event = events[-2]
        assert isinstance(tool_event, ModelToolCall)
        self.assertEqual(
            tool_event.call,
            ToolCall(
                "toolu-1",
                "read_file",
                {"encoding": "utf-8", "path": "a.py"},
            ),
        )
        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(
            (completed.stop_reason, completed.input_tokens, completed.output_tokens),
            ("tool_use", 12, 9),
        )
        assert completed.usage is not None
        self.assertIs(
            completed.usage.input_token_semantics,
            ModelInputTokenSemantics.UNCACHED_TAIL,
        )
        self.assertEqual(completed.usage.cache_read_tokens, 8)
        self.assertEqual(completed.usage.cache_write_tokens, 4)
        self.assertEqual(completed.usage.processed_input_tokens, 24)
        self.assertEqual(captured["url"], "https://api.anthropic.invalid/v1/messages")
        self.assertEqual(captured["api_key"], "secret-key")
        self.assertEqual(captured["version"], "2023-06-01")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "claude-fixture")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["system"], "Be precise.")
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][1]["content"][1]["name"], "read_file")
        self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "old-1")
        self.assertEqual(body["messages"][2]["content"][1]["text"], "Continue.")
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")

    async def test_server_search_lifecycle_preserves_native_context_and_visible_sources(
        self,
    ) -> None:
        observed: list[Mapping[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            body = _sse(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-search",
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 10},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "server_tool_use",
                        "id": "srvtoolu-search",
                        "name": "web_search",
                        "input": {"query": "Anthropic server tools"},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu-search",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://docs.example.com/server-tools",
                                "title": "Server tools",
                                "encrypted_content": "opaque-secret-native-payload",
                            }
                        ],
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {
                        "type": "text",
                        "text": "Anthropic documents server tools.",
                        "citations": [
                            {
                                "type": "web_search_result_location",
                                "url": "https://docs.example.com/server-tools",
                                "title": "Server tools",
                                "cited_text": "server tools",
                            }
                        ],
                    },
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 7},
                },
                {"type": "message_stop"},
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            builtin_tools=("web_search",),
            transport=httpx.MockTransport(handler),
            response_observer=observed.append,
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "search"),)),
                (),
            )
        ]

        self.assertEqual(
            [type(event) for event in events],
            [
                ModelBackendToolStarted,
                ModelBackendToolCompleted,
                ModelTextDelta,
                ModelTextDelta,
                ModelCompleted,
            ],
        )
        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(completed.stop_reason, "end_turn")
        self.assertIn("https://docs.example.com/server-tools", completed.response_text or "")
        self.assertNotIn("opaque-secret-native-payload", completed.response_text or "")
        self.assertEqual(len(completed.context_items), 1)
        self.assertIs(completed.context_items[0].kind, ContextItemKind.BACKEND_TOOL_CALL)
        self.assertIn(
            "opaque-secret-native-payload",
            str(completed.context_items[0].to_dict()),
        )
        self.assertEqual(len(observed), 1)

    async def test_pause_turn_continuation_reuses_tools_and_emits_one_completion(self) -> None:
        captured_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            if len(captured_bodies) == 1:
                return httpx.Response(
                    200,
                    text=_sse(
                        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {
                                "type": "server_tool_use",
                                "id": "srvtoolu-pause",
                                "name": "web_search",
                                "input": {"query": "pause"},
                            },
                        },
                        {
                            "type": "content_block_start",
                            "index": 1,
                            "content_block": {
                                "type": "web_search_tool_result",
                                "tool_use_id": "srvtoolu-pause",
                                "content": [],
                            },
                        },
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "pause_turn"},
                            "usage": {"output_tokens": 2},
                        },
                        {"type": "message_stop"},
                    ),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                text=_sse(
                    {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": "done"},
                    },
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 5},
                    },
                    {"type": "message_stop"},
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            builtin_tools=("web_search",),
            transport=httpx.MockTransport(handler),
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "pause"),)),
                (),
            )
        ]

        self.assertEqual(len(captured_bodies), 2)
        self.assertEqual(captured_bodies[0]["tools"], captured_bodies[1]["tools"])
        second_messages = captured_bodies[1]["messages"]
        assert isinstance(second_messages, list)
        self.assertEqual(second_messages[-1]["role"], "assistant")
        self.assertEqual([type(event) for event in events].count(ModelCompleted), 1)
        completion = events[-1]
        assert isinstance(completion, ModelCompleted)
        self.assertEqual(completion.response_text, "done")
        assert completion.usage is not None
        self.assertEqual(completion.usage.input_tokens, 7)

    async def test_pause_turn_continuation_is_bounded(self) -> None:
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                200,
                text=_sse(
                    {"type": "message_start", "message": {"usage": {}}},
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "server_tool_use",
                            "id": f"srvtoolu-{request_count}",
                            "name": "web_search",
                        },
                    },
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "pause_turn"},
                    },
                    {"type": "message_stop"},
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            builtin_tools=("web_search",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderError, "continuation limit"):
            [
                event
                async for event in provider.stream(
                    ModelContext((Message(Role.USER, "pause"),)),
                    (),
                )
            ]
        self.assertEqual(request_count, 4)

    async def test_server_result_error_does_not_emit_completed_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {"type": "message_start", "message": {"usage": {}}},
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "server_tool_use",
                            "id": "srvtoolu-error",
                            "name": "web_search",
                        },
                    },
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "web_search_tool_result",
                            "tool_use_id": "srvtoolu-error",
                            "content": {
                                "type": "web_search_tool_result_error",
                                "error_code": "max_uses_exceeded",
                            },
                        },
                    },
                    {"type": "message_stop"},
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            builtin_tools=("web_search",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderError, "max_uses_exceeded"):
            [
                event
                async for event in provider.stream(
                    ModelContext((Message(Role.USER, "error"),)),
                    (),
                )
            ]

    async def test_mixed_server_and_client_tools_replay_native_assistant_once(self) -> None:
        captured_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            if len(captured_bodies) == 1:
                return httpx.Response(
                    200,
                    text=_sse(
                        {"type": "message_start", "message": {"usage": {}}},
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {
                                "type": "server_tool_use",
                                "id": "srvtoolu-mixed",
                                "name": "web_search",
                            },
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": '{"query":"mixed"}',
                            },
                        },
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "content_block_start",
                            "index": 1,
                            "content_block": {
                                "type": "tool_use",
                                "id": "toolu-client",
                                "name": "read_file",
                                "input": {},
                            },
                        },
                        {"type": "content_block_stop", "index": 1},
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "tool_use"},
                            "usage": {"output_tokens": 2},
                        },
                        {"type": "message_stop"},
                    ),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                text=_sse(
                    {"type": "message_start", "message": {"usage": {}}},
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "web_search_tool_result",
                            "tool_use_id": "srvtoolu-mixed",
                            "content": [],
                        },
                    },
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {"type": "text", "text": "finished"},
                    },
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 3},
                    },
                    {"type": "message_stop"},
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = AnthropicProvider(
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            builtin_tools=("web_search",),
            transport=httpx.MockTransport(handler),
        )
        first_events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "mixed"),)),
                (ToolDefinition("read_file", "Read", {"type": "object"}),),
            )
        ]
        first_completion = first_events[-1]
        assert isinstance(first_completion, ModelCompleted)
        self.assertEqual(
            [type(event) for event in first_events],
            [ModelBackendToolStarted, ModelToolCall, ModelCompleted],
        )
        native = first_completion.context_items[0]
        native_payload = native.to_dict()
        self.assertEqual(
            native_payload["kind"]["content"][0]["input"],
            {"query": "mixed"},
        )
        second_context = ModelContext(
            (
                Message(Role.USER, "mixed"),
                native,
                Message(
                    Role.ASSISTANT,
                    "duplicate standard projection",
                    tool_calls=(ToolCall("toolu-client", "read_file", {}),),
                ),
                Message(Role.TOOL, "file contents", tool_call_id="toolu-client"),
            ),
            source_provider="anthropic",
            source_model="claude-sonnet-4-6",
        )
        second_events = [
            event
            async for event in provider.stream(
                second_context,
                (ToolDefinition("read_file", "Read", {"type": "object"}),),
            )
        ]
        second_messages = captured_bodies[1]["messages"]
        assert isinstance(second_messages, list)
        self.assertEqual(
            [message["role"] for message in second_messages],
            ["user", "assistant", "user"],
        )
        self.assertNotIn("duplicate standard projection", str(second_messages))
        self.assertEqual(
            [type(event) for event in second_events],
            [ModelBackendToolCompleted, ModelTextDelta, ModelCompleted],
        )

    def test_disabled_tool_policy_omits_tools_without_sticky_state(self) -> None:
        provider = AnthropicProvider(
            model="claude-fixture",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            max_output_tokens=4096,
        )
        messages = (Message(Role.SYSTEM, "Be precise."), Message(Role.USER, "Inspect it."))
        tools = (ToolDefinition("read_file", "Read", {"type": "object"}),)

        allowed = provider._request_body(messages, tools)
        disabled = provider._request_body(
            messages,
            tools,
            tool_policy=ModelToolPolicy.DISABLED,
        )
        allowed_again = provider._request_body(messages, tools)

        self.assertEqual(allowed["tools"], allowed_again["tools"])
        self.assertNotIn("tools", disabled)
        self.assertNotIn("tool_choice", disabled)
        self.assertNotIn("beta", disabled)
        self.assertEqual(disabled["model"], "claude-fixture")
        self.assertEqual(disabled["max_tokens"], 4096)
        self.assertEqual(disabled["system"], "Be precise.")
        self.assertEqual(disabled["cache_control"], {"type": "ephemeral"})
        self.assertEqual(tools[0].name, "read_file")

    def test_prompt_caching_can_be_explicitly_disabled_for_a_compatible_gateway(self) -> None:
        provider = AnthropicProvider(
            model="claude-fixture",
            base_url="https://gateway.invalid",
            api_key="fixture",
            max_output_tokens=4096,
            prompt_caching=False,
        )

        body = provider._request_body(
            (Message(Role.SYSTEM, "Be precise."), Message(Role.USER, "Inspect it.")),
            (),
        )

        self.assertEqual(body["system"], "Be precise.")
        self.assertNotIn("cache_control", body)

    def test_prompt_caching_uses_an_automatic_top_level_breakpoint(self) -> None:
        provider = AnthropicProvider(
            model="claude-fixture",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            max_output_tokens=4096,
        )
        messages = (
            Message(Role.SYSTEM, "Be precise."),
            Message(Role.USER, "Inspect the repository."),
            Message(Role.ASSISTANT, "I will inspect it."),
            Message(Role.USER, "Continue with the next file."),
        )

        body = provider._request_body(messages, ())

        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["system"], "Be precise.")
        self.assertEqual(
            [message["role"] for message in body["messages"]],
            ["user", "assistant", "user"],
        )
        self.assertNotIn("cache_control", body["messages"][0]["content"][0])

    def test_prompt_caching_keeps_anthropic_wire_prefix_append_only(self) -> None:
        """A growing tool conversation must retain its prior wire prefix.

        持续增长的工具对话必须保留先前的实际请求前缀。
        """

        provider = AnthropicProvider(
            model="claude-fixture",
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            max_output_tokens=4096,
        )
        tools = (ToolDefinition("read_file", "Read a file", {"type": "object"}),)
        first_request = (
            Message(Role.SYSTEM, "Be precise."),
            Message(Role.USER, "Inspect the repository."),
            Message(
                Role.ASSISTANT,
                tool_calls=(ToolCall("call-1", "read_file", {"path": "a.py"}),),
            ),
            Message(Role.TOOL, "first evidence", tool_call_id="call-1"),
            Message(Role.USER, "Runtime plan update:\nContinue the inspection."),
        )
        second_request = (
            *first_request,
            Message(
                Role.ASSISTANT,
                tool_calls=(ToolCall("call-2", "read_file", {"path": "b.py"}),),
            ),
            Message(Role.TOOL, "second evidence", tool_call_id="call-2"),
            Message(Role.USER, "Runtime budget guidance [conserve]: focus on evidence."),
        )

        first_body = provider._request_body(first_request, tools)
        second_body = provider._request_body(second_request, tools)

        self.assertEqual(first_body["system"], second_body["system"])
        self.assertEqual(first_body["tools"], second_body["tools"])
        first_messages = first_body["messages"]
        second_messages = second_body["messages"]
        self.assertEqual(first_messages, second_messages[: len(first_messages)])
        self.assertEqual(second_body["cache_control"], {"type": "ephemeral"})

    async def test_stream_rejects_http_json_stream_and_transport_failures(self) -> None:
        cases = (
            (
                httpx.MockTransport(lambda request: httpx.Response(401, text="bad must-not-leak")),
                "HTTP 401",
            ),
            (
                httpx.MockTransport(lambda request: httpx.Response(200, text="data: {broken}\n\n")),
                "malformed streaming JSON",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse(
                            {
                                "type": "error",
                                "error": {"message": "must-not-leak overloaded"},
                            }
                        ),
                    )
                ),
                "stream error",
            ),
        )
        for transport, expected in cases:
            with self.subTest(expected=expected):
                provider = AnthropicProvider(
                    model="model",
                    base_url="https://provider.invalid/v1",
                    api_key="must-not-leak",
                    transport=transport,
                )
                with self.assertRaisesRegex(ProviderError, expected) as raised:
                    [
                        event
                        async for event in provider.stream(
                            ModelContext((Message(Role.USER, "hi"),)), ()
                        )
                    ]
                self.assertNotIn("must-not-leak", str(raised.exception))

        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        provider = AnthropicProvider(
            model="model",
            base_url="https://provider.invalid/v1/messages",
            api_key="key",
            transport=httpx.MockTransport(fail),
        )
        with self.assertRaisesRegex(ProviderError, "stream failed"):
            [
                event
                async for event in provider.stream(ModelContext((Message(Role.USER, "hi"),)), ())
            ]

    async def test_stream_rejects_invalid_and_truncated_tool_events(self) -> None:
        event_cases = (
            (
                (
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "id",
                            "name": "tool",
                            "input": {},
                        },
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": "{"},
                    },
                    {"type": "content_block_stop", "index": 0},
                ),
                "invalid JSON",
            ),
            (
                (
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "id",
                            "name": "tool",
                            "input": {},
                        },
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": "[]"},
                    },
                    {"type": "content_block_stop", "index": 0},
                ),
                "JSON object",
            ),
            (
                (
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "",
                            "name": "",
                            "input": {},
                        },
                    },
                    {"type": "content_block_stop", "index": 0},
                ),
                "incomplete tool call",
            ),
            (
                (
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "id",
                            "name": "tool",
                            "input": [],
                        },
                    },
                ),
                "tool input must be a JSON object",
            ),
            (
                (
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "id",
                            "name": "tool",
                            "input": {},
                        },
                    },
                    {"type": "message_stop"},
                ),
                "ended during a tool call",
            ),
            (({"type": "message_delta", "delta": {}},), "without message_stop"),
        )
        for events, expected in event_cases:
            with self.subTest(expected=expected):
                transport = httpx.MockTransport(
                    lambda request, value=events: httpx.Response(200, text=_sse(*value))
                )
                provider = AnthropicProvider(
                    model="model",
                    base_url="https://provider.invalid/v1",
                    api_key="key",
                    transport=transport,
                )
                with self.assertRaisesRegex(ProviderError, expected):
                    [
                        event
                        async for event in provider.stream(
                            ModelContext((Message(Role.USER, "hi"),)), ()
                        )
                    ]

    def test_message_validation_and_endpoint_variants(self) -> None:
        with self.assertRaisesRegex(ProviderError, "tool_call_id"):
            AnthropicProvider._convert_messages((Message(Role.TOOL, "result"),))
        provider = AnthropicProvider(
            model="model",
            base_url="https://provider.invalid/v1/messages/",
            api_key="key",
        )
        self.assertEqual(provider._endpoint, "https://provider.invalid/v1/messages")
        body = provider._request_body((Message(Role.USER, "hello"),), ())
        self.assertNotIn("system", body)
        self.assertNotIn("tools", body)


if __name__ == "__main__":
    unittest.main()
