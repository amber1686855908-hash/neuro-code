"""SQLite persistence schema owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3

from neuro_code.domain.leader import LeaderDecisionKind
from neuro_code.domain.task_dag_replan import MAX_DAG_REPLAN_DEPTH
from neuro_code.shared.errors import SessionError
from neuro_code.shared.limits import MAX_SUBAGENT_PARALLELISM


def _ensure_base_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL
        )
        """
    )
    connection.execute("INSERT OR IGNORE INTO schema_meta(singleton, version) VALUES (1, 1)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            messages_json TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (session_id, sequence),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_search_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_search_documents (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS session_search_fts USING fts5(
                title,
                content,
                content = 'session_search_documents',
                content_rowid = 'rowid',
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS session_search_documents_ai
            AFTER INSERT ON session_search_documents BEGIN
                INSERT INTO session_search_fts(rowid, title, content)
                VALUES (new.rowid, new.title, new.content);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS session_search_documents_ad
            AFTER DELETE ON session_search_documents BEGIN
                INSERT INTO session_search_fts(session_search_fts, rowid, title, content)
                VALUES ('delete', old.rowid, old.title, old.content);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS session_search_documents_au
            AFTER UPDATE OF title, content ON session_search_documents BEGIN
                INSERT INTO session_search_fts(session_search_fts, rowid, title, content)
                VALUES ('delete', old.rowid, old.title, old.content);
                INSERT INTO session_search_fts(rowid, title, content)
                VALUES (new.rowid, new.title, new.content);
            END
            """
        )
    except sqlite3.OperationalError as error:
        if "fts5" in str(error).casefold():
            raise SessionError("the installed SQLite build does not support FTS5") from error
        raise


def _ensure_session_alias_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_aliases (
            namespace TEXT NOT NULL,
            external_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (namespace, external_id),
            UNIQUE (namespace, session_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_session_plan_schema(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)").fetchall()}
    if "plan_json" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN plan_json TEXT NOT NULL DEFAULT ''")


def _ensure_session_task_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            plan_snapshot_json TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(session_tasks)").fetchall()
    }
    if "plan_snapshot_json" not in columns:
        connection.execute(
            "ALTER TABLE session_tasks ADD COLUMN plan_snapshot_json TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_tasks_by_session_started
        ON session_tasks(session_id, started_at DESC, task_id DESC)
        """
    )


def _ensure_session_plan_comment_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_plan_comments (
            comment_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_plan_comments_by_current_plan
        ON session_plan_comments(session_id, plan_fingerprint, created_at, comment_id)
        """
    )


def _ensure_session_execution_record_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_execution_records (
            session_id TEXT PRIMARY KEY,
            event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
            status TEXT NOT NULL,
            reason_code TEXT,
            finalized INTEGER NOT NULL CHECK (finalized IN (0, 1)),
            recoverable INTEGER NOT NULL CHECK (recoverable IN (0, 1)),
            completed_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_session_background_wake_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_background_wake_state (
            session_id TEXT PRIMARY KEY,
            announced_task_ids_json TEXT NOT NULL,
            pending_task_ids_json TEXT NOT NULL,
            wake_count INTEGER NOT NULL CHECK (wake_count >= 0),
            last_wake_at TEXT,
            wake_in_flight INTEGER NOT NULL CHECK (wake_in_flight IN (0, 1)),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )


def _ensure_subagent_link_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS subagent_links (
            parent_session_id TEXT NOT NULL,
            parent_task_id TEXT NOT NULL,
            child_session_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (parent_session_id, parent_task_id),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_task_id) REFERENCES session_tasks(task_id) ON DELETE CASCADE,
            FOREIGN KEY (child_session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS subagent_links_by_child
        ON subagent_links(child_session_id)
        """
    )


