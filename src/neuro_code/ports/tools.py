from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.ports.background_tasks import BackgroundTaskManager
from neuro_code.ports.sandbox import ShellSandbox


@dataclass(frozen=True, slots=True)
class ToolContext:
    cwd: Path
    output_byte_limit: int = 200_000
    command_timeout_seconds: float = 120.0
    termination_grace_seconds: float = 1.0
    sandbox_profile: SandboxProfile = SandboxProfile.OFF
    shell_sandbox: ShellSandbox | None = None
    protected_environment_variables: frozenset[str] = frozenset()
    background_tasks: BackgroundTaskManager | None = None


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
