"""SQLite persistence agent_swarm owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from neuro_code.application.ports.agent_swarm import (
    AgentSwarmRunClaim,
    AgentSwarmStoreError,
    ProcessLivenessProbe,
)
from neuro_code.domain.agent_swarm import AgentSwarmRun, AgentSwarmRunState
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.shared.async_utils import run_blocking


class AgentSwarmMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def get_swarm_run(self, swarm_run_id: str) -> AgentSwarmRun | None:
        _validated_swarm_identifier(swarm_run_id)

        def load() -> AgentSwarmRun | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_agent_swarm_run(connection, swarm_run_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise AgentSwarmStoreError(
                    "Swarm run record is invalid",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise AgentSwarmStoreError("Swarm run could not be loaded") from error

        return await run_blocking(load)

    async def claim_swarm_run(
        self,
        run: AgentSwarmRun,
        *,
        now: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> AgentSwarmRunClaim:
        if not isinstance(run, AgentSwarmRun):
            raise TypeError("Swarm run must be canonical")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("Swarm claim time must be timezone-aware")
        if not callable(owner_is_alive):
            raise TypeError("Swarm owner liveness probe is required")
        _validated_swarm_identifier(run.swarm_run_id)
        _validated_swarm_identifier(run.parent_session_id)
        _validated_swarm_fingerprint(run.objective_fingerprint)
        now_utc = now.astimezone(UTC)
        prepared = replace(run, created_at=now_utc, updated_at=now_utc)

        def claim() -> AgentSwarmRunClaim:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_agent_swarm_run(connection, prepared.swarm_run_id)
                if current is None:
                    connection.execute(_SWARM_RUN_INSERT, _agent_swarm_run_values(prepared))
                    connection.commit()
                    return AgentSwarmRunClaim(prepared, True)
                if not current.same_identity(prepared):
                    raise AgentSwarmStoreError(
                        "Swarm run identity is already bound to different input",
                        kind="integrity",
                    )
                if current.terminal:
                    connection.commit()
                    return AgentSwarmRunClaim(current, False)
                if (
                    current.owner_id == prepared.owner_id
                    and current.owner_pid == prepared.owner_pid
                    and current.owner_token == prepared.owner_token
                ):
                    connection.commit()
                    return AgentSwarmRunClaim(current, False)
                if owner_is_alive(current.owner_pid):
                    connection.commit()
                    return AgentSwarmRunClaim(current, False)
                cursor = connection.execute(
                    """
                    UPDATE orchestration_swarm_runs
                    SET owner_id = ?, owner_pid = ?, owner_token = ?,
                        lease_expires_at = ?, generation = ?, updated_at = ?
                    WHERE swarm_run_id = ? AND generation = ? AND owner_id = ?
                      AND owner_pid = ? AND owner_token = ? AND state NOT IN (?, ?, ?)
                    """,
                    (
                        prepared.owner_id,
                        prepared.owner_pid,
                        prepared.owner_token,
                        prepared.lease_expires_at.astimezone(UTC).isoformat(),
                        current.generation + 1,
                        now_utc.isoformat(),
                        current.swarm_run_id,
                        current.generation,
                        current.owner_id,
                        current.owner_pid,
                        current.owner_token,
                        AgentSwarmRunState.COMPLETED.value,
                        AgentSwarmRunState.FAILED.value,
                        AgentSwarmRunState.INDETERMINATE.value,
                    ),
                )
                if cursor.rowcount == 1:
                    connection.commit()
                    refreshed = _load_agent_swarm_run(connection, current.swarm_run_id)
                    if refreshed is None:
                        raise AgentSwarmStoreError("Swarm run disappeared after takeover")
                    return AgentSwarmRunClaim(refreshed, True)
                connection.commit()
                return AgentSwarmRunClaim(current, False)
            except AgentSwarmStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise AgentSwarmStoreError(
                    "Swarm run could not be claimed",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise AgentSwarmStoreError("Swarm run claim failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def compare_and_transition_swarm_run(
        self,
        run: AgentSwarmRun,
        *,
        expected_generation: int,
        expected_state: AgentSwarmRunState,
    ) -> AgentSwarmRun:
        if not isinstance(run, AgentSwarmRun):
            raise TypeError("Swarm run must be canonical")
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 0
        ):
            raise TypeError("Swarm expected generation is invalid")
        if not isinstance(expected_state, AgentSwarmRunState):
            raise TypeError("Swarm expected state is invalid")
        if run.generation != expected_generation + 1:
            raise AgentSwarmStoreError(
                "Swarm transition generation is invalid",
                kind="protocol",
            )
        _validated_swarm_identifier(run.swarm_run_id)

        def transition() -> AgentSwarmRun:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_agent_swarm_run(connection, run.swarm_run_id)
                if current is None:
                    raise AgentSwarmStoreError("Swarm run is missing", kind="unmanaged")
                _verify_agent_swarm_run_update(current, run)
                if current.state is run.state and current == run:
                    connection.commit()
                    return current
                if current.state is not expected_state or current.generation != expected_generation:
                    raise AgentSwarmStoreError(
                        "Swarm run lifecycle snapshot is stale",
                        kind="concurrent_modification",
                    )
                if not current.state.can_transition_to(run.state):
                    raise AgentSwarmStoreError(
                        "Swarm run lifecycle transition is not allowed",
                        kind="protocol",
                    )
                if (
                    current.owner_id != run.owner_id
                    or current.owner_pid != run.owner_pid
                    or current.owner_token != run.owner_token
                ):
                    raise AgentSwarmStoreError(
                        "Swarm run owner fence does not match",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_swarm_runs SET
                        state = ?, generation = ?, owner_id = ?, owner_pid = ?,
                        owner_token = ?, lease_expires_at = ?, planner_session_id = ?,
                        planner_turn_id = ?, proposal_fingerprint = ?, root_dag_id = ?,
                        current_dag_id = ?, current_dag_generation = ?,
                        current_dag_definition_fingerprint = ?, replan_revision_id = ?,
                        successor_dag_id = ?, final_response = ?,
                        final_result_fingerprint = ?, updated_at = ?
                    WHERE swarm_run_id = ? AND state = ? AND generation = ?
                      AND owner_id = ? AND owner_pid = ? AND owner_token = ?
                    """,
                    (
                        run.state.value,
                        run.generation,
                        run.owner_id,
                        run.owner_pid,
                        run.owner_token,
                        run.lease_expires_at.astimezone(UTC).isoformat(),
                        run.planner_session_id,
                        run.planner_turn_id,
                        run.proposal_fingerprint,
                        run.root_dag_id,
                        run.current_dag_id,
                        run.current_dag_generation,
                        run.current_dag_definition_fingerprint,
                        run.replan_revision_id,
                        run.successor_dag_id,
                        run.final_response,
                        run.final_result_fingerprint,
                        run.updated_at.astimezone(UTC).isoformat(),
                        run.swarm_run_id,
                        expected_state.value,
                        expected_generation,
                        current.owner_id,
                        current.owner_pid,
                        current.owner_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AgentSwarmStoreError(
                        "Swarm run transition was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                refreshed = _load_agent_swarm_run(connection, run.swarm_run_id)
                if refreshed is None:
                    raise AgentSwarmStoreError("Swarm run disappeared after transition")
                return refreshed
            except AgentSwarmStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise AgentSwarmStoreError(
                    "Swarm run transition violated durable identity",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise AgentSwarmStoreError("Swarm run transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)

    async def get_agent_swarm_run(self, swarm_run_id: str) -> AgentSwarmRun | None:
        return await self.get_swarm_run(swarm_run_id)

    async def claim_agent_swarm_run(
        self,
        run: AgentSwarmRun,
        *,
        now: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> AgentSwarmRunClaim:
        return await self.claim_swarm_run(
            run,
            now=now,
            owner_is_alive=owner_is_alive,
        )

    async def compare_and_transition_agent_swarm_run(
        self,
        run: AgentSwarmRun,
        *,
        expected_generation: int,
        expected_state: AgentSwarmRunState,
    ) -> AgentSwarmRun:
        return await self.compare_and_transition_swarm_run(
            run,
            expected_generation=expected_generation,
            expected_state=expected_state,
        )


_SWARM_RUN_SELECT = """
    SELECT swarm_run_id, parent_session_id, objective_fingerprint, planning_id,
           state, generation, owner_id, owner_pid, owner_token, lease_expires_at,
           planner_session_id, planner_turn_id, proposal_fingerprint,
           root_dag_id, current_dag_id, current_dag_generation,
           current_dag_definition_fingerprint, replan_revision_id, successor_dag_id,
           final_response, final_result_fingerprint, created_at, updated_at
    FROM orchestration_swarm_runs
"""

_SWARM_RUN_INSERT = """
    INSERT INTO orchestration_swarm_runs(
        swarm_run_id, parent_session_id, objective_fingerprint, planning_id,
        state, generation, owner_id, owner_pid, owner_token, lease_expires_at,
        planner_session_id, planner_turn_id, proposal_fingerprint,
        root_dag_id, current_dag_id, current_dag_generation,
        current_dag_definition_fingerprint, replan_revision_id, successor_dag_id,
        final_response, final_result_fingerprint, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _validated_swarm_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Swarm identifier is invalid")


def _validated_swarm_fingerprint(value: str) -> None:
    _validated_swarm_identifier(value)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("Swarm fingerprint is invalid")


def _agent_swarm_run_values(run: AgentSwarmRun) -> tuple[object, ...]:
    return (
        run.swarm_run_id,
        run.parent_session_id,
        run.objective_fingerprint,
        run.planning_id,
        run.state.value,
        run.generation,
        run.owner_id,
        run.owner_pid,
        run.owner_token,
        run.lease_expires_at.astimezone(UTC).isoformat(),
        run.planner_session_id,
        run.planner_turn_id,
        run.proposal_fingerprint,
        run.root_dag_id,
        run.current_dag_id,
        run.current_dag_generation,
        run.current_dag_definition_fingerprint,
        run.replan_revision_id,
        run.successor_dag_id,
        run.final_response,
        run.final_result_fingerprint,
        run.created_at.astimezone(UTC).isoformat(),
        run.updated_at.astimezone(UTC).isoformat(),
    )


def _load_agent_swarm_run(
    connection: sqlite3.Connection,
    swarm_run_id: str,
) -> AgentSwarmRun | None:
    row = connection.execute(
        _SWARM_RUN_SELECT + " WHERE swarm_run_id = ?",
        (swarm_run_id,),
    ).fetchone()
    return _agent_swarm_run_from_row(row) if row is not None else None


def _agent_swarm_run_from_row(row: Sequence[object]) -> AgentSwarmRun:
    if len(row) != 23:
        raise ValueError("Swarm run record is malformed")
    (
        swarm_run_id,
        parent_session_id,
        objective_fingerprint,
        planning_id,
        raw_state,
        generation,
        owner_id,
        owner_pid,
        owner_token,
        lease_expires_at,
        planner_session_id,
        planner_turn_id,
        proposal_fingerprint,
        root_dag_id,
        current_dag_id,
        current_dag_generation,
        current_dag_definition_fingerprint,
        replan_revision_id,
        successor_dag_id,
        final_response,
        final_result_fingerprint,
        created_at,
        updated_at,
    ) = row
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool):
        raise ValueError("Swarm owner PID is invalid")
    if current_dag_generation is not None and (
        not isinstance(current_dag_generation, int) or isinstance(current_dag_generation, bool)
    ):
        raise ValueError("Swarm DAG generation is invalid")
    try:
        return AgentSwarmRun(
            swarm_run_id=cast(str, swarm_run_id),
            parent_session_id=cast(str, parent_session_id),
            objective_fingerprint=cast(str, objective_fingerprint),
            planning_id=cast(str, planning_id),
            state=AgentSwarmRunState(cast(str, raw_state)),
            generation=cast(int, generation),
            owner_id=cast(str, owner_id),
            owner_pid=owner_pid,
            owner_token=cast(str, owner_token),
            lease_expires_at=datetime.fromisoformat(str(lease_expires_at)),
            created_at=datetime.fromisoformat(str(created_at)),
            updated_at=datetime.fromisoformat(str(updated_at)),
            planner_session_id=cast(str | None, planner_session_id),
            planner_turn_id=cast(str | None, planner_turn_id),
            proposal_fingerprint=cast(str | None, proposal_fingerprint),
            root_dag_id=cast(str | None, root_dag_id),
            current_dag_id=cast(str | None, current_dag_id),
            current_dag_generation=current_dag_generation,
            current_dag_definition_fingerprint=cast(
                str | None,
                current_dag_definition_fingerprint,
            ),
            replan_revision_id=cast(str | None, replan_revision_id),
            successor_dag_id=cast(str | None, successor_dag_id),
            final_response=cast(str | None, final_response),
            final_result_fingerprint=cast(str | None, final_result_fingerprint),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Swarm run record contains invalid values") from error


def _verify_agent_swarm_run_update(
    current: AgentSwarmRun,
    proposed: AgentSwarmRun,
) -> None:
    if not current.same_identity(proposed):
        raise AgentSwarmStoreError(
            "Swarm run immutable identity conflicts",
            kind="integrity",
        )
    for field_name in (
        "planner_session_id",
        "planner_turn_id",
        "proposal_fingerprint",
        "root_dag_id",
        "replan_revision_id",
        "successor_dag_id",
        "final_response",
        "final_result_fingerprint",
    ):
        before = getattr(current, field_name)
        after = getattr(proposed, field_name)
        if before is not None and after != before:
            raise AgentSwarmStoreError(
                f"Swarm run immutable field {field_name} conflicts",
                kind="integrity",
            )
    if current.current_dag_id is not None:
        if proposed.current_dag_id != current.current_dag_id:
            if (
                current.successor_dag_id is not None
                or proposed.successor_dag_id is None
                or proposed.current_dag_id != proposed.successor_dag_id
            ):
                raise AgentSwarmStoreError(
                    "Swarm current DAG lineage conflicts",
                    kind="integrity",
                )
        elif (
            proposed.current_dag_generation is not None
            and current.current_dag_generation is not None
            and proposed.current_dag_generation < current.current_dag_generation
        ):
            raise AgentSwarmStoreError(
                "Swarm current DAG generation regressed",
                kind="integrity",
            )
    if (
        proposed.current_dag_id == current.current_dag_id
        and current.current_dag_definition_fingerprint is not None
        and proposed.current_dag_definition_fingerprint
        != current.current_dag_definition_fingerprint
    ):
        raise AgentSwarmStoreError(
            "Swarm current DAG definition fingerprint conflicts",
            kind="integrity",
        )
