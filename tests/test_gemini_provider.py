from __future__ import annotations

import json
import unittest

import httpx

from neuro_code.domain.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPart,
    Message,
    Role,
    ToolCall,
)
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import (
    ModelCompleted,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.providers.gemini import GeminiProvider
from neuro_code.shared.errors import ProviderError


def _sse(*chunks: object) -> str:
    return "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)


class GeminiProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_structured_user_images_use_native_inline_and_file_parts(self) -> None:
        message = Message(
            Role.USER,
            content_parts=(
                ContentPart.from_text("before"),
                ContentPart.from_image("data:image/png;base64,aW1hZ2U="),
                ContentPart.from_image(
                    "https://generativelanguage.googleapis.com/v1beta/files/file-1"
                ),
            ),
        )

        _, converted = GeminiProvider._convert_messages((message,))

        self.assertEqual(
            converted[0]["parts"],
            [
                {"text": "before"},
                {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
                {
                    "fileData": {
                        "fileUri": ("https://generativelanguage.googleapis.com/v1beta/files/file-1")
                    }
                },
            ],
        )

    def test_public_url_invalid_and_tool_images_use_the_safe_text_projection(self) -> None:
        user = Message(
            Role.USER,
            content_parts=(
                ContentPart.from_image("https://example.com/screenshot.png"),
                ContentPart.from_image("data:image/png;base64,not-base64"),
            ),
        )
        tool = Message(
            Role.TOOL,
            name="read_file",
            tool_call_id="call-1",
            content_parts=(
                ContentPart.from_text("result"),
                ContentPart.from_image("data:image/png;base64,aW1hZ2U="),
            ),
        )

        _, converted = GeminiProvider._convert_messages((user, tool))

        self.assertEqual(
            converted[0]["parts"][:2],
            [{"text": IMAGE_MODEL_PLACEHOLDER}, {"text": IMAGE_MODEL_PLACEHOLDER}],
        )
        self.assertEqual(
            converted[0]["parts"][2]["functionResponse"]["response"],
            {"result": f"result\n{IMAGE_MODEL_PLACEHOLDER}"},
        )

    async def test_stream_converts_messages_and_preserves_provider_tool_metadata(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["api_key"] = request.headers.get("x-goog-api-key")
            captured["body"] = json.loads(request.content)
            body = _sse(
                [],
                {"usageMetadata": {"promptTokenCount": 8}},
                {"candidates": [None]},
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    None,
                                    {"text": "Answer "},
                                    {"text": "checking", "thought": True},
                                    {
                                        "functionCall": {
                                            "id": "provider-call-2",
                                            "name": "read_file",
                                            "args": {"path": "b.py"},
                                        },
                                        "thoughtSignature": "signature-new",
                                    },
                                    {
                                        "functionCall": {
                                            "name": "list_files",
                                            "args": {"path": "."},
                                        }
                                    },
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 6,
                    },
                },
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        provider = GeminiProvider(
            model="models/gemini/fixture",
            base_url="https://generativelanguage.invalid",
            api_key="secret-key",
            max_output_tokens=2048,
            transport=httpx.MockTransport(handler),
        )
        old_call = ToolCall(
            "local-old",
            "read_file",
            {"path": "a.py"},
            {
                "gemini.call_id": "provider-call-1",
                "gemini.thought_signature": "signature-old",
            },
        )
        messages = (
            Message(Role.SYSTEM, "Be precise."),
            Message(Role.USER, "Inspect it."),
            Message(Role.ASSISTANT, "Calling.", tool_calls=(old_call,)),
            Message(
                Role.TOOL,
                '{"content":"old contents"}',
                name="read_file",
                tool_call_id="local-old",
            ),
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

        self.assertEqual(provider.provider_name, "gemini")
        self.assertEqual(provider.model_name, "gemini/fixture")
        self.assertEqual(
            [type(event) for event in events],
            [ModelTextDelta, ModelReasoningDelta, ModelToolCall, ModelToolCall, ModelCompleted],
        )
        first_call = events[2]
        second_call = events[3]
        assert isinstance(first_call, ModelToolCall)
        assert isinstance(second_call, ModelToolCall)
        self.assertEqual(first_call.call.id, "provider-call-2")
        self.assertEqual(first_call.call.metadata["gemini.thought_signature"], "signature-new")
        self.assertEqual(first_call.call.metadata["gemini.call_id"], "provider-call-2")
        self.assertEqual(second_call.call.id, "gemini-call-1")
        completed = events[-1]
        assert isinstance(completed, ModelCompleted)
        self.assertEqual(
            (completed.stop_reason, completed.input_tokens, completed.output_tokens),
            ("stop", 10, 6),
        )
        self.assertIn(
            "/v1beta/models/gemini%2Ffixture:streamGenerateContent?alt=sse",
            captured["url"],
        )
        self.assertEqual(captured["api_key"], "secret-key")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 2048)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "Be precise.")
        old_part = body["contents"][1]["parts"][1]
        self.assertEqual(old_part["functionCall"]["id"], "provider-call-1")
        self.assertEqual(old_part["thoughtSignature"], "signature-old")
        response = body["contents"][2]["parts"][0]["functionResponse"]
        self.assertEqual(response["id"], "provider-call-1")
        self.assertEqual(response["response"], {"content": "old contents"})
        self.assertEqual(body["contents"][2]["parts"][1]["text"], "Continue.")
        self.assertEqual(body["tools"][0]["functionDeclarations"][0]["name"], "read_file")

    async def test_stream_rejects_http_json_provider_and_transport_failures(self) -> None:
        cases = (
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(403, text="denied must-not-leak")
                ),
                "HTTP 403",
            ),
            (
                httpx.MockTransport(lambda request: httpx.Response(200, text="data: {broken}\n\n")),
                "malformed streaming JSON",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, text=_sse({"error": {"message": "must-not-leak unavailable"}})
                    )
                ),
                "stream error",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse({"promptFeedback": {"blockReason": "SAFETY"}}),
                    )
                ),
                "blocked the prompt",
            ),
        )
        for transport, expected in cases:
            with self.subTest(expected=expected):
                provider = GeminiProvider(
                    model="model",
                    base_url="https://provider.invalid/v1beta",
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

        provider = GeminiProvider(
            model="model",
            base_url="https://provider.invalid/v1beta",
            api_key="key",
            transport=httpx.MockTransport(fail),
        )
        with self.assertRaisesRegex(ProviderError, "stream failed"):
            [
                event
                async for event in provider.stream(ModelContext((Message(Role.USER, "hi"),)), ())
            ]

    async def test_stream_rejects_invalid_function_calls(self) -> None:
        cases = (
            ({"name": "", "args": {}}, "incomplete tool call"),
            ({"name": "tool", "args": []}, "JSON object"),
        )
        for function_call, expected in cases:
            chunk = {"candidates": [{"content": {"parts": [{"functionCall": function_call}]}}]}
            provider = GeminiProvider(
                model="model",
                base_url="https://provider.invalid/v1beta",
                api_key="key",
                transport=httpx.MockTransport(
                    lambda request, value=chunk: httpx.Response(200, text=_sse(value))
                ),
            )
            with self.subTest(expected=expected), self.assertRaisesRegex(ProviderError, expected):
                [
                    event
                    async for event in provider.stream(
                        ModelContext((Message(Role.USER, "hi"),)), ()
                    )
                ]

    def test_message_validation_response_wrapping_and_endpoint_variants(self) -> None:
        with self.assertRaisesRegex(ProviderError, "tool name"):
            GeminiProvider._convert_messages((Message(Role.TOOL, "result"),))
        self.assertEqual(GeminiProvider._tool_response("plain"), {"result": "plain"})
        self.assertEqual(GeminiProvider._tool_response("[1, 2]"), {"result": [1, 2]})
        provider = GeminiProvider(
            model="model",
            base_url="https://provider.invalid/v1/",
            api_key="key",
        )
        self.assertEqual(
            provider._endpoint,
            "https://provider.invalid/v1/models/model:streamGenerateContent?alt=sse",
        )
        body = provider._request_body((Message(Role.USER, "hello"),), ())
        self.assertNotIn("systemInstruction", body)
        self.assertNotIn("tools", body)


if __name__ == "__main__":
    unittest.main()
