"""SQLite persistence ultracode owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

from neuro_code.application.ports.agent_swarm import ProcessLivenessProbe
from neuro_code.application.ports.ultracode import UltracodeExecutionClaim, UltracodeStoreError
from neuro_code.domain.ultracode import (
    UltracodeDelegationDecision,
    UltracodeExecution,
    UltracodeExecutionState,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.shared.async_utils import run_blocking


class UltracodeMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def get_ultracode_execution(
        self,
        execution_id: str,
    ) -> UltracodeExecution | None:
        _validated_ultracode_identifier(execution_id)

        def load() -> UltracodeExecution | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_ultracode_execution(connection, execution_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise UltracodeStoreError(
                    "Ultracode execution record is invalid",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise UltracodeStoreError("Ultracode execution could not be loaded") from error

        return await run_blocking(load)

    async def claim_ultracode_execution(
        self,
        execution: UltracodeExecution,
        *,
        now: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> UltracodeExecutionClaim:
        if not isinstance(execution, UltracodeExecution):
            raise TypeError("Ultracode execution must be canonical")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("Ultracode claim time must be timezone-aware")
        if not callable(owner_is_alive):
            raise TypeError("Ultracode owner liveness probe is required")
        _validated_ultracode_identifier(execution.execution_id)
        _validated_ultracode_identifier(execution.parent_session_id)
        _validated_ultracode_identifier(execution.parent_turn_id)
        _validated_ultracode_fingerprint(execution.input_fingerprint)
        _validated_ultracode_fingerprint(execution.context_fingerprint)
        now_utc = now.astimezone(UTC)
        prepared = replace(execution, created_at=now_utc, updated_at=now_utc)

        def claim() -> UltracodeExecutionClaim:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_ultracode_execution(connection, prepared.execution_id)
                if current is None:
                    connection.execute(
                        _ULTRACODE_EXECUTION_INSERT,
                        _ultracode_execution_values(prepared),
                    )
                    connection.commit()
                    return UltracodeExecutionClaim(prepared, True)
                if not current.same_identity(prepared):
                    raise UltracodeStoreError(
                        "Ultracode execution identity is already bound to different input",
                        kind="integrity",
                    )
                if current.terminal:
                    connection.commit()
                    return UltracodeExecutionClaim(current, False)
                if (
                    current.owner_id == prepared.owner_id
                    and current.owner_pid == prepared.owner_pid
                    and current.owner_token == prepared.owner_token
                ):
                    connection.commit()
                    return UltracodeExecutionClaim(current, False)
                if owner_is_alive(current.owner_pid):
                    connection.commit()
                    return UltracodeExecutionClaim(current, False)
                cursor = connection.execute(
                    """
                    UPDATE orchestration_ultracode_executions
                    SET owner_id = ?, owner_pid = ?, owner_token = ?,
                        lease_expires_at = ?, generation = ?, updated_at = ?
                    WHERE execution_id = ? AND generation = ? AND owner_id = ?
                      AND owner_pid = ? AND owner_token = ?
                      AND state NOT IN (?, ?)
                    """,
                    (
                        prepared.owner_id,
                        prepared.owner_pid,
                        prepared.owner_token,
                        prepared.lease_expires_at.astimezone(UTC).isoformat(),
                        current.generation + 1,
                        now_utc.isoformat(),
                        current.execution_id,
                        current.generation,
                        current.owner_id,
                        current.owner_pid,
                        current.owner_token,
                        UltracodeExecutionState.COMPLETED.value,
                        UltracodeExecutionState.INDETERMINATE.value,
                    ),
                )
                if cursor.rowcount == 1:
                    connection.commit()
                    refreshed = _load_ultracode_execution(connection, current.execution_id)
                    if refreshed is None:
                        raise UltracodeStoreError(
                            "Ultracode execution disappeared after takeover",
                            kind="integrity",
                        )
                    return UltracodeExecutionClaim(refreshed, True)
                connection.commit()
                return UltracodeExecutionClaim(current, False)
            except UltracodeStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise UltracodeStoreError(
                    "Ultracode execution could not be claimed",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise UltracodeStoreError("Ultracode execution claim failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def compare_and_transition_ultracode_execution(
        self,
        execution: UltracodeExecution,
        *,
        expected_generation: int,
        expected_state: UltracodeExecutionState,
    ) -> UltracodeExecution:
        if not isinstance(execution, UltracodeExecution):
            raise TypeError("Ultracode execution must be canonical")
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 0
        ):
            raise TypeError("Ultracode expected generation is invalid")
        if not isinstance(expected_state, UltracodeExecutionState):
            raise TypeError("Ultracode expected state is invalid")
        if execution.generation != expected_generation + 1:
            raise UltracodeStoreError(
                "Ultracode transition generation is invalid",
                kind="protocol",
            )
        _validated_ultracode_identifier(execution.execution_id)

        def transition() -> UltracodeExecution:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_ultracode_execution(connection, execution.execution_id)
                if current is None:
                    raise UltracodeStoreError(
                        "Ultracode execution is missing",
                        kind="unmanaged",
                    )
                if not current.same_identity(execution):
                    raise UltracodeStoreError(
                        "Ultracode execution identity changed",
                        kind="integrity",
                    )
                if current.state is execution.state and current == execution:
                    connection.commit()
                    return current
                if current.state is not expected_state or current.generation != expected_generation:
                    raise UltracodeStoreError(
                        "Ultracode lifecycle snapshot is stale",
                        kind="concurrent_modification",
                    )
                if not current.state.can_transition_to(execution.state):
                    raise UltracodeStoreError(
                        "Ultracode lifecycle transition is not allowed",
                        kind="protocol",
                    )
                if (
                    current.owner_id != execution.owner_id
                    or current.owner_pid != execution.owner_pid
                    or current.owner_token != execution.owner_token
                ):
                    raise UltracodeStoreError(
                        "Ultracode owner fence does not match",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_ultracode_executions SET
                        state = ?, generation = ?, owner_id = ?, owner_pid = ?,
                        owner_token = ?, lease_expires_at = ?, final_response = ?,
                        final_result_fingerprint = ?, updated_at = ?
                    WHERE execution_id = ? AND state = ? AND generation = ?
                      AND owner_id = ? AND owner_pid = ? AND owner_token = ?
                    """,
                    (
                        execution.state.value,
                        execution.generation,
                        execution.owner_id,
                        execution.owner_pid,
                        execution.owner_token,
                        execution.lease_expires_at.astimezone(UTC).isoformat(),
                        execution.final_response,
                        execution.final_result_fingerprint,
                        execution.updated_at.astimezone(UTC).isoformat(),
                        execution.execution_id,
                        expected_state.value,
                        expected_generation,
                        current.owner_id,
                        current.owner_pid,
                        current.owner_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise UltracodeStoreError(
                        "Ultracode lifecycle transition was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                refreshed = _load_ultracode_execution(connection, execution.execution_id)
                if refreshed is None:
                    raise UltracodeStoreError(
                        "Ultracode execution disappeared after transition",
                        kind="integrity",
                    )
                return refreshed
            except UltracodeStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise UltracodeStoreError(
                    "Ultracode lifecycle transition violated durable identity",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise UltracodeStoreError("Ultracode lifecycle transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)

    async def get_agent_ultracode_execution(
        self,
        execution_id: str,
    ) -> UltracodeExecution | None:
        """Compatibility spelling for the single canonical projection."""

        return await self.get_ultracode_execution(execution_id)


_ULTRACODE_EXECUTION_SELECT = """
    SELECT execution_id, parent_session_id, parent_turn_id,
           input_fingerprint, context_fingerprint, decision, downstream_id,
           provider_name, model_name, context_affinity, state, generation,
           owner_id, owner_pid, owner_token, lease_expires_at,
           final_response, final_result_fingerprint, created_at, updated_at
    FROM orchestration_ultracode_executions
"""

_ULTRACODE_EXECUTION_INSERT = """
    INSERT INTO orchestration_ultracode_executions(
        execution_id, parent_session_id, parent_turn_id,
        input_fingerprint, context_fingerprint, decision, downstream_id,
        provider_name, model_name, context_affinity, state, generation,
        owner_id, owner_pid, owner_token, lease_expires_at,
        final_response, final_result_fingerprint, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _validated_ultracode_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Ultracode identifier is invalid")


def _validated_ultracode_fingerprint(value: str) -> None:
    _validated_ultracode_identifier(value)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("Ultracode fingerprint is invalid")


def _ultracode_execution_values(execution: UltracodeExecution) -> tuple[object, ...]:
    return (
        execution.execution_id,
        execution.parent_session_id,
        execution.parent_turn_id,
        execution.input_fingerprint,
        execution.context_fingerprint,
        execution.decision.value,
        execution.downstream_id,
        execution.provider_name,
        execution.model_name,
        execution.context_affinity,
        execution.state.value,
        execution.generation,
        execution.owner_id,
        execution.owner_pid,
        execution.owner_token,
        execution.lease_expires_at.astimezone(UTC).isoformat(),
        execution.final_response,
        execution.final_result_fingerprint,
        execution.created_at.astimezone(UTC).isoformat(),
        execution.updated_at.astimezone(UTC).isoformat(),
    )


def _load_ultracode_execution(
    connection: sqlite3.Connection,
    execution_id: str,
) -> UltracodeExecution | None:
    row = connection.execute(
        _ULTRACODE_EXECUTION_SELECT + " WHERE execution_id = ?",
        (execution_id,),
    ).fetchone()
    return _ultracode_execution_from_row(row) if row is not None else None


def _ultracode_execution_from_row(row: Sequence[object]) -> UltracodeExecution:
    if len(row) != 20:
        raise ValueError("Ultracode execution record is malformed")
    (
        execution_id,
        parent_session_id,
        parent_turn_id,
        input_fingerprint,
        context_fingerprint,
        raw_decision,
        downstream_id,
        provider_name,
        model_name,
        context_affinity,
        raw_state,
        generation,
        owner_id,
        owner_pid,
        owner_token,
        lease_expires_at,
        final_response,
        final_result_fingerprint,
        created_at,
        updated_at,
    ) = row
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool):
        raise ValueError("Ultracode owner PID is invalid")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise ValueError("Ultracode generation is invalid")
    return UltracodeExecution(
        execution_id=str(execution_id),
        parent_session_id=str(parent_session_id),
        parent_turn_id=str(parent_turn_id),
        input_fingerprint=str(input_fingerprint),
        context_fingerprint=str(context_fingerprint),
        decision=UltracodeDelegationDecision(str(raw_decision)),
        downstream_id=str(downstream_id),
        provider_name=str(provider_name),
        model_name=str(model_name),
        context_affinity=(str(context_affinity) if context_affinity is not None else None),
        state=UltracodeExecutionState(str(raw_state)),
        generation=generation,
        owner_id=str(owner_id),
        owner_pid=owner_pid,
        owner_token=str(owner_token),
        lease_expires_at=datetime.fromisoformat(str(lease_expires_at)),
        created_at=datetime.fromisoformat(str(created_at)),
        updated_at=datetime.fromisoformat(str(updated_at)),
        final_response=(str(final_response) if final_response is not None else None),
        final_result_fingerprint=(
            str(final_result_fingerprint) if final_result_fingerprint is not None else None
        ),
    )
