from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from neuro_code.domain.conversation.messages import Message, PreservedContextItem, SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort

UPSTREAM_IMPORT_PROVIDER = "upstream-rust-import"


@dataclass(frozen=True, slots=True)
class ModelContext:
    """Ordered model input plus the session origin used for replay decisions.

    表示有序模型输入及用于重放决策的会话来源."""

    items: tuple[SessionItem, ...]
    source_provider: str | None = None
    source_model: str | None = None
    source_context_affinity: str | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            raise TypeError("model context reasoning effort must be a ReasoningEffort")
        if (self.source_provider is None) != (self.source_model is None):
            raise ValueError("model context source provider and model must be set together")
        if self.source_provider == "" or self.source_model == "":
            raise ValueError("model context source fields must not be empty")
        if self.source_context_affinity == "":
            raise ValueError("model context source affinity must not be empty")
        if self.source_context_affinity is not None and self.source_provider is None:
            raise ValueError("model context source affinity requires provider/model origin")

    @classmethod
    def from_messages(cls, messages: Sequence[Message]) -> ModelContext:
        return cls(tuple(messages))

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(item for item in self.items if isinstance(item, Message))

    @property
    def preserved_items(self) -> tuple[PreservedContextItem, ...]:
        return tuple(item for item in self.items if isinstance(item, PreservedContextItem))


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens without loading a provider-specific tokenizer.

    The estimate intentionally remains approximate. Provider-reported usage replaces it
    as soon as a model step returns token counts.

    在不加载 Provider 专用 tokenizer 的情况下估算 token 数. Provider 返回实际用量后会替代估算值.
    """

    if not text:
        return 0
    ascii_characters = sum(character.isascii() for character in text)
    non_ascii_characters = len(text) - ascii_characters
    return max(1, math.ceil(ascii_characters * 0.3 + non_ascii_characters * 0.6))


def _estimate_value_tokens(value: Any) -> int:
    if isinstance(value, str):
        return estimate_text_tokens(value)
    if isinstance(value, Mapping):
        return 2 + sum(
            estimate_text_tokens(str(key)) + _estimate_value_tokens(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return 2 + sum(_estimate_value_tokens(item) for item in value)
    if value is None:
        return 1
    return estimate_text_tokens(str(value))


def estimate_context_tokens(items: Sequence[SessionItem]) -> int:
    """Estimate the reusable conversation context represented by session items.

    估算会话条目所表示的可复用会话上下文 token 数."""

    total = 0
    for item in items:
        if isinstance(item, Message):
            total += 4
            total += estimate_text_tokens(item.role.value)
            total += estimate_text_tokens(item.model_content())
            total += estimate_text_tokens(item.name or "")
            total += estimate_text_tokens(item.tool_call_id or "")
            total += estimate_text_tokens(item.reasoning_content or "")
            total += _estimate_value_tokens(tuple(call.to_dict() for call in item.tool_calls))
        elif isinstance(item, PreservedContextItem):
            total += 4 + _estimate_value_tokens(item.payload)
    return total


__all__ = [
    "UPSTREAM_IMPORT_PROVIDER",
    "ModelContext",
    "estimate_context_tokens",
    "estimate_text_tokens",
]
