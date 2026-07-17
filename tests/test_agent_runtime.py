from __future__ import annotations

import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.domain.events import AgentEventKind
from neuro_code.domain.messages import (
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
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ProviderError
from neuro_code.permissions import PermissionManager, PermissionMode
from neuro_code.ports.tools import ToolContext
from neuro_code.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.runtime import AgentRuntime
from neuro_code.tools import default_tool_registry


class ScriptedProvider:
    provider_name = "scripted"
    model_name = "fixture-model"
    context_affinity = "profile-v1:scripted"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[ModelContext] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        self.calls.append(context)
        script = self._scripts.pop(0)
        for event in script:
            yield event


class FailingProvider:
    def __init__(self, name: str) -> None:
        self.provider_name = name
        self.model_name = f"{name}-model"
        self.context_affinity = f"profile-v1:{name}"

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        raise ProviderError(f"{self.provider_name} unavailable")
        if False:
            yield ModelCompleted("stop")


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_output_failover_is_audited_and_updates_new_session_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = FailingProvider("primary")
            fallback = ScriptedProvider(
                ((ModelTextDelta("fallback response"), ModelCompleted("stop")),)
            )
            fallback.provider_name = "fallback"
            fallback.model_name = "fallback-model"
            fallback.context_affinity = "profile-v1:fallback"
            provider = FailoverModelProvider(
                (
                    ProviderCandidate(
                        primary.provider_name,
                        primary.model_name,
                        primary.context_affinity,
                        lambda: primary,
                    ),
                    ProviderCandidate(
                        fallback.provider_name,
                        fallback.model_name,
                        fallback.context_affinity,
                        lambda: fallback,
                    ),
                )
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("hello")

            self.assertEqual(result.response, "fallback response")
            kinds = [event.kind for event in result.events]
            self.assertIn(AgentEventKind.PROVIDER_ATTEMPT_FAILED, kinds)
            self.assertIn(AgentEventKind.PROVIDER_SELECTED, kinds)
            selected = next(
                event for event in result.events if event.kind is AgentEventKind.PROVIDER_SELECTED
            )
            self.assertEqual(selected.data["provider"], "fallback")
            self.assertTrue(selected.data["failover"])
            self.assertTrue(selected.data["session_origin_updated"])
            assert result.session_id is not None
            summary = await store.get_session(result.session_id)
            self.assertEqual(summary.provider, "fallback")
            self.assertEqual(summary.model, "fallback-model")
            self.assertEqual(summary.context_affinity, "profile-v1:fallback")

    async def test_failover_does_not_relabel_existing_foreign_opaque_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "original",
                "original-model",
                "profile-v1:original",
            )
            preserved = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "old-reasoning",
                    "summary": [],
                    "encrypted_content": "foreign-opaque-state",
                },
            )
            initial_items = (Message(Role.SYSTEM, "system"), preserved)
            await store.save_session_items(session_id, initial_items)

            primary = FailingProvider("primary")
            fallback = ScriptedProvider(((ModelTextDelta("safe"), ModelCompleted("stop")),))
            fallback.provider_name = "fallback"
            fallback.model_name = "fallback-model"
            fallback.context_affinity = "profile-v1:fallback"
            provider = FailoverModelProvider(
                (
                    ProviderCandidate(
                        primary.provider_name,
                        primary.model_name,
                        primary.context_affinity,
                        lambda: primary,
                    ),
                    ProviderCandidate(
                        fallback.provider_name,
                        fallback.model_name,
                        fallback.context_affinity,
                        lambda: fallback,
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run(
                "continue",
                initial_items=initial_items,
                source_provider="original",
                source_model="original-model",
                source_context_affinity="profile-v1:original",
                session_id=session_id,
            )

            selected = next(
                event for event in result.events if event.kind is AgentEventKind.PROVIDER_SELECTED
            )
            self.assertFalse(selected.data["session_origin_updated"])
            summary = await store.get_session(session_id)
            self.assertEqual(summary.provider, "original")
            self.assertEqual(summary.context_affinity, "profile-v1:original")

    async def test_full_headless_read_edit_command_vertical_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            verify_command = (
                f'"{sys.executable}" -c "from pathlib import Path; '
                "assert Path('note.txt').read_text() == 'changed'\""
            )
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(ToolCall("read", "read_file", {"path": "note.txt"})),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelToolCall(
                            ToolCall(
                                "edit",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelToolCall(ToolCall("verify", "bash", {"command": verify_command})),
                        ModelCompleted("tool_calls"),
                    ),
                    (
                        ModelTextDelta("Read, edited, and verified note.txt."),
                        ModelCompleted("stop"),
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(root),
            )

            result = await runtime.run("Update and verify note.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "changed")
            self.assertEqual(result.steps, 4)
            completed_tools = [
                event for event in result.events if event.kind is AgentEventKind.TOOL_COMPLETED
            ]
            self.assertEqual(
                [event.data["name"] for event in completed_tools],
                [
                    "read_file",
                    "search_replace",
                    "bash",
                ],
            )
            self.assertEqual(result.response, "Read, edited, and verified note.txt.")

    async def test_read_tool_round_trip_and_session_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("source evidence", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelReasoningDelta("Need to inspect the file."),
                        ModelToolCall(ToolCall("call-1", "read_file", {"path": "note.txt"})),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("The file contains source evidence."), ModelCompleted("stop")),
                )
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("Read note.txt")

            self.assertEqual(result.response, "The file contains source evidence.")
            self.assertEqual(result.steps, 2)
            self.assertIsNotNone(result.session_id)
            tool_messages = [message for message in result.messages if message.role is Role.TOOL]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("source evidence", tool_messages[0].content)
            self.assertIn(AgentEventKind.TOOL_COMPLETED, [event.kind for event in result.events])
            self.assertIn(AgentEventKind.REASONING_DELTA, [event.kind for event in result.events])
            assert result.session_id is not None
            persisted = await store.load_messages(result.session_id)
            self.assertEqual(persisted, list(result.messages))
            self.assertEqual(len(provider.calls), 2)
            prior_assistant = next(
                message for message in provider.calls[1].messages if message.role is Role.ASSISTANT
            )
            self.assertEqual(prior_assistant.reasoning_content, "Need to inspect the file.")

    async def test_default_headless_policy_denies_edit_and_agent_can_recover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")
            provider = ScriptedProvider(
                (
                    (
                        ModelToolCall(
                            ToolCall(
                                "call-1",
                                "search_replace",
                                {"path": "note.txt", "old": "original", "new": "changed"},
                            )
                        ),
                        ModelCompleted("tool_calls"),
                    ),
                    (ModelTextDelta("I could not edit without approval."), ModelCompleted("stop")),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(mode=PermissionMode.DEFAULT, interactive=False),
                tool_context=ToolContext(root),
            )

            result = await runtime.run("Edit note.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertIn(AgentEventKind.TOOL_FAILED, [event.kind for event in result.events])
            second_request = provider.calls[1].messages
            denial = [message for message in second_request if message.role is Role.TOOL]
            self.assertIn("permission denied", denial[0].content)

    async def test_imported_context_and_origin_reach_every_model_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preserved = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "reasoning-imported",
                    "summary": [{"type": "summary_text", "text": "source reasoning"}],
                    "encrypted_content": "opaque-provider-state",
                },
            )
            initial_items = (
                Message(Role.SYSTEM, "source system"),
                Message(Role.USER, "source question"),
                preserved,
                Message(Role.ASSISTANT, "source answer"),
            )
            provider = ScriptedProvider(((ModelTextDelta("continued"), ModelCompleted("stop")),))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            result = await runtime.run(
                "continue",
                initial_items=initial_items,
                source_provider=UPSTREAM_IMPORT_PROVIDER,
                source_model="xai-test-model",
            )

            self.assertEqual(len(provider.calls), 1)
            request = provider.calls[0]
            self.assertEqual(request.source_provider, UPSTREAM_IMPORT_PROVIDER)
            self.assertEqual(request.source_model, "xai-test-model")
            self.assertIn(preserved, request.preserved_items)
            self.assertEqual(result.items[: len(initial_items)], initial_items)
            self.assertNotIn(preserved, result.messages)

    async def test_provider_native_items_are_replayed_and_persisted_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("native context", encoding="utf-8")
            reasoning = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "reasoning-native",
                    "summary": [{"type": "summary_text", "text": "read the file"}],
                    "encrypted_content": "opaque-native-state",
                    "status": "completed",
                },
            )
            provider = ScriptedProvider(
                (
                    (
                        ModelReasoningDelta("read the file"),
                        ModelToolCall(ToolCall("call-native", "read_file", {"path": "note.txt"})),
                        ModelCompleted("tool_calls", context_items=(reasoning,)),
                    ),
                    (ModelTextDelta("done"), ModelCompleted("stop")),
                )
            )
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            result = await runtime.run("inspect note.txt")

            self.assertEqual(provider.calls[1].source_provider, "scripted")
            self.assertEqual(provider.calls[1].source_model, "fixture-model")
            self.assertEqual(
                provider.calls[1].source_context_affinity,
                "profile-v1:scripted",
            )
            self.assertIn(reasoning, provider.calls[1].preserved_items)
            reasoning_index = result.items.index(reasoning)
            first_assistant_index = next(
                index
                for index, item in enumerate(result.items)
                if isinstance(item, Message) and item.role is Role.ASSISTANT
            )
            self.assertLess(reasoning_index, first_assistant_index)
            assert result.session_id is not None
            self.assertEqual(await store.load_session_items(result.session_id), list(result.items))
            self.assertEqual(
                (await store.get_session(result.session_id)).context_affinity,
                "profile-v1:scripted",
            )

    async def test_provider_terminal_text_is_canonical_for_results_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                (
                    (
                        ModelTextDelta("streamed text"),
                        ModelCompleted("stop", response_text="canonical text"),
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            result = await runtime.run("answer")

            self.assertEqual(result.response, "canonical text")
            self.assertEqual(result.messages[-1].content, "canonical text")
            deltas = [
                event.data["text"]
                for event in result.events
                if event.kind is AgentEventKind.TEXT_DELTA
            ]
            self.assertEqual(deltas, ["streamed text"])

    async def test_backend_tools_emit_audit_events_without_local_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                (
                    (
                        ModelBackendToolStarted("server-1", "web_search"),
                        ModelBackendToolCompleted("server-1", "web_search"),
                        ModelTextDelta("server-side research complete"),
                        ModelCompleted("stop"),
                    ),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
            )

            result = await runtime.run("research")

            backend_events = [
                event
                for event in result.events
                if event.kind
                in {
                    AgentEventKind.BACKEND_TOOL_STARTED,
                    AgentEventKind.BACKEND_TOOL_COMPLETED,
                }
            ]
            self.assertEqual(
                [(event.kind, dict(event.data)) for event in backend_events],
                [
                    (
                        AgentEventKind.BACKEND_TOOL_STARTED,
                        {"id": "server-1", "name": "web_search"},
                    ),
                    (
                        AgentEventKind.BACKEND_TOOL_COMPLETED,
                        {"id": "server-1", "name": "web_search"},
                    ),
                ],
            )
            self.assertFalse(any(message.role is Role.TOOL for message in result.messages))
            self.assertFalse(
                any(
                    event.kind
                    in {
                        AgentEventKind.TOOL_REQUESTED,
                        AgentEventKind.TOOL_PERMISSION,
                        AgentEventKind.TOOL_STARTED,
                        AgentEventKind.TOOL_COMPLETED,
                        AgentEventKind.TOOL_FAILED,
                    }
                    for event in result.events
                )
            )


if __name__ == "__main__":
    unittest.main()
