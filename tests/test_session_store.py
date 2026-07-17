from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pygrok_build.adapters.sqlite_session import SqliteSessionStore
from pygrok_build.domain.events import AgentEvent, AgentEventKind
from pygrok_build.domain.messages import Message, Role, ToolCall
from pygrok_build.domain.sessions import SessionSnapshot, SessionSummary
from pygrok_build.errors import SessionError


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_snapshot_is_atomic_and_preserves_identity_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            snapshot = SessionSnapshot(
                summary=SessionSummary(
                    id="imported-id",
                    cwd="/rust/workspace",
                    provider="grok-build-import",
                    model="grok-4.5",
                    created_at=datetime(2026, 7, 1, tzinfo=UTC),
                    updated_at=datetime(2026, 7, 2, tzinfo=UTC),
                ),
                messages=(Message(Role.USER, "imported"), Message(Role.ASSISTANT, "done")),
            )

            imported_id = await store.import_session(snapshot)

            self.assertEqual(imported_id, "imported-id")
            self.assertEqual(await store.get_session(imported_id), snapshot.summary)
            self.assertEqual(await store.load_messages(imported_id), list(snapshot.messages))
            self.assertEqual(await store.load_events(imported_id), [])
            self.assertEqual(await store.next_event_sequence(imported_id), 1)
            with self.assertRaisesRegex(SessionError, "session already exists"):
                await store.import_session(snapshot)
            self.assertEqual(await store.load_messages(imported_id), list(snapshot.messages))

    async def test_round_trip_messages_and_ordered_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fake", "test-model")
            messages = [
                Message(Role.USER, "inspect"),
                Message(
                    Role.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            "call-1",
                            "read_file",
                            {"path": "a.py"},
                            {"provider_signature": "opaque"},
                        ),
                    ),
                ),
                Message(Role.TOOL, "content", name="read_file", tool_call_id="call-1"),
            ]
            await store.save_messages(session_id, messages)
            await store.append_event(
                session_id,
                AgentEvent.create(1, AgentEventKind.USER_MESSAGE, {"content": "inspect"}),
            )
            await store.append_event(
                session_id,
                AgentEvent.create(2, AgentEventKind.TURN_COMPLETED, {"step": 1}),
            )

            loaded = await store.load_messages(session_id)
            events = await store.load_events(session_id)
            self.assertEqual(loaded, messages)
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertEqual(await store.next_event_sequence(session_id), 3)
            summary = await store.get_session(session_id)
            self.assertEqual(summary.id, session_id)
            self.assertEqual(summary.cwd, "/workspace")
            self.assertEqual((await store.list_sessions())[0], summary)

    async def test_unknown_sessions_and_invalid_limit_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            for operation in (
                store.get_session("missing"),
                store.load_messages("missing"),
                store.next_event_sequence("missing"),
                store.list_sessions(limit=0),
            ):
                with self.assertRaises(SessionError):
                    await operation


if __name__ == "__main__":
    unittest.main()
