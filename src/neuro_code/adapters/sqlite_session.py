"""Compatibility facade for the canonical SQLite session store.

提供 SQLite 会话存储的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore

__all__ = ["SqliteSessionStore"]
