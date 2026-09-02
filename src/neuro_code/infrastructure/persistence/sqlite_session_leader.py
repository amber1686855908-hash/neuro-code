"""SQLite persistence leader owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

from neuro_code.application.ports.leader import LeaderAttemptClaim, LeaderStoreError
from neuro_code.domain.leader import (
    LeaderAttempt,
    LeaderAttemptState,
    LeaderDecision,
    LeaderDecisionKind,
    LeaderDecisionRecord,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_dag import _load_task_dag
from neuro_code.shared.async_utils import run_blocking


class LeaderMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def claim_leader_attempt(
        self,
        attempt: LeaderAttempt,
        *,
        now: datetime,
    ) -> LeaderAttemptClaim:
        if not isinstance(attempt, LeaderAttempt):
            raise TypeError("leader attempt must be canonical")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("leader attempt claim time must be timezone-aware")
        now_utc = now.astimezone(UTC)
        prepared = replace(attempt, created_at=now_utc, updated_at=now_utc)

        def claim() -> LeaderAttemptClaim:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_leader_attempt_for_snapshot(
                    connection,
                    prepared.dag_id,
                    dag_generation=prepared.dag_generation,
                    definition_fingerprint=prepared.definition_fingerprint,
                    evidence_fingerprint=prepared.evidence_fingerprint,
                    objective_fingerprint=prepared.objective_fingerprint,
                )
                if current is None:
                    connection.execute(
                        _LEADER_ATTEMPT_INSERT,
                        _leader_attempt_values(prepared),
                    )
                    connection.commit()
                    return LeaderAttemptClaim(prepared, True)
                if (
                    current.state is LeaderAttemptState.CLAIMED
                    and current.lease_expires_at <= now_utc
                ):
                    if current.model_response is not None or current.decision_id is not None:
                        raise LeaderStoreError(
                            "expired leader attempt has committed output",
                            kind="integrity",
                        )
                    cursor = connection.execute(
                        """
                        UPDATE leader_attempts
                        SET parent_session_id = ?, leader_session_id = ?, owner_id = ?, lease_expires_at = ?,
                            turn_id = ?, updated_at = ?
                        WHERE attempt_id = ? AND state = ? AND lease_expires_at <= ?
                        """,
                        (
                            prepared.parent_session_id,
                            prepared.leader_session_id,
                            prepared.owner_id,
                            prepared.lease_expires_at.isoformat(),
                            prepared.turn_id,
                            now_utc.isoformat(),
                            current.attempt_id,
                            LeaderAttemptState.CLAIMED.value,
                            now_utc.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        connection.commit()
                        refreshed = _load_leader_attempt(connection, current.attempt_id)
                        if refreshed is None:
                            raise LeaderStoreError("leader attempt disappeared after claim")
                        return LeaderAttemptClaim(refreshed, True)
                connection.commit()
                return LeaderAttemptClaim(current, False)
            except LeaderStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise LeaderStoreError("leader attempt could not be claimed") from error
            except sqlite3.Error as error:
                connection.rollback()
                raise LeaderStoreError("leader attempt claim failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def fence_leader_attempt(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        leader_session_id: str,
        turn_id: str,
        updated_at: datetime,
    ) -> LeaderAttempt:
        _validated_leader_identifier(attempt_id)
        _validated_leader_identifier(owner_id)
        _validated_leader_identifier(leader_session_id)
        _validated_leader_identifier(turn_id)
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("leader provider fence time must be timezone-aware")
        updated_at_utc = updated_at.astimezone(UTC)

        def fence() -> LeaderAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_leader_attempt(connection, attempt_id)
                if current is None:
                    raise LeaderStoreError("leader attempt is missing", kind="unmanaged")
                if current.state is LeaderAttemptState.PROVIDER_FENCED:
                    if (
                        current.owner_id == owner_id
                        and current.leader_session_id == leader_session_id
                        and current.turn_id == turn_id
                    ):
                        connection.commit()
                        return current
                    raise LeaderStoreError(
                        "leader provider fence identity conflicts",
                        kind="concurrent_modification",
                    )
                if (
                    current.state is not LeaderAttemptState.CLAIMED
                    or current.owner_id != owner_id
                    or current.leader_session_id != leader_session_id
                    or current.turn_id != turn_id
                    or current.lease_expires_at <= updated_at_utc
                ):
                    raise LeaderStoreError(
                        "leader attempt is no longer fenced by this controller",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE leader_attempts
                    SET state = ?, updated_at = ?
                    WHERE attempt_id = ? AND state = ? AND owner_id = ?
                      AND leader_session_id = ? AND turn_id = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        LeaderAttemptState.PROVIDER_FENCED.value,
                        updated_at_utc.isoformat(),
                        attempt_id,
                        LeaderAttemptState.CLAIMED.value,
                        owner_id,
                        leader_session_id,
                        turn_id,
                        updated_at_utc.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaderStoreError(
                        "leader provider fence was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_leader_attempt(connection, attempt_id)
                if result is None:
                    raise LeaderStoreError("leader attempt disappeared after provider fence")
                return result
            except LeaderStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise LeaderStoreError("leader provider fence failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(fence)

    async def get_leader_attempt_for_snapshot(
        self,
        dag_id: str,
        *,
        dag_generation: int,
        definition_fingerprint: str,
        evidence_fingerprint: str,
        objective_fingerprint: str,
    ) -> LeaderAttempt | None:
        _validated_leader_identifier(dag_id)

        def load() -> LeaderAttempt | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_leader_attempt_for_snapshot(
                        connection,
                        dag_id,
                        dag_generation=dag_generation,
                        definition_fingerprint=definition_fingerprint,
                        evidence_fingerprint=evidence_fingerprint,
                        objective_fingerprint=objective_fingerprint,
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise LeaderStoreError(
                    "leader attempt record is invalid", kind="integrity"
                ) from error
            except sqlite3.Error as error:
                raise LeaderStoreError("leader attempt could not be loaded") from error

        return await run_blocking(load)

    async def mark_leader_model_committed(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        leader_session_id: str,
        turn_id: str,
        model_response: str,
        updated_at: datetime,
    ) -> LeaderAttempt:
        _validated_leader_identifier(attempt_id)
        _validated_leader_identifier(owner_id)
        _validated_leader_identifier(leader_session_id)
        _validated_leader_identifier(turn_id)
        if not isinstance(model_response, str) or not model_response.strip():
            raise ValueError("leader model response must not be empty")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("leader model commit time must be timezone-aware")

        def commit_model() -> LeaderAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_leader_attempt(connection, attempt_id)
                if current is None:
                    raise LeaderStoreError("leader attempt is missing", kind="unmanaged")
                if current.state is LeaderAttemptState.MODEL_COMMITTED:
                    if (
                        current.leader_session_id == leader_session_id
                        and current.turn_id == turn_id
                        and current.model_response == model_response
                    ):
                        connection.commit()
                        return current
                    raise LeaderStoreError(
                        "leader attempt model result conflicts",
                        kind="integrity",
                    )
                if (
                    current.state is not LeaderAttemptState.PROVIDER_FENCED
                    or current.owner_id != owner_id
                    or current.leader_session_id != leader_session_id
                    or current.turn_id != turn_id
                ):
                    raise LeaderStoreError(
                        "leader attempt is no longer owned by this controller",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE leader_attempts
                    SET state = ?, turn_id = ?, model_response = ?, updated_at = ?
                    WHERE attempt_id = ? AND state = ? AND owner_id = ?
                      AND leader_session_id = ? AND turn_id = ?
                    """,
                    (
                        LeaderAttemptState.MODEL_COMMITTED.value,
                        turn_id,
                        model_response,
                        updated_at.astimezone(UTC).isoformat(),
                        attempt_id,
                        LeaderAttemptState.PROVIDER_FENCED.value,
                        owner_id,
                        leader_session_id,
                        turn_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaderStoreError(
                        "leader attempt model commit was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_leader_attempt(connection, attempt_id)
                if result is None:
                    raise LeaderStoreError("leader attempt disappeared after model commit")
                return result
            except LeaderStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise LeaderStoreError("leader model result could not be committed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(commit_model)

    async def publish_leader_decision(
        self,
        attempt_id: str,
        *,
        owner_id: str,
        decision_id: str,
        decision: LeaderDecision,
        created_at: datetime,
        parent_session_id: str | None = None,
        selected_node_generations: tuple[int, ...] = (),
    ) -> LeaderDecisionRecord:
        _validated_leader_identifier(attempt_id)
        _validated_leader_identifier(owner_id)
        _validated_leader_identifier(decision_id)
        if not isinstance(decision, LeaderDecision):
            raise TypeError("leader decision must be canonical")
        if parent_session_id is not None:
            _validated_leader_identifier(parent_session_id)
        if not isinstance(selected_node_generations, tuple) or any(
            isinstance(generation, bool) or not isinstance(generation, int) or generation < 0
            for generation in selected_node_generations
        ):
            raise TypeError("leader selected node generations must be non-negative")
        if not isinstance(created_at, datetime) or created_at.tzinfo is None:
            raise TypeError("leader decision time must be timezone-aware")

        def publish() -> LeaderDecisionRecord:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_leader_attempt(connection, attempt_id)
                if current is None:
                    raise LeaderStoreError("leader attempt is missing", kind="unmanaged")
                dag = _load_task_dag(connection, current.dag_id)
                if dag is None:
                    raise LeaderStoreError("leader decision DAG is missing", kind="unmanaged")
                effective_parent_session_id = parent_session_id or dag.parent_session_id
                if effective_parent_session_id != dag.parent_session_id:
                    raise LeaderStoreError(
                        "leader decision parent session conflicts with the DAG",
                        kind="integrity",
                    )
                effective_generations = selected_node_generations
                if decision.selected_node_ids and not effective_generations:
                    try:
                        effective_generations = tuple(
                            dag.node(node_id).generation for node_id in decision.selected_node_ids
                        )
                    except KeyError as error:
                        # Preserve the observable invalid model decision so
                        # Leader validation can mark it stale without ever
                        # replaying the provider request.
                        del error
                        effective_generations = ()
                if current.state is LeaderAttemptState.DECISION_PUBLISHED:
                    if current.decision_id is None:
                        raise LeaderStoreError("published leader attempt has no decision id")
                    existing = _load_leader_decision(connection, current.decision_id)
                    if existing is None:
                        raise LeaderStoreError("published leader decision is missing")
                    if existing.decision != decision:
                        raise LeaderStoreError(
                            "leader decision conflicts with the durable record",
                            kind="integrity",
                        )
                    connection.commit()
                    return existing
                if current.state is not LeaderAttemptState.MODEL_COMMITTED:
                    raise LeaderStoreError(
                        "leader attempt is not ready for decision publication",
                        kind="concurrent_modification",
                    )
                record = LeaderDecisionRecord(
                    decision_id=decision_id,
                    attempt_id=current.attempt_id,
                    dag_id=current.dag_id,
                    parent_session_id=effective_parent_session_id,
                    leader_session_id=current.leader_session_id,
                    dag_generation=current.dag_generation,
                    definition_fingerprint=current.definition_fingerprint,
                    evidence_fingerprint=current.evidence_fingerprint,
                    decision=decision,
                    created_at=created_at.astimezone(UTC),
                    selected_node_generations=effective_generations,
                )
                connection.execute(
                    _LEADER_DECISION_INSERT,
                    _leader_decision_values(record),
                )
                cursor = connection.execute(
                    """
                    UPDATE leader_attempts
                    SET state = ?, decision_id = ?, updated_at = ?
                    WHERE attempt_id = ? AND state = ?
                    """,
                    (
                        LeaderAttemptState.DECISION_PUBLISHED.value,
                        decision_id,
                        created_at.astimezone(UTC).isoformat(),
                        attempt_id,
                        LeaderAttemptState.MODEL_COMMITTED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaderStoreError(
                        "leader decision publication was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                return record
            except LeaderStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise LeaderStoreError("leader decision could not be published") from error
            except sqlite3.Error as error:
                connection.rollback()
                raise LeaderStoreError("leader decision publication failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(publish)

    async def transition_leader_attempt(
        self,
        attempt_id: str,
        *,
        expected_state: LeaderAttemptState,
        state: LeaderAttemptState,
        owner_id: str | None = None,
        updated_at: datetime,
    ) -> LeaderAttempt:
        _validated_leader_identifier(attempt_id)
        if owner_id is not None:
            _validated_leader_identifier(owner_id)
        if not isinstance(expected_state, LeaderAttemptState) or not isinstance(
            state, LeaderAttemptState
        ):
            raise TypeError("leader attempt states must be canonical")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("leader attempt transition time must be timezone-aware")

        def transition() -> LeaderAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_leader_attempt(connection, attempt_id)
                if current is None:
                    raise LeaderStoreError("leader attempt is missing", kind="unmanaged")
                if current.state is state:
                    connection.commit()
                    return current
                if current.state is not expected_state:
                    raise LeaderStoreError(
                        "leader attempt state is stale",
                        kind="concurrent_modification",
                    )
                if not current.state.can_transition_to(state):
                    raise LeaderStoreError(
                        "leader attempt lifecycle transition is not allowed",
                        kind="protocol",
                    )
                owner_clause = ""
                owner_parameters: tuple[object, ...] = ()
                if owner_id is not None:
                    owner_clause = " AND owner_id = ?"
                    owner_parameters = (owner_id,)
                cursor = connection.execute(
                    """
                    UPDATE leader_attempts
                    SET state = ?, updated_at = ?
                    WHERE attempt_id = ? AND state = ?
                    """
                    + owner_clause,
                    (
                        state.value,
                        updated_at.astimezone(UTC).isoformat(),
                        attempt_id,
                        expected_state.value,
                        *owner_parameters,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaderStoreError(
                        "leader attempt transition was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_leader_attempt(connection, attempt_id)
                if result is None:
                    raise LeaderStoreError("leader attempt disappeared after transition")
                return result
            except LeaderStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise LeaderStoreError("leader attempt transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)

    async def get_leader_attempt(self, attempt_id: str) -> LeaderAttempt | None:
        _validated_leader_identifier(attempt_id)

        def load() -> LeaderAttempt | None:
            with closing(self._connect()) as connection:
                return _load_leader_attempt(connection, attempt_id)

        return await run_blocking(load)

    async def get_leader_decision(self, decision_id: str) -> LeaderDecisionRecord | None:
        _validated_leader_identifier(decision_id)

        def load() -> LeaderDecisionRecord | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_leader_decision(connection, decision_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise LeaderStoreError(
                    "leader decision record is invalid",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise LeaderStoreError("leader decision could not be loaded") from error

        return await run_blocking(load)

    async def list_leader_decisions(self, dag_id: str) -> tuple[LeaderDecisionRecord, ...]:
        _validated_leader_identifier(dag_id)

        def load() -> tuple[LeaderDecisionRecord, ...]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    _LEADER_DECISION_SELECT + " WHERE dag_id = ? ORDER BY created_at, decision_id",
                    (dag_id,),
                ).fetchall()
            try:
                return tuple(_leader_decision_from_row(row) for row in rows)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise LeaderStoreError(
                    "leader decision record is invalid", kind="integrity"
                ) from error

        return await run_blocking(load)


_LEADER_ATTEMPT_SELECT = """
    SELECT attempt_id, dag_id, parent_session_id, leader_session_id, objective_fingerprint,
           dag_generation, definition_fingerprint, evidence_fingerprint,
           state, owner_id, lease_expires_at, turn_id, model_response,
           decision_id, created_at, updated_at
    FROM leader_attempts
"""

_LEADER_ATTEMPT_INSERT = """
    INSERT INTO leader_attempts(
        attempt_id, dag_id, parent_session_id, leader_session_id, objective_fingerprint,
        dag_generation, definition_fingerprint, evidence_fingerprint,
        state, owner_id, lease_expires_at, turn_id, model_response,
        decision_id, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_LEADER_DECISION_SELECT = """
    SELECT decision_id, attempt_id, dag_id, parent_session_id, leader_session_id,
           dag_generation, definition_fingerprint, evidence_fingerprint,
           kind, selected_node_id, selected_node_ids_json,
           selected_node_generations_json, summary, created_at
    FROM leader_decisions
"""

_LEADER_DECISION_INSERT = """
    INSERT INTO leader_decisions(
        decision_id, attempt_id, dag_id, parent_session_id, leader_session_id,
        dag_generation, definition_fingerprint, evidence_fingerprint,
        kind, selected_node_id, selected_node_ids_json,
        selected_node_generations_json, summary, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _validated_leader_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Leader identifier is invalid")


def _leader_attempt_values(attempt: LeaderAttempt) -> tuple[object, ...]:
    if attempt.created_at is None or attempt.updated_at is None:
        raise LeaderStoreError("leader attempt timestamps are required", kind="protocol")
    return (
        attempt.attempt_id,
        attempt.dag_id,
        attempt.parent_session_id,
        attempt.leader_session_id,
        attempt.objective_fingerprint,
        attempt.dag_generation,
        attempt.definition_fingerprint,
        attempt.evidence_fingerprint,
        attempt.state.value,
        attempt.owner_id,
        attempt.lease_expires_at.astimezone(UTC).isoformat(),
        attempt.turn_id,
        attempt.model_response,
        attempt.decision_id,
        attempt.created_at.astimezone(UTC).isoformat(),
        attempt.updated_at.astimezone(UTC).isoformat(),
    )


def _leader_decision_values(record: LeaderDecisionRecord) -> tuple[object, ...]:
    selected_node_ids = list(record.decision.selected_node_ids)
    return (
        record.decision_id,
        record.attempt_id,
        record.dag_id,
        record.parent_session_id,
        record.leader_session_id,
        record.dag_generation,
        record.definition_fingerprint,
        record.evidence_fingerprint,
        record.decision.kind.value,
        record.decision.selected_node_id,
        json.dumps(selected_node_ids, ensure_ascii=False, separators=(",", ":")),
        json.dumps(
            list(record.selected_node_generations),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        record.decision.summary,
        record.created_at.astimezone(UTC).isoformat(),
    )


def _load_leader_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
) -> LeaderAttempt | None:
    row = connection.execute(
        _LEADER_ATTEMPT_SELECT + " WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    return _leader_attempt_from_row(row) if row is not None else None


def _load_leader_attempt_for_snapshot(
    connection: sqlite3.Connection,
    dag_id: str,
    *,
    dag_generation: int,
    definition_fingerprint: str,
    evidence_fingerprint: str,
    objective_fingerprint: str,
) -> LeaderAttempt | None:
    row = connection.execute(
        _LEADER_ATTEMPT_SELECT
        + " WHERE dag_id = ? AND dag_generation = ? AND definition_fingerprint = ?"
        " AND evidence_fingerprint = ? AND objective_fingerprint = ?",
        (
            dag_id,
            dag_generation,
            definition_fingerprint,
            evidence_fingerprint,
            objective_fingerprint,
        ),
    ).fetchone()
    return _leader_attempt_from_row(row) if row is not None else None


def _leader_attempt_from_row(row: Sequence[object]) -> LeaderAttempt:
    if len(row) != 16:
        raise ValueError("leader attempt record is malformed")
    (
        attempt_id,
        dag_id,
        parent_session_id,
        leader_session_id,
        objective_fingerprint,
        dag_generation,
        definition_fingerprint,
        evidence_fingerprint,
        raw_state,
        owner_id,
        raw_lease_expires_at,
        turn_id,
        model_response,
        decision_id,
        raw_created_at,
        raw_updated_at,
    ) = row
    if not isinstance(dag_generation, int):
        raise ValueError("leader attempt DAG generation is invalid")
    return LeaderAttempt(
        attempt_id=str(attempt_id),
        dag_id=str(dag_id),
        leader_session_id=str(leader_session_id),
        objective_fingerprint=str(objective_fingerprint),
        dag_generation=dag_generation,
        definition_fingerprint=str(definition_fingerprint),
        evidence_fingerprint=str(evidence_fingerprint),
        state=LeaderAttemptState(str(raw_state)),
        owner_id=str(owner_id),
        lease_expires_at=datetime.fromisoformat(str(raw_lease_expires_at)),
        turn_id=str(turn_id),
        model_response=str(model_response) if model_response is not None else None,
        decision_id=str(decision_id) if decision_id is not None else None,
        created_at=datetime.fromisoformat(str(raw_created_at)),
        updated_at=datetime.fromisoformat(str(raw_updated_at)),
        parent_session_id=(str(parent_session_id) if parent_session_id is not None else None),
    )


def _load_leader_decision(
    connection: sqlite3.Connection,
    decision_id: str,
) -> LeaderDecisionRecord | None:
    row = connection.execute(
        _LEADER_DECISION_SELECT + " WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    return _leader_decision_from_row(row) if row is not None else None


def _leader_decision_from_row(row: Sequence[object]) -> LeaderDecisionRecord:
    if len(row) != 14:
        raise ValueError("leader decision record is malformed")
    (
        decision_id,
        attempt_id,
        dag_id,
        parent_session_id,
        leader_session_id,
        dag_generation,
        definition_fingerprint,
        evidence_fingerprint,
        raw_kind,
        selected_node_id,
        raw_selected_node_ids,
        raw_selected_node_generations,
        summary,
        raw_created_at,
    ) = row
    if not isinstance(dag_generation, int):
        raise ValueError("leader decision DAG generation is invalid")
    kind = LeaderDecisionKind(str(raw_kind))
    selected_node_ids = json.loads(str(raw_selected_node_ids))
    selected_node_generations = json.loads(str(raw_selected_node_generations))
    if not isinstance(selected_node_ids, list) or not all(
        isinstance(node_id, str) for node_id in selected_node_ids
    ):
        raise ValueError("leader selected node ids are invalid")
    if not isinstance(selected_node_generations, list) or not all(
        isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0
        for generation in selected_node_generations
    ):
        raise ValueError("leader selected node generations are invalid")
    if kind is LeaderDecisionKind.SELECT_NODE:
        selected_id = str(selected_node_id) if selected_node_id is not None else None
        if not selected_node_ids and selected_id is not None:
            selected_node_ids = [selected_id]
        decision = LeaderDecision(
            kind,
            selected_node_id=selected_id,
            summary=str(summary),
        )
    elif kind is LeaderDecisionKind.SELECT_NODES:
        decision = LeaderDecision(
            kind,
            selected_node_ids=tuple(selected_node_ids),
            summary=str(summary),
        )
    else:
        decision = LeaderDecision(kind, summary=str(summary))
    return LeaderDecisionRecord(
        decision_id=str(decision_id),
        attempt_id=str(attempt_id),
        dag_id=str(dag_id),
        parent_session_id=(str(parent_session_id) if parent_session_id is not None else None),
        leader_session_id=str(leader_session_id),
        dag_generation=dag_generation,
        definition_fingerprint=str(definition_fingerprint),
        evidence_fingerprint=str(evidence_fingerprint),
        decision=decision,
        created_at=datetime.fromisoformat(str(raw_created_at)),
        selected_node_generations=tuple(selected_node_generations),
    )
