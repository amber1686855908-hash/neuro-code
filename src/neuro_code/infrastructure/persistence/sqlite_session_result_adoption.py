"""SQLite persistence result_adoption owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime

from neuro_code.application.ports.agent_swarm import ProcessLivenessProbe
from neuro_code.application.ports.result_adoption import (
    ResultAdoptionError,
    ResultAdoptionRecord,
    ResultAdoptionTargetRecord,
)
from neuro_code.domain.result_adoption import (
    ResultAdoptionPlan,
    ResultAdoptionState,
    ResultAdoptionTarget,
    ResultAdoptionTargetState,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.shared.async_utils import run_blocking


class ResultAdoptionMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def get_result_adoption(self, adoption_id: str) -> ResultAdoptionRecord | None:
        _validated_result_adoption_identifier(adoption_id)

        def load() -> ResultAdoptionRecord | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_result_adoption(connection, adoption_id)
            except ResultAdoptionError:
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ResultAdoptionError(
                    "result adoption record integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise ResultAdoptionError("result adoption record could not be loaded") from error

        return await run_blocking(load)

    async def insert_result_adoption(
        self,
        plan: ResultAdoptionPlan,
        *,
        owner_pid: int,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ResultAdoptionRecord:
        _validate_result_adoption_owner(owner_pid, owner_token)
        if not isinstance(plan, ResultAdoptionPlan):
            raise TypeError("result adoption plan must be canonical")
        now_utc, expiry_utc = _validate_result_adoption_times(now, lease_expires_at)

        def insert() -> ResultAdoptionRecord:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_result_adoption(connection, plan.adoption_id)
                if current is not None:
                    if current.plan != plan:
                        raise ResultAdoptionError(
                            "adoption identity is already bound to a different plan",
                            kind="integrity",
                        )
                    connection.commit()
                    return current
                plan_json = json.dumps(
                    plan.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
                )
                connection.execute(
                    """
                    INSERT INTO result_adoptions(
                        adoption_id, parent_session_id, plan_json, plan_fingerprint,
                        state, owner_pid, owner_token, lease_expires_at, created_at,
                        updated_at, error_kind, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.adoption_id,
                        plan.parent_session_id,
                        plan_json,
                        plan.fingerprint,
                        ResultAdoptionState.CLAIMED.value,
                        owner_pid,
                        owner_token,
                        expiry_utc.isoformat(),
                        now_utc.isoformat(),
                        now_utc.isoformat(),
                        None,
                        0,
                    ),
                )
                for ordinal, target in enumerate(plan.targets):
                    connection.execute(
                        """
                        INSERT INTO result_adoption_targets(
                            adoption_id, ordinal, target_json, path,
                            pre_image_fingerprint, desired_fingerprint, state,
                            observed_fingerprint, error_kind, updated_at, version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            plan.adoption_id,
                            ordinal,
                            json.dumps(
                                target.to_dict(),
                                ensure_ascii=True,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            target.path,
                            target.pre_image_fingerprint,
                            target.desired_fingerprint,
                            ResultAdoptionTargetState.NOT_STARTED.value,
                            None,
                            None,
                            now_utc.isoformat(),
                            0,
                        ),
                    )
                connection.commit()
                persisted = _load_result_adoption(connection, plan.adoption_id)
                if persisted is None:
                    raise ResultAdoptionError(
                        "result adoption disappeared after insert", kind="integrity"
                    )
                return persisted
            except ResultAdoptionError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise ResultAdoptionError(
                    "result adoption identity could not be persisted",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise ResultAdoptionError("result adoption could not be persisted") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(insert)

    async def claim_result_adoption(
        self,
        adoption_id: str,
        *,
        owner_pid: int,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> ResultAdoptionRecord:
        _validated_result_adoption_identifier(adoption_id)
        _validate_result_adoption_owner(owner_pid, owner_token)
        now_utc, expiry_utc = _validate_result_adoption_times(now, lease_expires_at)
        if not callable(owner_is_alive):
            raise TypeError("result adoption owner liveness probe is required")

        def claim() -> ResultAdoptionRecord:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_result_adoption(connection, adoption_id)
                if current is None:
                    raise ResultAdoptionError(
                        "result adoption record is missing",
                        kind="unmanaged",
                    )
                if current.state.terminal:
                    connection.commit()
                    return current
                if current.owner_pid == owner_pid and current.owner_token == owner_token:
                    connection.commit()
                    return current
                if owner_is_alive(current.owner_pid):
                    connection.commit()
                    return current
                cursor = connection.execute(
                    """
                    UPDATE result_adoptions
                    SET owner_pid = ?, owner_token = ?, lease_expires_at = ?,
                        updated_at = ?, version = ?
                    WHERE adoption_id = ? AND version = ? AND owner_pid = ?
                      AND owner_token = ? AND state NOT IN (?, ?, ?, ?)
                    """,
                    (
                        owner_pid,
                        owner_token,
                        expiry_utc.isoformat(),
                        now_utc.isoformat(),
                        current.version + 1,
                        adoption_id,
                        current.version,
                        current.owner_pid,
                        current.owner_token,
                        ResultAdoptionState.COMPLETED.value,
                        ResultAdoptionState.CONFLICT.value,
                        ResultAdoptionState.FAILED.value,
                        ResultAdoptionState.INDETERMINATE.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ResultAdoptionError(
                        "result adoption ownership changed during takeover",
                        kind="concurrent_modification",
                    )
                connection.commit()
                refreshed = _load_result_adoption(connection, adoption_id)
                if refreshed is None:
                    raise ResultAdoptionError(
                        "result adoption disappeared after takeover",
                        kind="integrity",
                    )
                return refreshed
            except ResultAdoptionError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ResultAdoptionError("result adoption claim failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def transition_result_adoption(
        self,
        record: ResultAdoptionRecord,
        *,
        expected_version: int,
        expected_state: ResultAdoptionState,
    ) -> ResultAdoptionRecord:
        if not isinstance(record, ResultAdoptionRecord):
            raise TypeError("result adoption record must be canonical")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
            or record.version != expected_version + 1
        ):
            raise TypeError("result adoption expected version is invalid")
        if not isinstance(expected_state, ResultAdoptionState):
            raise TypeError("result adoption expected state is invalid")
        _validate_result_adoption_owner(record.owner_pid, record.owner_token)

        def transition() -> ResultAdoptionRecord:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_result_adoption(connection, record.adoption_id)
                if current is None:
                    raise ResultAdoptionError("result adoption is missing", kind="unmanaged")
                if current.plan != record.plan:
                    raise ResultAdoptionError(
                        "result adoption plan identity is immutable",
                        kind="integrity",
                    )
                if current.state is record.state and current == record:
                    connection.commit()
                    return current
                if current.state is not expected_state or current.version != expected_version:
                    raise ResultAdoptionError(
                        "result adoption lifecycle snapshot is stale",
                        kind="concurrent_modification",
                    )
                if (
                    current.state.terminal
                    or record.state not in _RESULT_ADOPTION_TRANSITIONS[current.state]
                ):
                    raise ResultAdoptionError(
                        "result adoption lifecycle transition is not allowed",
                        kind="protocol",
                    )
                if (
                    current.owner_pid != record.owner_pid
                    or current.owner_token != record.owner_token
                ):
                    raise ResultAdoptionError(
                        "result adoption owner fence does not match",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE result_adoptions
                    SET state = ?, owner_pid = ?, owner_token = ?,
                        lease_expires_at = ?, updated_at = ?, error_kind = ?, version = ?
                    WHERE adoption_id = ? AND state = ? AND version = ?
                      AND owner_pid = ? AND owner_token = ?
                    """,
                    (
                        record.state.value,
                        record.owner_pid,
                        record.owner_token,
                        record.lease_expires_at.astimezone(UTC).isoformat(),
                        record.updated_at.astimezone(UTC).isoformat(),
                        record.error_kind,
                        record.version,
                        record.adoption_id,
                        expected_state.value,
                        expected_version,
                        current.owner_pid,
                        current.owner_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ResultAdoptionError(
                        "result adoption lifecycle transition was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                refreshed = _load_result_adoption(connection, record.adoption_id)
                if refreshed is None:
                    raise ResultAdoptionError(
                        "result adoption disappeared after transition",
                        kind="integrity",
                    )
                return refreshed
            except ResultAdoptionError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ResultAdoptionError("result adoption transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)

    async def get_result_adoption_target(
        self,
        adoption_id: str,
        ordinal: int,
    ) -> ResultAdoptionTargetRecord | None:
        _validated_result_adoption_identifier(adoption_id)
        _validate_result_adoption_ordinal(ordinal)

        def load() -> ResultAdoptionTargetRecord | None:
            try:
                with closing(self._connect()) as connection:
                    row = connection.execute(
                        _RESULT_ADOPTION_TARGET_SELECT + " WHERE adoption_id = ? AND ordinal = ?",
                        (adoption_id, ordinal),
                    ).fetchone()
                return _result_adoption_target_from_row(row) if row is not None else None
            except ResultAdoptionError:
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ResultAdoptionError(
                    "result adoption target integrity verification failed",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise ResultAdoptionError("result adoption target could not be loaded") from error

        return await run_blocking(load)

    async def transition_result_adoption_target(
        self,
        record: ResultAdoptionTargetRecord,
        *,
        adoption_id: str,
        ordinal: int,
        owner_pid: int,
        owner_token: str,
        expected_version: int,
        expected_state: ResultAdoptionTargetState,
    ) -> ResultAdoptionTargetRecord:
        if not isinstance(record, ResultAdoptionTargetRecord):
            raise TypeError("result adoption target record must be canonical")
        _validated_result_adoption_identifier(adoption_id)
        _validate_result_adoption_ordinal(ordinal)
        _validate_result_adoption_owner(owner_pid, owner_token)
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
            or record.version != expected_version + 1
        ):
            raise TypeError("result adoption target expected version is invalid")
        if not isinstance(expected_state, ResultAdoptionTargetState):
            raise TypeError("result adoption target expected state is invalid")

        def transition() -> ResultAdoptionTargetRecord:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                adoption = _load_result_adoption(connection, adoption_id)
                if adoption is None:
                    raise ResultAdoptionError("result adoption is missing", kind="unmanaged")
                if adoption.owner_pid != owner_pid or adoption.owner_token != owner_token:
                    raise ResultAdoptionError(
                        "result adoption target owner fence does not match",
                        kind="concurrent_modification",
                    )
                current = _load_result_adoption_target(connection, adoption_id, ordinal)
                if current is None:
                    raise ResultAdoptionError(
                        "result adoption target is missing",
                        kind="unmanaged",
                    )
                if current.target != record.target:
                    raise ResultAdoptionError(
                        "result adoption target identity is immutable",
                        kind="integrity",
                    )
                if current.state is record.state and current == record:
                    connection.commit()
                    return current
                if current.state is not expected_state or current.version != expected_version:
                    raise ResultAdoptionError(
                        "result adoption target snapshot is stale",
                        kind="concurrent_modification",
                    )
                if record.state not in _RESULT_ADOPTION_TARGET_TRANSITIONS[current.state]:
                    raise ResultAdoptionError(
                        "result adoption target transition is not allowed",
                        kind="protocol",
                    )
                updated_at = (
                    record.updated_at.astimezone(UTC)
                    if record.updated_at is not None
                    else datetime.now(UTC)
                )
                cursor = connection.execute(
                    """
                    UPDATE result_adoption_targets
                    SET state = ?, observed_fingerprint = ?, error_kind = ?,
                        updated_at = ?, version = ?
                    WHERE adoption_id = ? AND ordinal = ? AND state = ? AND version = ?
                    """,
                    (
                        record.state.value,
                        record.observed_fingerprint,
                        record.error_kind,
                        updated_at.isoformat(),
                        record.version,
                        adoption_id,
                        ordinal,
                        expected_state.value,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ResultAdoptionError(
                        "result adoption target transition was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                refreshed = _load_result_adoption_target(connection, adoption_id, ordinal)
                if refreshed is None:
                    raise ResultAdoptionError(
                        "result adoption target disappeared after transition",
                        kind="integrity",
                    )
                return refreshed
            except ResultAdoptionError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise ResultAdoptionError("result adoption target transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)


_RESULT_ADOPTION_SELECT = """
    SELECT adoption_id, parent_session_id, plan_json, plan_fingerprint,
           state, owner_pid, owner_token, lease_expires_at, created_at,
           updated_at, error_kind, version
    FROM result_adoptions
"""

_RESULT_ADOPTION_TARGET_SELECT = """
    SELECT adoption_id, ordinal, target_json, path, pre_image_fingerprint,
           desired_fingerprint, state, observed_fingerprint, error_kind,
           updated_at, version
    FROM result_adoption_targets
"""

_RESULT_ADOPTION_TRANSITIONS = {
    ResultAdoptionState.CLAIMED: frozenset(
        {
            ResultAdoptionState.VERIFIED,
            ResultAdoptionState.CONFLICT,
            ResultAdoptionState.FAILED,
            ResultAdoptionState.INDETERMINATE,
        }
    ),
    ResultAdoptionState.VERIFIED: frozenset(
        {
            ResultAdoptionState.APPLYING,
            ResultAdoptionState.VERIFYING,
            ResultAdoptionState.CONFLICT,
            ResultAdoptionState.FAILED,
            ResultAdoptionState.INDETERMINATE,
        }
    ),
    ResultAdoptionState.APPLYING: frozenset(
        {
            ResultAdoptionState.VERIFYING,
            ResultAdoptionState.CONFLICT,
            ResultAdoptionState.FAILED,
            ResultAdoptionState.INDETERMINATE,
        }
    ),
    ResultAdoptionState.VERIFYING: frozenset(
        {
            ResultAdoptionState.COMPLETED,
            ResultAdoptionState.CONFLICT,
            ResultAdoptionState.INDETERMINATE,
        }
    ),
    ResultAdoptionState.COMPLETED: frozenset(),
    ResultAdoptionState.CONFLICT: frozenset(),
    ResultAdoptionState.FAILED: frozenset(),
    ResultAdoptionState.INDETERMINATE: frozenset(),
}

_RESULT_ADOPTION_TARGET_TRANSITIONS = {
    ResultAdoptionTargetState.NOT_STARTED: frozenset(
        {
            ResultAdoptionTargetState.APPLYING,
            ResultAdoptionTargetState.APPLIED,
            ResultAdoptionTargetState.CONFLICT,
            ResultAdoptionTargetState.FAILED,
            ResultAdoptionTargetState.INDETERMINATE,
        }
    ),
    ResultAdoptionTargetState.APPLYING: frozenset(
        {
            ResultAdoptionTargetState.RETRYABLE,
            ResultAdoptionTargetState.APPLIED,
            ResultAdoptionTargetState.CONFLICT,
            ResultAdoptionTargetState.FAILED,
            ResultAdoptionTargetState.INDETERMINATE,
        }
    ),
    ResultAdoptionTargetState.RETRYABLE: frozenset(
        {
            ResultAdoptionTargetState.APPLYING,
            ResultAdoptionTargetState.APPLIED,
            ResultAdoptionTargetState.CONFLICT,
            ResultAdoptionTargetState.INDETERMINATE,
        }
    ),
    ResultAdoptionTargetState.APPLIED: frozenset({ResultAdoptionTargetState.INDETERMINATE}),
    ResultAdoptionTargetState.CONFLICT: frozenset(),
    ResultAdoptionTargetState.FAILED: frozenset(),
    ResultAdoptionTargetState.INDETERMINATE: frozenset(),
}


def _load_result_adoption(
    connection: sqlite3.Connection,
    adoption_id: str,
) -> ResultAdoptionRecord | None:
    row = connection.execute(
        _RESULT_ADOPTION_SELECT + " WHERE adoption_id = ?",
        (adoption_id,),
    ).fetchone()
    if row is None:
        return None
    if len(row) != 12:
        raise ValueError("result adoption record is malformed")
    (
        raw_id,
        raw_parent_session_id,
        raw_plan_json,
        raw_plan_fingerprint,
        raw_state,
        raw_owner_pid,
        raw_owner_token,
        raw_lease_expires_at,
        raw_created_at,
        raw_updated_at,
        raw_error_kind,
        raw_version,
    ) = row
    if not isinstance(raw_owner_pid, int) or isinstance(raw_owner_pid, bool):
        raise ValueError("result adoption owner PID is invalid")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise ValueError("result adoption version is invalid")
    if not isinstance(raw_plan_json, str) or not isinstance(raw_plan_fingerprint, str):
        raise ValueError("result adoption plan payload is invalid")
    plan = ResultAdoptionPlan.from_dict(json.loads(raw_plan_json))
    if (
        plan.adoption_id != raw_id
        or plan.parent_session_id != raw_parent_session_id
        or plan.fingerprint != raw_plan_fingerprint
    ):
        raise ValueError("result adoption plan fingerprint or identity is inconsistent")
    target_rows = connection.execute(
        _RESULT_ADOPTION_TARGET_SELECT + " WHERE adoption_id = ? ORDER BY ordinal ASC",
        (adoption_id,),
    ).fetchall()
    if any(
        len(target_row) < 2 or target_row[0] != adoption_id or target_row[1] != ordinal
        for ordinal, target_row in enumerate(target_rows)
    ):
        raise ValueError("result adoption target ordinals are not contiguous")
    targets = tuple(_result_adoption_target_from_row(target_row) for target_row in target_rows)
    return ResultAdoptionRecord(
        plan=plan,
        state=ResultAdoptionState(str(raw_state)),
        owner_pid=raw_owner_pid,
        owner_token=str(raw_owner_token),
        lease_expires_at=datetime.fromisoformat(str(raw_lease_expires_at)),
        created_at=datetime.fromisoformat(str(raw_created_at)),
        updated_at=datetime.fromisoformat(str(raw_updated_at)),
        targets=targets,
        error_kind=(str(raw_error_kind) if raw_error_kind is not None else None),
        version=raw_version,
    )


def _load_result_adoption_target(
    connection: sqlite3.Connection,
    adoption_id: str,
    ordinal: int,
) -> ResultAdoptionTargetRecord | None:
    row = connection.execute(
        _RESULT_ADOPTION_TARGET_SELECT + " WHERE adoption_id = ? AND ordinal = ?",
        (adoption_id, ordinal),
    ).fetchone()
    return _result_adoption_target_from_row(row) if row is not None else None


def _result_adoption_target_from_row(row: Sequence[object]) -> ResultAdoptionTargetRecord:
    if len(row) != 11:
        raise ValueError("result adoption target record is malformed")
    (
        raw_adoption_id,
        raw_ordinal,
        raw_target_json,
        raw_path,
        raw_pre_image_fingerprint,
        raw_desired_fingerprint,
        raw_state,
        raw_observed_fingerprint,
        raw_error_kind,
        raw_updated_at,
        raw_version,
    ) = row
    if (
        not isinstance(raw_adoption_id, str)
        or not isinstance(raw_ordinal, int)
        or isinstance(raw_ordinal, bool)
        or not isinstance(raw_target_json, str)
        or not isinstance(raw_path, str)
        or not isinstance(raw_pre_image_fingerprint, str)
        or not isinstance(raw_desired_fingerprint, str)
        or not isinstance(raw_version, int)
        or isinstance(raw_version, bool)
    ):
        raise ValueError("result adoption target fields are invalid")
    target = ResultAdoptionTarget.from_dict(json.loads(raw_target_json))
    if (
        target.path != raw_path
        or target.pre_image_fingerprint != raw_pre_image_fingerprint
        or target.desired_fingerprint != raw_desired_fingerprint
    ):
        raise ValueError("result adoption target fingerprint or identity is inconsistent")
    if raw_observed_fingerprint is not None and not isinstance(raw_observed_fingerprint, str):
        raise ValueError("result adoption observed fingerprint is invalid")
    if raw_error_kind is not None and not isinstance(raw_error_kind, str):
        raise ValueError("result adoption target error kind is invalid")
    return ResultAdoptionTargetRecord(
        target=target,
        state=ResultAdoptionTargetState(str(raw_state)),
        observed_fingerprint=raw_observed_fingerprint,
        error_kind=raw_error_kind,
        updated_at=datetime.fromisoformat(str(raw_updated_at)),
        version=raw_version,
    )


def _validated_result_adoption_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("result adoption identifier is invalid")


def _validate_result_adoption_owner(owner_pid: int, owner_token: str) -> None:
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
        raise ValueError("result adoption owner PID is invalid")
    if (
        not isinstance(owner_token, str)
        or not owner_token
        or "\x00" in owner_token
        or len(owner_token.encode("utf-8")) > 256
    ):
        raise ValueError("result adoption owner token is invalid")


def _validate_result_adoption_times(
    now: datetime,
    lease_expires_at: datetime,
) -> tuple[datetime, datetime]:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise TypeError("result adoption claim time must be timezone-aware")
    if not isinstance(lease_expires_at, datetime) or lease_expires_at.tzinfo is None:
        raise TypeError("result adoption lease expiry must be timezone-aware")
    now_utc = now.astimezone(UTC)
    expiry_utc = lease_expires_at.astimezone(UTC)
    if expiry_utc <= now_utc:
        raise ValueError("result adoption lease expiry must be in the future")
    return now_utc, expiry_utc


def _validate_result_adoption_ordinal(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("result adoption target ordinal is invalid")
