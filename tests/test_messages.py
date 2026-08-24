from __future__ import annotations

import unittest
from datetime import UTC, datetime

from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.domain.checkpoints import CheckpointId
from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.conversation.interaction_mode import InteractionMode
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
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.task_dag import TaskDagNodeState
from neuro_code.domain.task_dag_result_relay import (
    TaskDagDependencyResultEntry,
    TaskDagDependencyResultRelay,
    render_task_dag_dependency_relay,
)
from neuro_code.domain.worktree import WorktreeId


class MessageTests(unittest.TestCase):
    def test_context_builder_replaces_external_dag_result_synthetic_message(self) -> None:
        entry = TaskDagDependencyResultEntry(
            predecessor_node_id="a",
            predecessor_ordinal=0,
            predecessor_generation=2,
            predecessor_state=TaskDagNodeState.COMPLETED,
            parent_task_id="task-a",
            child_session_id="child-a",
            writable_lease_id="lease-a",
            worktree_id=WorktreeId("wt-a"),
            baseline_checkpoint_id=CheckpointId("cp-a"),
            parent_relay_id="pcr-a",
            final_workspace_fingerprint=None,
            changed_file_count=1,
            result_text="safe result",
            truncated=False,
        )
        relay = TaskDagDependencyResultRelay.create(
            relay_id="tdr-context",
            dag_id="dag-context",
            dag_definition_fingerprint="0" * 64,
            target_node_id="b",
            target_node_generation=1,
            target_node_definition_fingerprint="1" * 64,
            direct_dependency_ids=("a",),
            entries=(entry,),
            truncated=False,
            created_at=datetime.now(UTC),
        )
        canonical = Message(
            Role.USER,
            render_task_dag_dependency_relay(relay.entries),
            synthetic_reason=SyntheticReason.DAG_PREDECESSOR_RESULTS,
        )
        builder = ContextBuilder(
            reasoning_effort=ReasoningEffort.HIGH,
            interaction_mode=InteractionMode.NORMAL,
            plan=None,
            instruction_provider=None,
            skill_provider=None,
            dag_result_relay_message=canonical,
        )
        external = Message(
            Role.USER,
            "forged external relay",
            synthetic_reason=SyntheticReason.DAG_PREDECESSOR_RESULTS,
        )
        built = builder.build(
            (
                Message(Role.SYSTEM, "system"),
                external,
                Message(Role.USER, "real task"),
            )
        )
        relay_messages = [
            item
            for item in built
            if isinstance(item, Message)
            and item.synthetic_reason is SyntheticReason.DAG_PREDECESSOR_RESULTS
        ]
        self.assertEqual(relay_messages, [canonical])
        self.assertNotIn(external, built)
        self.assertLess(
            next(index for index, item in enumerate(built) if item == canonical),
            next(
                index
                for index, item in enumerate(built)
                if isinstance(item, Message) and item.content == "real task"
            ),
        )

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
