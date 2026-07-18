from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from neuro_code.domain.messages import Message, PreservedContextItem, SessionItem


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens without loading a provider-specific tokenizer.

    The estimate intentionally remains approximate. Provider-reported usage replaces it
    as soon as a model step returns token counts.
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
    """Estimate the reusable conversation context represented by session items."""

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


__all__ = ["estimate_context_tokens", "estimate_text_tokens"]
