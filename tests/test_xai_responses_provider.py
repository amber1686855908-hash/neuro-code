from __future__ import annotations

import json
import unittest

import httpx

from neuro_code.domain.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.model_context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.model_events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ConfigurationError, ProviderError
from neuro_code.providers.xai_responses import XAIResponsesProvider


def _sse(*events: object) -> str:
    lines = [f"data: {json.dumps(event)}" for event in events]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def _reasoning(*, encrypted: str = "opaque") -> PreservedContextItem:
    return PreservedContextItem(
        ContextItemKind.REASONING,
        {
            "type": "reasoning",
            "id": "reasoning-1",
            "summary": [{"type": "summary_text", "text": "visible summary"}],
            "content": [{"type": "reasoning_text", "text": "visible reasoning"}],
            "encrypted_content": encrypted,
            "status": "completed",
        },
    )


class XAIResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_official_native_affinity_is_independent_of_model_spelling(self) -> None:
        provider = XAIResponsesProvider(
            model="future-xai-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
        )
        context = ModelContext(
            (Message(Role.USER, "continue"),),
            source_provider=UPSTREAM_IMPORT_PROVIDER,
            source_model="legacy-xai-model",
        )

        self.assertTrue(provider._has_native_affinity(context))

    def test_request_body_replays_native_items_messages_images_and_tools(self) -> None:
        provider = XAIResponsesProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1/responses/",
            api_key="fixture",
            max_output_tokens=512,
        )
        backend = PreservedContextItem(
            ContextItemKind.BACKEND_TOOL_CALL,
            {
                "type": "backend_tool_call",
                "kind": {
                    "tool_type": "web_search",
                    "id": "web-1",
                    "status": "completed",
                    "action": {"type": "search", "query": "native context"},
                },
            },
        )
        context = ModelContext(
            (
                Message(Role.SYSTEM, "system"),
                Message(
                    Role.USER,
                    content_parts=(
                        ContentPart.from_text("look"),
                        ContentPart.from_image("data:image/png;base64,aW1hZ2U="),
                    ),
                ),
                _reasoning(),
                backend,
                Message(
                    Role.ASSISTANT,
                    "checking",
                    tool_calls=(ToolCall("call-1", "read_file", {"path": "a.py"}),),
                    reasoning_content="display-only reasoning",
                ),
                Message(Role.TOOL, "contents", name="read_file", tool_call_id="call-1"),
            ),
            source_provider=UPSTREAM_IMPORT_PROVIDER,
            source_model="xai-test-model",
        )
        tools = (
            ToolDefinition(
                "read_file",
                "Read a file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        )

        body = provider._request_body(context, tools)

        self.assertEqual(provider.provider_name, "xai-responses")
        self.assertEqual(provider.model_name, "xai-test-model")
        self.assertEqual(provider._endpoint, "https://api.x.ai/v1/responses")
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_output_tokens"], 512)
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertEqual(body["reasoning"], {"summary": "concise"})
        inputs = body["input"]
        self.assertEqual(inputs[0], {"type": "message", "role": "system", "content": "system"})
        self.assertEqual(
            inputs[1]["content"],
            [
                {"type": "input_text", "text": "look"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,aW1hZ2U=",
                    "detail": "auto",
                },
            ],
        )
        self.assertEqual(inputs[2]["type"], "reasoning")
        self.assertEqual(inputs[2]["encrypted_content"], "opaque")
        self.assertNotIn("status", inputs[2])
        self.assertEqual(inputs[3]["type"], "web_search_call")
        self.assertEqual(inputs[4]["content"], "checking")
        self.assertEqual(inputs[5]["type"], "function_call")
        self.assertEqual(json.loads(inputs[5]["arguments"]), {"path": "a.py"})
        self.assertEqual(inputs[6]["type"], "function_call_output")
        self.assertEqual(body["tools"][0]["name"], "read_file")
        self.assertNotIn("function", body["tools"][0])

    def test_builtin_tools_precede_functions_and_win_name_collisions(self) -> None:
        provider = XAIResponsesProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
            builtin_tools=("web_search", "x_search", "code_interpreter"),
        )
        tools = (
            ToolDefinition("web_search", "Local collision", {"type": "object"}),
            ToolDefinition("read_file", "Read", {"type": "object"}),
        )

        body = provider._request_body(
            ModelContext((Message(Role.USER, "research"),)),
            tools,
        )

        self.assertEqual(
            body["tools"],
            [
                {"type": "web_search"},
                {"type": "x_search"},
                {"type": "code_interpreter"},
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read",
                    "parameters": {"type": "object"},
                },
            ],
        )
        self.assertEqual(
            body["include"],
            [
                "reasoning.encrypted_content",
                "web_search_call.action.sources",
                "code_interpreter_call.outputs",
            ],
        )

    def test_builtin_tool_constructor_validation_is_defensive(self) -> None:
        invalid = (
            ("web_search", "sequence"),
            (("web_search", "web_search"), "duplicates"),
            (("file_search",), "unsupported"),
        )
        for builtin_tools, expected in invalid:
            with (
                self.subTest(builtin_tools=builtin_tools),
                self.assertRaisesRegex(ConfigurationError, expected),
            ):
                XAIResponsesProvider(
                    model="xai-test-model",
                    base_url="https://api.x.ai/v1",
                    api_key="fixture",
                    builtin_tools=builtin_tools,
                )

    def test_native_context_is_dropped_without_strict_affinity(self) -> None:
        items = (
            Message(Role.USER, "question"),
            _reasoning(encrypted="must-not-leak"),
            Message(Role.ASSISTANT, "answer"),
        )
        cases = (
            ("https://api.x.ai/v1", "xai-test-model", None, None),
            ("https://api.x.ai/v1", "xai-test-model", "other", "xai-test-model"),
            (
                "https://api.x.ai.evil/v1",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
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
                "https://api.x.ai/v1?gateway=1",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            (
                "https://api.x.ai/v1#gateway",
                "xai-test-model",
                UPSTREAM_IMPORT_PROVIDER,
                "xai-test-model",
            ),
            ("https://[broken", "xai-test-model", UPSTREAM_IMPORT_PROVIDER, "xai-test-model"),
        )
        for base_url, model, source_provider, source_model in cases:
            with self.subTest(base_url=base_url, model=model, source_provider=source_provider):
                provider = XAIResponsesProvider(
                    model=model,
                    base_url=base_url,
                    api_key="fixture",
                )
                context = ModelContext(
                    items,
                    source_provider=source_provider,
                    source_model=source_model,
                )

                inputs = provider._input_items(context)

                self.assertEqual(
                    inputs,
                    [
                        {"type": "message", "role": "user", "content": "question"},
                        {"type": "message", "role": "assistant", "content": "answer"},
                    ],
                )
                self.assertNotIn("must-not-leak", json.dumps(inputs))

    async def test_stream_normalizes_terminal_tools_usage_and_native_context(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization")
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            body = _sse(
                {"type": "response.created", "response": {"status": "in_progress"}},
                {"type": "response.reasoning_summary_text.delta", "delta": "plan"},
                {"type": "response.output_text.delta", "delta": "done"},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-1",
                        "status": "completed",
                        "output": [
                            {
                                "type": "reasoning",
                                "id": "reasoning-native",
                                "summary": [{"type": "summary_text", "text": "plan"}],
                                "encrypted_content": "encrypted-native",
                                "status": "completed",
                            },
                            {
                                "type": "web_search_call",
                                "id": "web-native",
                                "status": "completed",
                                "action": {"type": "search", "query": "fixture"},
                            },
                            {
                                "type": "function_call",
                                "call_id": "call-native",
                                "name": "read_file",
                                "arguments": '{"path":"a.py"}',
                            },
                        ],
                        "usage": {"input_tokens": 11, "output_tokens": 7},
                    },
                },
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        provider = XAIResponsesProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1",
            api_key="secret-key",
            transport=httpx.MockTransport(handler),
        )
        tools = (
            ToolDefinition(
                "read_file",
                "Read",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        )

        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "inspect"),)), tools
            )
        ]

        self.assertEqual(
            [type(event) for event in events],
            [
                ModelReasoningDelta,
                ModelTextDelta,
                ModelBackendToolStarted,
                ModelBackendToolCompleted,
                ModelToolCall,
                ModelCompleted,
            ],
        )
        self.assertEqual(events[0], ModelReasoningDelta("plan"))
        self.assertEqual(events[1], ModelTextDelta("done"))
        self.assertEqual(
            events[4],
            ModelToolCall(ToolCall("call-native", "read_file", {"path": "a.py"})),
        )
        self.assertEqual(events[2], ModelBackendToolStarted("web-native", "web_search"))
        self.assertEqual(events[3], ModelBackendToolCompleted("web-native", "web_search"))
        completed = events[5]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(
            (completed.stop_reason, completed.input_tokens, completed.output_tokens),
            ("tool_calls", 11, 7),
        )
        self.assertEqual(len(completed.context_items), 2)
        self.assertEqual(
            [item.kind for item in completed.context_items],
            [ContextItemKind.REASONING, ContextItemKind.BACKEND_TOOL_CALL],
        )
        self.assertEqual(completed.context_items[0].to_dict()["status"], "completed")
        self.assertEqual(
            completed.context_items[1].to_dict()["kind"]["tool_type"],
            "web_search",
        )
        self.assertEqual(captured["authorization"], "Bearer secret-key")
        self.assertEqual(captured["url"], "https://api.x.ai/v1/responses")
        request_body = captured["body"]
        assert isinstance(request_body, dict)
        self.assertFalse(request_body["store"])

    async def test_stream_normalizes_and_deduplicates_all_backend_tool_lifecycles(self) -> None:
        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "web_search_call", "id": "web-1", "status": "completed"},
                    {"type": "custom_tool_call", "id": "x-1", "status": "completed"},
                    {
                        "type": "code_interpreter_call",
                        "id": "code-1",
                        "status": "completed",
                    },
                ],
            },
        }
        stream = _sse(
            {"type": "response.web_search_call.in_progress", "item_id": "web-1"},
            {"type": "response.web_search_call.searching", "item_id": "web-1"},
            {
                "type": "response.output_item.done",
                "item": {"type": "web_search_call", "id": "web-1"},
            },
            {"type": "response.custom_tool_call_input.done", "item_id": "x-1"},
            {"type": "response.x_search_call.searching", "item_id": "x-1"},
            {
                "type": "response.output_item.done",
                "item": {"type": "custom_tool_call", "id": "x-1"},
            },
            {"type": "response.code_interpreter_call.interpreting", "item_id": "code-1"},
            {"type": "response.code_interpreter_call.completed", "item_id": "code-1"},
            terminal,
        )
        provider = XAIResponsesProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
            builtin_tools=("web_search", "x_search", "code_interpreter"),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=stream)),
        )

        events = [
            event
            async for event in provider.stream(ModelContext((Message(Role.USER, "research"),)), ())
        ]

        self.assertEqual(
            events[:-1],
            [
                ModelBackendToolStarted("web-1", "web_search"),
                ModelBackendToolCompleted("web-1", "web_search"),
                ModelBackendToolStarted("x-1", "x_search"),
                ModelBackendToolCompleted("x-1", "x_search"),
                ModelBackendToolStarted("code-1", "code_interpreter"),
                ModelBackendToolCompleted("code-1", "code_interpreter"),
            ],
        )
        self.assertIsInstance(events[-1], ModelCompleted)

    async def test_terminal_content_is_a_fallback_and_incomplete_reason_is_preserved(self) -> None:
        terminal = {
            "type": "response.incomplete",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "reasoning",
                        "id": "reasoning-fallback",
                        "summary": [],
                        "content": [{"type": "reasoning_text", "text": "fallback thought"}],
                        "encrypted_content": "encrypted-fallback",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "fallback answer"}],
                    },
                ],
            },
        }
        provider = XAIResponsesProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=_sse(terminal))),
        )

        events = [
            event
            async for event in provider.stream(ModelContext((Message(Role.USER, "hello"),)), ())
        ]

        self.assertEqual(events[0], ModelReasoningDelta("fallback thought"))
        self.assertEqual(events[1], ModelTextDelta("fallback answer"))
        completed = events[2]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(completed.stop_reason, "max_output_tokens")
        self.assertEqual(completed.response_text, "fallback answer")
        self.assertEqual(len(completed.context_items), 1)

    async def test_streaming_reasoning_repairs_an_encrypted_only_terminal_item(self) -> None:
        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "reasoning-encrypted",
                        "summary": [],
                        "encrypted_content": "encrypted-only",
                        "status": "completed",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "answer"}],
                    },
                ],
            },
        }
        provider = XAIResponsesProvider(
            model="xai-test-model",
            base_url="https://api.x.ai/v1",
            api_key="fixture",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=_sse(
                        {"type": "response.reasoning_text.delta", "delta": "streamed thought"},
                        terminal,
                    ),
                )
            ),
        )

        events = [
            event
            async for event in provider.stream(ModelContext((Message(Role.USER, "hello"),)), ())
        ]

        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(
            completed.context_items[0].to_dict()["summary"],
            [{"type": "summary_text", "text": "streamed thought"}],
        )
        self.assertEqual(
            completed.context_items[0].to_dict()["encrypted_content"],
            "encrypted-only",
        )

    async def test_custom_endpoint_never_persists_opaque_native_output(self) -> None:
        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "untrusted",
                        "summary": [],
                        "encrypted_content": "untrusted-opaque",
                    }
                ],
            },
        }
        provider = XAIResponsesProvider(
            model="xai-test-model",
            base_url="https://gateway.invalid/v1",
            api_key="fixture",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=_sse(terminal))),
        )

        events = [
            event
            async for event in provider.stream(ModelContext((Message(Role.USER, "hello"),)), ())
        ]

        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(completed.context_items, ())

    async def test_protocol_and_http_failures_are_bounded_and_redacted(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="secret-key " + "x" * 2000)

        cases = (
            (httpx.MockTransport(unauthorized), "HTTP 401"),
            (
                httpx.MockTransport(lambda request: httpx.Response(200, text="data: {bad}\n\n")),
                "malformed streaming JSON",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse(
                            {
                                "type": "response.failed",
                                "response": {
                                    "error": {
                                        "code": "failed",
                                        "message": "model failed secret-key",
                                    }
                                },
                            }
                        ),
                    )
                ),
                "model failed",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse({"type": "response.output_text.delta", "delta": "partial"}),
                    )
                ),
                "without a terminal response",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse(
                            {
                                "type": "response.completed",
                                "response": {"status": "completed"},
                            }
                        ),
                    )
                ),
                "omitted its output items",
            ),
        )
        for transport, expected in cases:
            with self.subTest(expected=expected):
                provider = XAIResponsesProvider(
                    model="xai-test-model",
                    base_url="https://api.x.ai/v1",
                    api_key="secret-key",
                    transport=transport,
                )
                with self.assertRaisesRegex(ProviderError, expected) as raised:
                    [
                        event
                        async for event in provider.stream(
                            ModelContext((Message(Role.USER, "hello"),)), ()
                        )
                    ]
                self.assertNotIn("secret-key", str(raised.exception))
                self.assertLess(len(str(raised.exception)), 1200)

    def test_invalid_function_calls_and_tool_results_are_rejected(self) -> None:
        responses = (
            (
                {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "id",
                            "name": "tool",
                            "arguments": "[1]",
                        }
                    ]
                },
                "JSON object",
            ),
            (
                {
                    "output": [
                        {"type": "function_call", "call_id": "id", "name": "tool", "arguments": "{"}
                    ]
                },
                "invalid JSON",
            ),
            (
                {
                    "output": [
                        {"type": "function_call", "call_id": "", "name": "tool", "arguments": "{}"}
                    ]
                },
                "incomplete function call",
            ),
        )
        for response, expected in responses:
            with self.subTest(expected=expected), self.assertRaisesRegex(ProviderError, expected):
                XAIResponsesProvider._response_tool_calls(response)

        with self.assertRaisesRegex(ProviderError, "require a tool call id"):
            XAIResponsesProvider._message_input_items(Message(Role.TOOL, "orphan"))


if __name__ == "__main__":
    unittest.main()
