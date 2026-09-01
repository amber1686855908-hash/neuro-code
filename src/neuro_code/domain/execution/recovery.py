"""Durable facts used to classify an interrupted execution turn.

Crash recovery is deliberately a small, fail-closed projection.  It is not a
workspace checkpoint and it never treats a missing process as permission to
replay a request.

用于判定中断执行回合的持久化事实。

崩溃恢复刻意保持为小型、失败关闭的投影,它不是工作区检查点,也绝不会把进程
消失自动解释为可以重放请求。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from neuro_code.domain.conversation.messages import ContentPart, ContentPartKind
from neuro_code.domain.execution.tasks import TurnSource

MAX_TURN_INPUT_BYTES = 256 * 1024
MAX_RECOVERY_REASON_BYTES = 512
MAX_RECOVERY_ID_BYTES = 512


class TurnRecoveryStatus(StrEnum):
    """The externally meaningful classification of one turn attempt."""

    COMMITTED = "committed"
    SAFELY_RETRYABLE = "safely_retryable"
    INDETERMINATE = "indeterminate"
    ABANDONED = "abandoned"


class TurnRecoveryResolution(StrEnum):
    """Durable terminal resolution of an attempt."""

    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class TurnRecoveryStage(StrEnum):
    """The latest sticky fact written for an attempt."""

    ACCEPTED = "accepted"
    REQUEST_STARTED = "request_started"
    MODEL_OUTPUT_STARTED = "model_output_started"
    TOOL_STARTED = "tool_started"
    TURN_FAILED = "turn_failed"
    TURN_COMPLETED = "turn_completed"
    ABANDONED = "abandoned"


class TurnRecoveryFactKind(StrEnum):
    """Facts whose event and projection must be written atomically."""

    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_OUTPUT_STARTED = "model_output_started"
    TOOL_STARTED = "tool_started"


def _bounded_identifier(value: str, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_RECOVERY_ID_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_part_from_dict(value: object) -> ContentPart:
    if not isinstance(value, dict):
        raise ValueError("turn input content part must be an object")
    raw_kind = value.get("type")
    if not isinstance(raw_kind, str):
        raise ValueError("turn input content part type is invalid")
    try:
        kind = ContentPartKind(raw_kind)
    except ValueError as error:
        raise ValueError("turn input content part type is unsupported") from error
    if kind is ContentPartKind.TEXT:
        text = value.get("text")
        if not isinstance(text, str):
            raise ValueError("turn input text part is invalid")
        return ContentPart.from_text(text)
    if kind is ContentPartKind.IMAGE:
        url = value.get("url")
        if not isinstance(url, str):
            raise ValueError("turn input image part is invalid")
        return ContentPart.from_image(url)
    data = value.get("data")
    mime_type = value.get("mime_type")
    if not isinstance(data, str) or not isinstance(mime_type, str):
        raise ValueError("turn input media part is invalid")
    if kind is ContentPartKind.AUDIO:
        return ContentPart.from_audio(data, mime_type)
    url = value.get("url")
    if not isinstance(url, str):
        raise ValueError("turn input blob part is invalid")
    return ContentPart.from_blob(url, data, mime_type)


@dataclass(frozen=True, slots=True)
class TurnInput:
    """The exact, turn-owned input needed for an explicit retry.

    System context, provider request bodies, credentials, and tool arguments
    are intentionally absent.  Background wake turns are never reconstructable
    because their child-task state can change independently of the session.
    """

    prompt: str
    content_parts: tuple[ContentPart, ...] = ()
    source: TurnSource = TurnSource.USER
    plan_execution_requested: bool = False
    plan_execution_task_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str):
            raise ValueError("turn input prompt must be a string")
        parts = tuple(self.content_parts)
        if not all(isinstance(part, ContentPart) for part in parts):
            raise ValueError("turn input content_parts must contain ContentPart values")
        object.__setattr__(self, "content_parts", parts)
        if not isinstance(self.source, TurnSource):
            raise ValueError("turn input source must be canonical")
        if not isinstance(self.plan_execution_requested, bool):
            raise ValueError("turn input plan flag must be boolean")
        if self.plan_execution_task_id is not None:
            _bounded_identifier(self.plan_execution_task_id, field_name="plan_execution_task_id")

    @property
    def background(self) -> bool:
        return self.source is TurnSource.BACKGROUND_TASK_AUTO_WAKE

    @property
    def reconstructable(self) -> bool:
        return not self.background

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "content_parts": [part.to_dict() for part in self.content_parts],
            "source": self.source.value,
            "plan_execution_requested": self.plan_execution_requested,
            "plan_execution_task_id": self.plan_execution_task_id,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> TurnInput:
        if not isinstance(value, dict):
            raise ValueError("turn input must be an object")
        prompt = value.get("prompt")
        raw_parts = value.get("content_parts", [])
        raw_source = value.get("source")
        requested = value.get("plan_execution_requested", False)
        task_id = value.get("plan_execution_task_id")
        if not isinstance(prompt, str) or not isinstance(raw_parts, list):
            raise ValueError("turn input payload is invalid")
        if not isinstance(raw_source, str):
            raise ValueError("turn input source is invalid")
        try:
            source = TurnSource(raw_source)
        except ValueError as error:
            raise ValueError("turn input source is unsupported") from error
        if not isinstance(requested, bool):
            raise ValueError("turn input plan flag is invalid")
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError("turn input task id is invalid")
        return cls(
            prompt,
            tuple(_content_part_from_dict(part) for part in raw_parts),
            source,
            requested,
            task_id,
        )


@dataclass(frozen=True, slots=True)
class TurnRecoveryFact:
    """Bounded metadata for one atomic recovery fact write."""

    kind: TurnRecoveryFactKind
    request_id: str | None = None
    step: int | None = None
    provider: str | None = None
    model: str | None = None
    output_kind: str | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    side_effecting: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TurnRecoveryFactKind):
            raise ValueError("recovery fact kind must be canonical")
        for name in (
            "request_id",
            "provider",
            "model",
            "output_kind",
            "tool_id",
            "tool_name",
        ):
            value = getattr(self, name)
            if value is not None:
                _bounded_identifier(value, field_name=name)
        if self.step is not None and (
            not isinstance(self.step, int) or isinstance(self.step, bool) or self.step <= 0
        ):
            raise ValueError("recovery fact step must be positive")
        if not isinstance(self.side_effecting, bool):
            raise ValueError("recovery fact side_effecting must be boolean")

    def to_event_data(self, turn_id: str) -> dict[str, object]:
        data: dict[str, object] = {"turn_id": turn_id, "recovery_fact": self.kind.value}
        for name in (
            "request_id",
            "step",
            "provider",
            "model",
            "output_kind",
            "tool_id",
            "tool_name",
            "side_effecting",
        ):
            value = getattr(self, name)
            if value is not None and (name != "side_effecting" or value):
                data[name] = value
        return data


@dataclass(frozen=True, slots=True)
class TurnRecoveryAttempt:
    """Canonical durable attempt projection returned by the storage port."""

    turn_id: str
    session_id: str
    source: TurnSource
    task_id: str | None
    input_fingerprint: str
    input: TurnInput | None
    input_reconstructable: bool
    accepted_at: datetime
    resolution: TurnRecoveryResolution | None = None
    resolution_at: datetime | None = None
    request_started_count: int = 0
    request_id: str | None = None
    step: int | None = None
    provider: str | None = None
    model: str | None = None
    output_started: bool = False
    tool_started_count: int = 0
    side_effecting_tool_started: bool = False
    last_tool_id: str | None = None
    last_tool_name: str | None = None
    last_stage: TurnRecoveryStage = TurnRecoveryStage.ACCEPTED
    last_stage_at: datetime | None = None
    fact_conflict: bool = False

    def __post_init__(self) -> None:
        _bounded_identifier(self.turn_id, field_name="turn_id")
        _bounded_identifier(self.session_id, field_name="session_id")
        if self.task_id is not None:
            _bounded_identifier(self.task_id, field_name="task_id")
        if len(self.input_fingerprint) != 64:
            raise ValueError("input fingerprint must be SHA-256")
        if not isinstance(self.source, TurnSource):
            raise ValueError("attempt source must be canonical")
        if self.input is not None and self.input.fingerprint != self.input_fingerprint:
            raise ValueError("attempt input fingerprint does not match input")
        if not isinstance(self.input_reconstructable, bool):
            raise ValueError("attempt input_reconstructable must be boolean")
        if self.accepted_at.tzinfo is None:
            raise ValueError("attempt accepted_at must be timezone-aware")
        if self.resolution_at is not None and self.resolution_at.tzinfo is None:
            raise ValueError("attempt resolution_at must be timezone-aware")
        if self.last_stage_at is not None and self.last_stage_at.tzinfo is None:
            raise ValueError("attempt last_stage_at must be timezone-aware")
        for name in (
            "request_id",
            "provider",
            "model",
            "last_tool_id",
            "last_tool_name",
        ):
            value = getattr(self, name)
            if value is not None:
                _bounded_identifier(value, field_name=name)
        for name in ("request_started_count", "tool_started_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.step is not None and (
            not isinstance(self.step, int) or isinstance(self.step, bool) or self.step <= 0
        ):
            raise ValueError("attempt step must be positive")
        if not isinstance(self.output_started, bool):
            raise ValueError("attempt output_started must be boolean")
        if not isinstance(self.side_effecting_tool_started, bool):
            raise ValueError("attempt side_effecting_tool_started must be boolean")
        if not isinstance(self.last_stage, TurnRecoveryStage):
            raise ValueError("attempt last_stage must be canonical")
        if not isinstance(self.fact_conflict, bool):
            raise ValueError("attempt fact_conflict must be boolean")

    @classmethod
    def create(
        cls,
        *,
        turn_id: str,
        session_id: str,
        input: TurnInput,
        task_id: str | None = None,
        accepted_at: datetime,
    ) -> TurnRecoveryAttempt:
        if not isinstance(input, TurnInput):
            raise TypeError("input must be a TurnInput")
        serialized = input.canonical_json().encode("utf-8")
        reconstructable = input.reconstructable and len(serialized) <= MAX_TURN_INPUT_BYTES
        return cls(
            turn_id,
            session_id,
            input.source,
            task_id,
            input.fingerprint,
            input if reconstructable else None,
            reconstructable,
            accepted_at,
        )

    @property
    def status(self) -> TurnRecoveryStatus:
        if self.resolution is TurnRecoveryResolution.COMMITTED:
            return TurnRecoveryStatus.COMMITTED
        if self.resolution is TurnRecoveryResolution.ABANDONED:
            return TurnRecoveryStatus.ABANDONED
        if self.resolution is not None:
            # Normal supervised failures are resolved and are not interrupted
            # attempts.  The recovery projection still exposes them as safely
            # closed rather than allowing a replay path.
            return TurnRecoveryStatus.ABANDONED
        if (
            self.plan_task_ownership_missing
            or self.fact_conflict
            or self.output_started
            or self.tool_started_count > 0
            or self.side_effecting_tool_started
            or not self.input_reconstructable
            or self.source is TurnSource.BACKGROUND_TASK_AUTO_WAKE
        ):
            return TurnRecoveryStatus.INDETERMINATE
        return TurnRecoveryStatus.SAFELY_RETRYABLE

    @property
    def plan_task_ownership_missing(self) -> bool:
        """Whether a plan attempt lacks its explicit durable task owner."""

        return (
            self.input is not None and self.input.plan_execution_requested and self.task_id is None
        )

    @property
    def retry_available(self) -> bool:
        """Whether the user-facing recovery surface may offer explicit retry."""

        return (
            self.resolution is None
            and self.status is TurnRecoveryStatus.SAFELY_RETRYABLE
            and self.input is not None
            and self.source is TurnSource.USER
            and not self.input.plan_execution_requested
        )

    @property
    def abandon_available(self) -> bool:
        """Whether explicit abandon is safe to offer for this projection."""

        return self.resolution is None and not self.plan_task_ownership_missing

    @property
    def status_reason(self) -> str:
        if self.resolution is TurnRecoveryResolution.COMMITTED:
            return "atomic_turn_commit"
        if self.resolution is TurnRecoveryResolution.ABANDONED:
            return "explicitly_abandoned"
        if self.resolution is not None:
            return "terminal_failure_recorded"
        if self.plan_task_ownership_missing:
            return "plan_task_ownership_missing"
        if self.fact_conflict:
            return "contradictory_recovery_facts"
        if self.output_started:
            return "model_output_started_before_commit"
        if self.side_effecting_tool_started:
            return "side_effecting_tool_started_before_commit"
        if self.tool_started_count > 0:
            return "tool_started_before_commit"
        if self.source is TurnSource.BACKGROUND_TASK_AUTO_WAKE:
            return "background_wake_is_not_reconstructable"
        if not self.input_reconstructable:
            return "exact_input_unavailable"
        return "no_output_or_tool_effect_before_commit"

    def safe_projection(self) -> dict[str, object]:
        """Return bounded metadata suitable for CLI/TUI/ACP rendering."""

        return {
            "turn_id": self.turn_id,
            "status": self.status.value,
            "reason": self.status_reason,
            "source": self.source.value,
            "task_id": self.task_id,
            "resolution": self.resolution.value if self.resolution is not None else None,
            "retry_available": self.retry_available,
            "abandon_available": self.abandon_available,
            "last_stage": self.last_stage.value,
            "accepted_at": self.accepted_at.isoformat(),
            "resolution_at": (
                self.resolution_at.isoformat() if self.resolution_at is not None else None
            ),
            "input_fingerprint": self.input_fingerprint,
            "input_reconstructable": self.input_reconstructable,
            "request_started_count": self.request_started_count,
            "request_id": self.request_id,
            "step": self.step,
            "provider": self.provider,
            "model": self.model,
            "output_started": self.output_started,
            "tool_started_count": self.tool_started_count,
            "side_effecting_tool_started": self.side_effecting_tool_started,
            "last_tool_id": self.last_tool_id,
            "last_tool_name": self.last_tool_name,
            "fact_conflict": self.fact_conflict,
        }


__all__ = [
    "MAX_RECOVERY_ID_BYTES",
    "MAX_RECOVERY_REASON_BYTES",
    "MAX_TURN_INPUT_BYTES",
    "TurnInput",
    "TurnRecoveryAttempt",
    "TurnRecoveryFact",
    "TurnRecoveryFactKind",
    "TurnRecoveryResolution",
    "TurnRecoveryStage",
    "TurnRecoveryStatus",
]
