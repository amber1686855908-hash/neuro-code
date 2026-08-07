from __future__ import annotations

import unittest

from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.conversation.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPart,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SyntheticReason,
    ToolCall,
)


class MessageTests(unittest.TestCase):
    def test_structured_content_preserves_images_and_has_a_safe_model_projection(self) -> None:
        message = Message(
            Role.USER,
            content_parts=(
                ContentPart.from_text("inspect"),
                ContentPart.from_image("data:image/png;base64,fixture"),
                ContentPart.from_text("continue"),
            ),
        )

        self.assertEqual(message.content, "inspect\ncontinue")
        self.assertEqual(
            message.model_content(),
            f"inspect\n{IMAGE_MODEL_PLACEHOLDER}\ncontinue",
        )
        self.assertEqual(
            message.to_dict()["content_parts"],
            [
                {"type": "text", "text": "inspect"},
                {"type": "image", "url": "data:image/png;base64,fixture"},
                {"type": "text", "text": "continue"},
            ],
        )

    def test_content_part_invariants_reject_ambiguous_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "text content parts require text"):
            ContentPart(ContentPartKind.TEXT)
        with self.assertRaisesRegex(ValueError, "image content parts require"):
            ContentPart(ContentPartKind.IMAGE, url="")
        with self.assertRaisesRegex(ValueError, "content must match"):
            Message(
                Role.USER,
                "different",
                content_parts=(ContentPart.from_text("text"),),
            )
        with self.assertRaisesRegex(ValueError, "only valid on assistant"):
            Message(Role.USER, "question", reasoning_content="private reasoning")
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            Message(Role.ASSISTANT, reasoning_content="")

    def test_assistant_reasoning_content_is_explicit_and_serializable(self) -> None:
        message = Message(
            Role.ASSISTANT,
            tool_calls=(ToolCall("call-1", "read_file", {"path": "a.py"}),),
            reasoning_content="Need repository evidence.",
        )

        self.assertEqual(
            message.to_dict()["reasoning_content"],
            "Need repository evidence.",
        )

    def test_synthetic_context_is_plain_user_text_and_not_serialized(self) -> None:
        message = Message(
            Role.USER,
            "repository guidance",
            synthetic_reason=SyntheticReason.PROJECT_INSTRUCTIONS,
        )

        self.assertNotIn("synthetic_reason", message.to_dict())
        with self.assertRaisesRegex(ValueError, "user role"):
            Message(
                Role.SYSTEM,
                "repository guidance",
                synthetic_reason=SyntheticReason.PROJECT_INSTRUCTIONS,
            )
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            Message(
                Role.USER,
                synthetic_reason=SyntheticReason.PROJECT_INSTRUCTIONS,
            )
        with self.assertRaisesRegex(ValueError, "plain text"):
            Message(
                Role.USER,
                "repository guidance",
                name="instructions",
                synthetic_reason=SyntheticReason.PROJECT_INSTRUCTIONS,
            )

    def test_preserved_context_payload_is_deeply_immutable_and_round_trips(self) -> None:
        payload = {
            "type": "backend_tool_call",
            "kind": {
                "tool_type": "web_search",
                "id": "web-1",
                "action": {
                    "type": "search",
                    "query": "fixture",
                    "sources": [{"url": "https://example.invalid"}],
                },
            },
        }
        item = PreservedContextItem(ContextItemKind.BACKEND_TOOL_CALL, payload)

        payload["kind"]["id"] = "mutated"
        payload["kind"]["action"]["sources"].append({"url": "changed"})

        restored = item.to_dict()
        self.assertEqual(restored["kind"]["id"], "web-1")
        self.assertEqual(
            restored["kind"]["action"]["sources"],
            [{"url": "https://example.invalid"}],
        )
        with self.assertRaisesRegex(ValueError, "context payload type"):
            PreservedContextItem(
                ContextItemKind.REASONING,
                {"type": "backend_tool_call"},
            )

    def test_model_context_retains_order_and_requires_complete_origin(self) -> None:
        message = Message(Role.USER, "question")
        preserved = PreservedContextItem(
            ContextItemKind.REASONING,
            {"type": "reasoning", "id": "reasoning-1", "summary": []},
        )
        context = ModelContext(
            (message, preserved),
            source_provider=UPSTREAM_IMPORT_PROVIDER,
            source_model="xai-test-model",
        )

        self.assertEqual(context.items, (message, preserved))
        self.assertEqual(context.messages, (message,))
        self.assertEqual(context.preserved_items, (preserved,))
        with self.assertRaisesRegex(ValueError, "must be set together"):
            ModelContext((message,), source_provider=UPSTREAM_IMPORT_PROVIDER)
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            ModelContext((message,), source_provider="", source_model="xai-test-model")


if __name__ == "__main__":
    unittest.main()
