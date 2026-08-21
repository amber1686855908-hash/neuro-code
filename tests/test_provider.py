from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import httpx

from neuro_code.application.ports.model import (
    CapabilityStatus,
    ModelCapability,
    ModelToolPolicy,
)
from neuro_code.configuration.app import AppConfig, ProviderProfile
from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
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
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers import create_provider, create_routed_provider
from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
from neuro_code.infrastructure.providers.failover import (
    FailoverModelProvider,
    ProviderCandidate,
)
from neuro_code.infrastructure.providers.gemini import GeminiProvider
from neuro_code.infrastructure.providers.gemini_interactions import (
    GeminiInteractionsProvider,
)
from neuro_code.infrastructure.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _ToolCallBuffer,
)
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.infrastructure.providers.resilience import ResilientModelProvider
from neuro_code.shared.errors import ConfigurationError, ProviderError


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

        kimi_legacy = OpenAICompatibleProvider(
            model="kimi-k2.5",
            base_url="https://api.moonshot.ai/v1",
            api_key="fixture",
            dialect="kimi",
        )
        self.assertNotIn("reasoning_content", kimi_legacy._message_payload(tool_turn))

    def test_china_dialects_replay_reasoning_and_map_official_request_fields(self) -> None:
        context = ModelContext(
            (
                Message(Role.USER, "continue"),
                Message(
                    Role.ASSISTANT,
                    "tool result pending",
                    reasoning_content="preserve this reasoning",
                    tool_calls=(ToolCall("call-1", "lookup", {"value": 1}),),
                ),
            ),
            reasoning_effort=ReasoningEffort.XHIGH,
        )
        tool = ToolDefinition(
            "lookup",
            "Look up a fixture value.",
            {"type": "object", "properties": {"value": {"type": "integer"}}},
        )

        kimi = OpenAICompatibleProvider(
            model="kimi-k3",
            base_url="https://api.moonshot.ai/v1",
            api_key="fixture",
            dialect="kimi",
        )
        kimi_body = kimi._request_body(context, (tool,))
        self.assertEqual(kimi_body["reasoning_effort"], "max")
        self.assertEqual(kimi_body["messages"][1]["reasoning_content"], "preserve this reasoning")

        max_context = ModelContext(
            context.items,
            reasoning_effort=ReasoningEffort.MAX,
        )
        self.assertEqual(kimi._request_body(max_context, (tool,))["reasoning_effort"], "max")

        glm = OpenAICompatibleProvider(
            model="glm-5.3",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key="fixture",
            dialect="glm",
        )
        glm_body = glm._request_body(context, (tool,))
        self.assertEqual(glm_body["thinking"], {"type": "enabled", "clear_thinking": False})
        self.assertEqual(glm_body["reasoning_effort"], "max")

        minimax = OpenAICompatibleProvider(
            model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1",
            api_key="fixture",
            provider_name="minimax-profile",
            dialect="minimax",
            context_affinity="profile-v1:minimax",
        )
        minimax_context = ModelContext(
            context.items,
            source_provider="minimax-profile",
            source_model="MiniMax-M3",
            source_context_affinity="profile-v1:minimax",
            reasoning_effort=ReasoningEffort.XHIGH,
        )
        minimax_body = minimax._request_body(minimax_context, (tool,))
        self.assertNotIn("max_tokens", minimax_body)
        self.assertEqual(minimax_body["max_completion_tokens"], 8192)
        self.assertTrue(minimax_body["reasoning_split"])
        self.assertEqual(
            minimax_body["messages"][1]["reasoning_content"], "preserve this reasoning"
        )

        native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "native": {
                    "type": "openai-chat-reasoning-details",
                    "provider": "minimax-profile",
                    "protocol": "openai-chat",
                    "model": "MiniMax-M3",
                    "details": [{"type": "reasoning.text", "text": "structured reasoning"}],
                },
            },
        )
        structured_body = minimax._request_body(
            ModelContext(
                (
                    Message(Role.USER, "continue"),
                    native,
                    Message(
                        Role.ASSISTANT,
                        "tool result pending",
                        reasoning_content="structured reasoning",
                        tool_calls=(ToolCall("call-1", "lookup", {"value": 1}),),
                    ),
                ),
                source_provider="minimax-profile",
                source_model="MiniMax-M3",
                source_context_affinity="profile-v1:minimax",
            ),
            (tool,),
        )
        self.assertEqual(
            structured_body["messages"][1]["reasoning_details"],
            [{"type": "reasoning.text", "text": "structured reasoning"}],
        )

    def test_kimi_specific_tool_choice_fails_closed_without_disabling_thinking(self) -> None:
        provider = OpenAICompatibleProvider(
            model="kimi-k2.6",
            base_url="https://api.moonshot.ai/v1",
            api_key="fixture",
            dialect="kimi",
            tool_choice={"type": "function", "function": {"name": "lookup"}},
        )
        tool = ToolDefinition("lookup", "Lookup", {"type": "object"})
        with self.assertRaisesRegex(ConfigurationError, "incompatible with thinking"):
            provider._request_body(ModelContext((Message(Role.USER, "lookup"),)), (tool,))

    async def test_kimi_and_glm_stream_fixtures_replay_tools_reasoning_and_usage(self) -> None:
        tool = ToolDefinition("lookup", "Lookup a fixture value.", {"type": "object"})
        for dialect, model, base_url in (
            ("kimi", "kimi-k2.6", "https://api.moonshot.ai/v1"),
            ("glm", "glm-5.3", "https://open.bigmodel.cn/api/paas/v4"),
        ):
            with self.subTest(dialect=dialect):
                captured: dict[str, object] = {}

                def handler(
                    request: httpx.Request, captured: dict[str, object] = captured
                ) -> httpx.Response:
                    captured["body"] = json.loads(request.content)
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        text=_sse(
                            {"choices": [{"delta": {"reasoning_content": "plan"}}]},
                            {
                                "choices": [
                                    {
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": 0,
                                                    "id": "call-",
                                                    "function": {
                                                        "name": "look",
                                                        "arguments": '{"value":',
                                                    },
                                                }
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                "choices": [
                                    {
                                        "finish_reason": "tool_calls",
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": 0,
                                                    "id": "1",
                                                    "function": {
                                                        "name": "up",
                                                        "arguments": "1}",
                                                    },
                                                }
                                            ]
                                        },
                                    }
                                ],
                                "usage": {
                                    "prompt_tokens": 12,
                                    "completion_tokens": 4,
                                    "prompt_tokens_details": {"cached_tokens": 3},
                                },
                            },
                        ),
                    )

                provider = OpenAICompatibleProvider(
                    model=model,
                    base_url=base_url,
                    api_key="fixture",
                    dialect=dialect,
                    transport=httpx.MockTransport(handler),
                )
                context = ModelContext(
                    (
                        Message(Role.USER, "lookup"),
                        Message(
                            Role.ASSISTANT,
                            reasoning_content="prior plan",
                            tool_calls=(ToolCall("old", "lookup", {"value": 0}),),
                        ),
                        Message(Role.TOOL, "fixture result", tool_call_id="old"),
                    )
                )
                events = [event async for event in provider.stream(context, (tool,))]

                reasoning = [
                    event.text for event in events if isinstance(event, ModelReasoningDelta)
                ]
                self.assertEqual(reasoning, ["plan"])
                tool_event = next(event for event in events if isinstance(event, ModelToolCall))
                self.assertEqual(tool_event.call, ToolCall("call-1", "lookup", {"value": 1}))
                completion = next(event for event in events if isinstance(event, ModelCompleted))
                assert completion.usage is not None
                self.assertEqual(completion.usage.cache_read_tokens, 3)
                body = captured["body"]
                assert isinstance(body, dict)
                self.assertEqual(body["messages"][1]["reasoning_content"], "prior plan")
                self.assertEqual(body["messages"][2]["tool_call_id"], "old")

    def test_glm_tool_choice_is_limited_to_official_auto_contract(self) -> None:
        provider = OpenAICompatibleProvider(
            model="glm-5.3",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key="fixture",
            dialect="glm",
            tool_choice="required",
        )
        tool = ToolDefinition("lookup", "Lookup", {"type": "object"})
        with self.assertRaisesRegex(ConfigurationError, "only tool_choice 'auto'"):
            provider._request_body(ModelContext((Message(Role.USER, "lookup"),)), (tool,))

    def test_kimi_thinking_rejects_required_tool_choice_without_disabling_thinking(self) -> None:
        provider = OpenAICompatibleProvider(
            model="kimi-k2.6",
            base_url="https://api.moonshot.ai/v1",
            api_key="fixture",
            dialect="kimi",
            tool_choice="required",
        )
        tool = ToolDefinition("lookup", "Lookup", {"type": "object"})
        with self.assertRaisesRegex(ConfigurationError, "incompatible with thinking"):
            provider._request_body(ModelContext((Message(Role.USER, "lookup"),)), (tool,))

    async def test_minimax_reasoning_details_are_accumulated_without_duplicate_text(self) -> None:
        provider = OpenAICompatibleProvider(
            model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1",
            api_key="fixture",
            provider_name="minimax-profile",
            dialect="minimax",
            context_affinity="profile-v1:minimax",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_sse(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "reasoning_details": [
                                            {"text": "think", "type": "reasoning.text"}
                                        ]
                                    }
                                }
                            ]
                        },
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "reasoning_details": [
                                            {"text": "thinking", "type": "reasoning.text"}
                                        ]
                                    }
                                }
                            ]
                        },
                        {"choices": [{"delta": {"content": "done"}}]},
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 3,
                                "prompt_tokens_details": {"cached_tokens": 4},
                            },
                        },
                    ),
                )
            ),
        )

        events = [
            event
            async for event in provider.stream(ModelContext((Message(Role.USER, "hello"),)), ())
        ]
        self.assertEqual(
            [event.text for event in events if isinstance(event, ModelReasoningDelta)],
            ["think", "ing"],
        )
        self.assertEqual(
            [event.text for event in events if isinstance(event, ModelTextDelta)],
            ["done"],
        )
        completion = next(event for event in events if isinstance(event, ModelCompleted))
        assert completion.usage is not None
        self.assertEqual(completion.usage.cache_read_tokens, 4)
        self.assertEqual(len(completion.context_items), 1)
        self.assertEqual(
            completion.context_items[0].to_dict()["native"]["details"],
            [{"text": "thinking", "type": "reasoning.text"}],
        )

    async def test_provider_native_reasoning_is_not_sent_to_fallback_kimi(self) -> None:
        native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "native": {
                    "type": "openai-chat-reasoning-details",
                    "provider": "minimax-profile",
                    "protocol": "openai-chat",
                    "model": "MiniMax-M3",
                    "details": [{"text": "must not cross", "type": "reasoning.text"}],
                },
            },
        )
        minimax = OpenAICompatibleProvider(
            model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1",
            api_key="fixture",
            provider_name="minimax-profile",
            dialect="minimax",
            context_affinity="profile-v1:minimax",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, text="primary offline")
            ),
        )
        captured: dict[str, object] = {}

        def kimi_handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"choices": [{"delta": {"content": "fallback"}}]},
                    {"choices": [{"finish_reason": "stop", "delta": {}}]},
                ),
            )

        kimi = OpenAICompatibleProvider(
            model="kimi-k2.6",
            base_url="https://api.moonshot.ai/v1",
            api_key="fixture",
            provider_name="kimi-profile",
            dialect="kimi",
            context_affinity="profile-v1:kimi",
            transport=httpx.MockTransport(kimi_handler),
        )
        router = FailoverModelProvider(
            (
                ProviderCandidate(
                    "minimax-profile",
                    "MiniMax-M3",
                    "profile-v1:minimax",
                    lambda: minimax,
                ),
                ProviderCandidate(
                    "kimi-profile",
                    "kimi-k2.6",
                    "profile-v1:kimi",
                    lambda: kimi,
                ),
            )
        )
        events = [
            event
            async for event in router.stream(
                ModelContext(
                    (
                        Message(Role.USER, "continue"),
                        native,
                        Message(
                            Role.ASSISTANT,
                            "answer",
                            reasoning_content="canonical reasoning",
                        ),
                    ),
                    source_provider="minimax-profile",
                    source_model="MiniMax-M3",
                    source_context_affinity="profile-v1:minimax",
                ),
                (),
            )
        ]

        self.assertIsInstance(events[0], ModelProviderAttemptFailed)
        self.assertIsInstance(events[1], ModelProviderSelected)
        self.assertIsInstance(events[2], ModelTextDelta)
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertNotIn("reasoning_details", json.dumps(body))
        self.assertNotIn("must not cross", json.dumps(body))

    def test_minimax_native_reasoning_requires_the_exact_profile_affinity(self) -> None:
        native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "native": {
                    "type": "openai-chat-reasoning-details",
                    "provider": "minimax-personal",
                    "protocol": "openai-chat",
                    "model": "MiniMax-M3",
                    "details": [{"text": "same service must not cross"}],
                },
            },
        )
        backup = OpenAICompatibleProvider(
            model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1",
            api_key="fixture",
            provider_name="minimax-backup",
            dialect="minimax",
            context_affinity="profile-v1:minimax-backup",
        )
        body = backup._request_body(
            ModelContext(
                (Message(Role.USER, "continue"), native, Message(Role.ASSISTANT, "answer")),
                source_provider="minimax-personal",
                source_model="MiniMax-M3",
                source_context_affinity="profile-v1:minimax-personal",
            ),
            (),
        )

        self.assertNotIn("reasoning_details", json.dumps(body))
        self.assertNotIn("same service must not cross", json.dumps(body))

    def test_minimax_native_reasoning_is_bounded_and_redacted_on_rejection(self) -> None:
        provider = OpenAICompatibleProvider(
            model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1",
            api_key="fixture",
            provider_name="minimax-profile",
            dialect="minimax",
            context_affinity="profile-v1:minimax",
        )
        oversized_marker = "oversized-native-marker"
        with self.assertRaisesRegex(ProviderError, "size limit") as oversized:
            provider._native_reasoning_item(({"text": oversized_marker + ("x" * 1_100_000)},))
        self.assertNotIn(oversized_marker, str(oversized.exception))

        with self.assertRaisesRegex(ProviderError, "JSON-safe") as unsafe:
            provider._native_reasoning_item(({"value": object()},))
        self.assertNotIn("object at", str(unsafe.exception))

    def test_china_dialects_expose_only_implemented_reasoning_and_cache(self) -> None:
        capabilities = OpenAICompatibleProvider.implementation_capabilities(dialect="kimi")
        self.assertTrue(capabilities.supports(ModelCapability.REASONING))
        self.assertTrue(capabilities.supports(ModelCapability.PROMPT_CACHE))
        self.assertFalse(capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH))

    def test_china_failover_keeps_safe_capability_intersection_and_context_affinity(self) -> None:
        profiles = {
            "kimi": ProviderProfile(
                name="kimi",
                service_id="kimi",
                protocol="openai-chat",
                dialect="kimi",
                model="kimi-k2.6",
                base_url="https://api.moonshot.ai/v1",
                api_key_env="KIMI_KEY",
                native_context="profile",
                proxy_mode="direct",
            ),
            "glm": ProviderProfile(
                name="glm",
                service_id="glm",
                protocol="openai-chat",
                dialect="glm",
                model="glm-5.3",
                base_url="https://open.bigmodel.cn/api/paas/v4",
                api_key_env="GLM_KEY",
                native_context="profile",
                proxy_mode="direct",
            ),
            "minimax": ProviderProfile(
                name="minimax",
                service_id="minimax",
                protocol="openai-chat",
                dialect="minimax",
                model="MiniMax-M3",
                base_url="https://api.minimaxi.com/v1",
                api_key_env="MINIMAX_KEY",
                native_context="profile",
                proxy_mode="direct",
            ),
        }
        for primary, fallback in (("kimi", "glm"), ("glm", "minimax")):
            with self.subTest(primary=primary, fallback=fallback):
                config = AppConfig(
                    Path("/tmp/neuro-code-p3a-fixture"),
                    Path("/tmp/neuro-code-p3a-fixture-state"),
                    profiles,
                    primary,
                    primary,
                    fallback_providers=(fallback,),
                )
                provider = create_routed_provider(config)

                self.assertIsInstance(provider, FailoverModelProvider)
                self.assertTrue(provider.capabilities.supports(ModelCapability.FUNCTION_TOOLS))
                self.assertTrue(provider.capabilities.supports(ModelCapability.REASONING))
                self.assertTrue(provider.capabilities.supports(ModelCapability.PROMPT_CACHE))
                self.assertEqual(
                    provider.capabilities.status(ModelCapability.HOSTED_WEB_SEARCH),
                    CapabilityStatus.UNKNOWN,
                )
                self.assertIsNotNone(provider.context_affinity)
                candidates = provider._candidates
                self.assertNotEqual(
                    candidates[0].context_affinity,
                    candidates[1].context_affinity,
                )

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
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 7,
                        "prompt_cache_hit_tokens": 3,
                        "prompt_cache_miss_tokens": 1,
                        "prompt_cache_write_tokens": 2,
                    },
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
        assert completed.usage is not None
        self.assertEqual(completed.usage.cache_read_tokens, 3)
        self.assertEqual(completed.usage.cache_miss_tokens, 1)
        self.assertEqual(completed.usage.cache_write_tokens, 2)
        self.assertEqual(captured["authorization"], "Bearer secret-key")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "fixture-model")
        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(body["messages"][1]["reasoning_content"], "prior tool reasoning")
        self.assertEqual(body["messages"][2]["tool_call_id"], "old")
        self.assertEqual(body["tools"][0]["function"]["name"], "read_file")

    async def test_tool_policy_disabled_omits_tools_without_sticky_state(self) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                text=_sse({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="fixture-model",
            base_url="https://provider.invalid/v1",
            api_key="fixture",
            transport=httpx.MockTransport(handler),
        )
        context = ModelContext((Message(Role.USER, "hello"),))
        tools = (
            ToolDefinition(
                "read_file",
                "Read a file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        )

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

        self.assertEqual(ModelToolPolicy.ALLOWED.value, "allowed")
        self.assertEqual(ModelToolPolicy.DISABLED.value, "disabled")
        self.assertIsInstance(first[-1], ModelCompleted)
        self.assertIsInstance(disabled[-1], ModelCompleted)
        self.assertIsInstance(second[-1], ModelCompleted)
        self.assertEqual(captured[0]["tools"], captured[2]["tools"])
        self.assertNotIn("tools", captured[1])
        self.assertNotIn("tool_choice", captured[1])
        self.assertNotIn("parallel_tool_calls", captured[1])
        self.assertEqual(tools[0].name, "read_file")

    async def test_disabled_tool_policy_preserves_illegal_remote_tool_call_events(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                text=_sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "remote-call",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": '{"path":"a.py"}',
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="fixture-model",
            base_url="https://provider.invalid/v1",
            api_key="fixture",
            transport=httpx.MockTransport(handler),
        )
        tools = (ToolDefinition("read_file", "Read", {"type": "object"}),)

        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                tools,
                tool_policy=ModelToolPolicy.DISABLED,
            )
        ]

        body = captured["body"]
        assert isinstance(body, dict)
        self.assertNotIn("tools", body)
        self.assertIsInstance(events[0], ModelToolCall)
        self.assertEqual(events[0].call.name, "read_file")

    async def test_deepseek_dsml_tool_call_is_parsed_across_sse_chunks(self) -> None:
        fragments = (
            "before ",
            "<|DSML|tool_",
            'calls><|DSML|invoke name="bash">',
            '<|DSML|parameter name="command" string="true">',
            "echo hi</|DSML|parameter></|DSML|invoke></|DSML|tool_calls>",
            " after",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_sse(
                    {"choices": [{"delta": {"reasoning_content": "planning"}}]},
                    *({"choices": [{"delta": {"content": fragment}}]} for fragment in fragments),
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="fixture",
            dialect="deepseek-v4",
            transport=httpx.MockTransport(handler),
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "inspect"),)),
                (),
            )
        ]

        text = "".join(event.text for event in events if isinstance(event, ModelTextDelta))
        calls = [event.call for event in events if isinstance(event, ModelToolCall)]
        self.assertEqual(text, "before  after")
        self.assertEqual(
            [event.text for event in events if isinstance(event, ModelReasoningDelta)],
            ["planning"],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "bash")
        self.assertEqual(calls[0].arguments, {"command": "echo hi"})
        self.assertNotIn("DSML", text)

    async def test_standard_deepseek_named_provider_does_not_enable_dsml(self) -> None:
        content = (
            '<|DSML|tool_calls><|DSML|invoke name="read_file">'
            '<|DSML|parameter name="path" string="true">a.py</|DSML|parameter>'
            "</|DSML|invoke></|DSML|tool_calls>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_sse({"choices": [{"delta": {"content": content}}]}),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="deepseek-v4-flash",
            base_url="https://proxy.invalid/v1",
            api_key="fixture",
            provider_name="deepseek-compatible",
            dialect="standard",
            transport=httpx.MockTransport(handler),
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "answer"),)),
                (),
            )
        ]

        self.assertFalse(any(isinstance(event, ModelToolCall) for event in events))
        self.assertEqual(
            [event.text for event in events if isinstance(event, ModelTextDelta)],
            [content],
        )

    async def test_deepseek_plain_assistant_text_remains_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_sse(
                    {"choices": [{"delta": {"content": "ordinary DeepSeek answer"}}]},
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="fixture",
            dialect="deepseek-v4",
            transport=httpx.MockTransport(handler),
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "answer"),)),
                (),
            )
        ]
        self.assertEqual(
            [event.text for event in events if isinstance(event, ModelTextDelta)],
            ["ordinary DeepSeek answer"],
        )

    async def test_deepseek_dsml_unicode_multiple_calls_and_json_parameters(self) -> None:
        content = (
            "<\uff5cDSML\uff5ctool_calls>"
            '<\uff5cDSML\uff5cinvoke name="read_file">'
            '<\uff5cDSML\uff5cparameter name="path" string="true">a.py</\uff5cDSML\uff5cparameter>'
            "</\uff5cDSML\uff5cinvoke>"
            '<\uff5cDSML\uff5cinvoke name="search">'
            '<\uff5cDSML\uff5cparameter name="limit" string="false">5</\uff5cDSML\uff5cparameter>'
            "</\uff5cDSML\uff5cinvoke>"
            "</\uff5cDSML\uff5ctool_calls>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            midpoint = len(content) // 2
            return httpx.Response(
                200,
                text=_sse(
                    {"choices": [{"delta": {"content": content[:midpoint]}}]},
                    {"choices": [{"delta": {"content": content[midpoint:]}}]},
                ),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="fixture-model",
            base_url="https://proxy.invalid/v1",
            api_key="fixture",
            provider_name="generic",
            dialect="deepseek-v4",
            transport=httpx.MockTransport(handler),
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "inspect"),)),
                (),
            )
        ]
        calls = [event.call for event in events if isinstance(event, ModelToolCall)]
        self.assertEqual(
            [(call.name, dict(call.arguments)) for call in calls],
            [("read_file", {"path": "a.py"}), ("search", {"limit": 5})],
        )
        self.assertEqual(
            "".join(event.text for event in events if isinstance(event, ModelTextDelta)), ""
        )

    async def test_deepseek_incomplete_dsml_is_rejected_without_leaking_protocol_text(self) -> None:
        content = '<|DSML|tool_calls><|DSML|invoke name="bash">'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=_sse({"choices": [{"delta": {"content": content}}]}),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="fixture",
            dialect="deepseek-v4",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderError, "DeepSeek DSML"):
            [
                event
                async for event in provider.stream(
                    ModelContext((Message(Role.USER, "inspect"),)),
                    (),
                )
            ]

    async def test_deepseek_dsml_with_disabled_tools_remains_a_model_tool_call(self) -> None:
        content = (
            '<|DSML|tool_calls><|DSML|invoke name="read_file">'
            '<|DSML|parameter name="path" string="true">a.py</|DSML|parameter>'
            "</|DSML|invoke></|DSML|tool_calls>"
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                text=_sse({"choices": [{"delta": {"content": content}}]}),
                headers={"content-type": "text/event-stream"},
            )

        provider = OpenAICompatibleProvider(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            api_key="fixture",
            dialect="deepseek-v4",
            transport=httpx.MockTransport(handler),
        )
        events = [
            event
            async for event in provider.stream(
                ModelContext((Message(Role.USER, "finalize"),)),
                (ToolDefinition("read_file", "Read", {"type": "object"}),),
                tool_policy=ModelToolPolicy.DISABLED,
            )
        ]
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertNotIn("tools", body)
        self.assertEqual(
            [event.call.name for event in events if isinstance(event, ModelToolCall)],
            ["read_file"],
        )

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
            "gemini-interactions": GeminiInteractionsProvider,
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
            deepseek_provider = create_provider(
                ProviderProfile(
                    name="deepseek",
                    protocol="openai-chat",
                    dialect="deepseek-v4",
                    model="deepseek-v4-flash",
                    base_url="https://proxy.invalid/v1",
                    api_key_env="TOKEN",
                    proxy_mode="direct",
                )
            )
            assert isinstance(deepseek_provider, OpenAICompatibleProvider)
            self.assertEqual(deepseek_provider._dialect, "deepseek-v4")
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
        self.assertIsInstance(direct, ResilientModelProvider)


if __name__ == "__main__":
    unittest.main()
