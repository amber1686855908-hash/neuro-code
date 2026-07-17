from __future__ import annotations

from dataclasses import dataclass

from pygrok_build.domain.messages import ToolCall


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
class ModelCompleted:
    stop_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None


type ModelEvent = ModelTextDelta | ModelReasoningDelta | ModelToolCall | ModelCompleted
