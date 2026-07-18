from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from neuro_code.domain.messages import Message, SessionItem
from neuro_code.domain.sandbox import SandboxProfile


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

    def __post_init__(self) -> None:
        if not all((self.id, self.cwd, self.provider, self.model)):
            raise ValueError("session summary fields must not be empty")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("session timestamps must be timezone-aware")
        if self.context_affinity == "":
            raise ValueError("session context affinity must not be empty")
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
