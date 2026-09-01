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
from neuro_code.application.workflows.subagent_capabilities import (
    NetworkAccess,
    SubagentCapabilitySet,
    WritableSubagentCapabilityGrant,
)
from neuro_code.application.workflows.writable_subagent import (
    RunWritableSubagentRequest,
    WritableSubagentRuntime,
    WritableSubagentRuntimeFactory,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.parent_context_relay import ParentContextRelay
from neuro_code.domain.worktree import WorktreeWorkspaceBinding
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
    __slots__ = ("_binding", "_capability_fingerprint", "_child_session_id", "_closed")

    def __init__(
        self,
        binding: ConversationBinding,
        child_session_id: str,
        capabilities: SubagentCapabilitySet,
    ) -> None:
        if binding.capabilities != capabilities:
            raise ConfigurationError("child binding capability metadata is inconsistent")
        self._binding = binding
        self._capability_fingerprint = capabilities.fingerprint
        self._child_session_id = child_session_id
        self._closed = False

    @property
    def child_session_id(self) -> str:
        return self._child_session_id

    @property
    def capability_fingerprint(self) -> str:
        return self._capability_fingerprint

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
        await self._binding.close()


class CompositionReadOnlySubagentRuntimeFactory(IsolatedSubagentRuntimeFactory):
    """Create fresh child conversations with no write-capable tools."""

    __slots__ = ("_composition",)

    def __init__(self, composition: ApplicationComposition) -> None:
        self._composition = composition

    def requested_capabilities(
        self,
        request: RunSubagentRequest,
        *,
        parent_capabilities: SubagentCapabilitySet,
    ) -> SubagentCapabilitySet:
        """Build a read-only request bounded by the actual parent manifest.

        ``READ_ONLY_SUBAGENT_TOOL_NAMES`` is only a request policy.  The
        workflow resolves it against the parent and global manifests before
        this factory is allowed to construct a binding.
        """

        if not isinstance(parent_capabilities, SubagentCapabilitySet):
            raise ConfigurationError("parent subagent capability metadata is required")
        tool_names = tuple(
            name
            for name in READ_ONLY_SUBAGENT_TOOL_NAMES
            if name in parent_capabilities.allowed_tool_names
        )
        return SubagentCapabilitySet.from_runtime(
            tool_names=tool_names,
            cwd=parent_capabilities.cwd,
            additional_workspace_roots=parent_capabilities.workspace_roots[1:],
            sandbox_profile=parent_capabilities.sandbox_profile,
            enable_background_tasks=False,
            max_steps=min(request.max_steps, parent_capabilities.max_steps),
        )

    async def create(
        self,
        request: RunSubagentRequest,
        *,
        parent_task_id: str,
        capabilities: SubagentCapabilitySet,
    ) -> IsolatedSubagentRuntime:
        if not parent_task_id:
            raise ValueError("parent task id must not be empty")
        if not isinstance(capabilities, SubagentCapabilitySet):
            raise ConfigurationError("child capabilities must be canonical")
        if (
            not capabilities.allowed_tool_names.issubset(READ_ONLY_SUBAGENT_TOOL_NAMES)
            or capabilities.filesystem_write
            or capabilities.bash
            or capabilities.terminal
            or capabilities.background_tasks
            or capabilities.mcp_tool_names
            or capabilities.mcp_server_names
            or capabilities.network_access is not NetworkAccess.NONE
        ):
            raise ConfigurationError("read-only subagent factory received an unsafe capability")
        selected_config = replace(
            _without_provider_builtin_tools(self._composition.config),
            cwd=capabilities.cwd,
            sandbox_profile=capabilities.sandbox_profile,
        )
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
                additional_workspace_roots=capabilities.workspace_roots[1:],
                capabilities=capabilities,
            )
        except BaseException:
            with suppress(BaseException):
                await asyncio.shield(self._composition.store.delete_session(child_session_id))
            raise
        return _CompositionReadOnlySubagentRuntime(binding, child_session_id, capabilities)


