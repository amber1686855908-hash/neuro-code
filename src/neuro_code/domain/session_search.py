from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from neuro_code.domain.messages import Message, Role, SessionItem
from neuro_code.domain.sessions import MAX_SESSION_TITLE_CHARS, SessionSummary

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_TITLE_SOURCE_LIMIT = 8_000
_TITLE_WORD_LIMIT = 10


@dataclass(frozen=True, slots=True)
class SessionSearchHit:
    summary: SessionSummary
    score: float
    matched_fields: tuple[str, ...]
    snippet: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "matched_fields", tuple(self.matched_fields))
        if not math.isfinite(self.score):
            raise ValueError("session search score must be finite")
        if not self.matched_fields or any(
            field not in {"title", "content"} for field in self.matched_fields
        ):
            raise ValueError("session search matched fields must be title or content")

    def to_dict(self) -> dict[str, object]:
        return {
            **self.summary.to_dict(),
            "score": self.score,
            "matched_fields": list(self.matched_fields),
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class SessionSearchPage:
    results: tuple[SessionSearchHit, ...]
    next_offset: int | None
    total_estimate: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        if self.next_offset is not None and self.next_offset < 0:
            raise ValueError("session search next offset must not be negative")
        if self.total_estimate < len(self.results):
            raise ValueError("session search total must cover the returned results")

    def to_dict(self) -> dict[str, object]:
        return {
            "results": [result.to_dict() for result in self.results],
            "next_offset": self.next_offset,
            "total_estimate": self.total_estimate,
        }


def fallback_session_title(items: Sequence[SessionItem]) -> str:
    """Return the stable no-model fallback title used for a new session."""

    for item in items:
        if not isinstance(item, Message) or item.role is not Role.USER:
            continue
        source = _SYSTEM_REMINDER.sub(" ", item.content)[:_TITLE_SOURCE_LIMIT]
        words = source.split()
        if words:
            return " ".join(words[:_TITLE_WORD_LIMIT])[:MAX_SESSION_TITLE_CHARS]
    return "New session"


def searchable_session_text(items: Sequence[SessionItem]) -> str:
    """Project visible conversation and tool text without provider-private context."""

    chunks: list[str] = []
    for item in items:
        if not isinstance(item, Message) or item.role is Role.SYSTEM:
            continue
        if item.role in {Role.USER, Role.ASSISTANT} and item.content.strip():
            chunks.append(item.content)
        if item.name:
            chunks.append(item.name)
        for tool_call in item.tool_calls:
            chunks.append(tool_call.name)
    return "\n".join(chunks)
