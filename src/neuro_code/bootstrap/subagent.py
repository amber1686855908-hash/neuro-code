"""Composition-root assembly for the explicit read-only subagent runtime.

组合根负责组装显式只读子代理运行时.

The application workflow owns lifecycle and durable parent/child linkage.  This
module owns only concrete dependency assembly: a fresh provider, a fresh child
conversation, and a fixed read-only tool capability set.
应用工作流负责生命周期和持久父子链接. 本模块只负责具体依赖组装:全新 Provider、全新子会话和固定只读工具能力集合.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.workflows.subagent import (
    IsolatedSubagentRuntime,
    IsolatedSubagentRuntimeFactory,
    RunSubagentRequest,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.configuration.app import AppConfig


READ_ONLY_SUBAGENT_TOOL_NAMES = (
    "read_file",
    "read_files",
    "list_dir",
    "list_tree",
    "glob",
    "grep",
    "grep_many",
    "skill",
)


class _CompositionReadOnlySubagentRuntime:
    __slots__ = ("_binding", "_child_session_id", "_closed")

    def __init__(
        self,
        binding: ConversationBinding,
        child_session_id: str,
    ) -> None:
        self._binding = binding
        self._child_session_id = child_session_id
        self._closed = False

    @property
    def child_session_id(self) -> str:
        return self._child_session_id

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        if self._closed:
            raise ConfigurationError("read-only subagent runtime is closed")
        result = await self._binding.runner.run(prompt, sink=sink)
        if result.session_id != self._child_session_id:
            raise ConfigurationError("subagent runtime returned a different child session")
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        background_tasks = self._binding.background_tasks
        if background_tasks is not None:
            await background_tasks.shutdown()


class CompositionReadOnlySubagentRuntimeFactory(IsolatedSubagentRuntimeFactory):
    """Create fresh child conversations with no write-capable tools."""

    __slots__ = ("_composition",)

    def __init__(self, composition: ApplicationComposition) -> None:
        self._composition = composition

    async def create(
        self,
        request: RunSubagentRequest,
        *,
        parent_task_id: str,
    ) -> IsolatedSubagentRuntime:
        if not parent_task_id:
            raise ValueError("parent task id must not be empty")
        selected_config = _without_provider_builtin_tools(self._composition.config)
        provider = selected_config.provider
        child_session_id = await self._composition.store.create_session(
            str(selected_config.cwd),
            provider.name,
            provider.model,
            provider.context_affinity,
            selected_config.sandbox_profile,
        )
        try:
            binding = await self._composition.create_binding(
                config=selected_config,
                resume_id=child_session_id,
                max_steps=request.max_steps,
                allowed_tool_names=READ_ONLY_SUBAGENT_TOOL_NAMES,
                enable_background_tasks=False,
            )
        except BaseException:
            with suppress(BaseException):
                await asyncio.shield(self._composition.store.delete_session(child_session_id))
            raise
        return _CompositionReadOnlySubagentRuntime(binding, child_session_id)


def _without_provider_builtin_tools(config: AppConfig) -> AppConfig:
    providers = {
        name: replace(profile, builtin_tools=()) for name, profile in config.providers.items()
    }
    return replace(config, providers=providers)


__all__ = [
    "READ_ONLY_SUBAGENT_TOOL_NAMES",
    "CompositionReadOnlySubagentRuntimeFactory",
]
