"""Canonical tool execution ports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.instructions import InstructionContextTracker
from neuro_code.application.ports.sandbox import ShellSandbox
from neuro_code.application.ports.skills import SkillContextTracker
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult


@dataclass(frozen=True, slots=True)
class ToolContext:
    cwd: Path
    additional_workspace_roots: tuple[Path, ...] = ()
    output_byte_limit: int = 200_000
    command_timeout_seconds: float = 120.0
    termination_grace_seconds: float = 1.0
    sandbox_profile: SandboxProfile = SandboxProfile.OFF
    shell_sandbox: ShellSandbox | None = None
    protected_environment_variables: frozenset[str] = frozenset()
    redaction_values: tuple[str, ...] = field(default=(), repr=False)
    background_tasks: BackgroundTaskManager | None = None
    instruction_tracker: InstructionContextTracker | None = None
    skill_tracker: SkillContextTracker | None = None


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    @property
    def side_effecting(self) -> bool: ...

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult: ...


class ToolCollection(Protocol):
    """Resolve tools and expose their ordered model definitions."""

    def get(self, name: str) -> Tool | None: ...

    def definitions(self) -> tuple[ToolDefinition, ...]: ...