class _CompositionWritableSubagentRuntime:
    __slots__ = ("_binding", "_capability_fingerprint", "_child_session_id", "_closed")

    def __init__(
        self,
        binding: ConversationBinding,
        child_session_id: str,
        capabilities: WritableSubagentCapabilityGrant,
    ) -> None:
        if binding.capabilities != capabilities.capabilities:
            raise ConfigurationError("writable child binding capability metadata is inconsistent")
        self._binding = binding
        self._capability_fingerprint = capabilities.fingerprint
        self._child_session_id = child_session_id
        self._closed = False

    @property
    def child_session_id(self) -> str:
        return self._child_session_id

    @property
    def capability_fingerprint(self) -> str:
        return self._capability_fingerprint

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        if self._closed:
            raise ConfigurationError("writable subagent runtime is closed")
        result = await self._binding.runner.run(prompt, sink=sink)
        if result.session_id != self._child_session_id:
            raise ConfigurationError("writable child runtime returned a different child session")
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._binding.close()


class CompositionWritableSubagentRuntimeFactory(WritableSubagentRuntimeFactory):
    """Assemble a fresh child binding rooted only at the managed worktree."""

    __slots__ = ("_composition",)

    def __init__(self, composition: ApplicationComposition) -> None:
        self._composition = composition

    async def create_session(
        self,
        request: RunWritableSubagentRequest,
        *,
        capabilities: WritableSubagentCapabilityGrant,
    ) -> str:
        if not isinstance(request, RunWritableSubagentRequest):
            raise ValueError("writable subagent request must be canonical")
        workspace_binding = _writable_workspace_binding(capabilities)
        selected_config = replace(
            _without_provider_builtin_tools(self._composition.config),
            cwd=workspace_binding.primary_root,
            sandbox_profile=capabilities.capabilities.sandbox_profile,
        )
        provider = selected_config.provider
        return await self._composition.store.create_session(
            str(selected_config.cwd),
            provider.name,
            provider.model,
            provider.context_affinity,
            selected_config.sandbox_profile,
        )

    async def create(
        self,
        request: RunWritableSubagentRequest,
        *,
        parent_task_id: str,
        child_session_id: str,
        capabilities: WritableSubagentCapabilityGrant,
        relay: ParentContextRelay,
    ) -> WritableSubagentRuntime:
        if not parent_task_id:
            raise ValueError("parent task id must not be empty")
        if not child_session_id:
            raise ValueError("child session id must not be empty")
        if not isinstance(capabilities, WritableSubagentCapabilityGrant):
            raise ConfigurationError("writable child capabilities must be canonical")
        if not isinstance(relay, ParentContextRelay):
            raise ConfigurationError("writable child parent relay must be canonical")
        if (
            relay.child_session_id != child_session_id
            or relay.capability_fingerprint != capabilities.capabilities.fingerprint
            or relay.grant_fingerprint != capabilities.workspace_grant.fingerprint
        ):
            raise ConfigurationError("writable child parent relay identity is inconsistent")
        workspace_binding = _writable_workspace_binding(capabilities)
        selected_config = replace(
            _without_provider_builtin_tools(self._composition.config),
            cwd=workspace_binding.primary_root,
            sandbox_profile=capabilities.capabilities.sandbox_profile,
        )
        binding = await self._composition.create_binding(
            config=selected_config,
            resume_id=child_session_id,
            additional_workspace_roots=workspace_binding.additional_roots,
            capabilities=capabilities.capabilities,
            enable_background_tasks=False,
            parent_context_relay=relay,
            dag_result_relay=request.dependency_result_relay,
        )
        return _CompositionWritableSubagentRuntime(binding, child_session_id, capabilities)


def _writable_workspace_binding(
    capabilities: WritableSubagentCapabilityGrant,
) -> WorktreeWorkspaceBinding:
    workspace_binding = capabilities.workspace_grant.workspace_binding
    if (
        workspace_binding.primary_root != capabilities.workspace_grant.canonical_child_root
        or workspace_binding.primary_root != capabilities.capabilities.cwd
        or workspace_binding.additional_roots
        or capabilities.capabilities.workspace_roots != (workspace_binding.primary_root,)
    ):
        raise ConfigurationError("writable child workspace binding identity is inconsistent")
    return workspace_binding


def _without_provider_builtin_tools(config: AppConfig) -> AppConfig:
    providers = {
        name: replace(profile, builtin_tools=()) for name, profile in config.providers.items()
    }
    return replace(config, providers=providers)


__all__ = [
    "READ_ONLY_SUBAGENT_TOOL_NAMES",
    "CompositionReadOnlySubagentRuntimeFactory",
    "CompositionWritableSubagentRuntimeFactory",
]
