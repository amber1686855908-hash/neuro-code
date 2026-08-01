"""Buffered, no-tool final responses for supervised agent execution.

This module intentionally has no connection to :mod:`agent`, persistence, or
user-interface event sinks.  A later runtime slice decides when it is safe to
invoke the finalizer; this component only makes a bounded, strictly no-tool
model request from already available context and evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.domain.execution import SupervisorReasonCode
from neuro_code.domain.messages import Message, Role, ToolCall
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelCompleted, ModelTextDelta, ModelToolCall
from neuro_code.domain.tools import ToolResult
from neuro_code.shared.errors import ProviderError
from neuro_code.shared.redaction import redact_sensitive_text

_DEFAULT_MAX_ATTEMPTS = 2
_MAX_EVIDENCE_ITEMS_PER_CATEGORY = 4
_MAX_EVIDENCE_ITEM_CHARS = 400
_MAX_EVIDENCE_BLOCKER_CHARS = 400
_MAX_STOP_REASON_CHARS = 160
_MAX_TOOL_NAME_CHARS = 80

_TOOL_REJECTION_CONTENT = (
    "Tool calls are unavailable while preparing the final response. The requested "
    "call was not executed. Provide a final answer using only the existing evidence."
)
_TOOL_REJECTION_FALLBACK = (
    "I could not produce a reliable final summary without calling tools. I stopped to "
    "avoid a loop; the existing conversation context is still available."
)
_EMPTY_RESPONSE_FALLBACK = (
    "I could not produce a final summary from the available evidence. No further model "
    "attempts were made to avoid a loop; the existing conversation context is still available."
)


class FinalizationStatus(StrEnum):
    """The accepted outcome of one bounded finalization request."""

    COMPLETED = "completed"
    TOOL_CALL_REJECTED = "tool_call_rejected"
    EMPTY_RESPONSE = "empty_response"


class Finalizer(Protocol):
    """The narrow runtime dependency needed to produce one final response."""

    async def finalize(
        self,
        context: ModelContext,
        evidence: FinalizationEvidence,
    ) -> FinalizationResult: ...


def _require_positive_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_optional_non_negative_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_non_negative_int(value, field_name=field_name)


def _bounded_text(value: str, *, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


def _bounded_evidence_items(value: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    items = tuple(value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{field_name} must contain strings")
    return tuple(
        _bounded_text(item, limit=_MAX_EVIDENCE_ITEM_CHARS)
        for item in items[:_MAX_EVIDENCE_ITEMS_PER_CATEGORY]
        if item
    )


@dataclass(frozen=True, slots=True)
class FinalizationAttempt:
    """A safe summary of one provider attempt, without its raw output."""

    attempt_number: int
    received_completion: bool
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    illegal_tool_call_count: int
    buffered_text_length: int

    def __post_init__(self) -> None:
        _require_positive_int(self.attempt_number, field_name="attempt_number")
        if not isinstance(self.received_completion, bool):
            raise ValueError("received_completion must be a bool")
        if self.stop_reason is not None and not isinstance(self.stop_reason, str):
            raise ValueError("stop_reason must be a string or None")
        if self.received_completion and self.stop_reason is None:
            raise ValueError("a completed attempt requires a stop_reason")
        _require_optional_non_negative_int(self.input_tokens, field_name="input_tokens")
        _require_optional_non_negative_int(self.output_tokens, field_name="output_tokens")
        _require_non_negative_int(
            self.illegal_tool_call_count,
            field_name="illegal_tool_call_count",
        )
        _require_non_negative_int(self.buffered_text_length, field_name="buffered_text_length")


@dataclass(frozen=True, slots=True)
class FinalizationEvidence:
    """Bounded factual evidence made available to a final no-tool request."""

    trigger: SupervisorReasonCode
    completed_items: tuple[str, ...] = field(default_factory=tuple)
    workspace_changes: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)
    unverified_items: tuple[str, ...] = field(default_factory=tuple)
    blocker: str | None = None
    uncertainty: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, SupervisorReasonCode):
            raise TypeError("trigger must be a SupervisorReasonCode")
        object.__setattr__(
            self,
            "completed_items",
            _bounded_evidence_items(self.completed_items, field_name="completed_items"),
        )
        object.__setattr__(
            self,
            "workspace_changes",
            _bounded_evidence_items(self.workspace_changes, field_name="workspace_changes"),
        )
        object.__setattr__(
            self,
            "verification",
            _bounded_evidence_items(self.verification, field_name="verification"),
        )
        object.__setattr__(
            self,
            "unverified_items",
            _bounded_evidence_items(self.unverified_items, field_name="unverified_items"),
        )
        object.__setattr__(
            self,
            "uncertainty",
            _bounded_evidence_items(self.uncertainty, field_name="uncertainty"),
        )
        if self.blocker is not None and not isinstance(self.blocker, str):
            raise TypeError("blocker must be a string or None")
        if self.blocker is not None:
            object.__setattr__(
                self,
                "blocker",
                _bounded_text(self.blocker, limit=_MAX_EVIDENCE_BLOCKER_CHARS),
            )


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    """The only data a runtime needs from one isolated finalization attempt."""

    status: FinalizationStatus
    response: str
    attempts: tuple[FinalizationAttempt, ...]
    total_input_tokens: int | None
    total_output_tokens: int | None
    illegal_tool_calls: int
    completed: bool
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FinalizationStatus):
            raise TypeError("status must be a FinalizationStatus")
        if not isinstance(self.response, str) or not self.response.strip():
            raise ValueError("response must be non-empty")
        attempts = tuple(self.attempts)
        if not attempts or not all(
            isinstance(attempt, FinalizationAttempt) for attempt in attempts
        ):
            raise ValueError("attempts must contain FinalizationAttempt values")
        if tuple(attempt.attempt_number for attempt in attempts) != tuple(
            range(1, len(attempts) + 1)
        ):
            raise ValueError("attempt numbers must be contiguous")
        object.__setattr__(self, "attempts", attempts)
        _require_optional_non_negative_int(
            self.total_input_tokens,
            field_name="total_input_tokens",
        )
        _require_optional_non_negative_int(
            self.total_output_tokens,
            field_name="total_output_tokens",
        )
        _require_non_negative_int(self.illegal_tool_calls, field_name="illegal_tool_calls")
        if self.illegal_tool_calls != sum(attempt.illegal_tool_call_count for attempt in attempts):
            raise ValueError("illegal_tool_calls must match attempt history")
        if not isinstance(self.completed, bool):
            raise ValueError("completed must be a bool")
        if self.completed is not (self.status is FinalizationStatus.COMPLETED):
            raise ValueError("completed must match finalization status")
        if self.stop_reason is not None and not isinstance(self.stop_reason, str):
            raise ValueError("stop_reason must be a string or None")


class AgentFinalizer:
    """Make a bounded, buffered final no-tool request from existing evidence."""

    __slots__ = ("_max_attempts", "_provider", "_redaction_values")

    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        redaction_values: Sequence[str] = (),
    ) -> None:
        _require_positive_int(max_attempts, field_name="max_attempts")
        values = tuple(redaction_values)
        if not all(isinstance(value, str) for value in values):
            raise TypeError("redaction_values must contain strings")
        self._provider = provider
        self._max_attempts = max_attempts
        self._redaction_values = values

    @property
    def max_attempts(self) -> int:
        """Return the finite number of allowed model attempts."""

        return self._max_attempts

    def _safe_text(self, value: str, *, limit: int | None = None) -> str:
        redacted = redact_sensitive_text(value, explicit_values=self._redaction_values)
        return _bounded_text(redacted, limit=limit) if limit is not None else redacted

    def _format_evidence_category(self, title: str, items: Sequence[str]) -> str:
        safe_items = tuple(self._safe_text(item, limit=_MAX_EVIDENCE_ITEM_CHARS) for item in items)
        if not safe_items:
            return f"{title}: none provided"
        return "\n".join((f"{title}:", *(f"- {item}" for item in safe_items)))

    def _instruction(self, evidence: FinalizationEvidence) -> str:
        blocker = (
            self._safe_text(evidence.blocker, limit=_MAX_EVIDENCE_BLOCKER_CHARS)
            if evidence.blocker
            else "none provided"
        )
        sections = (
            "You are producing the final response for the user.",
            "Do not call tools, request more searches, read files, run commands, or modify files.",
            "Use only the existing conversation and the confirmed evidence below.",
            "Do not claim an edit or validation happened unless the evidence explicitly confirms it.",
            "Clearly distinguish completed work, verified work, unverified work, blockers, and remaining uncertainty. If the work is partial, state that honestly.",
            self._format_evidence_category("Confirmed completed work", evidence.completed_items),
            self._format_evidence_category(
                "Confirmed workspace changes", evidence.workspace_changes
            ),
            self._format_evidence_category("Confirmed validation", evidence.verification),
            self._format_evidence_category("Unverified work", evidence.unverified_items),
            f"Blocker: {blocker}",
            self._format_evidence_category("Remaining uncertainty", evidence.uncertainty),
        )
        return "\n\n".join(sections)

    def _safe_tool_name(self, value: str) -> str:
        candidate = self._safe_text(value, limit=_MAX_TOOL_NAME_CHARS)
        normalized = "".join(
            character if character.isascii() and (character.isalnum() or character in "_-") else "_"
            for character in candidate
        ).strip("_")
        return normalized or "tool"

    def _rejection_messages(
        self,
        rejected_calls: Sequence[ToolCall],
        *,
        attempt_number: int,
    ) -> tuple[Message, ...]:
        if not rejected_calls:
            return ()
        calls: list[ToolCall] = []
        results: list[Message] = []
        for index, rejected in enumerate(rejected_calls, start=1):
            call_id = f"finalizer-rejected-{attempt_number}-{index}"
            name = self._safe_tool_name(rejected.name)
            calls.append(ToolCall(call_id, name, {}))
            result = ToolResult(_TOOL_REJECTION_CONTENT, is_error=True)
            results.append(Message(Role.TOOL, result.content, name=name, tool_call_id=call_id))
        return (Message(Role.ASSISTANT, tool_calls=tuple(calls)), *results)

    def _temporary_context(
        self,
        context: ModelContext,
        evidence: FinalizationEvidence,
        rejections: Sequence[Message],
    ) -> ModelContext:
        return ModelContext(
            (*context.items, *rejections, Message(Role.SYSTEM, self._instruction(evidence))),
            context.source_provider,
            context.source_model,
            context.source_context_affinity,
            context.reasoning_effort,
        )

    @staticmethod
    def _total_usage(values: Sequence[int | None]) -> int | None:
        total = 0
        for value in values:
            if value is None:
                return None
            total += value
        return total

    def _result(
        self,
        status: FinalizationStatus,
        response: str,
        attempts: Sequence[FinalizationAttempt],
    ) -> FinalizationResult:
        history = tuple(attempts)
        return FinalizationResult(
            status,
            self._safe_text(response),
            history,
            self._total_usage(tuple(attempt.input_tokens for attempt in history)),
            self._total_usage(tuple(attempt.output_tokens for attempt in history)),
            sum(attempt.illegal_tool_call_count for attempt in history),
            status is FinalizationStatus.COMPLETED,
            history[-1].stop_reason,
        )

    async def finalize(
        self,
        context: ModelContext,
        evidence: FinalizationEvidence,
    ) -> FinalizationResult:
        """Return a bounded final response without executing or exposing any tool."""

        if not isinstance(context, ModelContext):
            raise TypeError("context must be a ModelContext")
        if not isinstance(evidence, FinalizationEvidence):
            raise TypeError("evidence must be a FinalizationEvidence")

        attempts: list[FinalizationAttempt] = []
        rejections: tuple[Message, ...] = ()
        for attempt_number in range(1, self._max_attempts + 1):
            step_text: list[str] = []
            illegal_calls: list[ToolCall] = []
            completion: ModelCompleted | None = None
            temporary_context = self._temporary_context(context, evidence, rejections)
            async for event in self._provider.stream(
                temporary_context,
                (),
                tool_policy=ModelToolPolicy.DISABLED,
            ):
                if isinstance(event, ModelTextDelta):
                    step_text.append(event.text)
                elif isinstance(event, ModelToolCall):
                    illegal_calls.append(event.call)
                elif isinstance(event, ModelCompleted):
                    if completion is not None:
                        raise ProviderError("provider stream emitted multiple completion events")
                    completion = event

            if completion is None:
                raise ProviderError("provider stream ended without a completion event")

            response = (
                completion.response_text
                if completion.response_text is not None
                else "".join(step_text)
            )
            attempt = FinalizationAttempt(
                attempt_number,
                True,
                self._safe_text(completion.stop_reason, limit=_MAX_STOP_REASON_CHARS),
                completion.input_tokens,
                completion.output_tokens,
                len(illegal_calls),
                len(response),
            )
            attempts.append(attempt)

            if illegal_calls:
                if attempt_number == self._max_attempts:
                    return self._result(
                        FinalizationStatus.TOOL_CALL_REJECTED,
                        _TOOL_REJECTION_FALLBACK,
                        attempts,
                    )
                rejections = (
                    *rejections,
                    *self._rejection_messages(illegal_calls, attempt_number=attempt_number),
                )
                continue

            if response.strip():
                return self._result(FinalizationStatus.COMPLETED, response, attempts)
            if attempt_number == self._max_attempts:
                return self._result(
                    FinalizationStatus.EMPTY_RESPONSE,
                    _EMPTY_RESPONSE_FALLBACK,
                    attempts,
                )

        raise AssertionError("bounded finalization attempts must return a result")


__all__ = [
    "AgentFinalizer",
    "FinalizationAttempt",
    "FinalizationEvidence",
    "FinalizationResult",
    "FinalizationStatus",
    "Finalizer",
]
