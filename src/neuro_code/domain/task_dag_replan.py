"""Bounded, immutable Task DAG revision / replan contracts.

Replanning is a new publication lineage.  It never mutates the source DAG or
copies execution state into the successor graph.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from neuro_code.domain.model_planning import ModelDagProposal
from neuro_code.domain.task_dag import (
    MAX_TASK_DAG_ID_BYTES,
    MAX_TASK_DAG_NODE_DEPENDENCIES,
    TaskDagNodeState,
    TaskDagState,
)

MAX_DAG_REPLAN_DEPTH = 1
MAX_DAG_REPLAN_ID_BYTES = 512
MAX_DAG_REPLAN_COMPLETED_RESULT_BYTES = 4 * 1024
MAX_DAG_REPLAN_COMPLETED_RESULTS_BYTES = 16 * 1024
MAX_DAG_REPLAN_FAILURE_STATE_BYTES = 8 * 1024
MAX_DAG_REPLAN_EVIDENCE_BYTES = 32 * 1024
MAX_DAG_REPLAN_PROMPT_BYTES = 40 * 1024
MAX_DAG_REPLAN_RESPONSE_BYTES = 16 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _safe_identifier(value: str, *, field_name: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _bounded_text(
    value: str | None,
    *,
    field_name: str,
    limit: int,
    allow_empty: bool = True,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _fingerprint(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


class DagReplanAttemptState(StrEnum):
    """Durable provider/publication lifecycle for one explicit replan."""

    CLAIMED = "claimed"
    PROVIDER_FENCED = "provider_fenced"
    MODEL_COMMITTED = "model_committed"
    PROPOSAL_PUBLISHED = "proposal_published"
    SUCCESSOR_DAG_PUBLISHED = "successor_dag_published"
    COMPLETED = "completed"
    STALE = "stale"
    INDETERMINATE = "indeterminate"

    def can_transition_to(self, proposed: DagReplanAttemptState) -> bool:
        allowed = {
            DagReplanAttemptState.CLAIMED: {
                DagReplanAttemptState.PROVIDER_FENCED,
                DagReplanAttemptState.STALE,
                DagReplanAttemptState.INDETERMINATE,
            },
            DagReplanAttemptState.PROVIDER_FENCED: {
                DagReplanAttemptState.MODEL_COMMITTED,
                DagReplanAttemptState.STALE,
                DagReplanAttemptState.INDETERMINATE,
            },
            DagReplanAttemptState.MODEL_COMMITTED: {
                DagReplanAttemptState.PROPOSAL_PUBLISHED,
                DagReplanAttemptState.STALE,
                DagReplanAttemptState.INDETERMINATE,
            },
            DagReplanAttemptState.PROPOSAL_PUBLISHED: {
                DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED,
                DagReplanAttemptState.STALE,
                DagReplanAttemptState.INDETERMINATE,
            },
            DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED: {
                DagReplanAttemptState.COMPLETED,
            },
            DagReplanAttemptState.COMPLETED: set(),
            DagReplanAttemptState.STALE: set(),
            DagReplanAttemptState.INDETERMINATE: set(),
        }
        return proposed in allowed[self]


@dataclass(frozen=True, slots=True)
class DagReplanEvidenceNode:
    """One deterministic, redacted source-node evidence projection."""

    node_id: str
    ordinal: int
    state: TaskDagNodeState
    generation: int
    dependencies: tuple[str, ...] = ()
    result_projection: str | None = None
    result_truncated: bool = False
    failure_kind: str | None = None
    failure_summary: str | None = None
    changed_file_count: int | None = None

    def __post_init__(self) -> None:
        _safe_identifier(
            self.node_id, field_name="replan evidence node id", limit=MAX_TASK_DAG_ID_BYTES
        )
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("replan evidence node ordinal is invalid")
        if not isinstance(self.state, TaskDagNodeState):
            raise TypeError("replan evidence node state must be canonical")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("replan evidence node generation is invalid")
        if not isinstance(self.dependencies, tuple):
            raise TypeError("replan evidence dependencies must be a tuple")
        if len(self.dependencies) > MAX_TASK_DAG_NODE_DEPENDENCIES:
            raise ValueError("replan evidence node has too many dependencies")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("replan evidence dependencies must be unique")
        for dependency in self.dependencies:
            _safe_identifier(
                dependency,
                field_name="replan evidence dependency id",
                limit=MAX_TASK_DAG_ID_BYTES,
            )
        _bounded_text(
            self.result_projection,
            field_name="replan evidence result projection",
            limit=MAX_DAG_REPLAN_COMPLETED_RESULT_BYTES,
        )
        _bounded_text(
            self.failure_kind,
            field_name="replan evidence failure kind",
            limit=256,
        )
        _bounded_text(
            self.failure_summary,
            field_name="replan evidence failure summary",
            limit=MAX_DAG_REPLAN_FAILURE_STATE_BYTES,
        )
        if not isinstance(self.result_truncated, bool):
            raise TypeError("replan evidence result truncated must be boolean")
        if self.changed_file_count is not None and (
            isinstance(self.changed_file_count, bool)
            or not isinstance(self.changed_file_count, int)
            or self.changed_file_count < 0
        ):
            raise ValueError("replan evidence changed file count is invalid")
        if self.state is TaskDagNodeState.COMPLETED:
            if self.failure_kind is not None or self.failure_summary is not None:
                raise ValueError("completed replan evidence cannot carry failure state")
        elif self.result_projection is not None or self.result_truncated:
            raise ValueError("non-completed replan evidence cannot carry a result projection")

    @property
    def payload(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "ordinal": self.ordinal,
            "state": self.state.value,
            "generation": self.generation,
            "dependencies": list(self.dependencies),
            "result_projection": self.result_projection,
            "result_truncated": self.result_truncated,
            "failure_kind": self.failure_kind,
            "failure_summary": self.failure_summary,
            "changed_file_count": self.changed_file_count,
        }


@dataclass(frozen=True, slots=True)
class TaskDagReplanEvidenceEnvelope:
    """Immutable bounded evidence passed to the replan model."""

    source_dag_id: str
    source_definition_fingerprint: str
    source_terminal_state: TaskDagState
    source_generation: int
    nodes: tuple[DagReplanEvidenceNode, ...]

    def __post_init__(self) -> None:
        _safe_identifier(
            self.source_dag_id, field_name="replan source DAG id", limit=MAX_TASK_DAG_ID_BYTES
        )
        _fingerprint(
            self.source_definition_fingerprint,
            field_name="replan source definition fingerprint",
        )
        if self.source_terminal_state is not TaskDagState.FAILED:
            raise ValueError("replan evidence source state must be failed")
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 0
        ):
            raise ValueError("replan source generation is invalid")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise ValueError("replan evidence nodes must be a non-empty tuple")
        if not all(isinstance(node, DagReplanEvidenceNode) for node in self.nodes):
            raise TypeError("replan evidence nodes must be canonical")
        if any(
            not node.state.terminal or node.state is TaskDagNodeState.INDETERMINATE
            for node in self.nodes
        ):
            raise ValueError("replan evidence nodes must be terminal and determinate")
        ordered = tuple(sorted(self.nodes, key=lambda node: (node.ordinal, node.node_id)))
        if ordered != self.nodes:
            raise ValueError("replan evidence nodes must use canonical order")
        if tuple(node.ordinal for node in self.nodes) != tuple(range(len(self.nodes))):
            raise ValueError("replan evidence ordinals must match canonical order")
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("replan evidence node ids must be unique")
        known_node_ids = {node.node_id for node in self.nodes}
        if any(
            dependency not in known_node_ids
            for node in self.nodes
            for dependency in node.dependencies
        ):
            raise ValueError("replan evidence dependencies must reference known nodes")
        completed_bytes = sum(
            len((node.result_projection or "").encode("utf-8"))
            for node in self.nodes
            if node.state is TaskDagNodeState.COMPLETED
        )
        if completed_bytes > MAX_DAG_REPLAN_COMPLETED_RESULTS_BYTES:
            raise ValueError("replan completed-result evidence exceeds its byte budget")
        failure_bytes = sum(
            len((node.failure_kind or "").encode("utf-8"))
            + len((node.failure_summary or "").encode("utf-8"))
            for node in self.nodes
            if node.state is not TaskDagNodeState.COMPLETED
        )
        if failure_bytes > MAX_DAG_REPLAN_FAILURE_STATE_BYTES:
            raise ValueError("replan failure-state evidence exceeds its byte budget")
        if len(self.canonical_json.encode("utf-8")) > MAX_DAG_REPLAN_EVIDENCE_BYTES:
            raise ValueError("replan evidence envelope exceeds its byte budget")

    @property
    def payload(self) -> dict[str, object]:
        return {
            "source_dag_id": self.source_dag_id,
            "source_definition_fingerprint": self.source_definition_fingerprint,
            "source_terminal_state": self.source_terminal_state.value,
            "source_generation": self.source_generation,
            "nodes": [node.payload for node in self.nodes],
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.payload)

    @property
    def fingerprint(self) -> str:
        return _sha256(self.payload)

    def render(self) -> str:
        """Return the exact bounded bytes sent as evidence to the model."""

        return self.canonical_json

    @classmethod
    def parse(cls, value: str) -> TaskDagReplanEvidenceEnvelope:
        _bounded_text(
            value,
            field_name="replan evidence JSON",
            limit=MAX_DAG_REPLAN_EVIDENCE_BYTES,
            allow_empty=False,
        )
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("replan evidence must be strict JSON") from error
        if not isinstance(raw, dict) or set(raw) != {
            "source_dag_id",
            "source_definition_fingerprint",
            "source_terminal_state",
            "source_generation",
            "nodes",
        }:
            raise ValueError("replan evidence has unknown or missing fields")
        raw_nodes = raw["nodes"]
        if not isinstance(raw_nodes, list):
            raise ValueError("replan evidence nodes must be a list")
        nodes: list[DagReplanEvidenceNode] = []
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict) or set(raw_node) != {
                "node_id",
                "ordinal",
                "state",
                "generation",
                "dependencies",
                "result_projection",
                "result_truncated",
                "failure_kind",
                "failure_summary",
                "changed_file_count",
            }:
                raise ValueError("replan evidence node has unknown or missing fields")
            if not isinstance(raw_node["node_id"], str):
                raise ValueError("replan evidence node id must be a string")
            if (
                isinstance(raw_node["ordinal"], bool)
                or not isinstance(raw_node["ordinal"], int)
                or isinstance(raw_node["generation"], bool)
                or not isinstance(raw_node["generation"], int)
            ):
                raise ValueError("replan evidence node ordinal and generation are invalid")
            dependencies = raw_node["dependencies"]
            if not isinstance(dependencies, list) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError("replan evidence dependencies must be a list of strings")
            if not isinstance(raw_node["state"], str):
                raise ValueError("replan evidence node state must be a string")
            if not isinstance(raw_node["result_projection"], (str, type(None))):
                raise ValueError("replan evidence result projection is invalid")
            if not isinstance(raw_node["result_truncated"], bool):
                raise ValueError("replan evidence result truncated is invalid")
            if not isinstance(raw_node["failure_kind"], (str, type(None))) or not isinstance(
                raw_node["failure_summary"], (str, type(None))
            ):
                raise ValueError("replan evidence failure state is invalid")
            if raw_node["changed_file_count"] is not None and (
                isinstance(raw_node["changed_file_count"], bool)
                or not isinstance(raw_node["changed_file_count"], int)
            ):
                raise ValueError("replan evidence changed file count is invalid")
            try:
                state = TaskDagNodeState(raw_node["state"])
            except ValueError as error:
                raise ValueError("replan evidence node state is invalid") from error
            nodes.append(
                DagReplanEvidenceNode(
                    node_id=raw_node["node_id"],
                    ordinal=raw_node["ordinal"],
                    state=state,
                    generation=raw_node["generation"],
                    dependencies=tuple(dependencies),
                    result_projection=raw_node["result_projection"],
                    result_truncated=raw_node["result_truncated"],
                    failure_kind=raw_node["failure_kind"],
                    failure_summary=raw_node["failure_summary"],
                    changed_file_count=raw_node["changed_file_count"],
                )
            )
        try:
            if (
                not isinstance(raw["source_dag_id"], str)
                or not isinstance(raw["source_definition_fingerprint"], str)
                or not isinstance(raw["source_terminal_state"], str)
            ):
                raise ValueError("replan evidence source identity is invalid")
            terminal_state = TaskDagState(raw["source_terminal_state"])
            envelope = cls(
                source_dag_id=raw["source_dag_id"],
                source_definition_fingerprint=raw["source_definition_fingerprint"],
                source_terminal_state=terminal_state,
                source_generation=raw["source_generation"],
                nodes=tuple(nodes),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"replan evidence is invalid: {error}") from error
        if envelope.canonical_json != value:
            raise ValueError("replan evidence is not canonical JSON")
        return envelope


@dataclass(frozen=True, slots=True)
class DagReplanAttempt:
    """Durable identity and lifecycle for one explicit replan request."""

    revision_id: str
    parent_session_id: str
    source_dag_id: str
    source_definition_fingerprint: str
    source_generation: int
    source_state: TaskDagState
    revision_depth: int
    evidence_fingerprint: str
    evidence_json: str
    planner_session_id: str
    planner_turn_id: str
    intended_successor_dag_id: str
    state: DagReplanAttemptState
    owner_id: str
    lease_expires_at: datetime
    model_response: str | None = None
    proposal_fingerprint: str | None = None
    successor_dag_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.revision_id, "replan revision id"),
            (self.parent_session_id, "replan parent session id"),
            (self.planner_session_id, "replan planner session id"),
            (self.planner_turn_id, "replan planner turn id"),
            (self.owner_id, "replan owner id"),
        ):
            _safe_identifier(value, field_name=field_name, limit=MAX_DAG_REPLAN_ID_BYTES)
        for value, field_name in (
            (self.source_dag_id, "replan source DAG id"),
            (self.intended_successor_dag_id, "replan intended successor DAG id"),
        ):
            _safe_identifier(value, field_name=field_name, limit=MAX_TASK_DAG_ID_BYTES)
        if self.successor_dag_id is not None:
            _safe_identifier(
                self.successor_dag_id,
                field_name="replan successor DAG id",
                limit=MAX_TASK_DAG_ID_BYTES,
            )
        _fingerprint(
            self.source_definition_fingerprint,
            field_name="replan source definition fingerprint",
        )
        _fingerprint(self.evidence_fingerprint, field_name="replan evidence fingerprint")
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 0
        ):
            raise ValueError("replan source generation is invalid")
        if self.source_state is not TaskDagState.FAILED:
            raise ValueError("replan source state must be failed")
        if isinstance(self.revision_depth, bool) or not isinstance(self.revision_depth, int):
            raise ValueError("replan revision depth is invalid")
        if not 1 <= self.revision_depth <= MAX_DAG_REPLAN_DEPTH:
            raise ValueError("replan revision depth exceeds its bound")
        envelope = TaskDagReplanEvidenceEnvelope.parse(self.evidence_json)
        if (
            envelope.source_dag_id != self.source_dag_id
            or envelope.source_definition_fingerprint != self.source_definition_fingerprint
            or envelope.source_terminal_state is not self.source_state
            or envelope.source_generation != self.source_generation
            or envelope.fingerprint != self.evidence_fingerprint
        ):
            raise ValueError("replan evidence identity is inconsistent")
        if not isinstance(self.state, DagReplanAttemptState):
            raise TypeError("replan attempt state must be canonical")
        if not isinstance(self.lease_expires_at, datetime) or self.lease_expires_at.tzinfo is None:
            raise ValueError("replan lease expiry must be timezone-aware")
        _bounded_text(
            self.model_response,
            field_name="replan model response",
            limit=MAX_DAG_REPLAN_RESPONSE_BYTES,
            allow_empty=False,
        )
        if self.proposal_fingerprint is not None:
            _fingerprint(self.proposal_fingerprint, field_name="replan proposal fingerprint")
        for timestamp, field_name in (
            (self.created_at, "replan created at"),
            (self.updated_at, "replan updated at"),
        ):
            if timestamp is not None and (
                not isinstance(timestamp, datetime) or timestamp.tzinfo is None
            ):
                raise ValueError(f"{field_name} must be timezone-aware")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("replan updated_at must not precede created_at")
        if (
            self.state
            in {
                DagReplanAttemptState.MODEL_COMMITTED,
                DagReplanAttemptState.PROPOSAL_PUBLISHED,
                DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED,
                DagReplanAttemptState.COMPLETED,
            }
            and self.model_response is None
        ):
            raise ValueError("durable replan output is required for this lifecycle state")
        if (
            self.state
            in {
                DagReplanAttemptState.PROPOSAL_PUBLISHED,
                DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED,
                DagReplanAttemptState.COMPLETED,
            }
            and self.proposal_fingerprint is None
        ):
            raise ValueError("durable replan proposal identity is required for this state")
        if (
            self.state
            in {
                DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED,
                DagReplanAttemptState.COMPLETED,
            }
            and self.successor_dag_id is None
        ):
            raise ValueError("durable replan successor identity is required for this state")


@dataclass(frozen=True, slots=True)
class DagReplanProposalRecord:
    """Insert-only typed proposal bound to one immutable revision."""

    proposal_id: str
    revision_id: str
    parent_session_id: str
    source_dag_id: str
    source_definition_fingerprint: str
    source_generation: int
    evidence_fingerprint: str
    intended_successor_dag_id: str
    proposal: ModelDagProposal
    created_at: datetime

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.proposal_id, "replan proposal id"),
            (self.revision_id, "replan proposal revision id"),
            (self.parent_session_id, "replan proposal parent session id"),
        ):
            _safe_identifier(value, field_name=field_name, limit=MAX_DAG_REPLAN_ID_BYTES)
        _safe_identifier(
            self.source_dag_id,
            field_name="replan proposal source DAG id",
            limit=MAX_TASK_DAG_ID_BYTES,
        )
        _safe_identifier(
            self.intended_successor_dag_id,
            field_name="replan proposal intended successor DAG id",
            limit=MAX_TASK_DAG_ID_BYTES,
        )
        _fingerprint(
            self.source_definition_fingerprint,
            field_name="replan proposal source definition fingerprint",
        )
        _fingerprint(self.evidence_fingerprint, field_name="replan proposal evidence fingerprint")
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 0
        ):
            raise ValueError("replan proposal source generation is invalid")
        if not isinstance(self.proposal, ModelDagProposal):
            raise TypeError("replan proposal must be canonical")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("replan proposal created_at must be timezone-aware")

    @property
    def proposal_fingerprint(self) -> str:
        return self.proposal.fingerprint


# Compatibility aliases keep the capability discoverable without creating a
# second implementation or a second persistence vocabulary.
TaskDagReplanAttempt = DagReplanAttempt
TaskDagReplanAttemptState = DagReplanAttemptState
TaskDagReplanProposalRecord = DagReplanProposalRecord
ReplanEvidenceEnvelope = TaskDagReplanEvidenceEnvelope


__all__ = [
    "MAX_DAG_REPLAN_COMPLETED_RESULTS_BYTES",
    "MAX_DAG_REPLAN_COMPLETED_RESULT_BYTES",
    "MAX_DAG_REPLAN_DEPTH",
    "MAX_DAG_REPLAN_EVIDENCE_BYTES",
    "MAX_DAG_REPLAN_FAILURE_STATE_BYTES",
    "MAX_DAG_REPLAN_ID_BYTES",
    "MAX_DAG_REPLAN_PROMPT_BYTES",
    "MAX_DAG_REPLAN_RESPONSE_BYTES",
    "DagReplanAttempt",
    "DagReplanAttemptState",
    "DagReplanEvidenceNode",
    "DagReplanProposalRecord",
    "ReplanEvidenceEnvelope",
    "TaskDagReplanAttempt",
    "TaskDagReplanAttemptState",
    "TaskDagReplanEvidenceEnvelope",
    "TaskDagReplanProposalRecord",
    "_truncate_utf8",
]
