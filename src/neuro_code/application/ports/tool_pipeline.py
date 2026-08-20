"""Composable hooks around the canonical tool security pipeline.

工具安全流水线周围的可组合钩子.

Hooks receive redacted arguments and a canonical, redacted terminal result. A
pre-hook runs only after permission/approval has succeeded and immediately
before execution; a post-hook cannot grant permission or alter the result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from neuro_code.domain.tools import ToolExecutionResult


class ToolPipelineHook(Protocol):
    """Observe one tool call without owning security or execution."""

    async def before_tool(
        self,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        side_effecting: bool,
    ) -> None: ...

    async def after_tool(self, result: ToolExecutionResult) -> None: ...


__all__ = ["ToolPipelineHook"]