def _migrate_writable_subagent_lease_schema(connection: sqlite3.Connection) -> None:
    """Rebuild the populated lease table with session-retention FKs.

    SQLite does not support changing a foreign-key action with ``ALTER TABLE``.
    Drop only the old derived indexes, rename the legacy table, create the
    schema-16 table, copy every row, and recreate the indexes.  The caller
    owns the surrounding transaction, so any failure rolls back the complete
    migration without losing the durable lease rows.
    """

    table = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'writable_subagent_leases'
        """
    ).fetchone()
    if table is None:
        _ensure_writable_subagent_lease_schema(connection)
        return

    connection.execute("DROP INDEX IF EXISTS writable_subagent_active_parent")
    connection.execute("DROP INDEX IF EXISTS writable_subagent_active_worktree")
    connection.execute("DROP INDEX IF EXISTS writable_subagent_leases_by_state")
    connection.execute(
        "ALTER TABLE writable_subagent_leases RENAME TO writable_subagent_leases_v15"
    )
    _ensure_writable_subagent_lease_schema(connection)
    legacy_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(writable_subagent_leases_v15)").fetchall()
    }
    lease_columns = (
        "lease_id, parent_session_id, parent_task_id, worktree_id, "
        "parent_capability_fingerprint, parent_workspace_root, parent_common_dir, "
        "parent_source_worktree, parent_git_dir, parent_repository_head_sha, "
        "base_commit_sha, canonical_child_root, state, created_at, updated_at, "
        "worktree_common_dir, worktree_source_worktree, worktree_git_dir, "
        "worktree_repository_head_sha, worktree_path, worktree_branch, "
        "baseline_checkpoint_id, child_session_id, capability_fingerprint, "
        "grant_fingerprint, owner_pid, owner_token, final_workspace_fingerprint, "
        "workspace_changed, changed_file_count, error_kind"
    )
    source_scope = "execution_scope" if "execution_scope" in legacy_columns else "'standalone'"
    connection.execute(
        f"""
        INSERT INTO writable_subagent_leases(
            {lease_columns}, execution_scope, version
        )
        SELECT {lease_columns}, {source_scope}, version
        FROM writable_subagent_leases_v15
        """
    )
    connection.execute("DROP TABLE writable_subagent_leases_v15")


def _ensure_writable_subagent_lease_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS writable_subagent_leases (
            lease_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            parent_task_id TEXT NOT NULL,
            worktree_id TEXT NOT NULL,
            parent_capability_fingerprint TEXT NOT NULL,
            parent_workspace_root TEXT NOT NULL,
            parent_common_dir TEXT NOT NULL,
            parent_source_worktree TEXT NOT NULL,
            parent_git_dir TEXT NOT NULL,
            parent_repository_head_sha TEXT NOT NULL,
            base_commit_sha TEXT NOT NULL,
            canonical_child_root TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            worktree_common_dir TEXT,
            worktree_source_worktree TEXT,
            worktree_git_dir TEXT,
            worktree_repository_head_sha TEXT,
            worktree_path TEXT,
            worktree_branch TEXT,
            baseline_checkpoint_id TEXT,
            child_session_id TEXT,
            capability_fingerprint TEXT,
            grant_fingerprint TEXT,
            owner_pid INTEGER,
            owner_token TEXT NOT NULL,
            final_workspace_fingerprint TEXT,
            workspace_changed INTEGER,
            changed_file_count INTEGER,
            error_kind TEXT,
            execution_scope TEXT NOT NULL DEFAULT 'standalone'
                CHECK (execution_scope IN ('standalone', 'task_dag')),
            version INTEGER NOT NULL DEFAULT 0,
            UNIQUE(parent_session_id, parent_task_id),
            UNIQUE(worktree_id),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (child_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(writable_subagent_leases)")
    }
    connection.execute("DROP INDEX IF EXISTS writable_subagent_active_parent")
    if "execution_scope" in columns:
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS writable_subagent_active_parent
            ON writable_subagent_leases(parent_session_id)
            WHERE state IN ('allocating', 'worktree_ready', 'baseline_ready', 'active')
              AND execution_scope = 'standalone'
            """
        )
    else:
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS writable_subagent_active_parent
            ON writable_subagent_leases(parent_session_id)
            WHERE state IN ('allocating', 'worktree_ready', 'baseline_ready', 'active')
            """
        )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS writable_subagent_active_worktree
        ON writable_subagent_leases(worktree_id)
        WHERE state IN ('allocating', 'worktree_ready', 'baseline_ready', 'active')
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS writable_subagent_leases_by_state
        ON writable_subagent_leases(state, updated_at, lease_id)
        """
    )


