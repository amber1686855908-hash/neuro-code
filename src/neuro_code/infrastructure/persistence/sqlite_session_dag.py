"""SQLite persistence dag owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime

from neuro_code.application.ports.task_dag import TaskDagError
from neuro_code.application.ports.task_dag_recovery import (
    TaskDagRecoveryClaimError,
    TaskDagRecoveryClaimResult,
)
from neuro_code.application.ports.task_dag_result_relay import TaskDagDependencyResultRelayError
from neuro_code.domain.task_dag import (
    TaskDag,
    TaskDagNode,
    TaskDagNodeKind,
    TaskDagNodeState,
    TaskDagState,
)
from neuro_code.domain.task_dag_recovery import TaskDagRecoveryClaim
from neuro_code.domain.task_dag_result_relay import (
    TaskDagDependencyResultEntry,
    TaskDagDependencyResultRelay,
)
from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceState
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_subagents import (
    _PARENT_CONTEXT_RELAY_SELECT,
    _parent_context_relay_from_row,
)
from neuro_code.shared.async_utils import run_blocking


class DagMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def insert_task_dag(self, dag: TaskDag) -> TaskDag:
        if not isinstance(dag, TaskDag):
            raise TypeError("task DAG must be canonical")

        def insert() -> TaskDag:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = _load_task_dag(connection, dag.dag_id)
                    if current is not None:
                        if current.definition_fingerprint != dag.definition_fingerprint:
                            raise TaskDagError(
                                "task DAG identity already exists with a different definition",
                                kind="protocol",
                            )
                        if current.max_parallel != dag.max_parallel:
                            raise TaskDagError(
                                "task DAG identity already exists with a different max_parallel",
                                kind="protocol",
                            )
                        return current
                    if dag.created_at is None or dag.updated_at is None:
                        raise TaskDagError("task DAG timestamps are required", kind="protocol")
                    connection.execute(
                        """
                        INSERT INTO task_dags(
                            dag_id, parent_session_id, definition_fingerprint,
                            state, generation, created_at, updated_at, active_node_id, max_parallel
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dag.dag_id,
                            dag.parent_session_id,
                            dag.definition_fingerprint,
                            dag.state.value,
                            dag.generation,
                            dag.created_at.isoformat(),
                            dag.updated_at.isoformat(),
                            dag.active_node_id,
                            dag.max_parallel,
                        ),
                    )
                    for node in dag.nodes:
                        connection.execute(
                            """
                            INSERT INTO task_dag_nodes(
                                dag_id, node_id, ordinal, prompt, prompt_fingerprint,
                                dependencies_json, kind, state, generation,
                                parent_task_id, execution_owner_pid, execution_owner_token,
                                child_session_id, lease_id, worktree_id,
                                baseline_checkpoint_id, relay_id, error_kind, error_reason,
                                response_preview, final_workspace_fingerprint, changed_file_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            _task_dag_node_values(dag.dag_id, node),
                        )
                return dag
            except TaskDagError:
                raise
            except sqlite3.IntegrityError as error:
                raise TaskDagError("task DAG definition could not be persisted") from error
            except sqlite3.Error as error:
                raise TaskDagError("task DAG definition could not be persisted") from error

        async with self._write_lock:
            return await run_blocking(insert)

    async def get_task_dag(self, dag_id: str) -> TaskDag | None:
        _validated_task_dag_identifier(dag_id)

        def load() -> TaskDag | None:
            try:
                with closing(self._connect()) as connection:
                    # The DAG row and its node rows form one logical snapshot.  A
                    # read transaction is required because SQLite otherwise gives
                    # each SELECT its own snapshot in WAL mode; a concurrent
                    # finish could otherwise pair the old active_node_id with a
                    # new terminal node and fail TaskDag.__post_init__().
                    connection.execute("BEGIN")
                    try:
                        result = _load_task_dag(connection, dag_id)
                        connection.commit()
                        return result
                    except BaseException:
                        connection.rollback()
                        raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise TaskDagError("task DAG record is invalid", kind="integrity") from error
            except sqlite3.Error as error:
                raise TaskDagError("task DAG could not be loaded") from error

        return await run_blocking(load)

    async def compare_and_transition_task_dag(
        self,
        dag: TaskDag,
        *,
        expected_generation: int,
        expected_state: TaskDagState,
    ) -> TaskDag:
        if not isinstance(dag, TaskDag):
            raise TypeError("task DAG must be canonical")
        if isinstance(expected_generation, bool) or expected_generation < 0:
            raise TypeError("task DAG expected generation must be non-negative")
        if not isinstance(expected_state, TaskDagState):
            raise TypeError("task DAG expected state must be canonical")

        def transition() -> TaskDag:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = _load_task_dag(connection, dag.dag_id)
                    if current is None:
                        raise TaskDagError("task DAG is missing", kind="unmanaged")
                    _verify_task_dag_definition(current, dag)
                    if (
                        current.generation != expected_generation
                        or current.state is not expected_state
                        or dag.generation != expected_generation + 1
                        or dag.active_node_id != current.active_node_id
                        or dag.max_parallel != current.max_parallel
                    ):
                        raise TaskDagError(
                            "task DAG was changed by another scheduler",
                            kind="concurrent_modification",
                        )
                    if not _task_dag_state_transition_allowed(current.state, dag.state):
                        raise TaskDagError(
                            "invalid task DAG state transition",
                            kind="protocol",
                        )
                    if dag.state.terminal and current.running_node_ids:
                        raise TaskDagError(
                            "task DAG cannot become terminal while a node is running",
                            kind="protocol",
                        )
                    if dag.updated_at is None:
                        raise TaskDagError("task DAG update time is missing", kind="protocol")
                    cursor = connection.execute(
                        """
                        UPDATE task_dags
                        SET state = ?, generation = ?, updated_at = ?
                        WHERE dag_id = ? AND generation = ? AND state = ?
                        """,
                        (
                            dag.state.value,
                            dag.generation,
                            dag.updated_at.isoformat(),
                            dag.dag_id,
                            expected_generation,
                            expected_state.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise TaskDagError(
                            "task DAG was changed by another scheduler",
                            kind="concurrent_modification",
                        )
                    result = _load_task_dag(connection, dag.dag_id)
                    if result is None:
                        raise TaskDagError("task DAG disappeared after transition")
                    return result
            except TaskDagError:
                raise
            except sqlite3.Error as error:
                raise TaskDagError("task DAG transition failed") from error

        async with self._write_lock:
            return await run_blocking(transition)

    async def compare_and_transition_task_dag_node(
        self,
        dag_id: str,
        node: TaskDagNode,
        *,
        expected_generation: int,
        expected_state: TaskDagNodeState,
    ) -> TaskDag:
        _validated_task_dag_identifier(dag_id)
        if not isinstance(node, TaskDagNode):
            raise TypeError("task DAG node must be canonical")
        if isinstance(expected_generation, bool) or expected_generation < 0:
            raise TypeError("task DAG node expected generation must be non-negative")
        if not isinstance(expected_state, TaskDagNodeState):
            raise TypeError("task DAG node expected state must be canonical")

        def transition() -> TaskDag:
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current_dag = _load_task_dag(connection, dag_id)
                    if current_dag is None:
                        raise TaskDagError("task DAG is missing", kind="unmanaged")
                    if node.state is TaskDagNodeState.RUNNING:
                        raise TaskDagError(
                            "running task DAG nodes must use the atomic capacity claim",
                            kind="protocol",
                        )
                    current = current_dag.node(node.node_id)
                    _verify_task_dag_node_definition(current, node)
                    if (
                        current.generation != expected_generation
                        or current.state is not expected_state
                        or node.generation != expected_generation + 1
                        or not current.can_transition_to(node.state)
                    ):
                        raise TaskDagError(
                            "task DAG node was changed by another scheduler",
                            kind="concurrent_modification",
                        )
                    cursor = connection.execute(
                        _TASK_DAG_NODE_UPDATE
                        + " WHERE dag_id = ? AND node_id = ? AND generation = ? AND state = ?",
                        (
                            *_task_dag_node_mutable_values(node),
                            dag_id,
                            node.node_id,
                            expected_generation,
                            expected_state.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise TaskDagError(
                            "task DAG node was changed by another scheduler",
                            kind="concurrent_modification",
                        )
                    result = _load_task_dag(connection, dag_id)
                    if result is None:
                        raise TaskDagError("task DAG disappeared after node transition")
                    return result
            except TaskDagError:
                raise
            except sqlite3.Error as error:
                raise TaskDagError("task DAG node transition failed") from error

        async with self._write_lock:
            return await run_blocking(transition)

    async def claim_task_dag_node(
        self,
        dag_id: str,
        node: TaskDagNode,
        *,
        expected_generation: int,
        expected_state: TaskDagNodeState,
        updated_at: datetime,
        expected_dag_generation: int | None = None,
    ) -> TaskDag:
        _validated_task_dag_identifier(dag_id)
        if not isinstance(node, TaskDagNode) or node.state is not TaskDagNodeState.RUNNING:
            raise TypeError("task DAG claim must contain a running node")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("task DAG claim update time must be timezone-aware")
        if expected_dag_generation is not None and (
            isinstance(expected_dag_generation, bool)
            or not isinstance(expected_dag_generation, int)
            or expected_dag_generation < 0
        ):
            raise TypeError("task DAG expected graph generation must be non-negative")

        def claim() -> TaskDag:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current_dag = _load_task_dag(connection, dag_id)
                if current_dag is None:
                    raise TaskDagError("task DAG is missing", kind="unmanaged")
                if (
                    expected_dag_generation is not None
                    and current_dag.generation != expected_dag_generation
                ):
                    raise TaskDagError(
                        "task DAG graph generation was changed by another scheduler",
                        kind="concurrent_modification",
                    )
                if current_dag.state.terminal:
                    raise TaskDagError(
                        "task DAG is already terminal",
                        kind="concurrent_modification",
                    )
                running_count_row = connection.execute(
                    "SELECT COUNT(*) FROM task_dag_nodes WHERE dag_id = ? AND state = ?",
                    (dag_id, TaskDagNodeState.RUNNING.value),
                ).fetchone()
                running_count = int(running_count_row[0]) if running_count_row is not None else 0
                if running_count >= current_dag.max_parallel:
                    raise TaskDagError(
                        "task DAG parallel capacity is full",
                        kind="concurrent_modification",
                    )
                current = current_dag.node(node.node_id)
                _verify_task_dag_node_definition(current, node)
                if (
                    current.generation != expected_generation
                    or current.state is not expected_state
                    or node.generation != expected_generation + 1
                    or expected_state is not TaskDagNodeState.READY
                    or not current.can_transition_to(TaskDagNodeState.RUNNING)
                ):
                    raise TaskDagError(
                        "task DAG node cannot be claimed",
                        kind="concurrent_modification",
                    )
                node_cursor = connection.execute(
                    _TASK_DAG_NODE_UPDATE
                    + " WHERE dag_id = ? AND node_id = ? AND generation = ? AND state = ?",
                    (
                        *_task_dag_node_mutable_values(node),
                        dag_id,
                        node.node_id,
                        expected_generation,
                        expected_state.value,
                    ),
                )
                if node_cursor.rowcount != 1:
                    raise TaskDagError(
                        "task DAG node was changed by another scheduler",
                        kind="concurrent_modification",
                    )
                graph_cursor = connection.execute(
                    """
                    UPDATE task_dags
                    SET state = ?, generation = generation + 1,
                        updated_at = ?, active_node_id = ?
                    WHERE dag_id = ? AND generation = ?
                    """,
                    (
                        TaskDagState.RUNNING.value,
                        updated_at.isoformat(),
                        node.node_id if running_count == 0 else None,
                        dag_id,
                        current_dag.generation,
                    ),
                )
                if graph_cursor.rowcount != 1:
                    raise TaskDagError(
                        "task DAG parallel claim was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_task_dag(connection, dag_id)
                if result is None:
                    raise TaskDagError("task DAG disappeared after node claim")
                return result
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def finish_task_dag_node(
        self,
        dag_id: str,
        node: TaskDagNode,
        *,
        expected_generation: int,
        expected_state: TaskDagNodeState,
        updated_at: datetime,
    ) -> TaskDag:
        _validated_task_dag_identifier(dag_id)
        if not isinstance(node, TaskDagNode) or not node.state.terminal:
            raise TypeError("task DAG finish must contain a terminal node")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("task DAG finish update time must be timezone-aware")

        def finish() -> TaskDag:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current_dag = _load_task_dag(connection, dag_id)
                if current_dag is None:
                    raise TaskDagError("task DAG is missing", kind="unmanaged")
                current = current_dag.node(node.node_id)
                _verify_task_dag_node_definition(current, node)
                if (
                    current.generation != expected_generation
                    or current.state is not expected_state
                    or expected_state is not TaskDagNodeState.RUNNING
                    or node.generation != expected_generation + 1
                    or not current.can_transition_to(node.state)
                ):
                    raise TaskDagError(
                        "task DAG node cannot be finished",
                        kind="concurrent_modification",
                    )
                node_cursor = connection.execute(
                    _TASK_DAG_NODE_UPDATE
                    + " WHERE dag_id = ? AND node_id = ? AND generation = ? AND state = ?",
                    (
                        *_task_dag_node_mutable_values(node),
                        dag_id,
                        node.node_id,
                        expected_generation,
                        expected_state.value,
                    ),
                )
                if node_cursor.rowcount != 1:
                    raise TaskDagError(
                        "task DAG node was changed by another scheduler",
                        kind="concurrent_modification",
                    )
                running_rows = connection.execute(
                    """
                    SELECT node_id FROM task_dag_nodes
                    WHERE dag_id = ? AND state = ?
                    ORDER BY ordinal ASC, node_id ASC
                    """,
                    (dag_id, TaskDagNodeState.RUNNING.value),
                ).fetchall()
                legacy_active_node_id = str(running_rows[0][0]) if len(running_rows) == 1 else None
                graph_cursor = connection.execute(
                    """
                    UPDATE task_dags
                    SET generation = generation + 1, updated_at = ?, active_node_id = ?
                    WHERE dag_id = ? AND generation = ?
                    """,
                    (
                        updated_at.isoformat(),
                        legacy_active_node_id,
                        dag_id,
                        current_dag.generation,
                    ),
                )
                if graph_cursor.rowcount != 1:
                    raise TaskDagError(
                        "task DAG parallel finish was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_task_dag(connection, dag_id)
                if result is None:
                    raise TaskDagError("task DAG disappeared after node finish")
                return result
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(finish)

    async def insert_task_dag_dependency_relay(
        self,
        relay: TaskDagDependencyResultRelay,
    ) -> TaskDagDependencyResultRelay:
        """Publish one immutable relay with exact DAG/worker evidence checks."""

        if not isinstance(relay, TaskDagDependencyResultRelay):
            raise TypeError("DAG dependency result relay must be canonical")

        def insert() -> TaskDagDependencyResultRelay:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                dag = _load_task_dag(connection, relay.dag_id)
                if dag is None:
                    raise TaskDagDependencyResultRelayError(
                        "DAG dependency relay DAG is missing",
                        kind="unmanaged",
                    )
                current = _load_task_dag_dependency_result_relay(
                    connection,
                    relay_id=relay.relay_id,
                )
                by_target = _load_task_dag_dependency_result_relay_for_target(
                    connection,
                    relay.dag_id,
                    relay.target_node_id,
                    relay.target_node_generation,
                )
                existing = current or by_target
                if existing is not None:
                    if existing.publication_payload != relay.publication_payload:
                        raise TaskDagDependencyResultRelayError(
                            "an immutable DAG dependency relay already exists with a different payload",
                            kind="concurrent_modification",
                        )
                    connection.commit()
                    return existing
                _verify_task_dag_dependency_relay_linkage(connection, relay, dag)
                connection.execute(
                    """
                    INSERT INTO task_dag_dependency_relays(
                        relay_id, dag_id, dag_definition_fingerprint, target_node_id,
                        target_node_generation, target_node_definition_fingerprint,
                        direct_dependency_ids_json, entries_json, source_fingerprint,
                        content_fingerprint, byte_count, truncated, created_at,
                        integrity_fingerprint, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')
                    """,
                    _task_dag_dependency_result_relay_values(relay),
                )
                persisted = _load_task_dag_dependency_result_relay(
                    connection,
                    relay_id=relay.relay_id,
                )
                if persisted is None or persisted != relay:
                    raise TaskDagDependencyResultRelayError(
                        "DAG dependency relay was not durably verified",
                        kind="integrity",
                    )
                connection.commit()
                return persisted
            except TaskDagDependencyResultRelayError:
                connection.rollback()
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                raise TaskDagDependencyResultRelayError(
                    "DAG dependency relay integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise TaskDagDependencyResultRelayError(
                    "DAG dependency relay publication conflicts with existing evidence",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagDependencyResultRelayError(
                    "DAG dependency relay could not be persisted",
                ) from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(insert)

    async def get_task_dag_dependency_relay(
        self,
        relay_id: str,
    ) -> TaskDagDependencyResultRelay | None:
        _validated_task_dag_identifier(relay_id)

        def load() -> TaskDagDependencyResultRelay | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_task_dag_dependency_result_relay(
                        connection,
                        relay_id=relay_id,
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise TaskDagDependencyResultRelayError(
                    "DAG dependency relay integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise TaskDagDependencyResultRelayError(
                    "DAG dependency relay could not be loaded",
                ) from error

        return await run_blocking(load)

    async def get_task_dag_dependency_relay_for_target(
        self,
        dag_id: str,
        target_node_id: str,
        target_node_generation: int,
    ) -> TaskDagDependencyResultRelay | None:
        _validated_task_dag_identifier(dag_id)
        _validated_task_dag_identifier(target_node_id)
        if isinstance(target_node_generation, bool) or target_node_generation < 0:
            raise ValueError("DAG dependency relay target generation is invalid")

        def load() -> TaskDagDependencyResultRelay | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_task_dag_dependency_result_relay_for_target(
                        connection,
                        dag_id,
                        target_node_id,
                        target_node_generation,
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise TaskDagDependencyResultRelayError(
                    "DAG dependency relay integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise TaskDagDependencyResultRelayError(
                    "DAG dependency relay could not be loaded",
                ) from error

        return await run_blocking(load)

    async def get_task_dag_recovery_claim(
        self,
        dag_id: str,
        node_id: str,
        node_generation: int,
    ) -> TaskDagRecoveryClaim | None:
        _validated_task_dag_identifier(dag_id)
        _validated_task_dag_identifier(node_id)
        if isinstance(node_generation, bool) or node_generation < 0:
            raise ValueError("DAG recovery claim node generation is invalid")

        def load() -> TaskDagRecoveryClaim | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_task_dag_recovery_claim_for_execution(
                        connection,
                        dag_id=dag_id,
                        node_id=node_id,
                        node_generation=node_generation,
                    )
            except (KeyError, TypeError, ValueError) as error:
                raise TaskDagRecoveryClaimError(
                    "DAG recovery claim integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise TaskDagRecoveryClaimError("DAG recovery claim could not be loaded") from error

        return await run_blocking(load)

    async def insert_task_dag_recovery_claim(
        self,
        claim: TaskDagRecoveryClaim,
    ) -> TaskDagRecoveryClaimResult:
        if not isinstance(claim, TaskDagRecoveryClaim):
            raise TypeError("DAG recovery claim must be canonical")

        def insert() -> TaskDagRecoveryClaimResult:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _verify_task_dag_recovery_claim_linkage(connection, claim)
                current = _load_task_dag_recovery_claim_for_execution(
                    connection,
                    dag_id=claim.dag_id,
                    node_id=claim.node_id,
                    node_generation=claim.node_generation,
                )
                by_id = _load_task_dag_recovery_claim(connection, claim.claim_id)
                if by_id is not None and not by_id.same_execution(claim):
                    raise TaskDagRecoveryClaimError(
                        "DAG recovery claim id is already bound to another execution",
                        kind="protocol",
                    )
                if current is not None:
                    if not current.same_execution(claim):
                        raise TaskDagRecoveryClaimError(
                            "DAG recovery execution identity conflicts with existing claim",
                            kind="protocol",
                        )
                    connection.commit()
                    return TaskDagRecoveryClaimResult(
                        current,
                        acquired=(
                            current.owner_pid == claim.owner_pid
                            and current.owner_token == claim.owner_token
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO task_dag_recovery_claims(
                        claim_id, parent_session_id, dag_id,
                        dag_definition_fingerprint, node_id, node_generation,
                        node_definition_fingerprint, parent_task_id,
                        dependency_relay_id, dependency_relay_source_fingerprint,
                        dependency_relay_content_fingerprint,
                        dependency_relay_integrity_fingerprint, owner_pid,
                        owner_token, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _task_dag_recovery_claim_values(claim),
                )
                persisted = _load_task_dag_recovery_claim(connection, claim.claim_id)
                if persisted is None or persisted != claim:
                    raise TaskDagRecoveryClaimError(
                        "DAG recovery claim was not durably verified",
                        kind="integrity",
                    )
                connection.commit()
                return TaskDagRecoveryClaimResult(persisted, acquired=True)
            except TaskDagRecoveryClaimError:
                connection.rollback()
                raise
            except (KeyError, TypeError, ValueError) as error:
                connection.rollback()
                raise TaskDagRecoveryClaimError(
                    "DAG recovery claim integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise TaskDagRecoveryClaimError(
                    "DAG recovery claim conflicts with existing evidence",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagRecoveryClaimError(
                    "DAG recovery claim could not be persisted"
                ) from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(insert)

    async def compare_and_takeover_task_dag_recovery_claim(
        self,
        claim: TaskDagRecoveryClaim,
        *,
        expected_version: int,
        expected_owner_pid: int,
        expected_owner_token: str,
    ) -> TaskDagRecoveryClaim:
        if not isinstance(claim, TaskDagRecoveryClaim):
            raise TypeError("DAG recovery claim must be canonical")
        if isinstance(expected_version, bool) or expected_version < 0:
            raise TypeError("DAG recovery claim expected version must be non-negative")
        if isinstance(expected_owner_pid, bool) or expected_owner_pid <= 0:
            raise TypeError("DAG recovery claim expected owner PID must be positive")
        if not isinstance(expected_owner_token, str) or not expected_owner_token.strip():
            raise TypeError("DAG recovery claim expected owner token is invalid")

        def takeover() -> TaskDagRecoveryClaim:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _verify_task_dag_recovery_claim_linkage(connection, claim)
                current = _load_task_dag_recovery_claim(connection, claim.claim_id)
                if current is None:
                    raise TaskDagRecoveryClaimError(
                        "DAG recovery claim is missing",
                        kind="unmanaged",
                    )
                if (
                    not current.same_execution(claim)
                    or current.version != expected_version
                    or current.owner_pid != expected_owner_pid
                    or current.owner_token != expected_owner_token
                    or claim.version != expected_version + 1
                ):
                    raise TaskDagRecoveryClaimError(
                        "DAG recovery claim was changed by another controller",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE task_dag_recovery_claims
                    SET owner_pid = ?, owner_token = ?, version = ?, updated_at = ?
                    WHERE claim_id = ? AND version = ? AND owner_pid = ?
                      AND owner_token = ?
                    """,
                    (
                        claim.owner_pid,
                        claim.owner_token,
                        claim.version,
                        claim.updated_at.isoformat(),
                        claim.claim_id,
                        expected_version,
                        expected_owner_pid,
                        expected_owner_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskDagRecoveryClaimError(
                        "DAG recovery claim was changed by another controller",
                        kind="concurrent_modification",
                    )
                persisted = _load_task_dag_recovery_claim(connection, claim.claim_id)
                if persisted is None or persisted != claim:
                    raise TaskDagRecoveryClaimError(
                        "DAG recovery takeover was not durably verified",
                        kind="integrity",
                    )
                connection.commit()
                return persisted
            except TaskDagRecoveryClaimError:
                connection.rollback()
                raise
            except (KeyError, TypeError, ValueError) as error:
                connection.rollback()
                raise TaskDagRecoveryClaimError(
                    "DAG recovery takeover integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagRecoveryClaimError("DAG recovery claim takeover failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(takeover)


_TASK_DAG_SELECT = """
    SELECT dag_id, parent_session_id, definition_fingerprint,
           state, generation, created_at, updated_at, active_node_id, max_parallel
    FROM task_dags
    WHERE dag_id = ?
"""

_TASK_DAG_NODE_SELECT = """
    SELECT node_id, ordinal, prompt, prompt_fingerprint,
           dependencies_json, kind, state, generation,
           parent_task_id, execution_owner_pid, execution_owner_token,
           child_session_id, lease_id, worktree_id,
           baseline_checkpoint_id, relay_id, error_kind, error_reason,
           response_preview, final_workspace_fingerprint, changed_file_count
    FROM task_dag_nodes
    WHERE dag_id = ?
    ORDER BY ordinal ASC, node_id ASC
"""

_TASK_DAG_NODE_UPDATE = """
    UPDATE task_dag_nodes SET
        state = ?, generation = ?, parent_task_id = ?, execution_owner_pid = ?,
        execution_owner_token = ?, child_session_id = ?,
        lease_id = ?, worktree_id = ?, baseline_checkpoint_id = ?, relay_id = ?,
        error_kind = ?, error_reason = ?, response_preview = ?,
        final_workspace_fingerprint = ?, changed_file_count = ?
"""


def _validated_task_dag_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("task DAG identifier is invalid")


def _task_dag_state_transition_allowed(
    current: TaskDagState,
    proposed: TaskDagState,
) -> bool:
    if current is TaskDagState.READY:
        return proposed is TaskDagState.RUNNING
    if current is TaskDagState.RUNNING:
        return proposed in {
            TaskDagState.COMPLETED,
            TaskDagState.FAILED,
            TaskDagState.CANCELLED,
            TaskDagState.INDETERMINATE,
        }
    return False


def _verify_task_dag_definition(current: TaskDag, proposed: TaskDag) -> None:
    if (
        current.dag_id != proposed.dag_id
        or current.parent_session_id != proposed.parent_session_id
        or current.definition_fingerprint != proposed.definition_fingerprint
        or len(current.nodes) != len(proposed.nodes)
    ):
        raise TaskDagError("task DAG definition is immutable", kind="protocol")
    for current_node, proposed_node in zip(current.nodes, proposed.nodes, strict=True):
        _verify_task_dag_node_definition(current_node, proposed_node)


def _verify_task_dag_node_definition(current: TaskDagNode, proposed: TaskDagNode) -> None:
    if current.definition_payload != proposed.definition_payload:
        raise TaskDagError("task DAG node definition is immutable", kind="protocol")


def _task_dag_node_mutable_values(node: TaskDagNode) -> tuple[object, ...]:
    return (
        node.state.value,
        node.generation,
        node.parent_task_id,
        node.execution_owner_pid,
        node.execution_owner_token,
        node.child_session_id,
        node.lease_id,
        node.worktree_id,
        node.baseline_checkpoint_id,
        node.relay_id,
        node.error_kind,
        node.error_reason,
        node.response_preview,
        node.final_workspace_fingerprint,
        node.changed_file_count,
    )


def _task_dag_node_values(dag_id: str, node: TaskDagNode) -> tuple[object, ...]:
    return (
        dag_id,
        node.node_id,
        node.ordinal,
        node.prompt,
        node.prompt_fingerprint,
        json.dumps(list(node.dependencies), ensure_ascii=False, separators=(",", ":")),
        node.kind.value,
        *_task_dag_node_mutable_values(node),
    )


def _load_task_dag(
    connection: sqlite3.Connection,
    dag_id: str,
) -> TaskDag | None:
    row = connection.execute(_TASK_DAG_SELECT, (dag_id,)).fetchone()
    if row is None:
        return None
    if len(row) != 9:
        raise ValueError("task DAG record is malformed")
    (
        raw_dag_id,
        parent_session_id,
        definition_fingerprint,
        raw_state,
        generation,
        raw_created_at,
        raw_updated_at,
        active_node_id,
        max_parallel,
    ) = row
    node_rows = connection.execute(_TASK_DAG_NODE_SELECT, (dag_id,)).fetchall()
    nodes = tuple(_task_dag_node_from_row(node_row) for node_row in node_rows)
    dag = TaskDag(
        dag_id=str(raw_dag_id),
        parent_session_id=str(parent_session_id),
        nodes=nodes,
        state=TaskDagState(str(raw_state)),
        generation=int(generation),
        created_at=datetime.fromisoformat(str(raw_created_at)),
        updated_at=datetime.fromisoformat(str(raw_updated_at)),
        active_node_id=str(active_node_id) if active_node_id is not None else None,
        max_parallel=int(max_parallel),
    )
    if dag.definition_fingerprint != str(definition_fingerprint):
        raise ValueError("task DAG definition fingerprint is inconsistent")
    return dag


def _task_dag_node_from_row(row: Sequence[object]) -> TaskDagNode:
    if len(row) != 21:
        raise ValueError("task DAG node record is malformed")
    (
        node_id,
        ordinal,
        prompt,
        prompt_fingerprint,
        dependencies_json,
        raw_kind,
        raw_state,
        generation,
        parent_task_id,
        execution_owner_pid,
        execution_owner_token,
        child_session_id,
        lease_id,
        worktree_id,
        baseline_checkpoint_id,
        relay_id,
        error_kind,
        error_reason,
        response_preview,
        final_workspace_fingerprint,
        changed_file_count,
    ) = row
    if not isinstance(ordinal, int) or not isinstance(generation, int):
        raise ValueError("task DAG node ordinal or generation is invalid")
    if execution_owner_pid is not None and (
        isinstance(execution_owner_pid, bool) or not isinstance(execution_owner_pid, int)
    ):
        raise ValueError("task DAG node execution owner pid is invalid")
    if changed_file_count is not None and not isinstance(changed_file_count, int):
        raise ValueError("task DAG node changed file count is invalid")
    dependencies = json.loads(str(dependencies_json))
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        raise ValueError("task DAG node dependencies are invalid")
    node = TaskDagNode(
        node_id=str(node_id),
        ordinal=ordinal,
        prompt=str(prompt),
        dependencies=tuple(dependencies),
        kind=TaskDagNodeKind(str(raw_kind)),
        state=TaskDagNodeState(str(raw_state)),
        generation=generation,
        parent_task_id=str(parent_task_id) if parent_task_id is not None else None,
        execution_owner_pid=(int(execution_owner_pid) if execution_owner_pid is not None else None),
        execution_owner_token=(
            str(execution_owner_token) if execution_owner_token is not None else None
        ),
        child_session_id=str(child_session_id) if child_session_id is not None else None,
        lease_id=str(lease_id) if lease_id is not None else None,
        worktree_id=str(worktree_id) if worktree_id is not None else None,
        baseline_checkpoint_id=(
            str(baseline_checkpoint_id) if baseline_checkpoint_id is not None else None
        ),
        relay_id=str(relay_id) if relay_id is not None else None,
        error_kind=str(error_kind) if error_kind is not None else None,
        error_reason=str(error_reason) if error_reason is not None else None,
        response_preview=str(response_preview) if response_preview is not None else None,
        final_workspace_fingerprint=(
            str(final_workspace_fingerprint) if final_workspace_fingerprint is not None else None
        ),
        changed_file_count=changed_file_count,
    )
    if node.prompt_fingerprint != str(prompt_fingerprint):
        raise ValueError("task DAG node prompt fingerprint is inconsistent")
    return node


_TASK_DAG_RECOVERY_CLAIM_SELECT = """
    SELECT claim_id, parent_session_id, dag_id, dag_definition_fingerprint,
           node_id, node_generation, node_definition_fingerprint, parent_task_id,
           dependency_relay_id, dependency_relay_source_fingerprint,
           dependency_relay_content_fingerprint,
           dependency_relay_integrity_fingerprint, owner_pid, owner_token,
           version, created_at, updated_at
    FROM task_dag_recovery_claims
"""


def _task_dag_recovery_claim_values(claim: TaskDagRecoveryClaim) -> tuple[object, ...]:
    return (
        claim.claim_id,
        claim.parent_session_id,
        claim.dag_id,
        claim.dag_definition_fingerprint,
        claim.node_id,
        claim.node_generation,
        claim.node_definition_fingerprint,
        claim.parent_task_id,
        claim.dependency_relay_id,
        claim.dependency_relay_source_fingerprint,
        claim.dependency_relay_content_fingerprint,
        claim.dependency_relay_integrity_fingerprint,
        claim.owner_pid,
        claim.owner_token,
        claim.version,
        claim.created_at.astimezone(UTC).isoformat(),
        claim.updated_at.astimezone(UTC).isoformat(),
    )


def _load_task_dag_recovery_claim(
    connection: sqlite3.Connection,
    claim_id: str,
) -> TaskDagRecoveryClaim | None:
    row = connection.execute(
        _TASK_DAG_RECOVERY_CLAIM_SELECT + " WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    return _task_dag_recovery_claim_from_row(row) if row is not None else None


def _load_task_dag_recovery_claim_for_execution(
    connection: sqlite3.Connection,
    *,
    dag_id: str,
    node_id: str,
    node_generation: int,
) -> TaskDagRecoveryClaim | None:
    row = connection.execute(
        _TASK_DAG_RECOVERY_CLAIM_SELECT
        + " WHERE dag_id = ? AND node_id = ? AND node_generation = ?",
        (dag_id, node_id, node_generation),
    ).fetchone()
    return _task_dag_recovery_claim_from_row(row) if row is not None else None


def _task_dag_recovery_claim_from_row(row: Sequence[object]) -> TaskDagRecoveryClaim:
    if len(row) != 17:
        raise ValueError("DAG recovery claim record is malformed")
    (
        claim_id,
        parent_session_id,
        dag_id,
        dag_definition_fingerprint,
        node_id,
        node_generation,
        node_definition_fingerprint,
        parent_task_id,
        dependency_relay_id,
        dependency_relay_source_fingerprint,
        dependency_relay_content_fingerprint,
        dependency_relay_integrity_fingerprint,
        owner_pid,
        owner_token,
        version,
        created_at,
        updated_at,
    ) = row
    if (
        isinstance(node_generation, bool)
        or not isinstance(node_generation, int)
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or isinstance(version, bool)
        or not isinstance(version, int)
    ):
        raise ValueError("DAG recovery claim numeric fields are invalid")
    return TaskDagRecoveryClaim(
        claim_id=str(claim_id),
        parent_session_id=str(parent_session_id),
        dag_id=str(dag_id),
        dag_definition_fingerprint=str(dag_definition_fingerprint),
        node_id=str(node_id),
        node_generation=node_generation,
        node_definition_fingerprint=str(node_definition_fingerprint),
        parent_task_id=str(parent_task_id),
        dependency_relay_id=str(dependency_relay_id),
        dependency_relay_source_fingerprint=str(dependency_relay_source_fingerprint),
        dependency_relay_content_fingerprint=str(dependency_relay_content_fingerprint),
        dependency_relay_integrity_fingerprint=str(dependency_relay_integrity_fingerprint),
        owner_pid=owner_pid,
        owner_token=str(owner_token),
        version=version,
        created_at=datetime.fromisoformat(str(created_at)),
        updated_at=datetime.fromisoformat(str(updated_at)),
    )


def _verify_task_dag_recovery_claim_linkage(
    connection: sqlite3.Connection,
    claim: TaskDagRecoveryClaim,
) -> None:
    dag = _load_task_dag(connection, claim.dag_id)
    if dag is None:
        raise TaskDagRecoveryClaimError(
            "DAG recovery claim DAG is missing",
            kind="unmanaged",
        )
    if (
        dag.parent_session_id != claim.parent_session_id
        or dag.definition_fingerprint != claim.dag_definition_fingerprint
    ):
        raise TaskDagRecoveryClaimError(
            "DAG recovery claim DAG identity does not match",
            kind="protocol",
        )
    try:
        node = dag.node(claim.node_id)
    except KeyError as error:
        raise TaskDagRecoveryClaimError(
            "DAG recovery claim node is missing",
            kind="unmanaged",
        ) from error
    if (
        node.state is not TaskDagNodeState.RUNNING
        or node.generation != claim.node_generation
        or node.definition_fingerprint != claim.node_definition_fingerprint
        or node.parent_task_id != claim.parent_task_id
    ):
        raise TaskDagRecoveryClaimError(
            "DAG recovery claim node identity does not match",
            kind="protocol",
        )
    relay = _load_task_dag_dependency_result_relay(
        connection,
        relay_id=claim.dependency_relay_id,
    )
    if relay is None:
        raise TaskDagRecoveryClaimError(
            "DAG recovery claim dependency relay is missing",
            kind="unmanaged",
        )
    if (
        relay.dag_id != claim.dag_id
        or relay.dag_definition_fingerprint != claim.dag_definition_fingerprint
        or relay.target_node_id != claim.node_id
        or relay.target_node_generation != claim.node_generation
        or relay.target_node_definition_fingerprint != claim.node_definition_fingerprint
        or relay.source_fingerprint != claim.dependency_relay_source_fingerprint
        or relay.content_fingerprint != claim.dependency_relay_content_fingerprint
        or relay.integrity_fingerprint != claim.dependency_relay_integrity_fingerprint
    ):
        raise TaskDagRecoveryClaimError(
            "DAG recovery claim dependency relay identity does not match",
            kind="protocol",
        )


_TASK_DAG_DEPENDENCY_RESULT_RELAY_SELECT = """
    SELECT relay_id, dag_id, dag_definition_fingerprint, target_node_id,
           target_node_generation, target_node_definition_fingerprint,
           direct_dependency_ids_json, entries_json, source_fingerprint,
           content_fingerprint, byte_count, truncated, created_at,
           integrity_fingerprint, state
    FROM task_dag_dependency_relays
"""


def _task_dag_dependency_result_relay_values(
    relay: TaskDagDependencyResultRelay,
) -> tuple[object, ...]:
    return (
        relay.relay_id,
        relay.dag_id,
        relay.dag_definition_fingerprint,
        relay.target_node_id,
        relay.target_node_generation,
        relay.target_node_definition_fingerprint,
        json.dumps(
            list(relay.direct_dependency_ids),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        json.dumps(
            [entry.to_dict() for entry in relay.entries],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        relay.source_fingerprint,
        relay.content_fingerprint,
        relay.byte_count,
        int(relay.truncated),
        relay.created_at.isoformat(),
        relay.integrity_fingerprint,
    )


def _load_task_dag_dependency_result_relay(
    connection: sqlite3.Connection,
    *,
    relay_id: str,
) -> TaskDagDependencyResultRelay | None:
    row = connection.execute(
        _TASK_DAG_DEPENDENCY_RESULT_RELAY_SELECT + " WHERE relay_id = ?",
        (relay_id,),
    ).fetchone()
    return _task_dag_dependency_result_relay_from_row(row) if row is not None else None


def _load_task_dag_dependency_result_relay_for_target(
    connection: sqlite3.Connection,
    dag_id: str,
    target_node_id: str,
    target_node_generation: int,
) -> TaskDagDependencyResultRelay | None:
    row = connection.execute(
        _TASK_DAG_DEPENDENCY_RESULT_RELAY_SELECT
        + " WHERE dag_id = ? AND target_node_id = ? AND target_node_generation = ?",
        (dag_id, target_node_id, target_node_generation),
    ).fetchone()
    return _task_dag_dependency_result_relay_from_row(row) if row is not None else None


def _task_dag_dependency_result_relay_from_row(
    row: Sequence[object],
) -> TaskDagDependencyResultRelay:
    if len(row) != 15:
        raise ValueError("DAG dependency relay record is malformed")
    (
        relay_id,
        dag_id,
        dag_definition_fingerprint,
        target_node_id,
        target_node_generation,
        target_node_definition_fingerprint,
        raw_dependencies,
        raw_entries,
        source_fingerprint,
        content_fingerprint,
        byte_count,
        raw_truncated,
        created_at,
        integrity_fingerprint,
        state,
    ) = row
    if state != "ready":
        raise ValueError("DAG dependency relay is not READY")
    if not isinstance(target_node_generation, int) or isinstance(target_node_generation, bool):
        raise ValueError("DAG dependency relay target generation is invalid")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool):
        raise ValueError("DAG dependency relay byte count is invalid")
    if raw_truncated not in (0, 1) or isinstance(raw_truncated, bool):
        raise ValueError("DAG dependency relay truncated flag is invalid")
    dependencies = json.loads(str(raw_dependencies))
    entries_payload = json.loads(str(raw_entries))
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        raise ValueError("DAG dependency relay dependency payload is invalid")
    if not isinstance(entries_payload, list):
        raise ValueError("DAG dependency relay entry payload is invalid")
    relay = TaskDagDependencyResultRelay(
        relay_id=str(relay_id),
        dag_id=str(dag_id),
        dag_definition_fingerprint=str(dag_definition_fingerprint),
        target_node_id=str(target_node_id),
        target_node_generation=target_node_generation,
        target_node_definition_fingerprint=str(target_node_definition_fingerprint),
        direct_dependency_ids=tuple(dependencies),
        entries=tuple(TaskDagDependencyResultEntry.from_dict(entry) for entry in entries_payload),
        source_fingerprint=str(source_fingerprint),
        content_fingerprint=str(content_fingerprint),
        byte_count=byte_count,
        truncated=bool(raw_truncated),
        created_at=datetime.fromisoformat(str(created_at)),
    )
    if not isinstance(integrity_fingerprint, str) or (
        relay.integrity_fingerprint != integrity_fingerprint
    ):
        raise ValueError("DAG dependency relay integrity fingerprint is inconsistent")
    return relay


def _verify_task_dag_dependency_relay_linkage(
    connection: sqlite3.Connection,
    relay: TaskDagDependencyResultRelay,
    dag: TaskDag,
) -> None:
    if dag.definition_fingerprint != relay.dag_definition_fingerprint:
        raise TaskDagDependencyResultRelayError(
            "DAG dependency relay definition fingerprint does not match",
            kind="protocol",
        )
    target = dag.node(relay.target_node_id)
    if (
        target.state is not TaskDagNodeState.RUNNING
        or target.generation != relay.target_node_generation
        or target.definition_fingerprint != relay.target_node_definition_fingerprint
        or target.dependencies != relay.direct_dependency_ids
    ):
        raise TaskDagDependencyResultRelayError(
            "DAG dependency relay target snapshot is stale",
            kind="concurrent_modification",
        )
    for entry in relay.entries:
        predecessor = dag.node(entry.predecessor_node_id)
        if (
            predecessor.state is not TaskDagNodeState.COMPLETED
            or predecessor.ordinal != entry.predecessor_ordinal
            or predecessor.generation != entry.predecessor_generation
            or predecessor.parent_task_id != entry.parent_task_id
            or predecessor.child_session_id != entry.child_session_id
            or predecessor.lease_id != entry.writable_lease_id
            or predecessor.worktree_id != entry.worktree_id.value
            or predecessor.baseline_checkpoint_id != entry.baseline_checkpoint_id.value
            or predecessor.relay_id != entry.parent_relay_id
        ):
            raise TaskDagDependencyResultRelayError(
                "DAG dependency relay predecessor evidence is stale",
                kind="concurrent_modification",
            )
        task = connection.execute(
            "SELECT session_id, status FROM session_tasks WHERE task_id = ?",
            (entry.parent_task_id,),
        ).fetchone()
        if task is None or str(task[0]) != dag.parent_session_id or str(task[1]) != "completed":
            raise TaskDagDependencyResultRelayError(
                "DAG dependency relay predecessor task is not durably completed",
                kind="protocol",
            )
        lease = connection.execute(
            """
            SELECT parent_session_id, parent_task_id, child_session_id, worktree_id,
                   baseline_checkpoint_id, state, final_workspace_fingerprint,
                   changed_file_count
            FROM writable_subagent_leases
            WHERE lease_id = ?
            """,
            (entry.writable_lease_id,),
        ).fetchone()
        if lease is None or tuple(lease) != (
            dag.parent_session_id,
            entry.parent_task_id,
            entry.child_session_id,
            entry.worktree_id.value,
            entry.baseline_checkpoint_id.value,
            WritableSubagentWorkspaceState.PRESERVED.value,
            entry.final_workspace_fingerprint,
            entry.changed_file_count,
        ):
            raise TaskDagDependencyResultRelayError(
                "DAG dependency relay writable lease evidence is inconsistent",
                kind="protocol",
            )
        parent_relay_row = connection.execute(
            _PARENT_CONTEXT_RELAY_SELECT + " WHERE relay_id = ?",
            (entry.parent_relay_id,),
        ).fetchone()
        if parent_relay_row is None:
            raise TaskDagDependencyResultRelayError(
                "DAG dependency relay Parent Relay evidence is missing",
                kind="protocol",
            )
        try:
            parent_relay = _parent_context_relay_from_row(parent_relay_row)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TaskDagDependencyResultRelayError(
                "DAG dependency relay Parent Relay evidence is invalid",
                kind="integrity",
            ) from error
        if (
            parent_relay.lease_id != entry.writable_lease_id
            or parent_relay.parent_task_id != entry.parent_task_id
            or parent_relay.child_session_id != entry.child_session_id
            or parent_relay.worktree_id != entry.worktree_id
            or parent_relay.baseline_checkpoint_id != entry.baseline_checkpoint_id
        ):
            raise TaskDagDependencyResultRelayError(
                "DAG dependency relay Parent Relay identity is inconsistent",
                kind="protocol",
            )
