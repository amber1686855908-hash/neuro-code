"""Interface-neutral settings for composing one Neuro Code process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.permissions import PermissionMode, PermissionRule


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """Interface-neutral settings for composing one Neuro Code process."""

    cwd: Path | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    sandbox: str | None = None
    failover: bool = True
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    permission_rules: tuple[PermissionRule, ...] = ()
    max_steps: int = 24
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    resume_id: str | None = None
    launch_command: tuple[str, ...] = ()