def _ensure_parent_context_relay_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS parent_context_relays (
            relay_id TEXT PRIMARY KEY,
            lease_id TEXT NOT NULL UNIQUE,
            parent_session_id TEXT NOT NULL,
            parent_task_id TEXT NOT NULL,
            child_session_id TEXT NOT NULL UNIQUE,
            worktree_id TEXT NOT NULL UNIQUE,
            baseline_checkpoint_id TEXT NOT NULL,
            base_commit_sha TEXT NOT NULL,
            capability_fingerprint TEXT NOT NULL,
            grant_fingerprint TEXT NOT NULL,
            task_prompt_fingerprint TEXT NOT NULL,
            source_item_count INTEGER NOT NULL CHECK (source_item_count >= 0),
            items_json TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL,
            byte_count INTEGER NOT NULL CHECK (byte_count > 0),
            truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
            created_at TEXT NOT NULL,
            integrity_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state = 'ready'),
            FOREIGN KEY (lease_id) REFERENCES writable_subagent_leases(lease_id)
                ON DELETE RESTRICT,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (parent_task_id) REFERENCES session_tasks(task_id) ON DELETE RESTRICT,
            FOREIGN KEY (child_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS parent_context_relays_by_parent
        ON parent_context_relays(parent_session_id, created_at, relay_id)
        """
    )


def _migrate_task_dag_parallelism_schema(connection: sqlite3.Connection) -> None:
    """Add bounded DAG capacity and its scoped Writable lease policy."""

    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(task_dags)").fetchall()}
    if "max_parallel" not in columns:
        connection.execute(
            "ALTER TABLE task_dags ADD COLUMN max_parallel INTEGER NOT NULL DEFAULT 1 "
            f"CHECK (max_parallel >= 1 AND max_parallel <= {MAX_SUBAGENT_PARALLELISM})"
        )
    lease_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(writable_subagent_leases)").fetchall()
    }
    if "execution_scope" not in lease_columns:
        connection.execute(
            "ALTER TABLE writable_subagent_leases ADD COLUMN execution_scope TEXT NOT NULL "
            "DEFAULT 'standalone' CHECK (execution_scope IN ('standalone', 'task_dag'))"
        )


def _migrate_task_dag_execution_owner_schema(connection: sqlite3.Connection) -> None:
    """Persist the process owner that is provisioning each running DAG node."""

    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(task_dag_nodes)").fetchall()
    }
    if "execution_owner_pid" not in columns:
        connection.execute(
            "ALTER TABLE task_dag_nodes ADD COLUMN execution_owner_pid INTEGER "
            "CHECK (execution_owner_pid IS NULL OR execution_owner_pid > 0)"
        )
    if "execution_owner_token" not in columns:
        connection.execute("ALTER TABLE task_dag_nodes ADD COLUMN execution_owner_token TEXT")


def _ensure_task_dag_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS task_dags (
            dag_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            definition_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK (generation >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            active_node_id TEXT,
            max_parallel INTEGER NOT NULL DEFAULT 1
                CHECK (max_parallel >= 1 AND max_parallel <= {MAX_SUBAGENT_PARALLELISM}),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_dag_nodes (
            dag_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            prompt TEXT NOT NULL,
            prompt_fingerprint TEXT NOT NULL,
            dependencies_json TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind = 'writable_subagent'),
            state TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK (generation >= 0),
            parent_task_id TEXT,
            execution_owner_pid INTEGER CHECK (
                execution_owner_pid IS NULL OR execution_owner_pid > 0
            ),
            execution_owner_token TEXT,
            child_session_id TEXT,
            lease_id TEXT,
            worktree_id TEXT,
            baseline_checkpoint_id TEXT,
            relay_id TEXT,
            error_kind TEXT,
            error_reason TEXT,
            response_preview TEXT,
            final_workspace_fingerprint TEXT,
            changed_file_count INTEGER,
            PRIMARY KEY (dag_id, node_id),
            UNIQUE (dag_id, ordinal),
            FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS task_dags_by_parent
        ON task_dags(parent_session_id, updated_at, dag_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS task_dag_nodes_by_state
        ON task_dag_nodes(dag_id, state, ordinal, node_id)
        """
    )


