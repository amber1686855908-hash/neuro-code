from __future__ import annotations

from collections.abc import Iterable

from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.tools import Tool
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.tools import ToolDefinition
from neuro_code.shared.errors import ToolError


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


def default_tool_registry(
    sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    *,
    enable_background_tasks: bool = False,
    client_file_system: ClientFileSystem | None = None,
    client_terminal: ClientTerminal | None = None,
) -> ToolRegistry:
    from neuro_code.tools.background_tasks import KillTaskTool, TaskOutputTool, WaitTasksTool
    from neuro_code.tools.bash import BashTool
    from neuro_code.tools.client_terminal import (
        ClientTerminalKillTool,
        ClientTerminalOutputTool,
        ClientTerminalStartTool,
        ClientTerminalTool,
        ClientTerminalWaitTool,
    )
    from neuro_code.tools.filesystem import GrepTool, ListDirTool, ReadFileTool, SearchReplaceTool
    from neuro_code.tools.plans import UpdatePlanTool
    from neuro_code.tools.skills import SkillTool

    tools: list[Tool] = [ReadFileTool(), ListDirTool(), GrepTool(), SkillTool(), UpdatePlanTool()]
    if sandbox_profile.workspace_writable and (
        client_file_system is None
        or (client_file_system.supports_read and client_file_system.supports_write)
    ):
        tools.append(SearchReplaceTool())
    tools.append(BashTool(background_enabled=enable_background_tasks))
    if client_terminal is not None and not sandbox_profile.enabled:
        tools.extend(
            (
                ClientTerminalTool(),
                ClientTerminalStartTool(),
                ClientTerminalOutputTool(),
                ClientTerminalWaitTool(),
                ClientTerminalKillTool(),
            )
        )
    if enable_background_tasks:
        tools.extend((TaskOutputTool(), WaitTasksTool(), KillTaskTool()))
    return ToolRegistry(tools)
