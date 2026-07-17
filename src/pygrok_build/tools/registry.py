from __future__ import annotations

from collections.abc import Iterable

from pygrok_build.domain.tools import ToolDefinition
from pygrok_build.errors import ToolError
from pygrok_build.ports.tools import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if not name or name in self._tools:
            raise ToolError(f"duplicate or empty tool name: {name!r}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)


def default_tool_registry() -> ToolRegistry:
    from pygrok_build.tools.bash import BashTool
    from pygrok_build.tools.filesystem import GrepTool, ListDirTool, ReadFileTool, SearchReplaceTool

    return ToolRegistry(
        (ReadFileTool(), ListDirTool(), GrepTool(), SearchReplaceTool(), BashTool())
    )
