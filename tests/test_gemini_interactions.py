from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping
from typing import Any

import httpx

from neuro_code.application.ports.model import ModelCapability, ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.gemini_interactions import (
    GeminiInteractionsProvider,
)
from neuro_code.shared.errors import ConfigurationError, ProviderError


def _sse(*events: Mapping[str, object]) -> str:
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events) + "\n\n"


def _provider(
    transport: httpx.AsyncBaseTransport,
    *,
    builtin_tools: tuple[str, ...] = (),
    builtin_tool_options: Mapping[str, Mapping[str, object]] | None = None,
    tool_choice: str | Mapping[str, object] | None = None,
    affinity: str | None = "fixture-affinity",
    observer: Any | None = None,
) -> GeminiInteractionsProvider:
    return GeminiInteractionsProvider(
        model="gemini-3.6-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="fixture-secret",
        provider_name="gemini-profile",
        service_id="google-ai-studio",
        context_affinity=affinity,
        builtin_tools=builtin_tools,
        builtin_tool_options=builtin_tool_options,
        tool_choice=tool_choice,
        response_observer=observer,
        transport=transport,
    )


class GeminiInteractionsCapabilityTests(unittest.TestCase):
    def test_capabilities_are_model_and_builtin_specific(self) -> None:
        supported = GeminiInteractionsProvider.implementation_capabilities(
            model="gemini-3.6-flash",
            builtin_tools=("google_search", "url_context"),
        )
        self.assertTrue(supported.supports(ModelCapability.HOSTED_WEB_SEARCH))
        self.assertTrue(supported.supports(ModelCapability.HOSTED_WEB_FETCH))
        self.assertTrue(supported.supports(ModelCapability.MIXED_HOSTED_AND_CLIENT_TOOLS))

        unknown_model = GeminiInteractionsProvider.implementation_capabilities(
            model="gemini-future-unknown",
            builtin_tools=("google_search", "url_context"),
        )
        self.assertFalse(unknown_model.supports(ModelCapability.HOSTED_WEB_SEARCH))
        self.assertFalse(unknown_model.supports(ModelCapability.HOSTED_WEB_FETCH))
        self.assertFalse(unknown_model.supports(ModelCapability.MIXED_HOSTED_AND_CLIENT_TOOLS))


class GeminiInteractionsProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_invalid_builtin_and_tool_choice_configuration(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=""))
        invalid_values: tuple[tuple[dict[str, object], str], ...] = (
            ({"builtin_tools": ("google_search", "google_search")}, "duplicates"),
            ({"builtin_tools": ("unsupported",)}, "unsupported"),
            ({"tool_choice": "required"}, "unsupported Gemini Interactions tool_choice"),
            ({"tool_choice": object()}, "must be a string or mapping"),
            ({"tool_choice": {"mode": "any"}}, "requires allowed_tools"),
            (
                {"tool_choice": {"allowed_tools": {"mode": "required", "tools": ["x"]}}},
                "allowed_tools is invalid",
            ),
            (
                {"tool_choice": {"allowed_tools": {"mode": "any", "tools": [""]}}},
                "allowed_tools is invalid",
            ),
        )
        for kwargs, expected in invalid_values:
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(ConfigurationError, expected),
            ):
                _provider(transport, **kwargs)  # type: ignore[arg-type]

    def test_content_projection_and_disabled_tools_are_safe(self) -> None:
        provider = _provider(
            httpx.MockTransport(lambda request: httpx.Response(200, text="")),
            builtin_tools=("google_search",),
            builtin_tool_options={"google_search": {"config": {"mode": "dynamic"}}},
        )
        user = Message(
            Role.USER,
            content_parts=(
                ContentPart.from_text("look"),
                ContentPart.from_image("data:image/png;base64,aW1hZ2U="),
                ContentPart.from_image(
                    "https://generativelanguage.googleapis.com/v1beta/files/file-1"
                ),
                ContentPart.from_image("https://example.com/not-a-gemini-file.png"),
            ),
        )
        tools = (
            ToolDefinition("google_search", "shadowed", {"type": "object"}),
            ToolDefinition("read_file", "Read", {"type": "object"}),
        )
        body = provider._request_body(
            ModelContext((Message(Role.SYSTEM, "first"), Message(Role.SYSTEM, "second"), user)),
            tools,
        )
        self.assertEqual(body["system_instruction"], "first\n\nsecond")
        self.assertEqual(
            body["input"][0]["content"],
            [
                {"type": "text", "text": "look"},
                {"type": "image", "data": "aW1hZ2U=", "mime_type": "image/png"},
                {
                    "type": "image",
                    "uri": "https://generativelanguage.googleapis.com/v1beta/files/file-1",
                },
                {
                    "type": "text",
                    "text": "[image content preserved in session; binary replay is unavailable]",
                },
            ],
        )
        self.assertEqual(
            body["tools"],
            [
                {"type": "google_search", "config": {"mode": "dynamic"}},
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read",
                    "parameters": {"type": "object"},
                },
            ],
        )
        disabled = provider._request_body(
            ModelContext((user,)), tools, tool_policy=ModelToolPolicy.DISABLED
        )
        self.assertNotIn("tools", disabled)
        self.assertNotIn("tool_choice", disabled["generation_config"])
        self.assertEqual(GeminiInteractionsProvider._function_result('{"ok": true}'), {"ok": True})
        self.assertEqual(
            GeminiInteractionsProvider._function_result("plain"),
            [{"type": "text", "text": "plain"}],
        )

    def test_native_replay_filters_mismatched_or_malformed_context(self) -> None:
        provider = _provider(httpx.MockTransport(lambda request: httpx.Response(200, text="")))
        wrong_kind = PreservedContextItem(
            ContextItemKind.BACKEND_TOOL_CALL,
            {"type": "backend_tool_call", "kind": "not-a-mapping"},
        )
        wrong_identity = PreservedContextItem(
            ContextItemKind.BACKEND_TOOL_CALL,
            {
                "type": "backend_tool_call",
                "kind": {
                    "provider": "other",
                    "service": "google-ai-studio",
                    "protocol": "gemini-interactions",
                    "model": "gemini-3.6-flash",
                    "native_type": "gemini_interactions_steps",
                    "steps": [],
                },
            },
        )
        malformed_steps = PreservedContextItem(
            ContextItemKind.BACKEND_TOOL_CALL,
            {
                "type": "backend_tool_call",
                "kind": {
                    "provider": "gemini-profile",
                    "service": "google-ai-studio",
                    "protocol": "gemini-interactions",
                    "model": "gemini-3.6-flash",
                    "native_type": "gemini_interactions_steps",
                    "steps": "not-a-list",
                },
            },
        )
        self.assertIsNone(provider._native_steps(wrong_kind))
        self.assertIsNone(provider._native_steps(wrong_identity))
        self.assertIsNone(provider._native_steps(malformed_steps))

        with self.assertRaisesRegex(ProviderError, "tool results require a tool name"):
            provider._request_body(
                ModelContext((Message(Role.TOOL, "result", tool_call_id="call-1"),)), ()
            )
        with self.assertRaisesRegex(ProviderError, "function result requires call_id"):
            provider._request_body(ModelContext((Message(Role.TOOL, "result", name="read"),)), ())

    async def test_stream_rejects_http_protocol_and_terminal_failures_without_secrets(self) -> None:
        cases = (
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(400, text="fixture-secret denied")
                ),
                "HTTP 400",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse(
                            {
                                "event_type": "error",
                                "error": {"message": "fixture-secret unavailable"},
                            }
                        ),
                    )
                ),
                "stream error",
            ),
            (
                httpx.MockTransport(lambda request: httpx.Response(200, text="data: {broken}\n\n")),
                "malformed streaming JSON",
            ),
            (
                httpx.MockTransport(lambda request: httpx.Response(200, text="data: []\n\n")),
                "non-object streaming event",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text="event: ignored\n\ndata: [DONE]\n\n"
                        + _sse({"event_type": "interaction.created", "interaction": {"id": "x"}}),
                    )
                ),
                "without a terminal interaction",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse({"event_type": "interaction.completed"}),
                    )
                ),
                "terminal event omitted interaction",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse(
                            {
                                "event_type": "interaction.completed",
                                "interaction": {"id": "x", "status": "completed"},
                            }
                        ),
                    )
                ),
                "terminal interaction omitted steps",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse({"event_type": "interaction.status_update", "status": "failed"}),
                    )
                ),
                "failed with status failed",
            ),
            (
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        text=_sse(
                            {
                                "event_type": "interaction.completed",
                                "interaction": {"id": "x", "status": "failed"},
                            }
                        ),
                    )
                ),
                "failed with status failed",
            ),
        )
        for transport, expected in cases:
            with self.subTest(expected=expected):
                provider = _provider(transport)
                with self.assertRaisesRegex(ProviderError, expected) as raised:
                    [
                        event
                        async for event in provider.stream(
                            ModelContext((Message(Role.USER, "test"),)), ()
                        )
                    ]
                self.assertNotIn("fixture-secret", str(raised.exception))

    async def test_stream_rejects_invalid_step_lifecycle_shapes(self) -> None:
        cases = (
            (
                _sse({"event_type": "step.start", "index": -1, "step": {"type": "thought"}}),
                "invalid step index",
            ),
            (
                _sse({"event_type": "step.start", "index": 0, "step": "bad"}),
                "invalid step.start",
            ),
            (
                _sse({"event_type": "step.delta", "index": 0, "delta": {"type": "text"}}),
                "invalid step.delta",
            ),
            (
                _sse(
                    {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
                    {"event_type": "step.delta", "index": 0, "delta": {"type": "arguments"}},
                ),
                "arguments delta is invalid",
            ),
            (
                _sse({"event_type": "step.stop", "index": 0}),
                "invalid step.stop",
            ),
            (
                _sse(
                    {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
                    {"event_type": "step.start", "index": 0, "step": {"type": "model_output"}},
                ),
                "conflicting step.start",
            ),
        )
        for stream_text, expected in cases:
            with self.subTest(expected=expected):
                provider = _provider(
                    httpx.MockTransport(
                        lambda request, value=stream_text: httpx.Response(200, text=value)
                    )
                )
                with self.assertRaisesRegex(ProviderError, expected):
                    [
                        event
                        async for event in provider.stream(
                            ModelContext((Message(Role.USER, "test"),)), ()
                        )
                    ]

    async def test_stream_merges_deltas_and_projects_terminal_steps_and_usage(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {"event_type": "step.start", "index": 0, "step": {"type": "model_output"}},
                    {
                        "event_type": "step.delta",
                        "index": 0,
                        "delta": {
                            "type": "text",
                            "text": "a",
                            "annotations": [{"type": "url_citation", "url": "https://a.example"}],
                        },
                    },
                    {
                        "event_type": "step.delta",
                        "index": 0,
                        "delta": {
                            "type": "text",
                            "text": "b",
                            "annotations": [{"type": "url_citation", "url": "https://b.example"}],
                        },
                    },
                    {"event_type": "step.delta", "index": 0, "delta": {"type": "metadata", "x": 1}},
                    {"event_type": "step.start", "index": 1, "step": {"type": "thought"}},
                    {
                        "event_type": "step.delta",
                        "index": 1,
                        "delta": {"type": "thought", "text": "x"},
                    },
                    {
                        "event_type": "step.delta",
                        "index": 1,
                        "delta": {"type": "thought", "text": "y"},
                    },
                    {"event_type": "step.stop", "index": 0},
                    {"event_type": "step.stop", "index": 1},
                    {
                        "event_type": "interaction.completed",
                        "interaction": {
                            "id": "merged",
                            "status": "completed",
                            "usage": {"total_input_tokens": 3, "total_output_tokens": 4},
                        },
                    },
                ),
                headers={"content-type": "text/event-stream"},
            )

        events = [
            event
            async for event in _provider(httpx.MockTransport(handler)).stream(
                ModelContext((Message(Role.USER, "merge"),)), ()
            )
        ]
        self.assertEqual(
            [type(event) for event in events],
            [
                ModelTextDelta,
                ModelTextDelta,
                ModelReasoningDelta,
                ModelReasoningDelta,
                ModelTextDelta,
                ModelCompleted,
            ],
        )
        completion = events[-1]
        assert isinstance(completion, ModelCompleted)
        self.assertIn("ab", completion.response_text or "")
        self.assertIn("https://a.example", completion.response_text or "")
        self.assertIn("https://b.example", completion.response_text or "")

        terminal_only = _provider(
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=_sse(
                        {
                            "event_type": "interaction.completed",
                            "interaction": {
                                "id": "terminal-only",
                                "status": "completed",
                                "steps": [
                                    {
                                        "type": "thought",
                                        "summary": [{"type": "text", "text": "reason"}],
                                    },
                                    {
                                        "type": "model_output",
                                        "content": [{"type": "text", "text": "answer"}],
                                    },
                                ],
                                "usage": {
                                    "input_tokens": 7,
                                    "completion_tokens": 8,
                                    "cached_tokens": 2,
                                },
                            },
                        }
                    ),
                )
            )
        )
        terminal_events = [
            event
            async for event in terminal_only.stream(
                ModelContext((Message(Role.USER, "terminal"),)), ()
            )
        ]
        self.assertEqual(
            [type(event) for event in terminal_events],
            [ModelTextDelta, ModelReasoningDelta, ModelCompleted],
        )
        terminal_completion = terminal_events[-1]
        assert isinstance(terminal_completion, ModelCompleted)
        self.assertEqual(terminal_completion.input_tokens, 7)
        self.assertEqual(terminal_completion.output_tokens, 8)
        assert terminal_completion.usage is not None
        self.assertEqual(terminal_completion.usage.cache_read_tokens, 2)

    async def test_function_call_validation_and_native_context_limits(self) -> None:
        def response_for(step: Mapping[str, object]) -> httpx.Response:
            return httpx.Response(
                200,
                text=_sse(
                    {"event_type": "step.start", "index": 0, "step": step},
                    {"event_type": "step.stop", "index": 0},
                ),
            )

        for step, expected in (
            ({"type": "function_call", "id": "call"}, "incomplete function call"),
            ({"type": "function_call", "name": "read"}, "omitted id"),
            (
                {"type": "function_call", "id": "call", "name": "read", "arguments": []},
                "arguments must be a JSON object",
            ),
        ):
            with self.subTest(expected=expected):
                provider = _provider(
                    httpx.MockTransport(lambda request, value=step: response_for(value))
                )
                with self.assertRaisesRegex(ProviderError, expected):
                    [
                        event
                        async for event in provider.stream(
                            ModelContext((Message(Role.USER, "call"),)), ()
                        )
                    ]

        provider = _provider(
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=_sse(
                        {
                            "event_type": "step.start",
                            "index": 0,
                            "step": {
                                "type": "function_call",
                                "id": "step-id",
                                "call_id": "canonical-id",
                                "name": "read",
                                "arguments": '{"path":',
                            },
                        },
                        {
                            "event_type": "step.delta",
                            "index": 0,
                            "delta": {"type": "arguments_delta", "arguments": '"a.py"}'},
                        },
                        {"event_type": "step.stop", "index": 0},
                        {
                            "event_type": "interaction.completed",
                            "interaction": {
                                "id": "function",
                                "status": "completed",
                            },
                        },
                    ),
                )
            )
        )
        events = [
            event
            async for event in provider.stream(ModelContext((Message(Role.USER, "call"),)), ())
        ]
        call = next(event for event in events if isinstance(event, ModelToolCall))
        self.assertEqual(call.call.id, "canonical-id")
        self.assertEqual(call.call.metadata["gemini.step_id"], "step-id")

        with self.assertRaisesRegex(ProviderError, "exceeds its size limit"):
            GeminiInteractionsProvider._native_context_item(
                provider="p",
                service="s",
                model="m",
                steps=[{"type": "model_output", "text": "x" * 1_100_000}],
            )
        with self.assertRaisesRegex(ProviderError, "not JSON-safe"):
            GeminiInteractionsProvider._native_context_item(
                provider="p", service="s", model="m", steps=[{"value": object()}]
            )

    async def test_observer_and_transport_failures_are_wrapped(self) -> None:
        response = _sse(
            {
                "event_type": "interaction.completed",
                "interaction": {
                    "id": "observe",
                    "status": "completed",
                    "steps": [{"type": "model_output", "content": []}],
                },
            }
        )
        provider = _provider(
            httpx.MockTransport(lambda request: httpx.Response(200, text=response)),
            observer=lambda value: (_ for _ in ()).throw(RuntimeError("observer broke")),
        )
        with self.assertRaisesRegex(ProviderError, "response observer failed: RuntimeError"):
            [
                event
                async for event in provider.stream(
                    ModelContext((Message(Role.USER, "observe"),)), ()
                )
            ]

        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        provider = _provider(httpx.MockTransport(fail))
        with self.assertRaisesRegex(ProviderError, "stream failed: ConnectError"):
            [
                event
                async for event in provider.stream(
                    ModelContext((Message(Role.USER, "offline"),)), ()
                )
            ]

    async def test_request_is_stateless_and_uses_stable_v1_endpoint(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "interaction.completed",
                        "interaction": {
                            "id": "int-1",
                            "status": "completed",
                            "steps": [
                                {
                                    "type": "model_output",
                                    "content": [{"type": "text", "text": "ok"}],
                                }
                            ],
                        },
                    }
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = _provider(
            httpx.MockTransport(handler),
            builtin_tools=("google_search", "url_context"),
            tool_choice={"allowed_tools": {"mode": "any", "tools": ["google_search"]}},
        )
        context = ModelContext(
            (
                Message(Role.SYSTEM, "Be precise."),
                Message(Role.USER, "Find current facts."),
            )
        )
        tools = (
            ToolDefinition(
                "read_file",
                "Read a file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        )

        events = [event async for event in provider.stream(context, tools)]

        self.assertEqual(
            captured["url"], "https://generativelanguage.googleapis.com/v1/interactions"
        )
        body = captured["body"]
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertNotIn("previous_interaction_id", body)
        self.assertEqual(body["system_instruction"], "Be precise.")
        self.assertEqual(
            [tool["type"] for tool in body["tools"]],
            ["google_search", "url_context", "function"],
        )
        self.assertEqual(
            body["generation_config"]["tool_choice"],
            {"allowed_tools": {"mode": "any", "tools": ["google_search"]}},
        )
        self.assertIsInstance(events[-1], ModelCompleted)

    def test_mixed_builtin_and_client_tools_use_validated_tool_choice_by_default(self) -> None:
        provider = _provider(
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=_sse(
                        {
                            "event_type": "interaction.completed",
                            "interaction": {
                                "id": "int-mixed",
                                "status": "completed",
                                "steps": [
                                    {
                                        "type": "model_output",
                                        "content": [{"type": "text", "text": "ok"}],
                                    }
                                ],
                            },
                        }
                    ),
                    headers={"content-type": "text/event-stream"},
                )
            ),
            builtin_tools=("google_search",),
        )
        body = provider._request_body(
            ModelContext((Message(Role.USER, "search then inspect"),)),
            (ToolDefinition("read_file", "Read", {"type": "object"}),),
        )
        self.assertEqual(body["generation_config"]["tool_choice"], "validated")

    async def test_stream_maps_native_steps_and_replays_exactly(self) -> None:
        captured: dict[str, Any] = {}
        observer_values: list[Mapping[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                text=_sse(
                    {"event_type": "interaction.created", "interaction": {"id": "int-2"}},
                    {
                        "event_type": "interaction.in_progress",
                        "interaction": {"id": "int-2", "status": "in_progress"},
                    },
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {"type": "thought"},
                    },
                    {
                        "event_type": "step.delta",
                        "index": 0,
                        "delta": {"type": "thought", "text": "I need a file."},
                    },
                    {
                        "event_type": "step.delta",
                        "index": 0,
                        "delta": {"type": "thought_signature", "signature": "sig-0"},
                    },
                    {"event_type": "step.stop", "index": 0},
                    {
                        "event_type": "step.start",
                        "index": 1,
                        "step": {
                            "type": "function_call",
                            "id": "fc-1",
                            "name": "read_file",
                            "signature": "sig-fc-1",
                        },
                    },
                    {
                        "event_type": "step.delta",
                        "index": 1,
                        "delta": {"type": "arguments", "partial_arguments": '{"path":'},
                    },
                    {
                        "event_type": "step.delta",
                        "index": 1,
                        "delta": {"type": "arguments", "partial_arguments": ' "a.py"}'},
                    },
                    {"event_type": "step.stop", "index": 1},
                    {
                        "event_type": "interaction.requires_action",
                        "interaction": {"id": "int-2", "status": "requires_action"},
                    },
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = _provider(
            httpx.MockTransport(handler),
            observer=observer_values.append,
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "Inspect a.py"),)),
                (ToolDefinition("read_file", "Read", {"type": "object"}),),
            )
        ]
        self.assertEqual(
            [type(event) for event in events], [ModelReasoningDelta, ModelToolCall, ModelCompleted]
        )
        call = events[1]
        assert isinstance(call, ModelToolCall)
        self.assertEqual(call.call.id, "fc-1")
        self.assertEqual(call.call.arguments, {"path": "a.py"})
        completion = events[-1]
        assert isinstance(completion, ModelCompleted)
        self.assertEqual(completion.stop_reason, "tool_calls")
        self.assertEqual(len(completion.context_items), 1)
        self.assertEqual(observer_values[0]["status"], "requires_action")
        native_payload = completion.context_items[0].to_dict()
        self.assertEqual(native_payload["kind"]["steps"][0]["signature"], "sig-0")

        native_item = completion.context_items[0]
        self.assertIsInstance(native_item, PreservedContextItem)
        replay_context = ModelContext(
            (
                Message(Role.USER, "Inspect a.py"),
                native_item,
                Message(Role.ASSISTANT, "", tool_calls=(call.call,)),
                Message(Role.TOOL, "contents", name="read_file", tool_call_id="fc-1"),
            ),
            source_provider="gemini-profile",
            source_model="gemini-3.6-flash",
            source_context_affinity="fixture-affinity",
        )
        body = provider._request_body(replay_context, ())
        input_items = body["input"]
        self.assertEqual(
            [item["type"] for item in input_items],
            ["user_input", "thought", "function_call", "function_result"],
        )
        self.assertEqual(input_items[2]["id"], "fc-1")
        self.assertEqual(input_items[1]["signature"], "sig-0")
        self.assertEqual(input_items[2]["signature"], "sig-fc-1")
        self.assertEqual(input_items[3]["call_id"], "fc-1")
        self.assertEqual(input_items[3]["signature"], "sig-fc-1")

    async def test_search_lifecycle_and_annotations_ignore_html_suggestions(self) -> None:
        observed: list[Mapping[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {"type": "google_search_call", "id": "search-1"},
                    },
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {"type": "google_search_call", "id": "search-1"},
                    },
                    {"event_type": "step.stop", "index": 0},
                    {"event_type": "step.stop", "index": 0},
                    {
                        "event_type": "step.start",
                        "index": 1,
                        "step": {
                            "type": "google_search_result",
                            "call_id": "search-1",
                            "result": [{"search_suggestions": "<html>secret widget</html>"}],
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
                            "text": "Current answer.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://docs.example.com/current",
                                    "title": "Current docs",
                                    "start_index": 0,
                                    "end_index": 14,
                                }
                            ],
                        },
                    },
                    {"event_type": "step.stop", "index": 2},
                    {
                        "event_type": "interaction.completed",
                        "interaction": {
                            "id": "int-3",
                            "status": "completed",
                            "usage": {
                                "total_input_tokens": 12,
                                "total_output_tokens": 8,
                            },
                        },
                    },
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = _provider(
            httpx.MockTransport(handler),
            builtin_tools=("google_search",),
            observer=observed.append,
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "What is current?"),)), ()
            )
        ]
        self.assertEqual(
            [
                (
                    type(event),
                    getattr(event, "name", None),
                    isinstance(event, ModelBackendToolCompleted),
                )
                for event in events
            ],
            [
                (ModelBackendToolStarted, "google_search", False),
                (ModelBackendToolCompleted, "google_search", True),
                (ModelTextDelta, None, False),
                (ModelTextDelta, None, False),
                (ModelCompleted, None, False),
            ],
        )
        completion = events[-1]
        assert isinstance(completion, ModelCompleted)
        self.assertIn("https://docs.example.com/current", completion.response_text or "")
        self.assertEqual(completion.usage.input_tokens, 12)  # type: ignore[union-attr]
        self.assertNotIn("secret widget", completion.response_text or "")
        self.assertIn("search_suggestions", json.dumps(observed[0]))

    async def test_mixed_search_and_client_function_flow_uses_canonical_events(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {"type": "google_search_call", "id": "search-mixed"},
                    },
                    {"event_type": "step.stop", "index": 0},
                    {
                        "event_type": "step.start",
                        "index": 1,
                        "step": {
                            "type": "google_search_result",
                            "call_id": "search-mixed",
                            "status": "success",
                        },
                    },
                    {"event_type": "step.stop", "index": 1},
                    {
                        "event_type": "step.start",
                        "index": 2,
                        "step": {
                            "type": "function_call",
                            "id": "function-mixed",
                            "name": "read_file",
                        },
                    },
                    {
                        "event_type": "step.delta",
                        "index": 2,
                        "delta": {"type": "arguments", "partial_arguments": '{"path":"a.py"}'},
                    },
                    {"event_type": "step.stop", "index": 2},
                    {
                        "event_type": "step.start",
                        "index": 3,
                        "step": {"type": "model_output"},
                    },
                    {
                        "event_type": "step.delta",
                        "index": 3,
                        "delta": {"type": "text", "text": "done"},
                    },
                    {"event_type": "step.stop", "index": 3},
                    {
                        "event_type": "interaction.completed",
                        "interaction": {"id": "mixed", "status": "completed"},
                    },
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = _provider(
            httpx.MockTransport(handler),
            builtin_tools=("google_search",),
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "search and inspect"),)),
                (ToolDefinition("read_file", "Read", {"type": "object"}),),
            )
        ]
        self.assertEqual(
            [type(event) for event in events],
            [
                ModelBackendToolStarted,
                ModelBackendToolCompleted,
                ModelToolCall,
                ModelTextDelta,
                ModelCompleted,
            ],
        )
        call = events[2]
        assert isinstance(call, ModelToolCall)
        self.assertEqual(call.call.name, "read_file")
        self.assertEqual(call.call.arguments, {"path": "a.py"})
        completion = events[-1]
        assert isinstance(completion, ModelCompleted)
        self.assertEqual(completion.stop_reason, "tool_calls")
        self.assertEqual(completion.response_text, "done")

    async def test_cancellation_propagates_without_provider_error_wrapping(self) -> None:
        def cancel(request: httpx.Request) -> httpx.Response:
            del request
            raise asyncio.CancelledError

        provider = _provider(httpx.MockTransport(cancel))
        with self.assertRaises(asyncio.CancelledError):
            [
                event
                async for event in provider.stream(
                    ModelContext((Message(Role.USER, "cancel"),)), ()
                )
            ]

    async def test_malformed_function_arguments_fail_closed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {"type": "function_call", "id": "fc-2", "name": "read"},
                    },
                    {
                        "event_type": "step.delta",
                        "index": 0,
                        "delta": {"type": "arguments", "partial_arguments": "{broken"},
                    },
                    {"event_type": "step.stop", "index": 0},
                ),
            )

        provider = _provider(httpx.MockTransport(handler))
        with self.assertRaisesRegex(ProviderError, "invalid JSON arguments"):
            [
                event
                async for event in provider.stream(ModelContext((Message(Role.USER, "read"),)), ())
            ]

    async def test_url_context_unsafe_result_does_not_emit_completed_lifecycle(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {"type": "url_context_call", "id": "url-1"},
                    },
                    {"event_type": "step.stop", "index": 0},
                    {
                        "event_type": "step.start",
                        "index": 1,
                        "step": {
                            "type": "url_context_result",
                            "call_id": "url-1",
                            "status": "unsafe",
                        },
                    },
                    {"event_type": "step.stop", "index": 1},
                    {
                        "event_type": "interaction.completed",
                        "interaction": {
                            "id": "int-4",
                            "status": "completed",
                            "steps": [
                                {"type": "url_context_call", "id": "url-1"},
                                {
                                    "type": "url_context_result",
                                    "call_id": "url-1",
                                    "status": "unsafe",
                                },
                            ],
                        },
                    },
                ),
            )

        provider = _provider(
            httpx.MockTransport(handler),
            builtin_tools=("url_context",),
        )
        events = [
            event
            async for event in provider.stream(ModelContext((Message(Role.USER, "read URL"),)), ())
        ]
        self.assertEqual(
            [(type(event), isinstance(event, ModelBackendToolCompleted)) for event in events],
            [(ModelBackendToolStarted, False), (ModelCompleted, False)],
        )

    async def test_url_context_success_preserves_retrieved_url_and_citation(self) -> None:
        observed: list[Mapping[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "event_type": "step.start",
                        "index": 0,
                        "step": {
                            "type": "url_context_call",
                            "id": "url-2",
                            "arguments": {"urls": ["https://docs.example.com/page"]},
                            "signature": "url-call-signature",
                        },
                    },
                    {"event_type": "step.stop", "index": 0},
                    {
                        "event_type": "step.start",
                        "index": 1,
                        "step": {
                            "type": "url_context_result",
                            "call_id": "url-2",
                            "status": "success",
                            "url": "https://docs.example.com/page",
                            "result": [
                                {
                                    "url": "https://docs.example.com/page",
                                    "title": "Example page",
                                }
                            ],
                            "signature": "url-result-signature",
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
                            "text": "Retrieved page evidence.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://docs.example.com/page",
                                    "title": "Example page",
                                    "start_index": 0,
                                    "end_index": 24,
                                }
                            ],
                        },
                    },
                    {"event_type": "step.stop", "index": 2},
                    {
                        "event_type": "interaction.completed",
                        "interaction": {
                            "id": "int-url-success",
                            "status": "completed",
                            "usage": {"total_input_tokens": 5, "total_output_tokens": 6},
                        },
                    },
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = _provider(
            httpx.MockTransport(handler),
            builtin_tools=("url_context",),
            observer=observed.append,
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "read the page"),)), ()
            )
        ]
        self.assertEqual(
            [
                (event.name, isinstance(event, ModelBackendToolCompleted))
                for event in events
                if hasattr(event, "name")
            ],
            [("url_context", False), ("url_context", True)],
        )
        completion = events[-1]
        assert isinstance(completion, ModelCompleted)
        self.assertIn("https://docs.example.com/page", completion.response_text or "")
        self.assertEqual(observed[0]["steps"][1]["url"], "https://docs.example.com/page")  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
