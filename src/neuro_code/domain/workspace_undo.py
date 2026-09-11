"""Bounded domain values for the user-facing workspace undo capability.

The association is a latest-only projection.  It is deliberately separate
from the immutable checkpoint artifact and from turn recovery so a rollback
cannot rewrite historical assistant or recovery facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from neuro_code.domain.checkpoints import CheckpointId

MAX_WORKSPACE_UNDO_SESSION_ID_BYTES = 256
MAX_WORKSPACE_UNDO_TURN_ID_BYTES = 256
MAX_WORKSPACE_UNDO_MUTATION_ID_BYTES = 256


def _bounded(value: str, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text without NUL")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} is too long")
    return value


class WorkspaceUndoState(StrEnum):
    """Durable latest-only availability of the current session undo."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ROLLED_BACK = "rolled_back"


class WorkspaceUndoReason(StrEnum):
    """Bounded facts explaining why undo is unavailable or already consumed."""

    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    CHECKPOINT_FAILED = "checkpoint_failed"
    CHECKPOINT_TOO_LARGE = "checkpoint_too_large"
    UNSUPPORTED_WORKSPACE = "unsupported_workspace"
    UNBOUNDED_MUTATION = "unbounded_mutation"
    IGNORED_TARGET = "ignored_target"
    INVALIDATION_FAILED = "invalidation_failed"
    PERSISTENCE_FAILED = "persistence_failed"
    HEAD_CHANGED = "head_changed"
    LIVE_MUTATOR = "live_mutator"
    NO_CHECKPOINT = "no_checkpoint"
    ALREADY_ROLLED_BACK = "already_rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    ROLLBACK_INDETERMINATE = "rollback_indeterminate"


