"""Canonical session value objects.

This module owns the immutable summaries and snapshots used by the session
ports.  The package aggregate re-exports the public names so existing
``neuro_code.domain.sessions`` imports remain stable.

定义规范的会话值对象. 模块拥有会话端口使用的不可变摘要和快照,并重新导出公共名称以保持导入兼容.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from neuro_code.domain.conversation.messages import Message, SessionItem
from neuro_code.domain.sandbox.models import SandboxProfile

MAX_SESSION_TITLE_CHARS = 200


def normalize_session_title(title: str) -> str:
    """Normalize a persisted display title and enforce the domain invariant.

    规范化持久化的显示标题,并强制执行领域不变量."""

    normalized = " ".join(title.split())
    if not normalized:
        raise ValueError("session title must not be empty")
    return normalized[:MAX_SESSION_TITLE_CHARS]


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: str
    cwd: str
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime
    context_affinity: str | None = None
    sandbox_profile: SandboxProfile | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        if not all((self.id, self.cwd, self.provider, self.model)):
            raise ValueError("session summary fields must not be empty")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("session timestamps must be timezone-aware")
        if self.context_affinity == "":
            raise ValueError("session context affinity must not be empty")
        if self.title is not None:
            object.__setattr__(self, "title", normalize_session_title(self.title))
        if self.sandbox_profile is not None and not isinstance(
            self.sandbox_profile, SandboxProfile
        ):
            raise ValueError("session sandbox profile must be canonical")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "context_affinity": self.context_affinity,
            "sandbox_profile": (
                self.sandbox_profile.value if self.sandbox_profile is not None else None
            ),
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    summary: SessionSummary
    items: tuple[SessionItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(item for item in self.items if isinstance(item, Message))


__all__ = [
    "MAX_SESSION_TITLE_CHARS",
    "SessionSnapshot",
    "SessionSummary",
    "normalize_session_title",
]
