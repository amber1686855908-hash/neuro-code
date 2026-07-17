from __future__ import annotations

from dataclasses import dataclass

from neuro_code.domain.messages import PreservedContextItem, ToolCall


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call: ToolCall


@dataclass(frozen=True, slots=True)
class ModelBackendToolStarted:
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ModelBackendToolCompleted:
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ModelProviderAttemptFailed:
    provider: str
    model: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ModelProviderSelected:
    provider: str
    model: str
    context_affinity: str | None
    failover: bool


@dataclass(frozen=True, slots=True)
class ModelCompleted:
    stop_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    context_items: tuple[PreservedContextItem, ...] = ()
    response_text: str | None = None


type ModelEvent = (
    ModelTextDelta
    | ModelReasoningDelta
    | ModelToolCall
    | ModelBackendToolStarted
    | ModelBackendToolCompleted
    | ModelProviderAttemptFailed
    | ModelProviderSelected
    | ModelCompleted
)
