"""Concrete tool registry adapter.

This module is the canonical owner of :class:`ToolRegistry` and the
``default_tool_registry`` factory.  It is pure wiring: it does not execute
tools, hold side effects, or own permissions, sandbox, or cancellation
semantics.  Tool implementations are imported lazily inside the factory so
that importing this module does not load bash, background-task, client
terminal, filesystem, plan, or skill implementations.

The former ``neuro_code.tools.registry`` facade has been removed; this module
is the only registry owner.

定义具体的工具注册表适配器. 该模块只负责连接,不执行工具、不持有副作用、权限、沙箱或取消语义.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.tools import Tool
from neuro_code.domain.sandbox.models import SandboxProfile
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
    allowed_tool_names: Collection[str] | None = None,
    client_file_system: ClientFileSystem | None = None,
    client_terminal: ClientTerminal | None = None,
) -> ToolRegistry:
    from neuro_code.infrastructure.tools.background_tasks import (
        KillTaskTool,
        TaskOutputTool,
        WaitTasksTool,
    )
    from neuro_code.infrastructure.tools.bash import BashTool
    from neuro_code.infrastructure.tools.client_terminal import (
        ClientTerminalKillTool,
        ClientTerminalOutputTool,
        ClientTerminalStartTool,
        ClientTerminalTool,
        ClientTerminalWaitTool,
    )
    from neuro_code.infrastructure.tools.filesystem import (
        GrepTool,
        ListDirTool,
        ReadFileTool,
        SearchReplaceTool,
    )
    from neuro_code.infrastructure.tools.plans import UpdatePlanTool
    from neuro_code.infrastructure.tools.skills import SkillTool

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
    if allowed_tool_names is not None:
        allowed = frozenset(allowed_tool_names)
        tools = [tool for tool in tools if tool.definition.name in allowed]
    return ToolRegistry(tools)


__all__ = ["ToolRegistry", "default_tool_registry"]
