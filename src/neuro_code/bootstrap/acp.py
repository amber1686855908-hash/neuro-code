"""Bootstrap-owned ACP composition adapters.

ACP bootstrap wiring validates workspace identity and adapts concrete MCP and
composition resources to the narrow application-facing ACP contracts. It does
not own ACP protocol or transport behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from neuro_code.application.acp.contracts import (
    MAX_ADDITIONAL_DIRECTORIES,
    MAX_ADDITIONAL_DIRECTORY_BYTES,
    AcpBinding,
    AcpMcpHttpServerConfig,
    AcpMcpServerConfig,
    AcpMcpStdioServerConfig,
    AcpMcpToolError,
    AcpMcpTools,
    AcpPreparedSession,
    AcpResumeUnavailableError,
    AcpWorkspaceValidationError,
)
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.configuration import AppConfig
from neuro_code.application.ports.mcp import (
    McpElicitationHandler,
    McpPrompt,
    McpPromptMessage,
    McpResource,
    McpResourceContent,
    McpResourceTemplate,
    McpSamplingHandler,
)
from neuro_code.application.ports.sandbox import LocalProcessSandbox
from neuro_code.application.ports.tools import Tool
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.mcp.http import (
    McpHttpError,
    McpHttpServerConfig,
    McpHttpToolCollection,
)
from neuro_code.infrastructure.mcp.stdio import (
    MAX_MCP_TOTAL_TOOLS,
    McpStdioError,
    McpStdioServerConfig,
    McpStdioToolCollection,
)
from neuro_code.infrastructure.workspace.paths import workspaces_match
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ConfigurationError, SandboxError


class _BootstrapWorkspaceValidator:
    """Concrete workspace identity behavior selected only by bootstrap.

    表示仅由 bootstrap 选择的具体工作区身份行为."""

    def __init__(self, workspace: Path, sandbox_profile: SandboxProfile) -> None:
        self._workspace = workspace
        self._sandbox_profile = sandbox_profile

    async def validate(
        self,
        cwd: str,
        additional_directories: Sequence[str],
    ) -> tuple[Path, ...]:
        try:
            requested = Path(cwd)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise AcpWorkspaceValidationError("cwd_invalid", type(error).__name__) from None
        if not requested.is_absolute():
            raise AcpWorkspaceValidationError("cwd_not_absolute")
        try:
            normalized = await run_blocking(requested.resolve, strict=False)
        except (OSError, RuntimeError) as error:
            raise AcpWorkspaceValidationError("cwd_invalid", type(error).__name__) from None
        if not workspaces_match(normalized, self._workspace):
            raise AcpWorkspaceValidationError("cwd_workspace_mismatch")
        if not additional_directories:
            return ()
        if self._sandbox_profile.enabled:
            raise AcpWorkspaceValidationError("additional_directories_sandbox_unsupported")
        if len(additional_directories) > MAX_ADDITIONAL_DIRECTORIES:
            raise AcpWorkspaceValidationError("additional_directories_too_many")

        resolved_directories: list[Path] = []
        for directory in additional_directories:
            if not isinstance(directory, str) or not directory:
                raise AcpWorkspaceValidationError("additional_directory_invalid")
            try:
                directory_size = len(directory.encode("utf-8"))
            except UnicodeError:
                raise AcpWorkspaceValidationError("additional_directory_invalid") from None
            if directory_size > MAX_ADDITIONAL_DIRECTORY_BYTES:
                raise AcpWorkspaceValidationError("additional_directory_too_large")
            try:
                requested_directory = Path(directory)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise AcpWorkspaceValidationError(
                    "additional_directory_invalid", type(error).__name__
                ) from None
            if not requested_directory.is_absolute():
                raise AcpWorkspaceValidationError("additional_directory_not_absolute")
            try:
                resolved_directory = await run_blocking(
                    requested_directory.resolve,
                    strict=True,
                )
                is_directory = await run_blocking(resolved_directory.is_dir)
            except (OSError, RuntimeError) as error:
                raise AcpWorkspaceValidationError(
                    "additional_directory_invalid", type(error).__name__
                ) from None
            if not is_directory:
                raise AcpWorkspaceValidationError("additional_directory_not_directory")
            if any(
                resolved_directory == root
                or resolved_directory.is_relative_to(root)
                or root.is_relative_to(resolved_directory)
                for root in (normalized, *resolved_directories)
            ):
                raise AcpWorkspaceValidationError("additional_directory_overlaps_workspace")
            resolved_directories.append(resolved_directory)
        return tuple(resolved_directories)

    def matches(self, cwd: Path) -> bool:
        return workspaces_match(cwd, self._workspace)


class _BootstrapMcpToolFactory:
    """Adapter-backed MCP factory; opening remains session-lazy.

    提供由适配器支持的 MCP 工厂,打开操作仍延迟到会话创建之后."""

    def __init__(
        self,
        *,
        local_process_sandbox_factory: Callable[[], LocalProcessSandbox],
        sandbox_profile: SandboxProfile,
    ) -> None:
        self._local_process_sandbox_factory = local_process_sandbox_factory
        self._sandbox_profile = sandbox_profile

    async def open(
        self,
        configurations: Sequence[AcpMcpServerConfig],
        *,
        cwd: Path,
        explicit_redactions: Sequence[str],
        sampling_handler: McpSamplingHandler | None = None,
        elicitation_handler: McpElicitationHandler | None = None,
    ) -> AcpMcpTools:
        collections: list[AcpMcpTools] = []
        tools: list[Tool] = []
        names: set[str] = set()
        try:
            local_process_sandbox: LocalProcessSandbox | None = None
            for configuration in configurations:
                collection: AcpMcpTools
                if isinstance(configuration, AcpMcpStdioServerConfig):
                    if local_process_sandbox is None:
                        try:
                            local_process_sandbox = self._local_process_sandbox_factory()
                        except (OSError, SandboxError, ValueError):
                            raise AcpMcpToolError("mcp_child_sandbox_unavailable") from None
                    collection = await McpStdioToolCollection.open(
                        (
                            McpStdioServerConfig(
                                name=configuration.name,
                                command=configuration.command,
                                args=configuration.args,
                                env=configuration.env,
                            ),
                        ),
                        cwd=cwd,
                        explicit_redactions=explicit_redactions,
                        local_process_sandbox=local_process_sandbox,
                        sandbox_profile=self._sandbox_profile,
                        sampling_handler=sampling_handler,
                        elicitation_handler=elicitation_handler,
                    )
                elif isinstance(configuration, AcpMcpHttpServerConfig):
                    collection = await McpHttpToolCollection.open(
                        (
                            McpHttpServerConfig(
                                name=configuration.name,
                                url=configuration.url,
                                headers=configuration.headers,
                                transport=configuration.transport,
                            ),
                        ),
                        explicit_redactions=explicit_redactions,
                        sampling_handler=sampling_handler,
                        elicitation_handler=elicitation_handler,
                    )
                else:  # pragma: no cover - the validated union is exhaustive.
                    raise AcpMcpToolError("mcp_transport_unsupported")
                collections.append(collection)
                for tool in collection.tools:
                    if tool.definition.name in names:
                        raise AcpMcpToolError("mcp_tool_name_collision")
                    names.add(tool.definition.name)
                    tools.append(tool)
                    if len(tools) > MAX_MCP_TOTAL_TOOLS:
                        raise AcpMcpToolError("too_many_mcp_tools")
            return _CompositeMcpTools(tuple(collections), tuple(tools))
        except (McpHttpError, McpStdioError, AcpMcpToolError) as error:
            await asyncio.gather(
                *(collection.close() for collection in reversed(collections)),
                return_exceptions=True,
            )
            raise AcpMcpToolError(error.reason) from None


class _CompositeMcpTools:
    """Close heterogeneous session-owned MCP adapters as one resource.

    将多个类型不同的会话拥有 MCP 适配器作为一个资源关闭."""

    def __init__(self, collections: tuple[AcpMcpTools, ...], tools: tuple[Tool, ...]) -> None:
        self._collections = collections
        self.tools = tools
        self.resources: tuple[McpResource, ...] = self._collect_resources()
        self.resource_templates: tuple[McpResourceTemplate, ...] = (
            self._collect_resource_templates()
        )
        self.prompts: tuple[McpPrompt, ...] = self._collect_prompts()
        self._closed = False
        self._close_lock = asyncio.Lock()

    def _collect_resources(self) -> tuple[McpResource, ...]:
        return tuple(
            resource
            for collection in self._collections
            for resource in getattr(collection, "resources", ())
        )

    def _collect_resource_templates(self) -> tuple[McpResourceTemplate, ...]:
        return tuple(
            template
            for collection in self._collections
            for template in getattr(collection, "resource_templates", ())
        )

    def _collect_prompts(self) -> tuple[McpPrompt, ...]:
        return tuple(
            prompt
            for collection in self._collections
            for prompt in getattr(collection, "prompts", ())
        )

    async def refresh(self) -> None:
        await asyncio.gather(*(collection.refresh() for collection in self._collections))
        tools: list[Tool] = []
        names: set[str] = set()
        for collection in self._collections:
            for tool in getattr(collection, "tools", ()):
                if tool.definition.name in names:
                    raise AcpMcpToolError("mcp_tool_name_collision")
                names.add(tool.definition.name)
                tools.append(tool)
                if len(tools) > MAX_MCP_TOTAL_TOOLS:
                    raise AcpMcpToolError("too_many_mcp_tools")
        self.tools = tuple(tools)
        self.resources = self._collect_resources()
        self.resource_templates = self._collect_resource_templates()
        self.prompts = self._collect_prompts()

    async def read_resource(self, uri: str) -> tuple[McpResourceContent, ...]:
        for collection in self._collections:
            if any(resource.uri == uri for resource in getattr(collection, "resources", ())):
                read_resource = getattr(collection, "read_resource", None)
                if not callable(read_resource):
                    raise AcpMcpToolError("mcp_resources_unavailable")
                return cast(tuple[McpResourceContent, ...], await read_resource(uri))
        raise AcpMcpToolError("mcp_resource_not_found")

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, str] | None = None,
    ) -> tuple[McpPromptMessage, ...]:
        for collection in self._collections:
            if any(prompt.name == name for prompt in getattr(collection, "prompts", ())):
                get_prompt = getattr(collection, "get_prompt", None)
                if not callable(get_prompt):
                    raise AcpMcpToolError("mcp_prompts_unavailable")
                return cast(tuple[McpPromptMessage, ...], await get_prompt(name, arguments))
        raise AcpMcpToolError("mcp_prompt_not_found")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await asyncio.gather(
                *(collection.close() for collection in reversed(self._collections)),
                return_exceptions=True,
            )


@dataclass(frozen=True, slots=True)
class _PreparedCompositionAcpSession:
    application: ApplicationComposition
    config: AppConfig
    session_id: str

    @property
    def context_window_tokens(self) -> int | None:
        return self.config.provider.context_window_tokens

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None,
        additional_tools: Sequence[Tool],
        additional_workspace_roots: Sequence[Path],
        client_file_system: ClientFileSystem | None,
        client_terminal: ClientTerminal | None,
    ) -> ConversationBinding:
        return await self.application.create_binding(
            config=self.config,
            approver=approver,
            resume_id=self.session_id,
            additional_tools=additional_tools,
            additional_workspace_roots=additional_workspace_roots,
            client_file_system=client_file_system,
            client_terminal=client_terminal,
        )


class _CompositionAcpBindingFactory:
    """Composition-root adapter for ACP's narrow binding contract.

    提供组合根适配器,实现 ACP 的精简绑定契约."""

    def __init__(self, application: ApplicationComposition) -> None:
        self._application = application

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None,
        additional_tools: Sequence[Tool],
        additional_workspace_roots: Sequence[Path],
        client_file_system: ClientFileSystem | None,
        client_terminal: ClientTerminal | None,
    ) -> AcpBinding:
        binding = await self._application.create_binding(
            approver=approver,
            additional_tools=additional_tools,
            additional_workspace_roots=additional_workspace_roots,
            client_file_system=client_file_system,
            client_terminal=client_terminal,
        )
        return AcpBinding(
            binding=binding,
            context_window_tokens=self._application.config.provider.context_window_tokens,
        )

    async def prepare_session_resume(self, session_id: str) -> AcpPreparedSession:
        try:
            config = await self._application.config_for_session_resume(session_id)
        except ConfigurationError as error:
            message = str(error)
            if "workspace" in message:
                reason = "session_workspace_mismatch"
            elif "sandbox" in message:
                reason = "session_sandbox_mismatch"
            else:
                reason = "session_provider_unavailable"
            raise AcpResumeUnavailableError(reason) from None
        return _PreparedCompositionAcpSession(self._application, config, session_id)
