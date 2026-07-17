from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from pygrok_build.config import ProviderConfig
from pygrok_build.domain.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPart,
    Message,
    Role,
    ToolCall,
)
from pygrok_build.domain.model_events import (
    ModelCompleted,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from pygrok_build.domain.tools import ToolDefinition
from pygrok_build.errors import ConfigurationError, ProviderError
from pygrok_build.providers import create_provider
from pygrok_build.providers.anthropic import AnthropicProvider
from pygrok_build.providers.gemini import GeminiProvider
from pygrok_build.providers.openai_compatible import OpenAICompatibleProvider, _ToolCallBuffer


def _sse(*chunks: object) -> str:
    lines = [f"data: {json.dumps(chunk)}" for chunk in chunks]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


class OpenAICompatibleProviderTests(unittest.IsolatedAsyncioTestCase):
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

        events = [event async for event in provider.stream(messages, tools)]

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
            [event async for event in provider.stream((Message(Role.USER, "hello"),), ())]
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
            [event async for event in provider.stream((Message(Role.USER, "hello"),), ())]

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
                [event async for event in provider.stream((Message(Role.USER, "hello"),), ())]

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
            [event async for event in provider.stream((Message(Role.USER, "hello"),), ())]

    def test_tool_call_accumulator_ignores_invalid_fragments(self) -> None:
        buffers: dict[int, _ToolCallBuffer] = {}
        OpenAICompatibleProvider._accumulate_tool_calls(
            [None, {"index": "bad"}, {"index": 1, "id": 3}, {"index": 1, "function": None}],
            buffers,
        )
        self.assertIn(1, buffers)
        self.assertEqual(buffers[1], _ToolCallBuffer())

    def test_provider_factory_and_unknown_kind(self) -> None:
        expected_types = {
            "openai-compatible": OpenAICompatibleProvider,
            "anthropic": AnthropicProvider,
            "gemini": GeminiProvider,
        }
        with mock.patch.dict("os.environ", {"TOKEN": "value"}):
            for kind, expected_type in expected_types.items():
                with self.subTest(kind=kind):
                    provider = create_provider(
                        ProviderConfig(
                            kind=kind,
                            model="m",
                            base_url="https://example.invalid/v1",
                            api_key_env="TOKEN",
                        )
                    )
                    self.assertIsInstance(provider, expected_type)
        with self.assertRaises(ConfigurationError):
            create_provider(ProviderConfig(kind="unknown"))


if __name__ == "__main__":
    unittest.main()
