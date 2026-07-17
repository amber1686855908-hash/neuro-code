from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pygrok_build.domain.tools import ToolDefinition, ToolResult


@dataclass(frozen=True, slots=True)
class ToolContext:
    cwd: Path
    output_byte_limit: int = 200_000
    command_timeout_seconds: float = 120.0
    termination_grace_seconds: float = 1.0


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
