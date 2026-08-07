"""Compatibility facade for the canonical session-search domain module.

提供会话搜索领域模块的兼容门面,并重新导出规范实现."""

from neuro_code.domain.sessions.search import (
    SessionSearchHit,
    SessionSearchPage,
    fallback_session_title,
    searchable_session_text,
)

__all__ = [
    "SessionSearchHit",
    "SessionSearchPage",
    "fallback_session_title",
    "searchable_session_text",
]