def _ensure_task_dag_dependency_result_relay_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_dag_dependency_relays (
            relay_id TEXT PRIMARY KEY,
            dag_id TEXT NOT NULL,
            dag_definition_fingerprint TEXT NOT NULL,
            target_node_id TEXT NOT NULL,
            target_node_generation INTEGER NOT NULL CHECK (target_node_generation >= 0),
            target_node_definition_fingerprint TEXT NOT NULL,
            direct_dependency_ids_json TEXT NOT NULL,
            entries_json TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL,
            byte_count INTEGER NOT NULL CHECK (byte_count > 0),
            truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
            created_at TEXT NOT NULL,
            integrity_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state = 'ready'),
            UNIQUE (dag_id, target_node_id, target_node_generation),
            FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS task_dag_dependency_relays_by_target
        ON task_dag_dependency_relays(dag_id, target_node_id, target_node_generation)
        """
    )


def _ensure_task_dag_recovery_claim_schema(connection: sqlite3.Connection) -> None:
    """Create the cross-process owner fence for safe-not-started DAG recovery."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_dag_recovery_claims (
            claim_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            dag_id TEXT NOT NULL,
            dag_definition_fingerprint TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_generation INTEGER NOT NULL CHECK (node_generation >= 0),
            node_definition_fingerprint TEXT NOT NULL,
            parent_task_id TEXT NOT NULL,
            dependency_relay_id TEXT NOT NULL,
            dependency_relay_source_fingerprint TEXT NOT NULL,
            dependency_relay_content_fingerprint TEXT NOT NULL,
            dependency_relay_integrity_fingerprint TEXT NOT NULL,
            owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
            owner_token TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (dag_id, node_id, node_generation),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
            FOREIGN KEY (dependency_relay_id)
                REFERENCES task_dag_dependency_relays(relay_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS task_dag_recovery_claims_by_execution
        ON task_dag_recovery_claims(dag_id, node_id, node_generation)
        """
    )


def _migrate_leader_parallel_decision_schema(connection: sqlite3.Connection) -> None:
    """Add parent binding and canonical batch-selection projections."""

    attempt_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(leader_attempts)").fetchall()
    }
    if "parent_session_id" not in attempt_columns:
        connection.execute("ALTER TABLE leader_attempts ADD COLUMN parent_session_id TEXT")
    decision_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(leader_decisions)").fetchall()
    }
    if "parent_session_id" not in decision_columns:
        connection.execute("ALTER TABLE leader_decisions ADD COLUMN parent_session_id TEXT")
    if "selected_node_ids_json" not in decision_columns:
        connection.execute(
            "ALTER TABLE leader_decisions ADD COLUMN selected_node_ids_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "selected_node_generations_json" not in decision_columns:
        connection.execute(
            "ALTER TABLE leader_decisions ADD COLUMN selected_node_generations_json TEXT NOT NULL DEFAULT '[]'"
        )
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'leader_decisions'"
    ).fetchone()
    if table_row is not None and "SELECT_NODES" not in str(table_row[0]):
        connection.execute("DROP INDEX IF EXISTS leader_decisions_by_dag")
        connection.execute("ALTER TABLE leader_decisions RENAME TO leader_decisions_legacy")
        _ensure_leader_schema(connection)
        connection.execute(
            """
            INSERT INTO leader_decisions(
                decision_id, attempt_id, dag_id, parent_session_id, leader_session_id,
                dag_generation, definition_fingerprint, evidence_fingerprint,
                kind, selected_node_id, selected_node_ids_json,
                selected_node_generations_json, summary, created_at
            )
            SELECT decision_id, attempt_id, dag_id, parent_session_id, leader_session_id,
                   dag_generation, definition_fingerprint, evidence_fingerprint,
                   kind, selected_node_id, selected_node_ids_json,
                   selected_node_generations_json, summary, created_at
            FROM leader_decisions_legacy
            """
        )
        connection.execute("DROP TABLE leader_decisions_legacy")
    connection.execute(
        """
        UPDATE leader_attempts
        SET parent_session_id = (
            SELECT parent_session_id FROM task_dags WHERE task_dags.dag_id = leader_attempts.dag_id
        )
        WHERE parent_session_id IS NULL
        """
    )
    connection.execute(
        """
        UPDATE leader_decisions
        SET parent_session_id = (
            SELECT parent_session_id FROM task_dags WHERE task_dags.dag_id = leader_decisions.dag_id
        )
        WHERE parent_session_id IS NULL
        """
    )
    rows = connection.execute(
        "SELECT decision_id, kind, selected_node_id FROM leader_decisions"
    ).fetchall()
    for decision_id, raw_kind, selected_node_id in rows:
        selected = (
            [str(selected_node_id)]
            if str(raw_kind) == LeaderDecisionKind.SELECT_NODE.value
            and selected_node_id is not None
            else []
        )
        connection.execute(
            "UPDATE leader_decisions SET selected_node_ids_json = ? WHERE decision_id = ?",
            (json.dumps(selected, ensure_ascii=False, separators=(",", ":")), decision_id),
        )


def _ensure_leader_schema(connection: sqlite3.Connection) -> None:
    """Create the durable Leader attempt/decision projections.

    These rows are separate from Task DAG lifecycle rows: the Leader owns a
    model-decision attempt, while the DAG remains the only execution owner.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS leader_attempts (
            attempt_id TEXT PRIMARY KEY,
            dag_id TEXT NOT NULL,
            parent_session_id TEXT,
            leader_session_id TEXT NOT NULL,
            objective_fingerprint TEXT NOT NULL,
            dag_generation INTEGER NOT NULL CHECK (dag_generation >= 0),
            definition_fingerprint TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            turn_id TEXT NOT NULL UNIQUE,
            model_response TEXT,
            decision_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(
                dag_id,
                dag_generation,
                definition_fingerprint,
                evidence_fingerprint,
                objective_fingerprint
            ),
            FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
            FOREIGN KEY (leader_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS leader_decisions (
            decision_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE,
            dag_id TEXT NOT NULL,
            parent_session_id TEXT,
            leader_session_id TEXT NOT NULL,
            dag_generation INTEGER NOT NULL CHECK (dag_generation >= 0),
            definition_fingerprint TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('SELECT_NODE', 'SELECT_NODES', 'FINALIZE')),
            selected_node_id TEXT,
            selected_node_ids_json TEXT NOT NULL DEFAULT '[]',
            selected_node_generations_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (attempt_id) REFERENCES leader_attempts(attempt_id) ON DELETE RESTRICT,
            FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
            FOREIGN KEY (leader_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS leader_attempts_by_dag
        ON leader_attempts(dag_id, dag_generation, created_at, attempt_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS leader_decisions_by_dag
        ON leader_decisions(dag_id, created_at, decision_id)
        """
    )


def _migrate_model_planning_schema(connection: sqlite3.Connection) -> None:
    """Create the standalone planning projection during schema 24 -> 25."""

    _ensure_model_planning_schema(connection)


def _ensure_model_planning_schema(connection: sqlite3.Connection) -> None:
    """Create durable planner lifecycle and immutable proposal tables.

    The intended DAG id is deliberately not a foreign key: the planner
    attempt exists before TaskDag publication.  The published ``dag_id`` is
    linked once the existing TaskDag owner has inserted the graph.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_planning_attempts (
            planning_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            objective_fingerprint TEXT NOT NULL,
            context_fingerprint TEXT NOT NULL,
            planner_session_id TEXT NOT NULL,
            planner_turn_id TEXT NOT NULL UNIQUE,
            intended_dag_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK (state IN (
                'claimed', 'provider_fenced', 'model_committed',
                'proposal_published', 'dag_published', 'completed',
                'stale', 'indeterminate'
            )),
            owner_id TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            model_response TEXT,
            proposal_fingerprint TEXT,
            dag_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (planner_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_plan_proposals (
            proposal_id TEXT PRIMARY KEY,
            planning_id TEXT NOT NULL UNIQUE,
            parent_session_id TEXT NOT NULL,
            intended_dag_id TEXT NOT NULL,
            objective_fingerprint TEXT NOT NULL,
            context_fingerprint TEXT NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (planning_id)
                REFERENCES orchestration_planning_attempts(planning_id) ON DELETE RESTRICT,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS orchestration_planning_attempts_by_state
        ON orchestration_planning_attempts(state, updated_at, planning_id)
        """
    )


def _migrate_task_dag_replan_schema(connection: sqlite3.Connection) -> None:
    """Create the bounded revision projection during schema 25 -> 26."""

    _ensure_task_dag_replan_schema(connection)


def _ensure_task_dag_replan_schema(connection: sqlite3.Connection) -> None:
    """Create insert-only replan attempts and successor proposals.

    A replan source is linked to an existing immutable DAG.  The intended
    successor remains unlinked until the existing Task DAG owner publishes it;
    this mirrors the model-planning publication boundary.
    """

    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS orchestration_dag_replan_attempts (
            revision_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            source_dag_id TEXT NOT NULL,
            source_definition_fingerprint TEXT NOT NULL,
            source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
            source_state TEXT NOT NULL CHECK (source_state = 'failed'),
            revision_depth INTEGER NOT NULL CHECK (
                revision_depth >= 1 AND revision_depth <= {MAX_DAG_REPLAN_DEPTH}
            ),
            evidence_fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            planner_session_id TEXT NOT NULL,
            planner_turn_id TEXT NOT NULL UNIQUE,
            intended_successor_dag_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK (state IN (
                'claimed', 'provider_fenced', 'model_committed',
                'proposal_published', 'successor_dag_published', 'completed',
                'stale', 'indeterminate'
            )),
            owner_id TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            model_response TEXT,
            proposal_fingerprint TEXT,
            successor_dag_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(
                source_dag_id, source_definition_fingerprint, source_generation
            ),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
            FOREIGN KEY (planner_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (successor_dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_dag_replan_proposals (
            proposal_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL UNIQUE,
            parent_session_id TEXT NOT NULL,
            source_dag_id TEXT NOT NULL,
            source_definition_fingerprint TEXT NOT NULL,
            source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
            evidence_fingerprint TEXT NOT NULL,
            intended_successor_dag_id TEXT NOT NULL,
            proposal_fingerprint TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (revision_id)
                REFERENCES orchestration_dag_replan_attempts(revision_id) ON DELETE RESTRICT,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (source_dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS orchestration_dag_replan_attempts_by_source
        ON orchestration_dag_replan_attempts(source_dag_id, source_generation, revision_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS orchestration_dag_replan_attempts_by_state
        ON orchestration_dag_replan_attempts(state, updated_at, revision_id)
        """
    )


def _migrate_agent_swarm_schema(connection: sqlite3.Connection) -> None:
    """Create the durable Swarm run projection during schema 26 -> 27."""

    _ensure_agent_swarm_schema(connection)


def _ensure_agent_swarm_schema(connection: sqlite3.Connection) -> None:
    """Create the insert-once bounded Agent Swarm lifecycle projection."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_swarm_runs (
            swarm_run_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            objective_fingerprint TEXT NOT NULL,
            planning_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK (state IN (
                'claimed', 'planning', 'planned', 'executing', 'replanning',
                'finalizing', 'completed', 'failed', 'indeterminate'
            )),
            generation INTEGER NOT NULL CHECK (generation >= 0),
            owner_id TEXT NOT NULL,
            owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
            owner_token TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            planner_session_id TEXT,
            planner_turn_id TEXT,
            proposal_fingerprint TEXT,
            root_dag_id TEXT UNIQUE,
            current_dag_id TEXT,
            current_dag_generation INTEGER,
            current_dag_definition_fingerprint TEXT,
            replan_revision_id TEXT UNIQUE,
            successor_dag_id TEXT UNIQUE,
            final_response TEXT,
            final_result_fingerprint TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (planner_session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY (root_dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
            FOREIGN KEY (current_dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
            FOREIGN KEY (successor_dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS orchestration_swarm_runs_by_state
        ON orchestration_swarm_runs(state, updated_at, swarm_run_id)
        """
    )


def _migrate_ultracode_schema(connection: sqlite3.Connection) -> None:
    """Create the durable Ultracode projection during schema 27 -> 28."""

    _ensure_ultracode_schema(connection)


def _ensure_ultracode_schema(connection: sqlite3.Connection) -> None:
    """Create the insert-once, one-branch Ultracode lifecycle projection."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_ultracode_executions (
            execution_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            parent_turn_id TEXT NOT NULL UNIQUE,
            input_fingerprint TEXT NOT NULL,
            context_fingerprint TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('main_max', 'bounded_swarm')),
            downstream_id TEXT NOT NULL UNIQUE,
            provider_name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            context_affinity TEXT,
            state TEXT NOT NULL CHECK (state IN (
                'decided', 'main_max_running', 'bounded_swarm_running',
                'finalizing', 'completed', 'indeterminate'
            )),
            generation INTEGER NOT NULL CHECK (generation >= 0),
            owner_id TEXT NOT NULL,
            owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
            owner_token TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            final_response TEXT,
            final_result_fingerprint TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS orchestration_ultracode_executions_by_state
        ON orchestration_ultracode_executions(state, updated_at, execution_id)
        """
    )


def _migrate_result_adoption_schema(connection: sqlite3.Connection) -> None:
    """Create durable result-adoption state during schema 28 -> 29."""

    _ensure_result_adoption_schema(connection)


def _ensure_result_adoption_schema(connection: sqlite3.Connection) -> None:
    """Create one insert-once adoption plan and its per-target CAS rows."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS result_adoptions (
            adoption_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'claimed', 'verified', 'applying', 'verifying', 'completed',
                'conflict', 'failed', 'indeterminate'
            )),
            owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
            owner_token TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_kind TEXT,
            version INTEGER NOT NULL CHECK (version >= 0),
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS result_adoption_targets (
            adoption_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            target_json TEXT NOT NULL,
            path TEXT NOT NULL,
            pre_image_fingerprint TEXT NOT NULL,
            desired_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'not_started', 'applying', 'retryable', 'applied', 'conflict', 'failed',
                'indeterminate'
            )),
            observed_fingerprint TEXT,
            error_kind TEXT,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 0),
            PRIMARY KEY (adoption_id, ordinal),
            UNIQUE (adoption_id, path),
            FOREIGN KEY (adoption_id) REFERENCES result_adoptions(adoption_id)
                ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS result_adoptions_by_parent_state
        ON result_adoptions(parent_session_id, state, updated_at, adoption_id)
        """
    )


def _ensure_session_compaction_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_compaction_items (
            compaction_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            provider_name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            capacity_tokens INTEGER NOT NULL CHECK (capacity_tokens > 0),
            context_affinity TEXT,
            source_item_count INTEGER NOT NULL CHECK (source_item_count > 0),
            protected_item_count INTEGER NOT NULL CHECK (protected_item_count >= 0),
            recent_item_count INTEGER NOT NULL CHECK (recent_item_count >= 0),
            candidate_start INTEGER NOT NULL CHECK (candidate_start >= 0),
            candidate_end INTEGER NOT NULL CHECK (candidate_end > candidate_start),
            target_tokens INTEGER NOT NULL CHECK (target_tokens > 0),
            summary_tokens INTEGER NOT NULL CHECK (summary_tokens > 0),
            source_fingerprint TEXT NOT NULL,
            summary TEXT NOT NULL,
            summary_redacted INTEGER NOT NULL CHECK (summary_redacted = 1),
            summary_truncated INTEGER NOT NULL CHECK (summary_truncated IN (0, 1)),
            created_at TEXT NOT NULL,
            UNIQUE(session_id, source_fingerprint, candidate_start, candidate_end),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_compaction_items_by_session
        ON session_compaction_items(session_id, created_at ASC, compaction_id ASC)
        """
    )


def _ensure_session_turn_attempt_schema(connection: sqlite3.Connection) -> None:
    """Create the canonical crash-recovery attempt projection.

    The row is the small source of truth for accepted input, sticky lifecycle
    facts, and explicit resolution.  The append-only events table remains the
    ordered audit evidence and is updated in the same transaction as each fact.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_turn_attempts (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            source TEXT NOT NULL,
            task_id TEXT,
            input_json TEXT NOT NULL DEFAULT '',
            input_fingerprint TEXT NOT NULL,
            input_reconstructable INTEGER NOT NULL CHECK (input_reconstructable IN (0, 1)),
            accepted_at TEXT NOT NULL,
            resolution TEXT,
            resolution_at TEXT,
            request_started_count INTEGER NOT NULL DEFAULT 0 CHECK (request_started_count >= 0),
            request_id TEXT,
            step INTEGER,
            provider TEXT,
            model TEXT,
            output_started INTEGER NOT NULL DEFAULT 0 CHECK (output_started IN (0, 1)),
            tool_started_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_started_count >= 0),
            side_effecting_tool_started INTEGER NOT NULL DEFAULT 0
                CHECK (side_effecting_tool_started IN (0, 1)),
            last_tool_id TEXT,
            last_tool_name TEXT,
            last_stage TEXT NOT NULL DEFAULT 'accepted',
            last_stage_at TEXT,
            fact_conflict INTEGER NOT NULL DEFAULT 0 CHECK (fact_conflict IN (0, 1)),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS session_turn_attempts_by_session_status
        ON session_turn_attempts(session_id, resolution, accepted_at DESC, turn_id DESC)
        """
    )