@dataclass(frozen=True, slots=True)
class WorkspaceUndoAssociation:
    """Latest durable checkpoint association for one session."""

    session_id: str
    turn_id: str
    state: WorkspaceUndoState
    updated_at: datetime
    checkpoint_id: CheckpointId | None = None
    reason: WorkspaceUndoReason | None = None
    verification_mutation_id: str | None = None
    verification_handoff_consumed: bool = False

    def __post_init__(self) -> None:
        _bounded(
            self.session_id,
            name="workspace undo session id",
            limit=MAX_WORKSPACE_UNDO_SESSION_ID_BYTES,
        )
        _bounded(
            self.turn_id,
            name="workspace undo turn id",
            limit=MAX_WORKSPACE_UNDO_TURN_ID_BYTES,
        )
        if not isinstance(self.state, WorkspaceUndoState):
            raise TypeError("workspace undo state must be canonical")
        if self.updated_at.tzinfo is None:
            raise ValueError("workspace undo timestamp must be timezone-aware")
        object.__setattr__(self, "updated_at", self.updated_at.astimezone(UTC))
        if self.checkpoint_id is not None and not isinstance(self.checkpoint_id, CheckpointId):
            raise TypeError("workspace undo checkpoint id must be canonical")
        if self.reason is not None and not isinstance(self.reason, WorkspaceUndoReason):
            raise TypeError("workspace undo reason must be canonical")
        if self.verification_mutation_id is not None:
            _bounded(
                self.verification_mutation_id,
                name="workspace undo verification mutation id",
                limit=MAX_WORKSPACE_UNDO_MUTATION_ID_BYTES,
            )
        if not isinstance(self.verification_handoff_consumed, bool):
            raise TypeError("workspace undo handoff flag must be boolean")
        if self.state is WorkspaceUndoState.AVAILABLE:
            if self.checkpoint_id is None or self.reason is not None:
                raise ValueError("available undo must carry only a checkpoint")
            if self.verification_mutation_id is not None:
                raise ValueError("available undo cannot carry a verification mutation")
            if self.verification_handoff_consumed:
                raise ValueError("available undo cannot consume a verification handoff")
        elif self.state is WorkspaceUndoState.UNAVAILABLE:
            if self.reason is None or self.checkpoint_id is not None:
                raise ValueError("unavailable undo must carry only a reason")
            if self.verification_mutation_id is not None:
                raise ValueError("unavailable undo cannot carry a verification mutation")
            if self.verification_handoff_consumed:
                raise ValueError("unavailable undo cannot consume a verification handoff")
        else:
            if self.checkpoint_id is None or self.reason is not None:
                raise ValueError("rolled-back undo must carry only a checkpoint")
            if self.verification_mutation_id is None:
                raise ValueError("rolled-back undo must carry a verification mutation")

    def to_event_data(self) -> dict[str, object]:
        """Return the bounded durable session-event projection."""

        return {
            "schema": 1,
            "turn_id": self.turn_id,
            "state": self.state.value,
            "checkpoint_id": (self.checkpoint_id.value if self.checkpoint_id is not None else None),
            "reason": self.reason.value if self.reason is not None else None,
            "verification_mutation_id": self.verification_mutation_id,
            "verification_handoff_consumed": self.verification_handoff_consumed,
        }

    @classmethod
    def from_event_data(
        cls,
        session_id: str,
        data: object,
        *,
        updated_at: datetime,
    ) -> WorkspaceUndoAssociation:
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ValueError("workspace undo event schema is unsupported")
        turn_id = data.get("turn_id")
        raw_state = data.get("state")
        raw_checkpoint = data.get("checkpoint_id")
        raw_reason = data.get("reason")
        raw_mutation = data.get("verification_mutation_id")
        consumed = data.get("verification_handoff_consumed", False)
        if not isinstance(turn_id, str) or not isinstance(raw_state, str):
            raise ValueError("workspace undo event identity is malformed")
        if raw_checkpoint is not None and not isinstance(raw_checkpoint, str):
            raise ValueError("workspace undo checkpoint identity is malformed")
        if raw_reason is not None and not isinstance(raw_reason, str):
            raise ValueError("workspace undo reason is malformed")
        if raw_mutation is not None and not isinstance(raw_mutation, str):
            raise ValueError("workspace undo mutation identity is malformed")
        if not isinstance(consumed, bool):
            raise ValueError("workspace undo handoff flag is malformed")
        return cls(
            session_id=session_id,
            turn_id=turn_id,
            state=WorkspaceUndoState(raw_state),
            updated_at=updated_at,
            checkpoint_id=(CheckpointId(raw_checkpoint) if raw_checkpoint is not None else None),
            reason=(WorkspaceUndoReason(raw_reason) if raw_reason is not None else None),
            verification_mutation_id=raw_mutation,
            verification_handoff_consumed=consumed,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceUndoResult:
    """Bounded projection returned by explicit CLI/TUI undo commands."""

    state: WorkspaceUndoState
    reason: WorkspaceUndoReason | None = None
    restored: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.state, WorkspaceUndoState):
            raise TypeError("workspace undo result state must be canonical")
        if self.reason is not None and not isinstance(self.reason, WorkspaceUndoReason):
            raise TypeError("workspace undo result reason must be canonical")
        if not isinstance(self.restored, bool):
            raise TypeError("workspace undo result restored flag must be boolean")
        if self.state is WorkspaceUndoState.AVAILABLE and self.restored:
            raise ValueError("available undo cannot report restored")
        if self.state is WorkspaceUndoState.UNAVAILABLE and self.restored:
            raise ValueError("unavailable undo cannot report restored")
        if self.state is WorkspaceUndoState.ROLLED_BACK and not self.restored:
            raise ValueError("rolled-back undo must report restored")

    def to_dict(self) -> dict[str, object]:
        """Serialize without checkpoint, turn, path, or artifact identifiers."""

        return {
            "status": self.state.value,
            "reason": self.reason.value if self.reason is not None else None,
            "restored": self.restored,
        }


__all__ = [
    "MAX_WORKSPACE_UNDO_MUTATION_ID_BYTES",
    "MAX_WORKSPACE_UNDO_SESSION_ID_BYTES",
    "MAX_WORKSPACE_UNDO_TURN_ID_BYTES",
    "WorkspaceUndoAssociation",
    "WorkspaceUndoReason",
    "WorkspaceUndoResult",
    "WorkspaceUndoState",
]
