"""SQLite persistence dag_replan owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

from neuro_code.application.ports.task_dag_replan import (
    DagReplanAttemptClaim,
    TaskDagReplanStoreError,
)
from neuro_code.domain.model_planning import ModelDagProposal
from neuro_code.domain.task_dag import TaskDagNodeState, TaskDagState
from neuro_code.domain.task_dag_replan import (
    MAX_DAG_REPLAN_RESPONSE_BYTES,
    DagReplanAttempt,
    DagReplanAttemptState,
    DagReplanProposalRecord,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_dag import (
    _load_task_dag,
    _validated_task_dag_identifier,
)
from neuro_code.shared.async_utils import run_blocking


class DagReplanMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def get_task_dag_replan_attempt(
        self,
        revision_id: str,
    ) -> DagReplanAttempt | None:
        _validated_dag_replan_identifier(revision_id)

        def load() -> DagReplanAttempt | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_dag_replan_attempt(connection, revision_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise TaskDagReplanStoreError(
                    "DAG replan attempt record is invalid", kind="integrity"
                ) from error
            except sqlite3.Error as error:
                raise TaskDagReplanStoreError("DAG replan attempt could not be loaded") from error

        return await run_blocking(load)

    async def get_task_dag_replan_source_depth(self, source_dag_id: str) -> int:
        _validated_task_dag_identifier(source_dag_id)

        def load() -> int:
            try:
                with closing(self._connect()) as connection:
                    rows = connection.execute(
                        """
                        SELECT revision_depth
                        FROM orchestration_dag_replan_attempts
                        WHERE successor_dag_id = ?
                        """,
                        (source_dag_id,),
                    ).fetchall()
                if len(rows) > 1:
                    raise TaskDagReplanStoreError(
                        "source DAG has divergent replan parents", kind="integrity"
                    )
                return int(rows[0][0]) if rows else 0
            except TaskDagReplanStoreError:
                raise
            except (TypeError, ValueError) as error:
                raise TaskDagReplanStoreError(
                    "source DAG replan depth is invalid", kind="integrity"
                ) from error
            except sqlite3.Error as error:
                raise TaskDagReplanStoreError(
                    "source DAG replan depth could not be loaded"
                ) from error

        return await run_blocking(load)

    async def claim_task_dag_replan_attempt(
        self,
        attempt: DagReplanAttempt,
        *,
        now: datetime,
    ) -> DagReplanAttemptClaim:
        if not isinstance(attempt, DagReplanAttempt):
            raise TypeError("DAG replan attempt must be canonical")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("DAG replan claim time must be timezone-aware")
        now_utc = now.astimezone(UTC)
        prepared = replace(attempt, created_at=now_utc, updated_at=now_utc)

        def claim() -> DagReplanAttemptClaim:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                _verify_replan_source_snapshot(
                    connection,
                    source_dag_id=prepared.source_dag_id,
                    parent_session_id=prepared.parent_session_id,
                    source_definition_fingerprint=prepared.source_definition_fingerprint,
                    source_generation=prepared.source_generation,
                    source_state=prepared.source_state,
                )
                depth = _load_replan_source_depth(connection, prepared.source_dag_id)
                if depth != prepared.revision_depth - 1:
                    raise TaskDagReplanStoreError(
                        "DAG replan depth identity is inconsistent", kind="integrity"
                    )
                current = _load_dag_replan_attempt(connection, prepared.revision_id)
                if current is None:
                    source_row = connection.execute(
                        """
                        SELECT revision_id
                        FROM orchestration_dag_replan_attempts
                        WHERE source_dag_id = ?
                          AND source_definition_fingerprint = ?
                          AND source_generation = ?
                        """,
                        (
                            prepared.source_dag_id,
                            prepared.source_definition_fingerprint,
                            prepared.source_generation,
                        ),
                    ).fetchone()
                    if source_row is not None:
                        raise TaskDagReplanStoreError(
                            "source snapshot already belongs to another replan revision",
                            kind="integrity",
                        )
                    connection.execute(
                        _DAG_REPLAN_ATTEMPT_INSERT,
                        _dag_replan_attempt_values(prepared),
                    )
                    connection.commit()
                    return DagReplanAttemptClaim(prepared, True)
                if not _same_dag_replan_identity(current, prepared):
                    raise TaskDagReplanStoreError(
                        "DAG replan identity is already bound to different input",
                        kind="integrity",
                    )
                if (
                    current.state is DagReplanAttemptState.CLAIMED
                    and current.lease_expires_at <= now_utc
                ):
                    if (
                        current.model_response is not None
                        or current.proposal_fingerprint is not None
                        or current.successor_dag_id is not None
                    ):
                        raise TaskDagReplanStoreError(
                            "expired DAG replan attempt has committed output",
                            kind="integrity",
                        )
                    cursor = connection.execute(
                        """
                        UPDATE orchestration_dag_replan_attempts
                        SET planner_session_id = ?, planner_turn_id = ?, owner_id = ?,
                            lease_expires_at = ?, updated_at = ?
                        WHERE revision_id = ? AND state = ? AND lease_expires_at <= ?
                        """,
                        (
                            prepared.planner_session_id,
                            prepared.planner_turn_id,
                            prepared.owner_id,
                            prepared.lease_expires_at.astimezone(UTC).isoformat(),
                            now_utc.isoformat(),
                            current.revision_id,
                            DagReplanAttemptState.CLAIMED.value,
                            now_utc.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        connection.commit()
                        refreshed = _load_dag_replan_attempt(connection, current.revision_id)
                        if refreshed is None:
                            raise TaskDagReplanStoreError(
                                "DAG replan attempt disappeared after takeover"
                            )
                        return DagReplanAttemptClaim(refreshed, True)
                connection.commit()
                return DagReplanAttemptClaim(current, False)
            except TaskDagReplanStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise TaskDagReplanStoreError(
                    "DAG replan attempt could not be claimed",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagReplanStoreError("DAG replan attempt claim failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def fence_task_dag_replan_attempt(
        self,
        revision_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        source_dag_id: str,
        source_definition_fingerprint: str,
        source_generation: int,
        source_state: str,
        evidence_fingerprint: str,
        updated_at: datetime,
    ) -> DagReplanAttempt:
        _validated_dag_replan_identifier(revision_id)
        _validated_dag_replan_identifier(owner_id)
        _validated_dag_replan_identifier(planner_session_id)
        _validated_dag_replan_identifier(planner_turn_id)
        _validated_task_dag_identifier(source_dag_id)
        _validated_dag_replan_fingerprint(source_definition_fingerprint)
        _validated_dag_replan_fingerprint(evidence_fingerprint)
        if (
            isinstance(source_generation, bool)
            or not isinstance(source_generation, int)
            or source_generation < 0
        ):
            raise TypeError("DAG replan source generation must be non-negative")
        if source_state != TaskDagState.FAILED.value:
            raise ValueError("DAG replan source state must be failed")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("DAG replan provider fence time must be timezone-aware")
        updated_at_utc = updated_at.astimezone(UTC)

        def fence() -> DagReplanAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_dag_replan_attempt(connection, revision_id)
                if current is None:
                    raise TaskDagReplanStoreError("DAG replan attempt is missing", kind="unmanaged")
                if current.state is DagReplanAttemptState.PROVIDER_FENCED:
                    if (
                        current.owner_id == owner_id
                        and current.planner_session_id == planner_session_id
                        and current.planner_turn_id == planner_turn_id
                    ):
                        connection.commit()
                        return current
                    raise TaskDagReplanStoreError(
                        "DAG replan provider fence identity conflicts",
                        kind="concurrent_modification",
                    )
                if (
                    current.state is not DagReplanAttemptState.CLAIMED
                    or current.owner_id != owner_id
                    or current.planner_session_id != planner_session_id
                    or current.planner_turn_id != planner_turn_id
                    or current.lease_expires_at <= updated_at_utc
                ):
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt is no longer owned by this controller",
                        kind="concurrent_modification",
                    )
                if (
                    current.source_dag_id != source_dag_id
                    or current.source_definition_fingerprint != source_definition_fingerprint
                    or current.source_generation != source_generation
                    or current.source_state.value != source_state
                    or current.evidence_fingerprint != evidence_fingerprint
                ):
                    raise TaskDagReplanStoreError(
                        "DAG replan source snapshot identity conflicts",
                        kind="integrity",
                    )
                _verify_replan_source_snapshot(
                    connection,
                    source_dag_id=source_dag_id,
                    parent_session_id=current.parent_session_id,
                    source_definition_fingerprint=source_definition_fingerprint,
                    source_generation=source_generation,
                    source_state=current.source_state,
                )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_dag_replan_attempts
                    SET state = ?, updated_at = ?
                    WHERE revision_id = ? AND state = ? AND owner_id = ?
                      AND planner_session_id = ? AND planner_turn_id = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        DagReplanAttemptState.PROVIDER_FENCED.value,
                        updated_at_utc.isoformat(),
                        revision_id,
                        DagReplanAttemptState.CLAIMED.value,
                        owner_id,
                        planner_session_id,
                        planner_turn_id,
                        updated_at_utc.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskDagReplanStoreError(
                        "DAG replan provider fence was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_dag_replan_attempt(connection, revision_id)
                if result is None:
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt disappeared after provider fence"
                    )
                return result
            except TaskDagReplanStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagReplanStoreError("DAG replan provider fence failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(fence)

    async def mark_task_dag_replan_model_committed(
        self,
        revision_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        model_response: str,
        updated_at: datetime,
    ) -> DagReplanAttempt:
        _validated_dag_replan_identifier(revision_id)
        _validated_dag_replan_identifier(owner_id)
        _validated_dag_replan_identifier(planner_session_id)
        _validated_dag_replan_identifier(planner_turn_id)
        if not isinstance(model_response, str) or not model_response.strip():
            raise ValueError("DAG replan model response must not be empty")
        if len(model_response.encode("utf-8")) > MAX_DAG_REPLAN_RESPONSE_BYTES:
            raise ValueError("DAG replan model response is too large")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("DAG replan model commit time must be timezone-aware")

        def commit_model() -> DagReplanAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_dag_replan_attempt(connection, revision_id)
                if current is None:
                    raise TaskDagReplanStoreError("DAG replan attempt is missing", kind="unmanaged")
                if current.state is DagReplanAttemptState.MODEL_COMMITTED:
                    if (
                        current.planner_session_id == planner_session_id
                        and current.planner_turn_id == planner_turn_id
                        and current.model_response == model_response
                    ):
                        connection.commit()
                        return current
                    raise TaskDagReplanStoreError(
                        "DAG replan model result conflicts", kind="integrity"
                    )
                if (
                    current.state is not DagReplanAttemptState.PROVIDER_FENCED
                    or current.owner_id != owner_id
                    or current.planner_session_id != planner_session_id
                    or current.planner_turn_id != planner_turn_id
                ):
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt is no longer owned by this controller",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_dag_replan_attempts
                    SET state = ?, model_response = ?, updated_at = ?
                    WHERE revision_id = ? AND state = ? AND owner_id = ?
                      AND planner_session_id = ? AND planner_turn_id = ?
                    """,
                    (
                        DagReplanAttemptState.MODEL_COMMITTED.value,
                        model_response,
                        updated_at.astimezone(UTC).isoformat(),
                        revision_id,
                        DagReplanAttemptState.PROVIDER_FENCED.value,
                        owner_id,
                        planner_session_id,
                        planner_turn_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskDagReplanStoreError(
                        "DAG replan model commit was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_dag_replan_attempt(connection, revision_id)
                if result is None:
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt disappeared after model commit"
                    )
                return result
            except TaskDagReplanStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagReplanStoreError(
                    "DAG replan model result could not be committed"
                ) from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(commit_model)

    async def publish_task_dag_replan_proposal(
        self,
        proposal: DagReplanProposalRecord,
    ) -> DagReplanProposalRecord:
        if not isinstance(proposal, DagReplanProposalRecord):
            raise TypeError("DAG replan proposal must be canonical")

        def publish() -> DagReplanProposalRecord:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                attempt = _load_dag_replan_attempt(connection, proposal.revision_id)
                if attempt is None:
                    raise TaskDagReplanStoreError("DAG replan attempt is missing", kind="unmanaged")
                _verify_dag_replan_proposal_identity(proposal, attempt)
                current = _load_dag_replan_proposal(connection, proposal.revision_id)
                if current is not None:
                    if not _same_dag_replan_proposal(current, proposal):
                        raise TaskDagReplanStoreError(
                            "DAG replan proposal is immutable and already differs",
                            kind="integrity",
                        )
                    if attempt.state is DagReplanAttemptState.MODEL_COMMITTED:
                        _transition_dag_replan_in_transaction(
                            connection,
                            attempt,
                            expected=DagReplanAttemptState.MODEL_COMMITTED,
                            state=DagReplanAttemptState.PROPOSAL_PUBLISHED,
                            updated_at=proposal.created_at,
                        )
                    connection.commit()
                    return current
                if attempt.state not in {
                    DagReplanAttemptState.MODEL_COMMITTED,
                    DagReplanAttemptState.PROPOSAL_PUBLISHED,
                }:
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt is not ready for proposal publication",
                        kind="concurrent_modification",
                    )
                if (
                    attempt.proposal_fingerprint is not None
                    and attempt.proposal_fingerprint != proposal.proposal_fingerprint
                ):
                    raise TaskDagReplanStoreError(
                        "DAG replan proposal fingerprint conflicts", kind="integrity"
                    )
                connection.execute(
                    _DAG_REPLAN_PROPOSAL_INSERT,
                    _dag_replan_proposal_values(proposal),
                )
                if attempt.state is DagReplanAttemptState.MODEL_COMMITTED:
                    cursor = connection.execute(
                        """
                        UPDATE orchestration_dag_replan_attempts
                        SET state = ?, proposal_fingerprint = ?, updated_at = ?
                        WHERE revision_id = ? AND state = ?
                        """,
                        (
                            DagReplanAttemptState.PROPOSAL_PUBLISHED.value,
                            proposal.proposal_fingerprint,
                            proposal.created_at.astimezone(UTC).isoformat(),
                            proposal.revision_id,
                            DagReplanAttemptState.MODEL_COMMITTED.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise TaskDagReplanStoreError(
                            "DAG replan proposal publication was lost",
                            kind="concurrent_modification",
                        )
                persisted = _load_dag_replan_proposal(connection, proposal.revision_id)
                if persisted is None or not _same_dag_replan_proposal(persisted, proposal):
                    raise TaskDagReplanStoreError(
                        "DAG replan proposal was not durably verified", kind="integrity"
                    )
                connection.commit()
                return persisted
            except TaskDagReplanStoreError:
                connection.rollback()
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                raise TaskDagReplanStoreError(
                    "DAG replan proposal integrity verification failed", kind="integrity"
                ) from error
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise TaskDagReplanStoreError(
                    "DAG replan proposal conflicts with existing evidence",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagReplanStoreError("DAG replan proposal publication failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(publish)

    async def get_task_dag_replan_proposal(
        self,
        revision_id: str,
    ) -> DagReplanProposalRecord | None:
        _validated_dag_replan_identifier(revision_id)

        def load() -> DagReplanProposalRecord | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_dag_replan_proposal(connection, revision_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise TaskDagReplanStoreError(
                    "DAG replan proposal record is invalid", kind="integrity"
                ) from error
            except sqlite3.Error as error:
                raise TaskDagReplanStoreError("DAG replan proposal could not be loaded") from error

        return await run_blocking(load)

    async def mark_task_dag_replan_successor_published(
        self,
        revision_id: str,
        *,
        successor_dag_id: str,
        proposal_fingerprint: str,
        updated_at: datetime,
    ) -> DagReplanAttempt:
        _validated_dag_replan_identifier(revision_id)
        _validated_task_dag_identifier(successor_dag_id)
        _validated_dag_replan_fingerprint(proposal_fingerprint)
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("DAG replan successor publication time must be timezone-aware")

        def publish() -> DagReplanAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_dag_replan_attempt(connection, revision_id)
                if current is None:
                    raise TaskDagReplanStoreError("DAG replan attempt is missing", kind="unmanaged")
                if successor_dag_id == current.source_dag_id:
                    raise TaskDagReplanStoreError(
                        "DAG replan successor must differ from source", kind="integrity"
                    )
                if current.successor_dag_id is not None and (
                    current.successor_dag_id != successor_dag_id
                    or current.proposal_fingerprint != proposal_fingerprint
                ):
                    raise TaskDagReplanStoreError(
                        "DAG replan successor identity conflicts", kind="integrity"
                    )
                proposal = _load_dag_replan_proposal(connection, revision_id)
                if proposal is None or proposal.proposal_fingerprint != proposal_fingerprint:
                    raise TaskDagReplanStoreError(
                        "DAG replan proposal is missing or conflicts", kind="integrity"
                    )
                _verify_replan_source_snapshot(
                    connection,
                    source_dag_id=current.source_dag_id,
                    parent_session_id=current.parent_session_id,
                    source_definition_fingerprint=current.source_definition_fingerprint,
                    source_generation=current.source_generation,
                    source_state=current.source_state,
                )
                successor = _load_task_dag(connection, successor_dag_id)
                if successor is None or successor.parent_session_id != current.parent_session_id:
                    raise TaskDagReplanStoreError(
                        "DAG replan successor DAG is missing or outside its parent",
                        kind="integrity",
                    )
                if current.state in {
                    DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED,
                    DagReplanAttemptState.COMPLETED,
                }:
                    connection.commit()
                    return current
                if (
                    current.state is not DagReplanAttemptState.PROPOSAL_PUBLISHED
                    or current.proposal_fingerprint != proposal_fingerprint
                ):
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt is not ready for successor publication",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_dag_replan_attempts
                    SET state = ?, successor_dag_id = ?, updated_at = ?
                    WHERE revision_id = ? AND state = ? AND proposal_fingerprint = ?
                    """,
                    (
                        DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED.value,
                        successor_dag_id,
                        updated_at.astimezone(UTC).isoformat(),
                        revision_id,
                        DagReplanAttemptState.PROPOSAL_PUBLISHED.value,
                        proposal_fingerprint,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskDagReplanStoreError(
                        "DAG replan successor publication was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_dag_replan_attempt(connection, revision_id)
                if result is None:
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt disappeared after successor publication"
                    )
                return result
            except TaskDagReplanStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise TaskDagReplanStoreError(
                    "DAG replan successor identity conflicts", kind="integrity"
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagReplanStoreError("DAG replan successor publication failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(publish)

    async def transition_task_dag_replan_attempt(
        self,
        revision_id: str,
        *,
        expected_state: DagReplanAttemptState,
        state: DagReplanAttemptState,
        updated_at: datetime,
    ) -> DagReplanAttempt:
        _validated_dag_replan_identifier(revision_id)
        if not isinstance(expected_state, DagReplanAttemptState) or not isinstance(
            state, DagReplanAttemptState
        ):
            raise TypeError("DAG replan states must be canonical")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("DAG replan transition time must be timezone-aware")

        def transition() -> DagReplanAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_dag_replan_attempt(connection, revision_id)
                if current is None:
                    raise TaskDagReplanStoreError("DAG replan attempt is missing", kind="unmanaged")
                if current.state is state:
                    connection.commit()
                    return current
                if current.state is not expected_state:
                    raise TaskDagReplanStoreError(
                        "DAG replan attempt state is stale", kind="concurrent_modification"
                    )
                if not current.state.can_transition_to(state):
                    raise TaskDagReplanStoreError(
                        "DAG replan lifecycle transition is not allowed", kind="protocol"
                    )
                result = _transition_dag_replan_in_transaction(
                    connection,
                    current,
                    expected=expected_state,
                    state=state,
                    updated_at=updated_at,
                )
                connection.commit()
                return result
            except TaskDagReplanStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise TaskDagReplanStoreError("DAG replan transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)


_DAG_REPLAN_ATTEMPT_SELECT = """
    SELECT revision_id, parent_session_id, source_dag_id,
           source_definition_fingerprint, source_generation, source_state,
           revision_depth, evidence_fingerprint, evidence_json,
           planner_session_id, planner_turn_id, intended_successor_dag_id,
           state, owner_id, lease_expires_at, model_response,
           proposal_fingerprint, successor_dag_id, created_at, updated_at
    FROM orchestration_dag_replan_attempts
"""

_DAG_REPLAN_ATTEMPT_INSERT = """
    INSERT INTO orchestration_dag_replan_attempts(
        revision_id, parent_session_id, source_dag_id,
        source_definition_fingerprint, source_generation, source_state,
        revision_depth, evidence_fingerprint, evidence_json,
        planner_session_id, planner_turn_id, intended_successor_dag_id,
        state, owner_id, lease_expires_at, model_response,
        proposal_fingerprint, successor_dag_id, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_DAG_REPLAN_PROPOSAL_SELECT = """
    SELECT proposal_id, revision_id, parent_session_id, source_dag_id,
           source_definition_fingerprint, source_generation, evidence_fingerprint,
           intended_successor_dag_id, proposal_fingerprint, canonical_json, created_at
    FROM orchestration_dag_replan_proposals
"""

_DAG_REPLAN_PROPOSAL_INSERT = """
    INSERT INTO orchestration_dag_replan_proposals(
        proposal_id, revision_id, parent_session_id, source_dag_id,
        source_definition_fingerprint, source_generation, evidence_fingerprint,
        intended_successor_dag_id, proposal_fingerprint, canonical_json, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _validated_dag_replan_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("DAG replan identifier is invalid")


def _validated_dag_replan_fingerprint(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("DAG replan fingerprint is invalid")


def _dag_replan_attempt_values(attempt: DagReplanAttempt) -> tuple[object, ...]:
    if attempt.created_at is None or attempt.updated_at is None:
        raise TaskDagReplanStoreError("DAG replan attempt timestamps are required", kind="protocol")
    return (
        attempt.revision_id,
        attempt.parent_session_id,
        attempt.source_dag_id,
        attempt.source_definition_fingerprint,
        attempt.source_generation,
        attempt.source_state.value,
        attempt.revision_depth,
        attempt.evidence_fingerprint,
        attempt.evidence_json,
        attempt.planner_session_id,
        attempt.planner_turn_id,
        attempt.intended_successor_dag_id,
        attempt.state.value,
        attempt.owner_id,
        attempt.lease_expires_at.astimezone(UTC).isoformat(),
        attempt.model_response,
        attempt.proposal_fingerprint,
        attempt.successor_dag_id,
        attempt.created_at.astimezone(UTC).isoformat(),
        attempt.updated_at.astimezone(UTC).isoformat(),
    )


def _dag_replan_proposal_values(proposal: DagReplanProposalRecord) -> tuple[object, ...]:
    return (
        proposal.proposal_id,
        proposal.revision_id,
        proposal.parent_session_id,
        proposal.source_dag_id,
        proposal.source_definition_fingerprint,
        proposal.source_generation,
        proposal.evidence_fingerprint,
        proposal.intended_successor_dag_id,
        proposal.proposal_fingerprint,
        proposal.proposal.canonical_json,
        proposal.created_at.astimezone(UTC).isoformat(),
    )


def _same_dag_replan_identity(
    left: DagReplanAttempt,
    right: DagReplanAttempt,
) -> bool:
    return (
        left.revision_id == right.revision_id
        and left.parent_session_id == right.parent_session_id
        and left.source_dag_id == right.source_dag_id
        and left.source_definition_fingerprint == right.source_definition_fingerprint
        and left.source_generation == right.source_generation
        and left.source_state is right.source_state
        and left.revision_depth == right.revision_depth
        and left.evidence_fingerprint == right.evidence_fingerprint
        and left.evidence_json == right.evidence_json
        and left.intended_successor_dag_id == right.intended_successor_dag_id
    )


def _same_dag_replan_proposal(
    left: DagReplanProposalRecord,
    right: DagReplanProposalRecord,
) -> bool:
    return (
        left.revision_id == right.revision_id
        and left.parent_session_id == right.parent_session_id
        and left.source_dag_id == right.source_dag_id
        and left.source_definition_fingerprint == right.source_definition_fingerprint
        and left.source_generation == right.source_generation
        and left.evidence_fingerprint == right.evidence_fingerprint
        and left.intended_successor_dag_id == right.intended_successor_dag_id
        and left.proposal_fingerprint == right.proposal_fingerprint
        and left.proposal.canonical_json == right.proposal.canonical_json
    )


def _load_dag_replan_attempt(
    connection: sqlite3.Connection,
    revision_id: str,
) -> DagReplanAttempt | None:
    row = connection.execute(
        _DAG_REPLAN_ATTEMPT_SELECT + " WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()
    return _dag_replan_attempt_from_row(row) if row is not None else None


def _dag_replan_attempt_from_row(row: Sequence[object]) -> DagReplanAttempt:
    if len(row) != 20:
        raise ValueError("DAG replan attempt record is malformed")
    (
        revision_id,
        parent_session_id,
        source_dag_id,
        source_definition_fingerprint,
        source_generation,
        raw_source_state,
        revision_depth,
        evidence_fingerprint,
        evidence_json,
        planner_session_id,
        planner_turn_id,
        intended_successor_dag_id,
        raw_state,
        owner_id,
        raw_lease_expires_at,
        model_response,
        proposal_fingerprint,
        successor_dag_id,
        raw_created_at,
        raw_updated_at,
    ) = row
    return DagReplanAttempt(
        revision_id=str(revision_id),
        parent_session_id=str(parent_session_id),
        source_dag_id=str(source_dag_id),
        source_definition_fingerprint=str(source_definition_fingerprint),
        source_generation=int(str(source_generation)),
        source_state=TaskDagState(str(raw_source_state)),
        revision_depth=int(str(revision_depth)),
        evidence_fingerprint=str(evidence_fingerprint),
        evidence_json=str(evidence_json),
        planner_session_id=str(planner_session_id),
        planner_turn_id=str(planner_turn_id),
        intended_successor_dag_id=str(intended_successor_dag_id),
        state=DagReplanAttemptState(str(raw_state)),
        owner_id=str(owner_id),
        lease_expires_at=datetime.fromisoformat(str(raw_lease_expires_at)),
        model_response=str(model_response) if model_response is not None else None,
        proposal_fingerprint=(
            str(proposal_fingerprint) if proposal_fingerprint is not None else None
        ),
        successor_dag_id=str(successor_dag_id) if successor_dag_id is not None else None,
        created_at=datetime.fromisoformat(str(raw_created_at)),
        updated_at=datetime.fromisoformat(str(raw_updated_at)),
    )


def _load_dag_replan_proposal(
    connection: sqlite3.Connection,
    revision_id: str,
) -> DagReplanProposalRecord | None:
    row = connection.execute(
        _DAG_REPLAN_PROPOSAL_SELECT + " WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()
    return _dag_replan_proposal_from_row(row) if row is not None else None


def _dag_replan_proposal_from_row(row: Sequence[object]) -> DagReplanProposalRecord:
    if len(row) != 11:
        raise ValueError("DAG replan proposal record is malformed")
    (
        proposal_id,
        revision_id,
        parent_session_id,
        source_dag_id,
        source_definition_fingerprint,
        source_generation,
        evidence_fingerprint,
        intended_successor_dag_id,
        proposal_fingerprint,
        canonical_json,
        raw_created_at,
    ) = row
    proposal = ModelDagProposal.parse(str(canonical_json))
    if proposal.canonical_json != str(canonical_json):
        raise ValueError("DAG replan proposal canonical JSON is inconsistent")
    if proposal.fingerprint != str(proposal_fingerprint):
        raise ValueError("DAG replan proposal fingerprint is inconsistent")
    return DagReplanProposalRecord(
        proposal_id=str(proposal_id),
        revision_id=str(revision_id),
        parent_session_id=str(parent_session_id),
        source_dag_id=str(source_dag_id),
        source_definition_fingerprint=str(source_definition_fingerprint),
        source_generation=int(str(source_generation)),
        evidence_fingerprint=str(evidence_fingerprint),
        intended_successor_dag_id=str(intended_successor_dag_id),
        proposal=proposal,
        created_at=datetime.fromisoformat(str(raw_created_at)),
    )


def _load_replan_source_depth(connection: sqlite3.Connection, dag_id: str) -> int:
    rows = connection.execute(
        """
        SELECT revision_depth
        FROM orchestration_dag_replan_attempts
        WHERE successor_dag_id = ?
        """,
        (dag_id,),
    ).fetchall()
    if len(rows) > 1:
        raise TaskDagReplanStoreError("source DAG has divergent replan parents", kind="integrity")
    return int(rows[0][0]) if rows else 0


def _verify_replan_source_snapshot(
    connection: sqlite3.Connection,
    *,
    source_dag_id: str,
    parent_session_id: str,
    source_definition_fingerprint: str,
    source_generation: int,
    source_state: TaskDagState,
) -> None:
    source = _load_task_dag(connection, source_dag_id)
    if source is None:
        raise TaskDagReplanStoreError("source DAG is missing", kind="unmanaged")
    if (
        source.parent_session_id != parent_session_id
        or source.definition_fingerprint != source_definition_fingerprint
        or source.generation != source_generation
        or source.state is not source_state
        or source.state is not TaskDagState.FAILED
        or source.running_node_ids
        or any(node.state is TaskDagNodeState.INDETERMINATE for node in source.nodes)
        or not all(node.state.terminal for node in source.nodes)
    ):
        raise TaskDagReplanStoreError(
            "source DAG snapshot is no longer eligible for replan",
            kind="concurrent_modification",
        )


def _verify_dag_replan_proposal_identity(
    proposal: DagReplanProposalRecord,
    attempt: DagReplanAttempt,
) -> None:
    if (
        proposal.revision_id != attempt.revision_id
        or proposal.parent_session_id != attempt.parent_session_id
        or proposal.source_dag_id != attempt.source_dag_id
        or proposal.source_definition_fingerprint != attempt.source_definition_fingerprint
        or proposal.source_generation != attempt.source_generation
        or proposal.evidence_fingerprint != attempt.evidence_fingerprint
        or proposal.intended_successor_dag_id != attempt.intended_successor_dag_id
    ):
        raise TaskDagReplanStoreError("DAG replan proposal identity conflicts", kind="integrity")


def _transition_dag_replan_in_transaction(
    connection: sqlite3.Connection,
    current: DagReplanAttempt,
    *,
    expected: DagReplanAttemptState,
    state: DagReplanAttemptState,
    updated_at: datetime,
) -> DagReplanAttempt:
    if current.state is not expected:
        raise TaskDagReplanStoreError(
            "DAG replan attempt state is stale", kind="concurrent_modification"
        )
    if not current.state.can_transition_to(state):
        raise TaskDagReplanStoreError(
            "DAG replan lifecycle transition is not allowed", kind="protocol"
        )
    cursor = connection.execute(
        """
        UPDATE orchestration_dag_replan_attempts
        SET state = ?, updated_at = ?
        WHERE revision_id = ? AND state = ?
        """,
        (
            state.value,
            updated_at.astimezone(UTC).isoformat(),
            current.revision_id,
            expected.value,
        ),
    )
    if cursor.rowcount != 1:
        raise TaskDagReplanStoreError(
            "DAG replan transition was lost", kind="concurrent_modification"
        )
    result = _load_dag_replan_attempt(connection, current.revision_id)
    if result is None:
        raise TaskDagReplanStoreError("DAG replan attempt disappeared after transition")
    return result
