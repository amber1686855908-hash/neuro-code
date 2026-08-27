"""Bounded application-level Ultracode delegation identities.

``ULTRACODE`` is intentionally not a provider capability.  This module keeps
the small, durable contract that chooses one existing execution authority and
binds it to one exact parent turn.

定义有界的应用层 Ultracode 委派身份。
``ULTRACODE`` 有意不作为 Provider 能力;本模块只保留选择一个既有执行 owner
并将其绑定到一个精确 parent turn 所需的精简持久化契约。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

MAX_ULTRACODE_EXECUTION_ID_BYTES = 128
MAX_ULTRACODE_PARENT_TURN_ID_BYTES = 512
MAX_ULTRACODE_FINGERPRINT_BYTES = 64
MAX_ULTRACODE_RESULT_BYTES = 32 * 1024
MAX_ULTRACODE_OWNER_ID_BYTES = 128
MAX_ULTRACODE_OWNER_TOKEN_BYTES = 128

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _safe_identifier(value: str, *, field_name: str, limit: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")


def _fingerprint(value: str, *, field_name: str) -> None:
    _safe_identifier(value, field_name=field_name, limit=MAX_ULTRACODE_FINGERPRINT_BYTES)
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 fingerprint")


def _bounded_text(value: str | None, *, field_name: str, limit: int) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise ValueError(f"{field_name} is not bounded safe text")


def ultracode_result_fingerprint(execution_id: str, response: str) -> str:
    """Fingerprint the exact bounded parent-visible result projection."""

    _safe_identifier(
        execution_id,
        field_name="Ultracode execution id",
        limit=MAX_ULTRACODE_EXECUTION_ID_BYTES,
    )
    _bounded_text(response, field_name="Ultracode final response", limit=MAX_ULTRACODE_RESULT_BYTES)
    if not response.strip():
        raise ValueError("Ultracode final response must not be empty")
    payload = json.dumps(
        {"execution_id": execution_id, "response": response},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ultracode_execution_id(parent_session_id: str, parent_turn_id: str) -> str:
    """Derive one deterministic orchestration id from the exact parent identity."""

    _safe_identifier(parent_session_id, field_name="Ultracode parent session id", limit=512)
    _safe_identifier(
        parent_turn_id,
        field_name="Ultracode parent turn id",
        limit=MAX_ULTRACODE_PARENT_TURN_ID_BYTES,
    )
    digest = hashlib.sha256(f"{parent_session_id}\x00{parent_turn_id}".encode()).hexdigest()
    return f"ultracode-{digest[:48]}"


def ultracode_swarm_run_id(execution_id: str) -> str:
    """Derive the sole bounded Swarm identity owned by one Ultracode run."""

    _safe_identifier(
        execution_id,
        field_name="Ultracode execution id",
        limit=MAX_ULTRACODE_EXECUTION_ID_BYTES,
    )
    result = f"swarm-{execution_id}"
    _safe_identifier(result, field_name="Ultracode Swarm run id", limit=128)
    return result


class UltracodeDelegationDecision(StrEnum):
    """The only downstream authorities selectable by Ultracode."""

    MAIN_MAX = "main_max"
    BOUNDED_SWARM = "bounded_swarm"


class UltracodeExecutionState(StrEnum):
    """Durable lifecycle of one application-level delegation."""

    DECIDED = "decided"
    MAIN_MAX_RUNNING = "main_max_running"
    BOUNDED_SWARM_RUNNING = "bounded_swarm_running"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    INDETERMINATE = "indeterminate"

    @property
    def terminal(self) -> bool:
        return self in {
            UltracodeExecutionState.COMPLETED,
            UltracodeExecutionState.INDETERMINATE,
        }

    def can_transition_to(self, proposed: UltracodeExecutionState) -> bool:
        allowed = {
            UltracodeExecutionState.DECIDED: {
                UltracodeExecutionState.MAIN_MAX_RUNNING,
                UltracodeExecutionState.BOUNDED_SWARM_RUNNING,
                UltracodeExecutionState.INDETERMINATE,
            },
            UltracodeExecutionState.MAIN_MAX_RUNNING: {
                UltracodeExecutionState.COMPLETED,
                UltracodeExecutionState.FINALIZING,
                UltracodeExecutionState.INDETERMINATE,
            },
            UltracodeExecutionState.BOUNDED_SWARM_RUNNING: {
                UltracodeExecutionState.FINALIZING,
                UltracodeExecutionState.INDETERMINATE,
            },
            UltracodeExecutionState.FINALIZING: {
                UltracodeExecutionState.COMPLETED,
                UltracodeExecutionState.INDETERMINATE,
            },
            UltracodeExecutionState.COMPLETED: set(),
            UltracodeExecutionState.INDETERMINATE: set(),
        }
        return proposed in allowed[self]


@dataclass(frozen=True, slots=True)
class UltracodeExecution:
    """One exact parent-turn delegation identity and durable lifecycle."""

    execution_id: str
    parent_session_id: str
    parent_turn_id: str
    input_fingerprint: str
    context_fingerprint: str
    decision: UltracodeDelegationDecision
    downstream_id: str
    provider_name: str
    model_name: str
    context_affinity: str | None
    state: UltracodeExecutionState
    generation: int
    owner_id: str
    owner_pid: int
    owner_token: str
    lease_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    final_response: str | None = None
    final_result_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _safe_identifier(
            self.execution_id,
            field_name="Ultracode execution id",
            limit=MAX_ULTRACODE_EXECUTION_ID_BYTES,
        )
        _safe_identifier(
            self.parent_session_id,
            field_name="Ultracode parent session id",
            limit=512,
        )
        _safe_identifier(
            self.parent_turn_id,
            field_name="Ultracode parent turn id",
            limit=MAX_ULTRACODE_PARENT_TURN_ID_BYTES,
        )
        _fingerprint(self.input_fingerprint, field_name="Ultracode input fingerprint")
        _fingerprint(self.context_fingerprint, field_name="Ultracode context fingerprint")
        if not isinstance(self.decision, UltracodeDelegationDecision):
            raise ValueError("Ultracode delegation decision must be canonical")
        _safe_identifier(self.downstream_id, field_name="Ultracode downstream id", limit=128)
        _safe_identifier(self.provider_name, field_name="Ultracode provider name", limit=512)
        _safe_identifier(self.model_name, field_name="Ultracode model name", limit=512)
        if self.context_affinity is not None:
            _safe_identifier(
                self.context_affinity, field_name="Ultracode context affinity", limit=512
            )
        if not isinstance(self.state, UltracodeExecutionState):
            raise ValueError("Ultracode execution state must be canonical")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("Ultracode generation must be non-negative")
        _safe_identifier(
            self.owner_id, field_name="Ultracode owner id", limit=MAX_ULTRACODE_OWNER_ID_BYTES
        )
        if (
            isinstance(self.owner_pid, bool)
            or not isinstance(self.owner_pid, int)
            or self.owner_pid <= 0
        ):
            raise ValueError("Ultracode owner PID must be positive")
        _safe_identifier(
            self.owner_token,
            field_name="Ultracode owner token",
            limit=MAX_ULTRACODE_OWNER_TOKEN_BYTES,
        )
        for timestamp, field_name in (
            (self.lease_expires_at, "Ultracode lease expiry"),
            (self.created_at, "Ultracode creation time"),
            (self.updated_at, "Ultracode update time"),
        ):
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Ultracode update time must not precede creation time")
        _bounded_text(
            self.final_response,
            field_name="Ultracode final response",
            limit=MAX_ULTRACODE_RESULT_BYTES,
        )
        if self.final_result_fingerprint is not None:
            _fingerprint(
                self.final_result_fingerprint,
                field_name="Ultracode final result fingerprint",
            )
            if self.final_response is None:
                raise ValueError("Ultracode result fingerprint requires a response")
            if self.final_result_fingerprint != ultracode_result_fingerprint(
                self.execution_id,
                self.final_response,
            ):
                raise ValueError("Ultracode final result fingerprint is inconsistent")
        if self.state is UltracodeExecutionState.COMPLETED and (
            self.final_response is None or self.final_result_fingerprint is None
        ):
            raise ValueError("completed Ultracode execution requires its terminal result")
        if self.state is UltracodeExecutionState.FINALIZING and (
            self.final_response is None or self.final_result_fingerprint is None
        ):
            raise ValueError("finalizing Ultracode execution requires its pending result")

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    def same_identity(self, other: Any) -> bool:
        """Compare immutable request and downstream identity only."""

        return (
            isinstance(other, UltracodeExecution)
            and self.execution_id == other.execution_id
            and self.parent_session_id == other.parent_session_id
            and self.parent_turn_id == other.parent_turn_id
            and self.input_fingerprint == other.input_fingerprint
            and self.context_fingerprint == other.context_fingerprint
            and self.decision is other.decision
            and self.downstream_id == other.downstream_id
            and self.provider_name == other.provider_name
            and self.model_name == other.model_name
            and self.context_affinity == other.context_affinity
        )


__all__ = [
    "MAX_ULTRACODE_EXECUTION_ID_BYTES",
    "MAX_ULTRACODE_FINGERPRINT_BYTES",
    "MAX_ULTRACODE_OWNER_ID_BYTES",
    "MAX_ULTRACODE_OWNER_TOKEN_BYTES",
    "MAX_ULTRACODE_PARENT_TURN_ID_BYTES",
    "MAX_ULTRACODE_RESULT_BYTES",
    "UltracodeDelegationDecision",
    "UltracodeExecution",
    "UltracodeExecutionState",
    "ultracode_execution_id",
    "ultracode_result_fingerprint",
    "ultracode_swarm_run_id",
]
