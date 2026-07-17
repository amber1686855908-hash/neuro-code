from __future__ import annotations

import json
import unittest

import httpx

from neuro_code.domain.messages import ContextItemKind, Message, PreservedContextItem, Role
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelCompleted
from neuro_code.providers.openai_responses import OpenAIResponsesProvider


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


if __name__ == "__main__":
    unittest.main()
