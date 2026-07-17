from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: str
    cwd: str
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
