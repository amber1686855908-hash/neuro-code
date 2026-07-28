from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

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
from neuro_code.shared.errors import SessionError


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    def test_connect_retries_only_transient_wal_locks_and_closes_on_failure(self) -> None:
        store = SqliteSessionStore(Path("/unused/sessions.db"))
        retry_connection = Mock(spec=sqlite3.Connection)
        wal_attempts = 0

        def execute_with_transient_lock(statement: str) -> Mock:
            nonlocal wal_attempts
            if statement == "PRAGMA journal_mode = WAL":
                wal_attempts += 1
                if wal_attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
            return Mock()

        retry_connection.execute.side_effect = execute_with_transient_lock
        with (
            patch(
                "neuro_code.adapters.sqlite_session.sqlite3.connect",
                return_value=retry_connection,
            ),
            patch("neuro_code.adapters.sqlite_session.time.sleep") as sleep,
        ):
            self.assertIs(store._connect(), retry_connection)

        self.assertEqual(wal_attempts, 3)
        self.assertEqual(sleep.call_count, 2)
        retry_connection.close.assert_not_called()

        failed_connection = Mock(spec=sqlite3.Connection)

        def execute_with_permanent_failure(statement: str) -> Mock:
            if statement == "PRAGMA journal_mode = WAL":
                raise sqlite3.OperationalError("disk I/O error")
            return Mock()

        failed_connection.execute.side_effect = execute_with_permanent_failure
        with (
            patch(
                "neuro_code.adapters.sqlite_session.sqlite3.connect",
                return_value=failed_connection,
            ),
            self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"),
        ):
            store._connect()
        failed_connection.close.assert_called_once_with()

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
            imported_summary = await store.get_session(imported_id)
            self.assertEqual(imported_summary.title, "imported")
            self.assertEqual(
                replace(imported_summary, title=None),
                snapshot.summary,
            )
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

    async def test_session_aliases_are_durable_unique_and_support_legacy_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            first_id = await store.create_session("/workspace", "fixture", "model")
            second_id = await store.create_session("/workspace", "fixture", "model")
            third_id = await store.create_session("/workspace", "fixture", "model")

            await store.bind_session_alias("acp-v1", "acp-visible", first_id)
            await store.bind_session_alias("acp-v1", "acp-visible", first_id)

            reopened = SqliteSessionStore(store.database_path)
            await reopened.initialize()
            self.assertEqual(
                await reopened.resolve_session_alias("acp-v1", "acp-visible"),
                first_id,
            )
            self.assertEqual(
                await reopened.resolve_session_alias("acp-v1", second_id),
                second_id,
            )
            self.assertEqual(
                await reopened.get_or_create_session_alias(
                    "acp-v1",
                    first_id,
                    "unused-proposal",
                ),
                "acp-visible",
            )
            concurrent = await asyncio.gather(
                reopened.get_or_create_session_alias(
                    "acp-v1",
                    third_id,
                    "acp-third-a",
                ),
                store.get_or_create_session_alias(
                    "acp-v1",
                    third_id,
                    "acp-third-b",
                ),
            )
            self.assertEqual(len(set(concurrent)), 1)
            self.assertIn(concurrent[0], {"acp-third-a", "acp-third-b"})
            with self.assertRaisesRegex(SessionError, "already bound"):
                await reopened.bind_session_alias("acp-v1", "acp-visible", second_id)
            with self.assertRaisesRegex(SessionError, "already has an alias"):
                await reopened.bind_session_alias("acp-v1", "another-alias", first_id)
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await reopened.bind_session_alias("acp-v1", "missing", "missing")
            with self.assertRaisesRegex(SessionError, "unknown session alias"):
                await reopened.resolve_session_alias("acp-v1", "missing")
            with self.assertRaisesRegex(SessionError, "must not be empty"):
                await reopened.resolve_session_alias("", "acp-visible")
            with self.assertRaisesRegex(SessionError, "contains control"):
                await reopened.resolve_session_alias("acp-v1", "bad\nid")
            with self.assertRaisesRegex(SessionError, "too large"):
                await reopened.resolve_session_alias("acp-v1", "界" * 200)

    async def test_session_list_page_uses_stable_keyset_order_and_validates_cursor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            for session_id, day in (
                ("session-newest", 3),
                ("session-middle", 2),
                ("session-oldest", 1),
            ):
                timestamp = datetime(2026, 7, day, 12, tzinfo=UTC)
                await store.import_session(
                    SessionSnapshot(
                        SessionSummary(
                            id=session_id,
                            cwd="/workspace",
                            provider="fixture",
                            model="model",
                            created_at=timestamp,
                            updated_at=timestamp,
                        ),
                        (Message(Role.USER, session_id),),
                    )
                )

            first = await store.list_sessions_page(limit=2)
            second = await store.list_sessions_page(
                limit=2,
                before_updated_at=first[-1].updated_at,
                before_id=first[-1].id,
            )

            self.assertEqual(
                [summary.id for summary in first],
                ["session-newest", "session-middle"],
            )
            self.assertEqual([summary.id for summary in second], ["session-oldest"])
            with self.assertRaisesRegex(SessionError, "provided together"):
                await store.list_sessions_page(
                    limit=2,
                    before_updated_at=first[-1].updated_at,
                )
            with self.assertRaisesRegex(SessionError, "timezone-aware"):
                await store.list_sessions_page(
                    limit=2,
                    before_updated_at=datetime(2026, 7, 1, tzinfo=UTC).replace(tzinfo=None),
                    before_id="session-oldest",
                )

    async def test_manual_title_update_is_atomic_persistent_and_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                session_id,
                [Message(Role.USER, "original visible prompt")],
            )

            renamed = await store.update_session_title(
                session_id,
                "  Manual\n  searchable   title  ",
            )

            self.assertEqual(renamed.title, "Manual searchable title")
            manual_search = await store.search_sessions("manual searchable")
            self.assertEqual([hit.summary.id for hit in manual_search.results], [session_id])
            self.assertEqual(manual_search.results[0].matched_fields, ("title",))
            original_search = await store.search_sessions("original visible")
            self.assertEqual(original_search.results[0].matched_fields, ("content",))

            await store.save_messages(
                session_id,
                [
                    Message(Role.USER, "original visible prompt"),
                    Message(Role.ASSISTANT, "continued after rename"),
                ],
            )
            self.assertEqual(
                (await store.get_session(session_id)).title,
                "Manual searchable title",
            )

            with (
                patch(
                    "neuro_code.adapters.sqlite_session._upsert_search_document",
                    side_effect=RuntimeError("injected index failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected index failure"),
            ):
                await store.update_session_title(session_id, "Rolled back title")
            self.assertEqual(
                (await store.get_session(session_id)).title,
                "Manual searchable title",
            )
            self.assertEqual((await store.search_sessions("rolled back")).results, ())

            truncated = await store.update_session_title(session_id, "x" * 250)
            self.assertEqual(truncated.title, "x" * 200)
            with self.assertRaisesRegex(SessionError, "title must not be empty"):
                await store.update_session_title(session_id, " \n\t ")
            with self.assertRaisesRegex(SessionError, "unknown session"):
                await store.update_session_title("missing", "Valid title")

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
            self.assertEqual(version, (5,))
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
                (5,),
            )
            migrated.close()

    async def test_schema_v3_migration_backfills_escaped_content_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            messages = [
                {
                    "role": "user",
                    "content": 'debug escaped newlines\nand "quoted" sqlite content',
                }
            ]
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 3);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    context_affinity TEXT,
                    sandbox_profile TEXT
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    id, cwd, provider, model, messages_json, sandbox_profile
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "v3-id",
                    "/workspace",
                    "fixture",
                    "model",
                    json.dumps(messages),
                    "workspace",
                ),
            )
            connection.commit()
            connection.close()

            store = SqliteSessionStore(database)
            await store.initialize()

            summary = await store.get_session("v3-id")
            self.assertEqual(
                summary.title,
                'debug escaped newlines and "quoted" sqlite content',
            )
            page = await store.search_sessions(
                "escaped quoted",
                cwd="/workspace",
                include_content=True,
            )
            self.assertEqual([hit.summary.id for hit in page.results], ["v3-id"])
            self.assertIn("content", page.results[0].matched_fields)
            self.assertIsNotNone(page.results[0].snippet)

            migrated = sqlite3.connect(database)
            self.assertEqual(
                migrated.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (5,),
            )
            tables = {
                row[0]
                for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            migrated.close()
            self.assertIn("session_search_documents", tables)
            self.assertIn("session_search_fts", tables)

    async def test_schema_v4_migration_adds_session_aliases_without_rewriting_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            store = SqliteSessionStore(database)
            await store.initialize()
            session_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(session_id, [Message(Role.USER, "preserved")])
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE session_aliases")
            connection.execute("UPDATE schema_meta SET version = 4 WHERE singleton = 1")
            connection.commit()
            connection.close()

            migrated = SqliteSessionStore(database)
            await migrated.initialize()
            self.assertEqual(
                await migrated.load_messages(session_id),
                [Message(Role.USER, "preserved")],
            )
            await migrated.bind_session_alias("acp-v1", "acp-visible", session_id)
            self.assertEqual(
                await migrated.resolve_session_alias("acp-v1", "acp-visible"),
                session_id,
            )
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (5,),
            )
            connection.close()

    async def test_initialize_is_atomic_when_search_backfill_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 3);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    context_affinity TEXT,
                    sandbox_profile TEXT
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                """
            )
            connection.close()

            store = SqliteSessionStore(database)
            with (
                patch(
                    "neuro_code.adapters.sqlite_session._backfill_search_documents",
                    side_effect=RuntimeError("injected backfill failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "injected backfill failure"),
            ):
                await store.initialize()

            failed = sqlite3.connect(database)
            self.assertEqual(
                failed.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (3,),
            )
            columns = {row[1] for row in failed.execute("PRAGMA table_info(sessions)")}
            tables = {
                row[0]
                for row in failed.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            failed.close()
            self.assertNotIn("title", columns)
            self.assertNotIn("session_search_documents", tables)
            self.assertNotIn("session_search_fts", tables)

            await store.initialize()
            recovered = sqlite3.connect(database)
            self.assertEqual(
                recovered.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone(),
                (5,),
            )
            recovered.close()

    async def test_concurrent_initialize_is_serialized_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sessions.db"
            stores = [SqliteSessionStore(database) for _ in range(8)]

            await asyncio.gather(*(store.initialize() for store in stores))
            session_id = await stores[0].create_session("/workspace", "fixture", "model")
            await stores[0].save_messages(
                session_id,
                [Message(Role.USER, "concurrent migration search marker")],
            )
            await asyncio.gather(*(store.initialize() for store in reversed(stores)))

            page = await stores[-1].search_sessions("concurrent marker")
            self.assertEqual([hit.summary.id for hit in page.results], [session_id])
            connection = sqlite3.connect(database)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (5,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_search_documents").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_search_fts").fetchone(),
                (1,),
            )
            connection.close()

    async def test_search_indexes_visible_content_with_filters_and_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            primary_id = await store.create_session(
                "/workspace",
                "fixture",
                "model",
                sandbox_profile=SandboxProfile.WORKSPACE,
            )
            primary_items = [
                Message(
                    Role.USER,
                    "<system-reminder>private injected rules</system-reminder>\n"
                    "Fix SQLite session search for escaped quoted content across all platforms",
                ),
                PreservedContextItem(
                    ContextItemKind.REASONING,
                    {
                        "type": "reasoning",
                        "id": "private",
                        "summary": [{"type": "summary_text", "text": "privatecontextmarker"}],
                        "encrypted_content": "privateciphermarker",
                    },
                ),
                Message(
                    Role.ASSISTANT,
                    "I will inspect the index.",
                    tool_calls=(
                        ToolCall(
                            "call-1",
                            "read_file",
                            {"path": "src/search_index.py", "purpose": "toolmarker"},
                        ),
                    ),
                    reasoning_content="privatethoughtmarker",
                ),
                Message(
                    Role.TOOL,
                    "privatetoolresultmarker",
                    name="read_file",
                    tool_call_id="call-1",
                ),
            ]
            await store.save_session_items(primary_id, primary_items)

            second_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                second_id,
                [Message(Role.USER, "SQLite migration notes for another session")],
            )
            other_workspace_id = await store.create_session("/other", "fixture", "model")
            await store.save_messages(
                other_workspace_id,
                [Message(Role.USER, "SQLite search belongs to another workspace")],
            )

            summary = await store.get_session(primary_id)
            self.assertEqual(
                summary.title,
                "Fix SQLite session search for escaped quoted content across all",
            )
            await store.save_session_items(
                primary_id,
                [*primary_items, Message(Role.USER, "a later title must not replace the first")],
            )
            self.assertEqual((await store.get_session(primary_id)).title, summary.title)

            page = await store.search_sessions(
                "escaped quoted",
                cwd="/workspace",
                include_content=True,
            )
            self.assertEqual([hit.summary.id for hit in page.results], [primary_id])
            self.assertEqual(page.results[0].matched_fields, ("title", "content"))
            self.assertIsNotNone(page.results[0].snippet)

            tool_page = await store.search_sessions("read_file", cwd="/workspace")
            self.assertEqual([hit.summary.id for hit in tool_page.results], [primary_id])
            for private_query in (
                "privatecontextmarker",
                "privateciphermarker",
                "privatethoughtmarker",
                "privatetoolresultmarker",
                "toolmarker",
                "search_index",
            ):
                self.assertEqual(
                    (await store.search_sessions(private_query)).results,
                    (),
                )

            first_page = await store.search_sessions("SQLite", cwd="/workspace", limit=1)
            self.assertEqual(first_page.total_estimate, 2)
            self.assertEqual(first_page.next_offset, 1)
            second_page = await store.search_sessions(
                "SQLite",
                cwd="/workspace",
                limit=1,
                offset=1,
            )
            self.assertEqual(second_page.total_estimate, 2)
            self.assertIsNone(second_page.next_offset)
            self.assertNotEqual(
                first_page.results[0].summary.id,
                second_page.results[0].summary.id,
            )
            self.assertNotIn(
                other_workspace_id,
                {hit.summary.id for hit in first_page.results + second_page.results},
            )

            fallback = await store.search_sessions("quoted migration", cwd="/workspace")
            self.assertEqual(fallback.total_estimate, 2)

            for operation in (
                store.search_sessions(""),
                store.search_sessions("***"),
                store.search_sessions("query", limit=0),
                store.search_sessions("query", offset=-1),
            ):
                with self.assertRaises(SessionError):
                    await operation

    async def test_search_handles_unicode_syntax_ranking_and_bounded_snippets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()

            title_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(title_id, [Message(Role.USER, "priorityneedle")])
            body_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                body_id,
                [
                    Message(Role.USER, "Discuss unrelated adapters"),
                    Message(Role.ASSISTANT, "The body mentions priorityneedle once."),
                ],
            )
            unicode_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                unicode_id,
                [Message(Role.USER, "修复 中文会话 café 搜索")],
            )
            snippet_id = await store.create_session("/workspace", "fixture", "model")
            await store.save_messages(
                snippet_id,
                [
                    Message(Role.USER, "Bound the generated snippet"),
                    Message(Role.ASSISTANT, "snippetneedle" + ("x" * 1_000)),
                ],
            )

            ranked = await store.search_sessions("priorityneedle", cwd="/workspace")
            self.assertEqual(
                [hit.summary.id for hit in ranked.results],
                [title_id, body_id],
            )
            self.assertIn("title", ranked.results[0].matched_fields)
            self.assertEqual(ranked.results[1].matched_fields, ("content",))

            unicode_page = await store.search_sessions(
                "《中文会话》 CAFÉ",
                cwd="/workspace",
            )
            self.assertEqual([hit.summary.id for hit in unicode_page.results], [unicode_id])
            sanitized = await store.search_sessions(
                'priorityneedle OR "*"',
                cwd="/workspace",
            )
            self.assertEqual(
                {hit.summary.id for hit in sanitized.results},
                {title_id, body_id},
            )

            snippet_page = await store.search_sessions(
                "snippetneedle",
                cwd="/workspace",
                include_content=True,
            )
            self.assertEqual([hit.summary.id for hit in snippet_page.results], [snippet_id])
            self.assertIsNotNone(snippet_page.results[0].snippet)
            self.assertEqual(len(snippet_page.results[0].snippet or ""), 500)

            for operation in (
                store.search_sessions("x" * 1_001),
                store.search_sessions("query", cwd=""),
                store.search_sessions("query", limit=1_001),
                store.search_sessions("query", offset=1_000_001),
            ):
                with self.assertRaises(SessionError):
                    await operation

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
