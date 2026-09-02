"""SQLite persistence model_planning owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

from neuro_code.application.ports.model_planning import (
    ModelPlanningStoreError,
    PlanningAttemptClaim,
)
from neuro_code.domain.model_planning import (
    MAX_MODEL_PLANNING_RESPONSE_BYTES,
    ModelDagProposal,
    PlanningAttempt,
    PlanningAttemptState,
    PlanningProposalRecord,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_dag import _validated_task_dag_identifier
from neuro_code.shared.async_utils import run_blocking


class ModelPlanningMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def get_model_planning_attempt(self, planning_id: str) -> PlanningAttempt | None:
        _validated_model_planning_identifier(planning_id)

        def load() -> PlanningAttempt | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_model_planning_attempt(connection, planning_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ModelPlanningStoreError(
                    "model planning attempt record is invalid",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise ModelPlanningStoreError(
                    "model planning attempt could not be loaded"
                ) from error

        return await run_blocking(load)

    async def claim_model_planning_attempt(
        self,
        attempt: PlanningAttempt,
        *,
        now: datetime,
    ) -> PlanningAttemptClaim:
        if not isinstance(attempt, PlanningAttempt):
            raise TypeError("model planning attempt must be canonical")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("model planning claim time must be timezone-aware")
        now_utc = now.astimezone(UTC)
        prepared = replace(attempt, created_at=now_utc, updated_at=now_utc)

        def claim() -> PlanningAttemptClaim:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_model_planning_attempt(connection, prepared.planning_id)
                if current is None:
                    connection.execute(
                        _MODEL_PLANNING_ATTEMPT_INSERT,
                        _model_planning_attempt_values(prepared),
                    )
                    connection.commit()
                    return PlanningAttemptClaim(prepared, True)
                if (
                    current.parent_session_id != prepared.parent_session_id
                    or current.objective_fingerprint != prepared.objective_fingerprint
                    or current.context_fingerprint != prepared.context_fingerprint
                ):
                    raise ModelPlanningStoreError(
                        "planning identity is already bound to different input",
                        kind="integrity",
                    )
                if (
                    current.state is PlanningAttemptState.CLAIMED
                    and current.lease_expires_at <= now_utc
                ):
                    if (
                        current.model_response is not None
                        or current.proposal_fingerprint is not None
                        or current.dag_id is not None
                    ):
                        raise ModelPlanningStoreError(
                            "expired planning attempt has committed output",
                            kind="integrity",
                        )
                    cursor = connection.execute(
                        """
                        UPDATE orchestration_planning_attempts
                        SET planner_session_id = ?, planner_turn_id = ?, owner_id = ?,
                            lease_expires_at = ?, updated_at = ?
                        WHERE planning_id = ? AND state = ? AND lease_expires_at <= ?
                        """,
                        (
                            prepared.planner_session_id,
                            prepared.planner_turn_id,
                            prepared.owner_id,
                            prepared.lease_expires_at.astimezone(UTC).isoformat(),
                            now_utc.isoformat(),
                            current.planning_id,
                            PlanningAttemptState.CLAIMED.value,
                            now_utc.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        connection.commit()
                        refreshed = _load_model_planning_attempt(
                            connection,
                            current.planning_id,
                        )
                        if refreshed is None:
                            raise ModelPlanningStoreError(
                                "model planning attempt disappeared after takeover"
                            )
                        return PlanningAttemptClaim(refreshed, True)
                connection.commit()
                return PlanningAttemptClaim(current, False)
            except ModelPlanningStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise ModelPlanningStoreError(
                    "model planning attempt could not be claimed",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise ModelPlanningStoreError("model planning attempt claim failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def fence_model_planning_attempt(
        self,
        planning_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        updated_at: datetime,
    ) -> PlanningAttempt:
        _validated_model_planning_identifier(planning_id)
        _validated_model_planning_identifier(owner_id)
        _validated_model_planning_identifier(planner_session_id)
        _validated_model_planning_identifier(planner_turn_id)
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("model planning provider fence time must be timezone-aware")
        updated_at_utc = updated_at.astimezone(UTC)

        def fence() -> PlanningAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_model_planning_attempt(connection, planning_id)
                if current is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt is missing",
                        kind="unmanaged",
                    )
                if current.state is PlanningAttemptState.PROVIDER_FENCED:
                    if (
                        current.owner_id == owner_id
                        and current.planner_session_id == planner_session_id
                        and current.planner_turn_id == planner_turn_id
                    ):
                        connection.commit()
                        return current
                    raise ModelPlanningStoreError(
                        "model planning provider fence identity conflicts",
                        kind="concurrent_modification",
                    )
                if (
                    current.state is not PlanningAttemptState.CLAIMED
                    or current.owner_id != owner_id
                    or current.planner_session_id != planner_session_id
                    or current.planner_turn_id != planner_turn_id
                    or current.lease_expires_at <= updated_at_utc
                ):
                    raise ModelPlanningStoreError(
                        "model planning attempt is no longer owned by this controller",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_planning_attempts
                    SET state = ?, updated_at = ?
                    WHERE planning_id = ? AND state = ? AND owner_id = ?
                      AND planner_session_id = ? AND planner_turn_id = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        PlanningAttemptState.PROVIDER_FENCED.value,
                        updated_at_utc.isoformat(),
                        planning_id,
                        PlanningAttemptState.CLAIMED.value,
                        owner_id,
                        planner_session_id,
                        planner_turn_id,
                        updated_at_utc.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ModelPlanningStoreError(
                        "model planning provider fence was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_model_planning_attempt(connection, planning_id)
                if result is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt disappeared after provider fence"
                    )
                return result
            except ModelPlanningStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ModelPlanningStoreError("model planning provider fence failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(fence)

    async def mark_model_planning_model_committed(
        self,
        planning_id: str,
        *,
        owner_id: str,
        planner_session_id: str,
        planner_turn_id: str,
        model_response: str,
        updated_at: datetime,
    ) -> PlanningAttempt:
        _validated_model_planning_identifier(planning_id)
        _validated_model_planning_identifier(owner_id)
        _validated_model_planning_identifier(planner_session_id)
        _validated_model_planning_identifier(planner_turn_id)
        if not isinstance(model_response, str) or not model_response.strip():
            raise ValueError("model planning response must not be empty")
        if len(model_response.encode("utf-8")) > MAX_MODEL_PLANNING_RESPONSE_BYTES:
            raise ValueError("model planning response is too large")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("model planning model commit time must be timezone-aware")

        def commit_model() -> PlanningAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_model_planning_attempt(connection, planning_id)
                if current is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt is missing",
                        kind="unmanaged",
                    )
                if current.state is PlanningAttemptState.MODEL_COMMITTED:
                    if (
                        current.planner_session_id == planner_session_id
                        and current.planner_turn_id == planner_turn_id
                        and current.model_response == model_response
                    ):
                        connection.commit()
                        return current
                    raise ModelPlanningStoreError(
                        "model planning model result conflicts",
                        kind="integrity",
                    )
                if (
                    current.state is not PlanningAttemptState.PROVIDER_FENCED
                    or current.owner_id != owner_id
                    or current.planner_session_id != planner_session_id
                    or current.planner_turn_id != planner_turn_id
                ):
                    raise ModelPlanningStoreError(
                        "model planning attempt is no longer owned by this controller",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_planning_attempts
                    SET state = ?, model_response = ?, updated_at = ?
                    WHERE planning_id = ? AND state = ? AND owner_id = ?
                      AND planner_session_id = ? AND planner_turn_id = ?
                    """,
                    (
                        PlanningAttemptState.MODEL_COMMITTED.value,
                        model_response,
                        updated_at.astimezone(UTC).isoformat(),
                        planning_id,
                        PlanningAttemptState.PROVIDER_FENCED.value,
                        owner_id,
                        planner_session_id,
                        planner_turn_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ModelPlanningStoreError(
                        "model planning model commit was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_model_planning_attempt(connection, planning_id)
                if result is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt disappeared after model commit"
                    )
                return result
            except ModelPlanningStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ModelPlanningStoreError(
                    "model planning model result could not be committed"
                ) from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(commit_model)

    async def publish_model_planning_proposal(
        self,
        proposal: PlanningProposalRecord,
        *,
        owner_id: str,
    ) -> PlanningProposalRecord:
        if not isinstance(proposal, PlanningProposalRecord):
            raise TypeError("model planning proposal must be canonical")
        _validated_model_planning_identifier(owner_id)

        def publish() -> PlanningProposalRecord:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                attempt = _load_model_planning_attempt(connection, proposal.planning_id)
                if attempt is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt is missing",
                        kind="unmanaged",
                    )
                if (
                    attempt.parent_session_id != proposal.parent_session_id
                    or attempt.intended_dag_id != proposal.intended_dag_id
                    or attempt.objective_fingerprint != proposal.objective_fingerprint
                    or attempt.context_fingerprint != proposal.context_fingerprint
                ):
                    raise ModelPlanningStoreError(
                        "model planning proposal identity conflicts",
                        kind="integrity",
                    )
                current = _load_model_planning_proposal(connection, proposal.planning_id)
                if current is not None:
                    if not _same_model_planning_proposal(current, proposal):
                        raise ModelPlanningStoreError(
                            "model planning proposal is immutable and already differs",
                            kind="integrity",
                        )
                    if attempt.proposal_fingerprint not in {
                        None,
                        proposal.proposal_fingerprint,
                    }:
                        raise ModelPlanningStoreError(
                            "model planning attempt proposal fingerprint conflicts",
                            kind="integrity",
                        )
                    if attempt.state is PlanningAttemptState.MODEL_COMMITTED:
                        cursor = connection.execute(
                            """
                            UPDATE orchestration_planning_attempts
                            SET state = ?, proposal_fingerprint = ?, updated_at = ?
                            WHERE planning_id = ? AND state = ?
                            """,
                            (
                                PlanningAttemptState.PROPOSAL_PUBLISHED.value,
                                current.proposal_fingerprint,
                                current.created_at.astimezone(UTC).isoformat(),
                                attempt.planning_id,
                                PlanningAttemptState.MODEL_COMMITTED.value,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ModelPlanningStoreError(
                                "model planning proposal publication was lost",
                                kind="concurrent_modification",
                            )
                    connection.commit()
                    return current
                if attempt.state not in {
                    PlanningAttemptState.MODEL_COMMITTED,
                    PlanningAttemptState.PROPOSAL_PUBLISHED,
                }:
                    raise ModelPlanningStoreError(
                        "model planning attempt is not ready for proposal publication",
                        kind="concurrent_modification",
                    )
                if (
                    attempt.proposal_fingerprint is not None
                    and attempt.proposal_fingerprint != proposal.proposal_fingerprint
                ):
                    raise ModelPlanningStoreError(
                        "model planning proposal fingerprint conflicts",
                        kind="integrity",
                    )
                connection.execute(
                    _MODEL_PLANNING_PROPOSAL_INSERT,
                    _model_planning_proposal_values(proposal),
                )
                if attempt.state is PlanningAttemptState.MODEL_COMMITTED:
                    cursor = connection.execute(
                        """
                        UPDATE orchestration_planning_attempts
                        SET state = ?, proposal_fingerprint = ?, updated_at = ?
                        WHERE planning_id = ? AND state = ?
                        """,
                        (
                            PlanningAttemptState.PROPOSAL_PUBLISHED.value,
                            proposal.proposal_fingerprint,
                            proposal.created_at.astimezone(UTC).isoformat(),
                            proposal.planning_id,
                            PlanningAttemptState.MODEL_COMMITTED.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ModelPlanningStoreError(
                            "model planning proposal publication was lost",
                            kind="concurrent_modification",
                        )
                persisted = _load_model_planning_proposal(connection, proposal.planning_id)
                if persisted is None or not _same_model_planning_proposal(persisted, proposal):
                    raise ModelPlanningStoreError(
                        "model planning proposal was not durably verified",
                        kind="integrity",
                    )
                connection.commit()
                return persisted
            except ModelPlanningStoreError:
                connection.rollback()
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                connection.rollback()
                raise ModelPlanningStoreError(
                    "model planning proposal integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise ModelPlanningStoreError(
                    "model planning proposal conflicts with existing evidence",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise ModelPlanningStoreError(
                    "model planning proposal publication failed"
                ) from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(publish)

    async def get_model_planning_proposal(
        self,
        planning_id: str,
    ) -> PlanningProposalRecord | None:
        _validated_model_planning_identifier(planning_id)

        def load() -> PlanningProposalRecord | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_model_planning_proposal(connection, planning_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ModelPlanningStoreError(
                    "model planning proposal record is invalid",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise ModelPlanningStoreError(
                    "model planning proposal could not be loaded"
                ) from error

        return await run_blocking(load)

    async def mark_model_planning_dag_published(
        self,
        planning_id: str,
        *,
        owner_id: str,
        dag_id: str,
        proposal_fingerprint: str,
        updated_at: datetime,
    ) -> PlanningAttempt:
        _validated_model_planning_identifier(planning_id)
        _validated_model_planning_identifier(owner_id)
        _validated_task_dag_identifier(dag_id)
        if not isinstance(proposal_fingerprint, str) or len(proposal_fingerprint) != 64:
            raise ValueError("model planning proposal fingerprint is invalid")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("model planning DAG publication time must be timezone-aware")

        def publish() -> PlanningAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_model_planning_attempt(connection, planning_id)
                if current is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt is missing",
                        kind="unmanaged",
                    )
                if current.dag_id is not None and (
                    current.dag_id != dag_id or current.proposal_fingerprint != proposal_fingerprint
                ):
                    raise ModelPlanningStoreError(
                        "published model planning DAG identity conflicts",
                        kind="integrity",
                    )
                if current.intended_dag_id != dag_id:
                    raise ModelPlanningStoreError(
                        "published model planning DAG is not the intended DAG",
                        kind="integrity",
                    )
                if current.state in {
                    PlanningAttemptState.DAG_PUBLISHED,
                    PlanningAttemptState.COMPLETED,
                }:
                    if (
                        current.dag_id != dag_id
                        or current.proposal_fingerprint != proposal_fingerprint
                    ):
                        raise ModelPlanningStoreError(
                            "published model planning DAG identity conflicts",
                            kind="integrity",
                        )
                    connection.commit()
                    return current
                if (
                    current.state is not PlanningAttemptState.PROPOSAL_PUBLISHED
                    or current.proposal_fingerprint != proposal_fingerprint
                ):
                    raise ModelPlanningStoreError(
                        "model planning attempt is not ready for DAG publication",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_planning_attempts
                    SET state = ?, dag_id = ?, updated_at = ?
                    WHERE planning_id = ? AND state = ?
                      AND proposal_fingerprint = ?
                    """,
                    (
                        PlanningAttemptState.DAG_PUBLISHED.value,
                        dag_id,
                        updated_at.astimezone(UTC).isoformat(),
                        planning_id,
                        PlanningAttemptState.PROPOSAL_PUBLISHED.value,
                        proposal_fingerprint,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ModelPlanningStoreError(
                        "model planning DAG publication was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_model_planning_attempt(connection, planning_id)
                if result is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt disappeared after DAG publication"
                    )
                return result
            except ModelPlanningStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ModelPlanningStoreError("model planning DAG publication failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(publish)

    async def transition_model_planning_attempt(
        self,
        planning_id: str,
        *,
        expected_state: PlanningAttemptState,
        state: PlanningAttemptState,
        owner_id: str | None = None,
        updated_at: datetime,
    ) -> PlanningAttempt:
        _validated_model_planning_identifier(planning_id)
        if owner_id is not None:
            _validated_model_planning_identifier(owner_id)
        if not isinstance(expected_state, PlanningAttemptState) or not isinstance(
            state, PlanningAttemptState
        ):
            raise TypeError("model planning states must be canonical")
        if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
            raise TypeError("model planning transition time must be timezone-aware")

        def transition() -> PlanningAttempt:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_model_planning_attempt(connection, planning_id)
                if current is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt is missing",
                        kind="unmanaged",
                    )
                if current.state is state:
                    connection.commit()
                    return current
                if current.state is not expected_state:
                    raise ModelPlanningStoreError(
                        "model planning attempt state is stale",
                        kind="concurrent_modification",
                    )
                if not current.state.can_transition_to(state):
                    raise ModelPlanningStoreError(
                        "model planning lifecycle transition is not allowed",
                        kind="protocol",
                    )
                owner_clause = ""
                owner_parameters: tuple[object, ...] = ()
                if owner_id is not None:
                    owner_clause = " AND owner_id = ?"
                    owner_parameters = (owner_id,)
                cursor = connection.execute(
                    """
                    UPDATE orchestration_planning_attempts
                    SET state = ?, updated_at = ?
                    WHERE planning_id = ? AND state = ?
                    """
                    + owner_clause,
                    (
                        state.value,
                        updated_at.astimezone(UTC).isoformat(),
                        planning_id,
                        expected_state.value,
                        *owner_parameters,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ModelPlanningStoreError(
                        "model planning transition was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                result = _load_model_planning_attempt(connection, planning_id)
                if result is None:
                    raise ModelPlanningStoreError(
                        "model planning attempt disappeared after transition"
                    )
                return result
            except ModelPlanningStoreError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ModelPlanningStoreError("model planning transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)


_MODEL_PLANNING_ATTEMPT_SELECT = """
    SELECT planning_id, parent_session_id, objective_fingerprint,
           context_fingerprint, planner_session_id, planner_turn_id,
           intended_dag_id, state, owner_id, lease_expires_at,
           model_response, proposal_fingerprint, dag_id, created_at, updated_at
    FROM orchestration_planning_attempts
"""

_MODEL_PLANNING_ATTEMPT_INSERT = """
    INSERT INTO orchestration_planning_attempts(
        planning_id, parent_session_id, objective_fingerprint, context_fingerprint,
        planner_session_id, planner_turn_id, intended_dag_id, state, owner_id,
        lease_expires_at, model_response, proposal_fingerprint, dag_id,
        created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_MODEL_PLANNING_PROPOSAL_SELECT = """
    SELECT proposal_id, planning_id, parent_session_id, intended_dag_id,
           objective_fingerprint, context_fingerprint, proposal_fingerprint,
           canonical_json, created_at
    FROM orchestration_plan_proposals
"""

_MODEL_PLANNING_PROPOSAL_INSERT = """
    INSERT INTO orchestration_plan_proposals(
        proposal_id, planning_id, parent_session_id, intended_dag_id,
        objective_fingerprint, context_fingerprint, proposal_fingerprint,
        canonical_json, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _validated_model_planning_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("model planning identifier is invalid")


def _model_planning_attempt_values(attempt: PlanningAttempt) -> tuple[object, ...]:
    if attempt.created_at is None or attempt.updated_at is None:
        raise ModelPlanningStoreError(
            "model planning attempt timestamps are required",
            kind="protocol",
        )
    return (
        attempt.planning_id,
        attempt.parent_session_id,
        attempt.objective_fingerprint,
        attempt.context_fingerprint,
        attempt.planner_session_id,
        attempt.planner_turn_id,
        attempt.intended_dag_id,
        attempt.state.value,
        attempt.owner_id,
        attempt.lease_expires_at.astimezone(UTC).isoformat(),
        attempt.model_response,
        attempt.proposal_fingerprint,
        attempt.dag_id,
        attempt.created_at.astimezone(UTC).isoformat(),
        attempt.updated_at.astimezone(UTC).isoformat(),
    )


def _model_planning_proposal_values(proposal: PlanningProposalRecord) -> tuple[object, ...]:
    return (
        proposal.proposal_id,
        proposal.planning_id,
        proposal.parent_session_id,
        proposal.intended_dag_id,
        proposal.objective_fingerprint,
        proposal.context_fingerprint,
        proposal.proposal_fingerprint,
        proposal.proposal.canonical_json,
        proposal.created_at.astimezone(UTC).isoformat(),
    )


def _same_model_planning_proposal(
    left: PlanningProposalRecord,
    right: PlanningProposalRecord,
) -> bool:
    """Compare immutable publication content, excluding generated audit IDs."""

    return (
        left.planning_id == right.planning_id
        and left.parent_session_id == right.parent_session_id
        and left.intended_dag_id == right.intended_dag_id
        and left.objective_fingerprint == right.objective_fingerprint
        and left.context_fingerprint == right.context_fingerprint
        and left.proposal_fingerprint == right.proposal_fingerprint
        and left.proposal.canonical_json == right.proposal.canonical_json
    )


def _load_model_planning_attempt(
    connection: sqlite3.Connection,
    planning_id: str,
) -> PlanningAttempt | None:
    row = connection.execute(
        _MODEL_PLANNING_ATTEMPT_SELECT + " WHERE planning_id = ?",
        (planning_id,),
    ).fetchone()
    return _model_planning_attempt_from_row(row) if row is not None else None


def _model_planning_attempt_from_row(row: Sequence[object]) -> PlanningAttempt:
    if len(row) != 15:
        raise ValueError("model planning attempt record is malformed")
    (
        planning_id,
        parent_session_id,
        objective_fingerprint,
        context_fingerprint,
        planner_session_id,
        planner_turn_id,
        intended_dag_id,
        raw_state,
        owner_id,
        raw_lease_expires_at,
        model_response,
        proposal_fingerprint,
        dag_id,
        raw_created_at,
        raw_updated_at,
    ) = row
    return PlanningAttempt(
        planning_id=str(planning_id),
        parent_session_id=str(parent_session_id),
        objective_fingerprint=str(objective_fingerprint),
        context_fingerprint=str(context_fingerprint),
        planner_session_id=str(planner_session_id),
        planner_turn_id=str(planner_turn_id),
        intended_dag_id=str(intended_dag_id),
        state=PlanningAttemptState(str(raw_state)),
        owner_id=str(owner_id),
        lease_expires_at=datetime.fromisoformat(str(raw_lease_expires_at)),
        model_response=str(model_response) if model_response is not None else None,
        proposal_fingerprint=(
            str(proposal_fingerprint) if proposal_fingerprint is not None else None
        ),
        dag_id=str(dag_id) if dag_id is not None else None,
        created_at=datetime.fromisoformat(str(raw_created_at)),
        updated_at=datetime.fromisoformat(str(raw_updated_at)),
    )


def _load_model_planning_proposal(
    connection: sqlite3.Connection,
    planning_id: str,
) -> PlanningProposalRecord | None:
    row = connection.execute(
        _MODEL_PLANNING_PROPOSAL_SELECT + " WHERE planning_id = ?",
        (planning_id,),
    ).fetchone()
    return _model_planning_proposal_from_row(row) if row is not None else None


def _model_planning_proposal_from_row(row: Sequence[object]) -> PlanningProposalRecord:
    if len(row) != 9:
        raise ValueError("model planning proposal record is malformed")
    (
        proposal_id,
        planning_id,
        parent_session_id,
        intended_dag_id,
        objective_fingerprint,
        context_fingerprint,
        proposal_fingerprint,
        canonical_json,
        raw_created_at,
    ) = row
    proposal = ModelDagProposal.parse(str(canonical_json))
    if proposal.canonical_json != str(canonical_json):
        raise ValueError("model planning proposal canonical JSON is inconsistent")
    if proposal.fingerprint != str(proposal_fingerprint):
        raise ValueError("model planning proposal fingerprint is inconsistent")
    return PlanningProposalRecord(
        proposal_id=str(proposal_id),
        planning_id=str(planning_id),
        parent_session_id=str(parent_session_id),
        intended_dag_id=str(intended_dag_id),
        objective_fingerprint=str(objective_fingerprint),
        context_fingerprint=str(context_fingerprint),
        proposal=proposal,
        created_at=datetime.fromisoformat(str(raw_created_at)),
    )
