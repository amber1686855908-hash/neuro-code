"""Durable, bounded Agent Swarm orchestration values.

The Swarm owns one orchestration identity and its lifecycle summary.  Planner,
Leader, Task DAG, Writable Subagent, Relay, Worktree, Checkpoint, and Replan
remain the authorities for their respective capabilities.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from neuro_code.domain.task_dag import TaskDag

MAX_SWARM_RUN_ID_BYTES = 128
MAX_SWARM_OBJECTIVE_FINGERPRINT_BYTES = 64
MAX_SWARM_RESULT_BYTES = 16 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _safe_identifier(value: str, *, field_name: str, limit: int = MAX_SWARM_RUN_ID_BYTES) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _digest(value: str, *, field_name: str) -> str:
    _safe_identifier(value, field_name=field_name, limit=MAX_SWARM_OBJECTIVE_FINGERPRINT_BYTES)
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _bounded_text(value: str | None, *, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise ValueError(f"{field_name} is not bounded safe text")
    return value


def objective_fingerprint(objective: str) -> str:
    """Return the stable identity fingerprint without retaining the objective."""

    if not isinstance(objective, str) or not objective.strip() or "\x00" in objective:
        raise ValueError("swarm objective must be non-empty text")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in objective):
        raise ValueError("swarm objective contains an unsafe control character")
    return hashlib.sha256(objective.encode("utf-8")).hexdigest()


def terminal_result_fingerprint(
    swarm_run_id: str,
    dag_id: str,
    dag_generation: int,
    dag_definition_fingerprint: str,
    response: str,
) -> str:
    """Fingerprint the bounded final projection retained for recovery."""

    _safe_identifier(swarm_run_id, field_name="swarm run id")
    _safe_identifier(dag_id, field_name="swarm DAG id")
    if (
        isinstance(dag_generation, bool)
        or not isinstance(dag_generation, int)
        or dag_generation < 0
    ):
        raise ValueError("swarm DAG generation must be non-negative")
    _digest(dag_definition_fingerprint, field_name="swarm DAG definition fingerprint")
    bounded = _bounded_text(
        response, field_name="swarm final response", limit=MAX_SWARM_RESULT_BYTES
    )
    if bounded is None or not bounded.strip():
        raise ValueError("swarm final response must not be empty")
    payload = {
        "swarm_run_id": swarm_run_id,
        "dag_id": dag_id,
        "dag_generation": dag_generation,
        "dag_definition_fingerprint": dag_definition_fingerprint,
        "response": bounded,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


class AgentSwarmRunState(StrEnum):
    """Durable orchestration phases owned by the Swarm layer."""

    CLAIMED = "claimed"
    PLANNING = "planning"
    PLANNED = "planned"
    EXECUTING = "executing"
    REPLANNING = "replanning"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"

    @property
    def terminal(self) -> bool:
        return self in {
            AgentSwarmRunState.COMPLETED,
            AgentSwarmRunState.FAILED,
            AgentSwarmRunState.INDETERMINATE,
        }

    def can_transition_to(self, proposed: AgentSwarmRunState) -> bool:
        allowed = {
            AgentSwarmRunState.CLAIMED: {
                AgentSwarmRunState.PLANNING,
                AgentSwarmRunState.INDETERMINATE,
            },
            AgentSwarmRunState.PLANNING: {
                AgentSwarmRunState.PLANNED,
                AgentSwarmRunState.INDETERMINATE,
            },
            AgentSwarmRunState.PLANNED: {
                AgentSwarmRunState.EXECUTING,
                AgentSwarmRunState.INDETERMINATE,
            },
            AgentSwarmRunState.EXECUTING: {
                AgentSwarmRunState.REPLANNING,
                AgentSwarmRunState.FINALIZING,
                AgentSwarmRunState.FAILED,
                AgentSwarmRunState.INDETERMINATE,
            },
            AgentSwarmRunState.REPLANNING: {
                AgentSwarmRunState.EXECUTING,
                AgentSwarmRunState.FAILED,
                AgentSwarmRunState.INDETERMINATE,
            },
            AgentSwarmRunState.FINALIZING: {
                AgentSwarmRunState.COMPLETED,
                AgentSwarmRunState.INDETERMINATE,
            },
            AgentSwarmRunState.COMPLETED: set(),
            AgentSwarmRunState.FAILED: set(),
            AgentSwarmRunState.INDETERMINATE: set(),
        }
        return proposed in allowed[self]


@dataclass(frozen=True, slots=True)
class AgentSwarmRun:
    """One durable bounded orchestration identity and lifecycle projection."""

    swarm_run_id: str
    parent_session_id: str
    objective_fingerprint: str
    planning_id: str
    state: AgentSwarmRunState
    generation: int
    owner_id: str
    owner_pid: int
    owner_token: str
    lease_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    planner_session_id: str | None = None
    planner_turn_id: str | None = None
    proposal_fingerprint: str | None = None
    root_dag_id: str | None = None
    current_dag_id: str | None = None
    current_dag_generation: int | None = None
    current_dag_definition_fingerprint: str | None = None
    replan_revision_id: str | None = None
    successor_dag_id: str | None = None
    final_response: str | None = None
    final_result_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.swarm_run_id, "swarm run id"),
            (self.parent_session_id, "swarm parent session id"),
            (self.planning_id, "swarm planning id"),
            (self.owner_id, "swarm owner id"),
            (self.owner_token, "swarm owner token"),
        ):
            _safe_identifier(value, field_name=field_name)
        _digest(self.objective_fingerprint, field_name="swarm objective fingerprint")
        if not isinstance(self.state, AgentSwarmRunState):
            raise ValueError("swarm run state must be canonical")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("swarm run generation must be non-negative")
        if (
            isinstance(self.owner_pid, bool)
            or not isinstance(self.owner_pid, int)
            or self.owner_pid <= 0
        ):
            raise ValueError("swarm owner pid must be positive")
        for optional_value, field_name in (
            (self.planner_session_id, "swarm planner session id"),
            (self.planner_turn_id, "swarm planner turn id"),
            (self.root_dag_id, "swarm root DAG id"),
            (self.current_dag_id, "swarm current DAG id"),
            (self.replan_revision_id, "swarm replan revision id"),
            (self.successor_dag_id, "swarm successor DAG id"),
        ):
            if optional_value is not None:
                _safe_identifier(optional_value, field_name=field_name)
        if (self.planner_session_id is None) != (self.planner_turn_id is None):
            raise ValueError("swarm planner identity must be complete")
        if self.proposal_fingerprint is not None:
            _digest(self.proposal_fingerprint, field_name="swarm proposal fingerprint")
        if (self.current_dag_generation is None) != (
            self.current_dag_definition_fingerprint is None
        ):
            raise ValueError("swarm current DAG identity must be complete")
        if self.current_dag_generation is not None and (
            isinstance(self.current_dag_generation, bool)
            or not isinstance(self.current_dag_generation, int)
            or self.current_dag_generation < 0
        ):
            raise ValueError("swarm current DAG generation must be non-negative")
        if self.current_dag_definition_fingerprint is not None:
            _digest(
                self.current_dag_definition_fingerprint,
                field_name="swarm current DAG definition fingerprint",
            )
        if self.root_dag_id is None and any(
            value is not None
            for value in (
                self.current_dag_id,
                self.current_dag_generation,
                self.current_dag_definition_fingerprint,
                self.successor_dag_id,
            )
        ):
            raise ValueError("swarm DAG lineage cannot exist without a root DAG")
        if self.successor_dag_id is not None and self.replan_revision_id is None:
            raise ValueError("swarm successor DAG requires a replan identity")
        if self.successor_dag_id is not None and self.successor_dag_id == self.root_dag_id:
            raise ValueError("swarm successor DAG must be distinct from the root DAG")
        _bounded_text(
            self.final_response,
            field_name="swarm final response",
            limit=MAX_SWARM_RESULT_BYTES,
        )
        if self.final_result_fingerprint is not None:
            _digest(self.final_result_fingerprint, field_name="swarm final result fingerprint")
            if self.final_response is None:
                raise ValueError("swarm final result fingerprint requires a final response")
            if (
                self.current_dag_id is not None
                and self.current_dag_generation is not None
                and self.current_dag_definition_fingerprint is not None
                and self.final_result_fingerprint
                != terminal_result_fingerprint(
                    self.swarm_run_id,
                    self.current_dag_id,
                    self.current_dag_generation,
                    self.current_dag_definition_fingerprint,
                    self.final_response,
                )
            ):
                raise ValueError("swarm final result fingerprint is inconsistent")
        if self.state is AgentSwarmRunState.COMPLETED and (
            self.current_dag_id is None
            or self.final_response is None
            or self.final_result_fingerprint is None
        ):
            raise ValueError("completed swarm run requires its terminal result identity")
        if self.state is AgentSwarmRunState.FINALIZING and (
            self.current_dag_id is None
            or self.final_response is None
            or self.final_result_fingerprint is None
        ):
            raise ValueError("finalizing swarm run requires its pending terminal result")
        for timestamp, field_name in (
            (self.lease_expires_at, "swarm lease expiry"),
            (self.created_at, "swarm creation time"),
            (self.updated_at, "swarm update time"),
        ):
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("swarm update time must not precede creation time")

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    def same_identity(self, other: AgentSwarmRun) -> bool:
        """Compare only the immutable caller/run identity fields."""

        return (
            isinstance(other, AgentSwarmRun)
            and self.swarm_run_id == other.swarm_run_id
            and self.parent_session_id == other.parent_session_id
            and self.objective_fingerprint == other.objective_fingerprint
            and self.planning_id == other.planning_id
        )


@dataclass(frozen=True, slots=True)
class AgentSwarmResult:
    """Canonical terminal result returned by a completed Swarm run."""

    run: AgentSwarmRun
    dag: TaskDag

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run, AgentSwarmRun)
            or self.run.state is not AgentSwarmRunState.COMPLETED
        ):
            raise ValueError("swarm result requires a completed durable run")
        if not isinstance(self.dag, TaskDag) or not self.dag.state.terminal:
            raise ValueError("swarm result requires a terminal DAG")
        if self.run.current_dag_id != self.dag.dag_id:
            raise ValueError("swarm result DAG does not match durable current DAG")

    @property
    def swarm_run_id(self) -> str:
        return self.run.swarm_run_id

    @property
    def final_response(self) -> str:
        assert self.run.final_response is not None
        return self.run.final_response


# Compatibility spellings make the capability discoverable without creating a
# second abstraction or second persistence identity.
SwarmRun = AgentSwarmRun
SwarmRunState = AgentSwarmRunState
SwarmRunResult = AgentSwarmResult


__all__ = [
    "MAX_SWARM_OBJECTIVE_FINGERPRINT_BYTES",
    "MAX_SWARM_RESULT_BYTES",
    "MAX_SWARM_RUN_ID_BYTES",
    "AgentSwarmResult",
    "AgentSwarmRun",
    "AgentSwarmRunState",
    "SwarmRun",
    "SwarmRunResult",
    "SwarmRunState",
    "objective_fingerprint",
    "terminal_result_fingerprint",
]
