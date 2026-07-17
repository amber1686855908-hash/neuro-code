from __future__ import annotations

import json
import unittest

import httpx

from pygrok_build.domain.messages import Message, Role, ToolCall
from pygrok_build.domain.model_events import (
    ModelCompleted,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from pygrok_build.domain.tools import ToolDefinition
from pygrok_build.errors import ProviderError
from pygrok_build.providers.anthropic import AnthropicProvider


def _sse(*events: object) -> str:
    return "".join(
        f"event: fixture\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
    )


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase):
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
                    "message": {"usage": {"input_tokens": 12, "output_tokens": 1}},
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

        events = [event async for event in provider.stream(messages, tools)]

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
        self.assertEqual(captured["url"], "https://api.anthropic.invalid/v1/messages")
        self.assertEqual(captured["api_key"], "secret-key")
        self.assertEqual(captured["version"], "2023-06-01")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "claude-fixture")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["system"], "Be precise.")
        self.assertEqual(body["messages"][1]["content"][1]["name"], "read_file")
        self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "old-1")
        self.assertEqual(body["messages"][2]["content"][1]["text"], "Continue.")
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")

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
                    [event async for event in provider.stream((Message(Role.USER, "hi"),), ())]
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
            [event async for event in provider.stream((Message(Role.USER, "hi"),), ())]

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
                    [event async for event in provider.stream((Message(Role.USER, "hi"),), ())]

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
