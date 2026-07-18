from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary
from neuro_code.errors import SessionError


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_snapshot_is_atomic_and_preserves_identity_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            snapshot = SessionSnapshot(
                summary=SessionSummary(
                    id="imported-id",
                    cwd="/rust/workspace",
                    provider="upstream-rust-import",
                    model="xai-test-model",
                    created_at=datetime(2026, 7, 1, tzinfo=UTC),
                    updated_at=datetime(2026, 7, 2, tzinfo=UTC),
                    sandbox_profile=SandboxProfile.STRICT,
                ),
                items=(
                    Message(
                        Role.USER,
                        "imported",
                        content_parts=(
                            ContentPart.from_text("imported"),
                            ContentPart.from_image("data:image/png;base64,fixture"),
                        ),
                    ),
                    PreservedContextItem(
                        ContextItemKind.REASONING,
                        {
                            "type": "reasoning",
                            "id": "reasoning-1",
                            "summary": [],
                            "encrypted_content": "opaque",
                        },
                    ),
                    Message(Role.ASSISTANT, "done"),
                ),
            )

            imported_id = await store.import_session(snapshot)

            self.assertEqual(imported_id, "imported-id")
            self.assertEqual(await store.get_session(imported_id), snapshot.summary)
            self.assertEqual(await store.load_messages(imported_id), list(snapshot.messages))
            self.assertEqual(await store.load_session_items(imported_id), list(snapshot.items))
            self.assertEqual(await store.load_events(imported_id), [])
            self.assertEqual(await store.next_event_sequence(imported_id), 1)
            with self.assertRaisesRegex(SessionError, "session already exists"):
                await store.import_session(snapshot)
            self.assertEqual(await store.load_messages(imported_id), list(snapshot.messages))

            continued = [*snapshot.messages, Message(Role.USER, "continue")]
            await store.save_messages(imported_id, continued)
            self.assertEqual(
                await store.load_session_items(imported_id),
                [*snapshot.items, continued[-1]],
            )
            with self.assertRaisesRegex(SessionError, "cannot rewrite the imported prefix"):
                await store.save_messages(imported_id, [Message(Role.USER, "rewritten")])

            native = PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "reasoning-2",
                    "summary": [],
                    "encrypted_content": "native-opaque",
                },
            )
            extended_items = [*snapshot.items, continued[-1], native, Message(Role.ASSISTANT, "ok")]
            await store.save_session_items(imported_id, extended_items)
            self.assertEqual(await store.load_session_items(imported_id), extended_items)
            with self.assertRaisesRegex(
                SessionError, "cannot rewrite the persisted session item prefix"
            ):
                await store.save_session_items(imported_id, [Message(Role.USER, "replacement")])

    async def test_round_trip_messages_and_ordered_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                "/workspace",
                "fake",
                "test-model",
                "profile-v1:fixture",
                SandboxProfile.READ_ONLY,
            )
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
                    reasoning_content="Need to read a.py.",
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
            self.assertEqual(summary.context_affinity, "profile-v1:fixture")
            self.assertIs(summary.sandbox_profile, SandboxProfile.READ_ONLY)
            self.assertIs(
                await store.peek_session_sandbox_profile(session_id),
                SandboxProfile.READ_ONLY,
            )
            await store.update_session_provider(
                session_id,
                "fallback",
                "fallback-model",
                "profile-v1:fallback",
            )
            summary = await store.get_session(session_id)
            self.assertEqual(summary.provider, "fallback")
            self.assertEqual(summary.model, "fallback-model")
            self.assertEqual(summary.context_affinity, "profile-v1:fallback")
            self.assertIs(summary.sandbox_profile, SandboxProfile.READ_ONLY)
            self.assertEqual((await store.list_sessions())[0], summary)

    async def test_schema_v1_is_migrated_without_rewriting_existing_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 1);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                INSERT INTO sessions(id, cwd, provider, model)
                VALUES ('legacy-id', '/legacy', 'xai-responses', 'xai-test-model');
                """
            )
            connection.commit()
            connection.close()

            store = SqliteSessionStore(database)
            await store.initialize()

            summary = await store.get_session("legacy-id")
            self.assertEqual(summary.provider, "xai-responses")
            self.assertIsNone(summary.context_affinity)
            self.assertIsNone(summary.sandbox_profile)
            migrated = sqlite3.connect(database)
            version = migrated.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone()
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(sessions)").fetchall()}
            migrated.close()
            self.assertEqual(version, (3,))
            self.assertIn("context_affinity", columns)
            self.assertIn("sandbox_profile", columns)

    async def test_schema_v2_peek_is_read_only_then_migrates_as_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 2);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    context_affinity TEXT
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                INSERT INTO sessions(id, cwd, provider, model, context_affinity)
                VALUES ('v2-id', '/legacy', 'fixture', 'model', 'profile-v1:old');
                """
            )
            connection.commit()
            connection.close()
            before = database.read_bytes()

            store = SqliteSessionStore(database)
            self.assertIsNone(await store.peek_session_sandbox_profile("v2-id"))
            self.assertEqual(database.read_bytes(), before)
            before_migration = sqlite3.connect(database)
            self.assertEqual(
                before_migration.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (2,),
            )
            before_migration.close()

            await store.initialize()
            summary = await store.get_session("v2-id")
            self.assertEqual(summary.context_affinity, "profile-v1:old")
            self.assertIsNone(summary.sandbox_profile)
            migrated = sqlite3.connect(database)
            self.assertEqual(
                migrated.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (3,),
            )
            migrated.close()

    async def test_sandbox_peek_never_creates_state_and_rejects_corrupt_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing" / "sessions.db"
            missing_store = SqliteSessionStore(missing)
            self.assertIsNone(await missing_store.peek_session_sandbox_profile("absent"))
            self.assertFalse(missing.parent.exists())

            database = root / "sessions.db"
            store = SqliteSessionStore(database)
            await store.initialize()
            session_id = await store.create_session(
                "/workspace",
                "fixture",
                "model",
                sandbox_profile=SandboxProfile.WORKSPACE,
            )
            names_before = set(os.listdir(root))
            bytes_before = database.read_bytes()
            self.assertIs(
                await store.peek_session_sandbox_profile(session_id),
                SandboxProfile.WORKSPACE,
            )
            self.assertEqual(database.read_bytes(), bytes_before)
            self.assertEqual(set(os.listdir(root)), names_before)

            active = sqlite3.connect(database)
            active.execute(
                "UPDATE sessions SET sandbox_profile = 'strict' WHERE id = ?",
                (session_id,),
            )
            active.commit()
            with self.assertRaisesRegex(SessionError, "active WAL"):
                await store.peek_session_sandbox_profile(session_id)
            active.close()

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE sessions SET sandbox_profile = 'custom-unsafe' WHERE id = ?",
                (session_id,),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SessionError, "unsupported sandbox profile"):
                await store.peek_session_sandbox_profile(session_id)
            with self.assertRaisesRegex(SessionError, "unsupported sandbox profile"):
                await store.get_session(session_id)

    async def test_unknown_sessions_and_invalid_limit_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            for operation in (
                store.get_session("missing"),
                store.load_messages("missing"),
                store.load_session_items("missing"),
                store.next_event_sequence("missing"),
                store.update_session_provider("missing", "provider", "model", None),
                store.list_sessions(limit=0),
            ):
                with self.assertRaises(SessionError):
                    await operation


if __name__ == "__main__":
    unittest.main()
