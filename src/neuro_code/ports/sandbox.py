from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.domain.sandbox import SandboxProfile


@dataclass(frozen=True, slots=True)
class ShellLaunch:
    """An argv-safe shell launch prepared by a platform sandbox adapter."""

    executable: str
    arguments: tuple[str, ...]


class ShellSandbox(Protocol):
    @property
    def profile(self) -> SandboxProfile: ...

    def shell_launch(self, command: str) -> ShellLaunch: ...
