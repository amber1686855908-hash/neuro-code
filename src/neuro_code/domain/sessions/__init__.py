"""Canonical session domain package.

定义规范的会话领域包."""

from neuro_code.domain.sessions.models import (
    MAX_SESSION_TITLE_CHARS,
    SessionSnapshot,
    SessionSummary,
    normalize_session_title,
)
from neuro_code.domain.sessions.search import (
    SessionSearchHit,
    SessionSearchPage,
    fallback_session_title,
    searchable_session_text,
)

__all__ = [
    "MAX_SESSION_TITLE_CHARS",
    "SessionSearchHit",
    "SessionSearchPage",
    "SessionSnapshot",
    "SessionSummary",
    "fallback_session_title",
    "normalize_session_title",
    "searchable_session_text",
]
