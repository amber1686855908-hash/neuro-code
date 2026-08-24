"""Bounded durable Leader domain contracts.

The Leader is an orchestration decision maker.  It never owns a worker,
workspace, or tool capability; those authorities remain in the Task DAG and
Writable Subagent application services.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from neuro_code.domain.task_dag import TaskDagNodeState, TaskDagState

MAX_LEADER_OBJECTIVE_BYTES = 4_096
MAX_LEADER_REASON_BYTES = 1_024
MAX_LEADER_RESPONSE_BYTES = 16_384
MAX_LEADER_NODE_PROMPT_BYTES = 2_048
MAX_LEADER_NODE_PREVIEW_BYTES = 2_048
MAX_LEADER_NODE_COUNT = 8
MAX_LEADER_EVIDENCE_BYTES = 64 * 1024


def _bounded_text(value: str, *, field_name: str, limit: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{field_name} is invalid")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in value):
        raise ValueError(f"{field_name} contains an unsafe control character")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} is too large")
    return value


def _identifier(value: str, *, field_name: str, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _fingerprint(value: str, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class LeaderDecisionKind(StrEnum):
    SELECT_NODE = "SELECT_NODE"
    FINALIZE = "FINALIZE"


class LeaderAttemptState(StrEnum):
    CLAIMED = "claimed"
    MODEL_COMMITTED = "model_committed"
    DECISION_PUBLISHED = "decision_published"
    EXECUTED = "executed"
    STALE = "stale"
    INDETERMINATE = "indeterminate"

    def can_transition_to(self, proposed: LeaderAttemptState) -> bool:
        allowed = {
            LeaderAttemptState.CLAIMED: {
                LeaderAttemptState.MODEL_COMMITTED,
                LeaderAttemptState.STALE,
                LeaderAttemptState.INDETERMINATE,
            },
            LeaderAttemptState.MODEL_COMMITTED: {
                LeaderAttemptState.DECISION_PUBLISHED,
                LeaderAttemptState.STALE,
                LeaderAttemptState.INDETERMINATE,
            },
            LeaderAttemptState.DECISION_PUBLISHED: {
                LeaderAttemptState.EXECUTED,
                LeaderAttemptState.STALE,
            },
            LeaderAttemptState.EXECUTED: set(),
            LeaderAttemptState.STALE: set(),
            LeaderAttemptState.INDETERMINATE: set(),
        }
        return proposed in allowed[self]


@dataclass(frozen=True, slots=True)
class LeaderEvidenceNode:
    """One bounded, redacted node projection exposed to the Leader model."""

    node_id: str
    ordinal: int
    dependencies: tuple[str, ...]
    state: TaskDagNodeState
    prompt: str
    prompt_fingerprint: str
    response_preview: str | None = None
    error_kind: str | None = None
    error_reason: str | None = None
    changed_file_count: int | None = None
    final_workspace_fingerprint: str | None = None
    parent_task_id: str | None = None
    child_session_id: str | None = None
    lease_id: str | None = None
    worktree_id: str | None = None
    baseline_checkpoint_id: str | None = None
    relay_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.node_id, field_name="leader evidence node id")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("leader evidence node ordinal is invalid")
        if not isinstance(self.dependencies, tuple) or not all(
            isinstance(value, str) for value in self.dependencies
        ):
            raise ValueError("leader evidence dependencies are invalid")
        if not isinstance(self.state, TaskDagNodeState):
            raise ValueError("leader evidence node state is invalid")
        _bounded_text(
            self.prompt,
            field_name="leader evidence node prompt",
            limit=MAX_LEADER_NODE_PROMPT_BYTES,
            allow_empty=False,
        )
        _fingerprint(self.prompt_fingerprint, field_name="leader evidence prompt fingerprint")
        _bounded_text(
            self.response_preview or "",
            field_name="leader evidence response preview",
            limit=MAX_LEADER_NODE_PREVIEW_BYTES,
        )
        _bounded_text(self.error_kind or "", field_name="leader evidence error kind", limit=256)
        _bounded_text(
            self.error_reason or "",
            field_name="leader evidence error reason",
            limit=MAX_LEADER_REASON_BYTES,
        )
        if self.changed_file_count is not None and (
            isinstance(self.changed_file_count, bool)
            or not isinstance(self.changed_file_count, int)
            or self.changed_file_count < 0
        ):
            raise ValueError("leader evidence changed file count is invalid")
        if self.final_workspace_fingerprint is not None:
            _fingerprint(
                self.final_workspace_fingerprint,
                field_name="leader evidence workspace fingerprint",
            )
        for value, field_name in (
            (self.parent_task_id, "leader evidence parent task id"),
            (self.child_session_id, "leader evidence child session id"),
            (self.lease_id, "leader evidence lease id"),
            (self.worktree_id, "leader evidence worktree id"),
            (self.baseline_checkpoint_id, "leader evidence checkpoint id"),
            (self.relay_id, "leader evidence relay id"),
        ):
            if value is not None:
                _identifier(value, field_name=field_name)

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "ordinal": self.ordinal,
            "dependencies": list(self.dependencies),
            "state": self.state.value,
            "prompt": self.prompt,
            "prompt_fingerprint": self.prompt_fingerprint,
            "response_preview": self.response_preview,
            "error_kind": self.error_kind,
            "error_reason": self.error_reason,
            "changed_file_count": self.changed_file_count,
            "final_workspace_fingerprint": self.final_workspace_fingerprint,
            "parent_task_id": self.parent_task_id,
            "child_session_id": self.child_session_id,
            "lease_id": self.lease_id,
            "worktree_id": self.worktree_id,
            "baseline_checkpoint_id": self.baseline_checkpoint_id,
            "relay_id": self.relay_id,
        }


@dataclass(frozen=True, slots=True)
class LeaderEvidenceEnvelope:
    """Deterministic bounded evidence for one exact DAG snapshot."""

    objective: str
    dag_id: str
    definition_fingerprint: str
    generation: int
    state: TaskDagState
    active_node_id: str | None
    ready_node_ids: tuple[str, ...]
    nodes: tuple[LeaderEvidenceNode, ...]

    def __post_init__(self) -> None:
        _bounded_text(
            self.objective,
            field_name="leader objective",
            limit=MAX_LEADER_OBJECTIVE_BYTES,
            allow_empty=False,
        )
        _identifier(self.dag_id, field_name="leader evidence DAG id")
        _fingerprint(self.definition_fingerprint, field_name="leader definition fingerprint")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("leader evidence DAG generation is invalid")
        if not isinstance(self.state, TaskDagState):
            raise ValueError("leader evidence DAG state is invalid")
        if self.active_node_id is not None:
            _identifier(self.active_node_id, field_name="leader evidence active node id")
        if not isinstance(self.ready_node_ids, tuple) or not all(
            isinstance(value, str) for value in self.ready_node_ids
        ):
            raise ValueError("leader evidence ready node ids are invalid")
        if len(self.nodes) > MAX_LEADER_NODE_COUNT:
            raise ValueError("leader evidence contains too many nodes")
        if tuple(node.ordinal for node in self.nodes) != tuple(range(len(self.nodes))):
            raise ValueError("leader evidence node order is not deterministic")
        if len(_canonical_json(self.payload).encode("utf-8")) > MAX_LEADER_EVIDENCE_BYTES:
            raise ValueError("leader evidence is too large")

    @property
    def payload(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "dag_id": self.dag_id,
            "definition_fingerprint": self.definition_fingerprint,
            "generation": self.generation,
            "state": self.state.value,
            "active_node_id": self.active_node_id,
            "ready_node_ids": list(self.ready_node_ids),
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @property
    def fingerprint(self) -> str:
        return _sha256(self.payload)

    def to_dict(self) -> dict[str, object]:
        result = dict(self.payload)
        result["evidence_fingerprint"] = self.fingerprint
        return result


@dataclass(frozen=True, slots=True)
class LeaderDecision:
    """Strict model output contract; arbitrary prose is never authority."""

    kind: LeaderDecisionKind
    selected_node_id: str | None = None
    summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LeaderDecisionKind):
            raise ValueError("leader decision kind is invalid")
        if self.kind is LeaderDecisionKind.SELECT_NODE and self.selected_node_id is None:
            raise ValueError("SELECT_NODE requires a node id")
        if self.kind is LeaderDecisionKind.FINALIZE and self.selected_node_id is not None:
            raise ValueError("FINALIZE must not contain a node id")
        if self.selected_node_id is not None:
            _identifier(self.selected_node_id, field_name="leader selected node id")
        _bounded_text(
            self.summary, field_name="leader decision summary", limit=MAX_LEADER_REASON_BYTES
        )

    @classmethod
    def parse(cls, response: str) -> LeaderDecision:
        _bounded_text(
            response,
            field_name="leader model response",
            limit=MAX_LEADER_RESPONSE_BYTES,
            allow_empty=False,
        )
        try:
            value = json.loads(response)
        except json.JSONDecodeError as error:
            raise ValueError("leader model response must be strict JSON") from error
        if not isinstance(value, dict):
            raise ValueError("leader model response must be a JSON object")
        action = value.get("action")
        if not isinstance(action, str):
            raise ValueError("leader decision action is missing")
        try:
            kind = LeaderDecisionKind(action)
        except ValueError as error:
            raise ValueError("leader decision action is unknown") from error
        allowed = (
            {"action", "node_id", "reason"}
            if kind is LeaderDecisionKind.SELECT_NODE
            else {"action", "summary", "reason"}
        )
        if set(value) - allowed:
            raise ValueError("leader decision contains unknown fields")
        if kind is LeaderDecisionKind.SELECT_NODE:
            node_id = value.get("node_id")
            if not isinstance(node_id, str):
                raise ValueError("SELECT_NODE node_id is missing")
            reason = value.get("reason", "")
            if not isinstance(reason, str):
                raise ValueError("leader decision reason is invalid")
            return cls(kind, selected_node_id=node_id, summary=reason)
        summary = value.get("summary", value.get("reason", ""))
        if not isinstance(summary, str):
            raise ValueError("FINALIZE summary is invalid")
        return cls(kind, summary=summary)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"action": self.kind.value}
        if self.selected_node_id is not None:
            result["node_id"] = self.selected_node_id
        if self.summary:
            result["summary"] = self.summary
        return result


@dataclass(frozen=True, slots=True)
class LeaderAttempt:
    """Durable owner and replay state for one exact Leader snapshot."""

    attempt_id: str
    dag_id: str
    leader_session_id: str
    objective_fingerprint: str
    dag_generation: int
    definition_fingerprint: str
    evidence_fingerprint: str
    state: LeaderAttemptState
    owner_id: str
    lease_expires_at: datetime
    turn_id: str
    model_response: str | None = None
    decision_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for identifier_value, field_name in (
            (self.attempt_id, "leader attempt id"),
            (self.dag_id, "leader attempt DAG id"),
            (self.leader_session_id, "leader session id"),
            (self.owner_id, "leader owner id"),
            (self.turn_id, "leader turn id"),
        ):
            _identifier(identifier_value, field_name=field_name)
        _fingerprint(self.objective_fingerprint, field_name="leader objective fingerprint")
        _fingerprint(self.definition_fingerprint, field_name="leader definition fingerprint")
        _fingerprint(self.evidence_fingerprint, field_name="leader evidence fingerprint")
        if (
            isinstance(self.dag_generation, bool)
            or not isinstance(self.dag_generation, int)
            or self.dag_generation < 0
        ):
            raise ValueError("leader attempt DAG generation is invalid")
        if not isinstance(self.state, LeaderAttemptState):
            raise ValueError("leader attempt state is invalid")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("leader attempt lease expiry must be timezone-aware")
        if self.model_response is not None:
            _bounded_text(
                self.model_response,
                field_name="leader model response",
                limit=MAX_LEADER_RESPONSE_BYTES,
                allow_empty=False,
            )
        if self.decision_id is not None:
            _identifier(self.decision_id, field_name="leader decision id")
        for timestamp_value, field_name in (
            (self.created_at, "leader attempt creation time"),
            (self.updated_at, "leader attempt update time"),
        ):
            if timestamp_value is not None and timestamp_value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class LeaderDecisionRecord:
    """Durable decision bound to one exact evidence snapshot."""

    decision_id: str
    attempt_id: str
    dag_id: str
    leader_session_id: str
    dag_generation: int
    definition_fingerprint: str
    evidence_fingerprint: str
    decision: LeaderDecision
    created_at: datetime

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.decision_id, "leader decision id"),
            (self.attempt_id, "leader decision attempt id"),
            (self.dag_id, "leader decision DAG id"),
            (self.leader_session_id, "leader decision session id"),
        ):
            _identifier(value, field_name=field_name)
        _fingerprint(
            self.definition_fingerprint, field_name="leader decision definition fingerprint"
        )
        _fingerprint(self.evidence_fingerprint, field_name="leader decision evidence fingerprint")
        if (
            isinstance(self.dag_generation, bool)
            or not isinstance(self.dag_generation, int)
            or self.dag_generation < 0
        ):
            raise ValueError("leader decision DAG generation is invalid")
        if self.created_at.tzinfo is None:
            raise ValueError("leader decision creation time must be timezone-aware")


__all__ = [
    "MAX_LEADER_EVIDENCE_BYTES",
    "MAX_LEADER_NODE_COUNT",
    "MAX_LEADER_NODE_PREVIEW_BYTES",
    "MAX_LEADER_NODE_PROMPT_BYTES",
    "MAX_LEADER_OBJECTIVE_BYTES",
    "MAX_LEADER_REASON_BYTES",
    "MAX_LEADER_RESPONSE_BYTES",
    "LeaderAttempt",
    "LeaderAttemptState",
    "LeaderDecision",
    "LeaderDecisionKind",
    "LeaderDecisionRecord",
    "LeaderEvidenceEnvelope",
    "LeaderEvidenceNode",
]
