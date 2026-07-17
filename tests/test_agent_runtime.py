from __future__ import annotations

import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from pygrok_build.adapters.sqlite_session import SqliteSessionStore
from pygrok_build.domain.events import AgentEventKind
from pygrok_build.domain.messages import Message, Role, ToolCall
from pygrok_build.domain.model_events import (
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from pygrok_build.domain.tools import ToolDefinition
from pygrok_build.permissions import PermissionManager, PermissionMode
from pygrok_build.ports.tools import ToolContext
from pygrok_build.runtime import AgentRuntime
from pygrok_build.tools import default_tool_registry


class ScriptedProvider:
    provider_name = "scripted"
    model_name = "fixture-model"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[tuple[Message, ...]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        self.calls.append(tuple(messages))
        script = self._scripts.pop(0)
        for event in script:
            yield event


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
                message for message in provider.calls[1] if message.role is Role.ASSISTANT
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
            second_request = provider.calls[1]
            denial = [message for message in second_request if message.role is Role.TOOL]
            self.assertIn("permission denied", denial[0].content)


if __name__ == "__main__":
    unittest.main()
