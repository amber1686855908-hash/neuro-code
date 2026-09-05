"""Conversation binding construction owned by bootstrap.

会话绑定构造的组合根 owner.

This module assembles one binding's provider, tools, process sandbox, LSP
service, permissions, runtime, and conversation.  The binding resource scope
remains the cleanup authority for resources opened here.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Collection, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from neuro_code.application.execution_policy import ExecutionBudgetPolicy
from neuro_code.application.memory.compaction import ProviderContextWindow
from neuro_code.application.memory.compaction_runtime import ContextCompactionRuntimeGate
from neuro_code.application.memory.compaction_service import ContextCompactionApplicationService
from neuro_code.application.memory.compaction_trigger import ContextCompactionTriggerService
from neuro_code.application.memory.instruction_tracker import InstructionTracker
from neuro_code.application.memory.skill_tracker import SkillTracker
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
    PermissionRule,
    PermissionRuleStore,
)
from neuro_code.application.permissions.service import ToolApprovalService
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.background_tasks import (
    BackgroundTaskManager,
)
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.configuration import AppConfig
from neuro_code.application.ports.model import (
    CapabilityStatus,
    ModelCapability,
    ModelCapabilitySet,
    ModelProvider,
)
from neuro_code.application.ports.parent_context_relay import (
    ParentContextRelayError,
    ParentContextRelayStore,
)
from neuro_code.application.ports.sandbox import LocalProcessSandbox
from neuro_code.application.ports.task_dag import TaskDagError, TaskDagStore
from neuro_code.application.ports.task_dag_result_relay import (
    TaskDagDependencyResultRelayError,
    TaskDagDependencyResultRelayStore,
)
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.application.ports.user_interaction import UserInteractionPort
from neuro_code.application.ports.web_fetch import (
    WebFetchExecutionPath,
    WebFetchMode,
    resolve_web_fetch_path,
)
from neuro_code.application.ports.web_search import (
    WebSearchExecutionPath,
    WebSearchMode,
    resolve_web_search_path,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.sessions.binding import (
    ConversationBinding,
    ConversationBindingResourceScope,
)
from neuro_code.application.sessions.conversation import AgentConversation
from neuro_code.application.sessions.summary import (
    GetSessionSummaryRequest,
)
from neuro_code.application.web_fetch.service import WebFetchService
from neuro_code.application.web_search.service import WebSearchService
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.bootstrap.composition_contracts import CompositionRootMixin
from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.parent_context_relay import (
    ParentContextRelay,
    render_parent_context_relay,
)
from neuro_code.domain.task_dag import TaskDagNodeState
from neuro_code.domain.task_dag_result_relay import (
    TaskDagDependencyResultRelay,
    render_task_dag_dependency_relay,
)
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult
from neuro_code.infrastructure.lsp.manager import LanguageServerManager
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.infrastructure.providers.hosted_web_search import (
    RoutedWebSearchBackendResolver,
)
from neuro_code.infrastructure.tools.filesystem_mutation import ExactWorkspaceMutationTool
from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry
from neuro_code.infrastructure.tools.web_fetch import WebFetchTool
from neuro_code.infrastructure.tools.web_search import WebSearchTool
from neuro_code.infrastructure.tools.workspace_diff import WorkspaceMutationJournal
from neuro_code.infrastructure.web_fetch.local import LocalWebFetcher
from neuro_code.infrastructure.workspace.changes import MultiRootWorkspaceChangeObserver
from neuro_code.infrastructure.workspace.paths import FilesystemWorkspaceIdentity, workspaces_match
from neuro_code.shared.errors import ConfigurationError


def _without_main_inline_web_search(config: AppConfig) -> AppConfig:
    """Disable only MAIN hosted search for explicit sidecar/disabled modes."""

    route = config.main_route
    names = {route.provider_profile, *route.fallback_profiles}
    profiles = dict(config.providers)
    for name in names:
        profile = profiles.get(name)
        if profile is None or not ({"web_search", "google_search"} & set(profile.builtin_tools)):
            continue
        profiles[name] = replace(
            profile,
            builtin_tools=tuple(
                tool_name
                for tool_name in profile.builtin_tools
                if tool_name not in {"web_search", "google_search"}
            ),
        )
    return replace(config, providers=profiles)


def _without_main_inline_web_fetch(config: AppConfig) -> AppConfig:
    """Disable only MAIN hosted fetch tools for local/disabled modes."""

    route = config.main_route
    names = {route.provider_profile, *route.fallback_profiles}
    profiles = dict(config.providers)
    for name in names:
        profile = profiles.get(name)
        if profile is None or not ({"web_fetch", "url_context"} & set(profile.builtin_tools)):
            continue
        profiles[name] = replace(
            profile,
            builtin_tools=tuple(
                tool_name
                for tool_name in profile.builtin_tools
                if tool_name not in {"web_fetch", "url_context"}
            ),
        )
    return replace(config, providers=profiles)


class CompositionBindingMixin(CompositionRootMixin):
    """Assemble a conversation binding and its per-binding resources."""

    def create_local_process_sandbox(
        self: CompositionRootMixin,
        *,
        config: AppConfig | None = None,
    ) -> LocalProcessSandbox:
        """Create a composition-owned local process launcher for one config.

        Bootstrap adapters use this narrow factory when a session-scoped
        process, such as an ACP stdio MCP server, starts outside a conversation
        binding.  The launcher is still selected by the same composition path
        as Bash and background tasks.

        为一个配置创建由组合根拥有的本地进程启动器. Bootstrap 适配器在会话范围进程
        (例如 ACP stdio MCP server) 不属于 conversation binding 时使用该工厂.
        启动器仍由与 Bash 和后台任务相同的组合路径选择.
        """

        selected_config = config or self.config
        return self._local_process_sandbox_factory(
            selected_config.sandbox_profile,
            selected_config.cwd,
            selected_config.state_dir,
        )

    async def create_binding(
        self: CompositionRootMixin,
        *,
        config: AppConfig | None = None,
        approver: PermissionApprover | None = None,
        resume_id: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        additional_tools: Sequence[Tool] = (),
        additional_workspace_roots: Sequence[Path] = (),
        client_file_system: ClientFileSystem | None = None,
        client_terminal: ClientTerminal | None = None,
        max_steps: int | None = None,
        allowed_tool_names: Collection[str] | None = None,
        enable_background_tasks: bool | None = None,
        capabilities: SubagentCapabilitySet | None = None,
        user_interaction: UserInteractionPort | None = None,
        parent_context_relay: ParentContextRelay | None = None,
        dag_result_relay: TaskDagDependencyResultRelay | None = None,
        # Internal orchestration bindings opt out until their own verification
        # integration is implemented; user-facing bindings keep the default.
        final_output_gate_enabled: bool = True,
    ) -> ConversationBinding:
        if self._closed:
            raise RuntimeError("application composition is closed")
        selected_config = config or self.config
        if parent_context_relay is not None:
            if not isinstance(parent_context_relay, ParentContextRelay):
                raise ConfigurationError("parent context relay must be canonical")
            if capabilities is None or resume_id != parent_context_relay.child_session_id:
                raise ConfigurationError("parent context relay child binding is inconsistent")
            if capabilities.fingerprint != parent_context_relay.capability_fingerprint:
                raise ConfigurationError("parent context relay capability binding is inconsistent")
            try:
                durable_relay = await cast(
                    ParentContextRelayStore,
                    self.store,
                ).get_parent_context_relay(parent_context_relay.relay_id)
            except ParentContextRelayError as error:
                raise ConfigurationError(
                    f"parent context relay integrity verification failed: {error}"
                ) from error
            if durable_relay != parent_context_relay:
                raise ConfigurationError("parent context relay is not the published durable record")
        if dag_result_relay is not None:
            if not isinstance(dag_result_relay, TaskDagDependencyResultRelay):
                raise ConfigurationError("DAG result relay must be canonical")
            try:
                durable_dag_relay = await cast(
                    TaskDagDependencyResultRelayStore,
                    self.store,
                ).get_task_dag_dependency_relay(dag_result_relay.relay_id)
            except TaskDagDependencyResultRelayError as error:
                raise ConfigurationError(
                    f"DAG result relay integrity verification failed: {error}"
                ) from error
            if durable_dag_relay != dag_result_relay:
                raise ConfigurationError("DAG result relay is not the published durable record")
            try:
                durable_dag = await cast(TaskDagStore, self.store).get_task_dag(
                    dag_result_relay.dag_id
                )
            except TaskDagError as error:
                raise ConfigurationError(
                    f"DAG result relay target verification failed: {error}"
                ) from error
            if durable_dag is None:
                raise ConfigurationError("DAG result relay target DAG is missing")
            try:
                durable_target = durable_dag.node(dag_result_relay.target_node_id)
            except KeyError as error:
                raise ConfigurationError("DAG result relay target node is missing") from error
            if (
                durable_dag.definition_fingerprint != dag_result_relay.dag_definition_fingerprint
                or durable_target.state is not TaskDagNodeState.RUNNING
                or durable_target.generation != dag_result_relay.target_node_generation
                or durable_target.definition_fingerprint
                != dag_result_relay.target_node_definition_fingerprint
                or durable_target.dependencies != dag_result_relay.direct_dependency_ids
            ):
                raise ConfigurationError(
                    "DAG result relay target is not the active exact execution"
                )
        if capabilities is not None:
            if not isinstance(capabilities, SubagentCapabilitySet):
                raise ConfigurationError("child capabilities must be canonical")
            if selected_config.cwd != capabilities.cwd:
                raise ConfigurationError("child capability cwd does not match child config")
            if selected_config.sandbox_profile is not capabilities.sandbox_profile:
                raise ConfigurationError("child capability sandbox does not match child config")
            if max_steps is not None and max_steps != capabilities.max_steps:
                raise ConfigurationError("child max_steps conflicts with capability budget")
            if allowed_tool_names is not None and frozenset(allowed_tool_names) != (
                capabilities.allowed_tool_names
            ):
                raise ConfigurationError("raw tool allowlist conflicts with child capability")
            if enable_background_tasks is not None and (
                enable_background_tasks is not capabilities.background_tasks
            ):
                raise ConfigurationError("raw background-task flag conflicts with child capability")
            expected_additional_roots = capabilities.workspace_roots[1:]
            if (
                additional_workspace_roots
                and tuple(
                    path.expanduser().resolve(strict=False) for path in additional_workspace_roots
                )
                != expected_additional_roots
            ):
                raise ConfigurationError("raw workspace roots conflict with child capability")
            max_steps = capabilities.max_steps
            allowed_tool_names = capabilities.allowed_tool_names
            additional_workspace_roots = expected_additional_roots
            enable_background_tasks = capabilities.background_tasks
        elif enable_background_tasks is None:
            enable_background_tasks = True
        assert enable_background_tasks is not None
        selected_execution_budget = (
            self.settings.execution_budget
            if max_steps is None
            else ExecutionBudgetPolicy.from_max_steps(max_steps)
        )

        # Validate collisions before opening the binding-owned background scope.
        # Tool construction is pure wiring, so this preserves the existing
        # cleanup guarantee when an additional tool conflicts with a built-in.
        preview_tools = default_tool_registry(
            selected_config.sandbox_profile,
            enable_background_tasks=enable_background_tasks,
            allowed_tool_names=allowed_tool_names,
            client_file_system=client_file_system,
            client_terminal=client_terminal,
            user_interaction=user_interaction,
        )
        for tool in additional_tools:
            if allowed_tool_names is not None and tool.definition.name not in allowed_tool_names:
                raise ConfigurationError(
                    f"tool {tool.definition.name!r} is outside the selected capability set"
                )
            preview_tools.register_external(tool)
        if selected_config.web_fetch_mode is not WebFetchMode.DISABLED and any(
            tool.definition.name == "web_fetch" for tool in additional_tools
        ):
            raise ConfigurationError("duplicate or reserved tool name: 'web_fetch'")
        if selected_config.web_search_mode is WebSearchMode.SIDECAR and any(
            tool.definition.name == "web_search" for tool in additional_tools
        ):
            raise ConfigurationError("duplicate or reserved tool name: 'web_search'")

        async def prepare_provider_and_tools() -> tuple[
            BackgroundTaskManager,
            ModelProvider,
            ToolRegistry,
            LocalProcessSandbox,
            LanguageServerManager,
        ]:
            local_process_sandbox = self._local_process_sandbox_factory(
                selected_config.sandbox_profile,
                selected_config.cwd,
                selected_config.state_dir,
            )
            lsp_service = LanguageServerManager(
                config=selected_config,
                local_process_sandbox=local_process_sandbox,
                workspace_root=selected_config.cwd,
                additional_workspace_roots=tuple(additional_workspace_roots),
                redaction_values=selected_config.redaction_values(os.environ),
            )
            task_scope = self.background_tasks.open_scope(
                local_process_sandbox=local_process_sandbox,
            )
            try:
                provider_config = selected_config
                if selected_config.web_search_mode in {
                    WebSearchMode.DISABLED,
                    WebSearchMode.SIDECAR,
                }:
                    provider_config = _without_main_inline_web_search(selected_config)
                if selected_config.web_fetch_mode in {
                    WebFetchMode.DISABLED,
                    WebFetchMode.LOCAL,
                }:
                    provider_config = _without_main_inline_web_fetch(provider_config)
                provider = self._provider_factory(provider_config, self.settings.failover)
                provider_capabilities = getattr(
                    provider,
                    "capabilities",
                    ModelCapabilitySet.all_unknown(),
                )
                inline_fetch_supported = (
                    isinstance(provider_capabilities, ModelCapabilitySet)
                    and provider_capabilities.status(ModelCapability.HOSTED_WEB_FETCH)
                    is CapabilityStatus.SUPPORTED
                )
                fetch_path = resolve_web_fetch_path(
                    selected_config.web_fetch_mode,
                    inline_supported=inline_fetch_supported,
                )
                if fetch_path is WebFetchExecutionPath.UNAVAILABLE:
                    raise ConfigurationError(
                        "inline web fetch was explicitly requested but MAIN does not have "
                        "an explicitly supported hosted-fetch capability"
                    )
                if (
                    fetch_path is WebFetchExecutionPath.LOCAL
                    and selected_config.web_fetch_mode is WebFetchMode.AUTO
                ):
                    provider_config = _without_main_inline_web_fetch(provider_config)
                    provider = self._provider_factory(provider_config, self.settings.failover)
                    provider_capabilities = getattr(
                        provider,
                        "capabilities",
                        ModelCapabilitySet.all_unknown(),
                    )
                search_resolver = RoutedWebSearchBackendResolver(selected_config)
                search_route = selected_config.web_search_route
                sidecar_available = (
                    search_route is not None and search_resolver.resolve(search_route) is not None
                )
                client_tool_names = tuple(
                    name
                    for name in preview_tools.names()
                    if name not in {"web_search", "google_search", "url_context"}
                )
                if fetch_path is WebFetchExecutionPath.LOCAL and (
                    allowed_tool_names is None or "web_fetch" in allowed_tool_names
                ):
                    client_tool_names += ("web_fetch",)
                inline_supported = (
                    provider_capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH)
                    and (
                        not client_tool_names
                        or provider_capabilities.supports(
                            ModelCapability.MIXED_HOSTED_AND_CLIENT_TOOLS
                        )
                    )
                    if isinstance(provider_capabilities, ModelCapabilitySet)
                    else False
                )
                execution_path = resolve_web_search_path(
                    selected_config.web_search_mode,
                    inline_supported=inline_supported,
                    sidecar_available=sidecar_available,
                )
                if (
                    selected_config.web_search_mode is WebSearchMode.INLINE
                    and execution_path is WebSearchExecutionPath.UNAVAILABLE
                ):
                    raise ConfigurationError(
                        "inline web search was explicitly requested but MAIN does not have "
                        "an explicitly supported hosted-search capability"
                    )
                if (
                    selected_config.web_search_mode is WebSearchMode.AUTO
                    and execution_path is not WebSearchExecutionPath.INLINE_HOSTED
                    and any(
                        tool_name in {"web_search", "google_search"}
                        for profile_name in {
                            selected_config.main_route.provider_profile,
                            *selected_config.main_route.fallback_profiles,
                        }
                        for tool_name in selected_config.providers[profile_name].builtin_tools
                    )
                ):
                    # AUTO may discover too late that the MAIN model cannot
                    # combine its hosted tool with the local client tools. In
                    # that case the route has already resolved to a sidecar
                    # (or unavailable), so rebuild the provider without the
                    # inline hosted tool instead of exposing both paths.
                    provider_config = _without_main_inline_web_search(selected_config)
                    if fetch_path is not WebFetchExecutionPath.INLINE_HOSTED:
                        provider_config = _without_main_inline_web_fetch(provider_config)
                    provider = self._provider_factory(provider_config, self.settings.failover)
                    provider_capabilities = getattr(
                        provider,
                        "capabilities",
                        ModelCapabilitySet.all_unknown(),
                    )
                tools = default_tool_registry(
                    selected_config.sandbox_profile,
                    enable_background_tasks=enable_background_tasks,
                    allowed_tool_names=allowed_tool_names,
                    client_file_system=client_file_system,
                    client_terminal=client_terminal,
                    user_interaction=user_interaction,
                    lsp_service=lsp_service,
                )
                if fetch_path is WebFetchExecutionPath.LOCAL and (
                    allowed_tool_names is None or "web_fetch" in allowed_tool_names
                ):
                    tools.register(
                        WebFetchTool(
                            WebFetchService(
                                LocalWebFetcher(
                                    redaction_values=selected_config.redaction_values(os.environ),
                                ),
                                redaction_values=selected_config.redaction_values(os.environ),
                            )
                        )
                    )
                if execution_path is WebSearchExecutionPath.SIDECAR_HOSTED and (
                    allowed_tool_names is None or "web_search" in allowed_tool_names
                ):
                    tools.register(
                        WebSearchTool(
                            WebSearchService(
                                selected_config,
                                search_resolver,
                                redaction_values=selected_config.redaction_values(os.environ),
                            )
                        )
                    )
                for tool in additional_tools:
                    if (
                        allowed_tool_names is not None
                        and tool.definition.name not in allowed_tool_names
                    ):
                        raise ConfigurationError(
                            f"tool {tool.definition.name!r} is outside the selected capability set"
                        )
                    tools.register_external(tool)
                return task_scope, provider, tools, local_process_sandbox, lsp_service
            except BaseException:
                await asyncio.shield(lsp_service.close())
                await asyncio.shield(task_scope.shutdown())
                raise

        (
            task_scope,
            provider,
            tools,
            local_process_sandbox,
            lsp_service,
        ) = await prepare_provider_and_tools()
        self._lsp_services.add(lsp_service)
        try:
            compaction_persistence = ContextCompactionApplicationService(
                self.store,
                provider,
                redaction_values=selected_config.redaction_values(os.environ),
            )
            compaction_gate = ContextCompactionRuntimeGate(
                ContextCompactionTriggerService(compaction_persistence)
            )
            approval_service = ToolApprovalService(approver) if approver is not None else None
            # Build a per-binding instruction tracker that re-discovers
            # AGENTS.md files from the workspace root toward the current
            # focus directory.  File-access tools call ``check_path()`` to
            # move the target deeper, enabling deep-directory AGENTS.md
            # discovery.  The ``instruction_provider`` closure reads the
            # tracker's current result before each model step, so file
            # content changes take effect on the next turn without a restart.
            tracker = InstructionTracker(
                discovery=self._instruction_discovery,
                workspace_root=selected_config.cwd,
                initial_target=selected_config.cwd,
            )

            def instruction_provider() -> InstructionDiscoveryResult | None:
                return tracker.model_context_result()

            # Build a per-binding skill tracker that re-discovers SKILL.md
            # files from the workspace's skills directories.  Like the
            # instruction tracker, the skill tracker maintains a moving
            # target that shifts as tools access different paths.  Discovery
            # walks upward from the target to the workspace root (inclusive),
            # finding skills at any depth.  The ``skill_provider`` closure
            # reads the tracker's current result before each model step, so
            # skill file changes take effect on the next turn without a
            # restart.
            skill_tracker = SkillTracker(
                discovery=self._skill_discovery,
                workspace_root=selected_config.cwd,
                initial_target=selected_config.cwd,
            )

            def skill_provider() -> SkillDiscoveryResult | None:
                return skill_tracker.current_result()

            persisted_rules: tuple[PermissionRule, ...] = ()
            if self.settings.permission_rules_path is not None:
                try:
                    persisted_rules = PermissionRuleStore(
                        self.settings.permission_rules_path
                    ).load()
                except ValueError as error:
                    raise ConfigurationError(str(error)) from error
            permissions = PermissionManager(
                mode=self.settings.permission_mode,
                rules=(
                    *persisted_rules,
                    *self.settings.permission_rules,
                    *(
                        PermissionRule(PermissionEffect.ASK, tool.definition.name)
                        for tool in additional_tools
                    ),
                ),
                interactive=approval_service is not None,
            )
            lsp_service.set_visibility_policy(permissions)
            workspace_change_observer = self._workspace_change_observer_factory()
            if additional_workspace_roots:
                workspace_change_observer = MultiRootWorkspaceChangeObserver(
                    workspace_change_observer,
                    additional_workspace_roots,
                )
            workspace_change_journal = (
                WorkspaceMutationJournal() if client_file_system is None else None
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=tools,
                workspace_change_observer=workspace_change_observer,
                permissions=permissions,
                tool_context=ToolContext(
                    selected_config.cwd,
                    additional_workspace_roots=tuple(additional_workspace_roots),
                    sandbox_profile=selected_config.sandbox_profile,
                    local_process_sandbox=local_process_sandbox,
                    protected_environment_variables=(
                        selected_config.protected_environment_variables
                    ),
                    redaction_values=selected_config.redaction_values(os.environ),
                    background_tasks=task_scope if enable_background_tasks else None,
                    instruction_tracker=tracker,
                    skill_tracker=skill_tracker,
                    client_file_system=client_file_system,
                    client_terminal=client_terminal,
                    output_artifact_store=FileToolOutputArtifactStore(
                        selected_config.state_dir / "tool-output",
                        redaction_values=selected_config.redaction_values(os.environ),
                    ),
                    workspace_change_journal=workspace_change_journal,
                    user_interaction=user_interaction,
                ),
                approver=approval_service,
                session_store=self.store,
                workspace_mutation_tool=ExactWorkspaceMutationTool(),
                execution_budget=selected_execution_budget,
                reasoning_effort=reasoning_effort or self.settings.reasoning_effort,
                execution_control_mode=self.settings.execution_control_mode,
                final_output_gate_enabled=final_output_gate_enabled,
                compaction_runtime_gate=compaction_gate,
                provider_context_window=(
                    ProviderContextWindow(
                        selected_config.provider.name,
                        selected_config.provider.model,
                        selected_config.provider.context_window_tokens,
                        selected_config.provider.context_affinity,
                    )
                    if selected_config.provider.context_window_tokens is not None
                    else None
                ),
                instruction_provider=instruction_provider,
                skill_provider=skill_provider,
                parent_relay_message=(
                    Message(
                        Role.USER,
                        render_parent_context_relay(parent_context_relay.items),
                        synthetic_reason=SyntheticReason.PARENT_RELAY,
                    )
                    if parent_context_relay is not None
                    else None
                ),
                dag_result_relay_message=(
                    Message(
                        Role.USER,
                        render_task_dag_dependency_relay(dag_result_relay.entries),
                        synthetic_reason=SyntheticReason.DAG_PREDECESSOR_RESULTS,
                    )
                    if dag_result_relay is not None
                    else None
                ),
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=self.store,
                cwd=selected_config.cwd,
                workspace_identity=FilesystemWorkspaceIdentity(),
                resume_id=resume_id,
            )
            binding_capabilities = SubagentCapabilitySet.from_runtime(
                tool_names=tools.names(),
                provider_tool_names=selected_config.provider.builtin_tools,
                mcp_tool_names=tuple(tool.definition.name for tool in additional_tools),
                cwd=selected_config.cwd,
                additional_workspace_roots=additional_workspace_roots,
                sandbox_profile=selected_config.sandbox_profile,
                enable_background_tasks=enable_background_tasks,
                max_steps=selected_execution_budget.max_model_calls,
            )
            if capabilities is not None and binding_capabilities != capabilities:
                raise ConfigurationError(
                    "child binding capability metadata does not match its construction"
                )

            async def close_binding_resources() -> None:
                self._lsp_services.discard(lsp_service)
                await lsp_service.close()
                await task_scope.shutdown()

            return ConversationBinding(
                runner=conversation,
                provider=provider,
                background_tasks=task_scope,
                capabilities=binding_capabilities,
                resource_scope=ConversationBindingResourceScope(close_binding_resources),
                workspace_root=selected_config.cwd,
                workspace_mutation=runtime.workspace_mutation,
            )
        except BaseException:
            self._lsp_services.discard(lsp_service)
            await asyncio.shield(lsp_service.close())
            if task_scope is not None:
                await asyncio.shield(task_scope.shutdown())
            raise

    async def config_for_session_resume(
        self: CompositionRootMixin,
        session_id: str,
    ) -> AppConfig:
        """Select a safe application configuration for a persisted session.

        为持久化会话选择安全的应用配置.
        """

        if self._closed:
            raise RuntimeError("application composition is closed")
        summary = await self.session_summary_queries.get_session_summary(
            GetSessionSummaryRequest(session_id)
        )
        if not workspaces_match(summary.cwd, self.config.cwd):
            raise ConfigurationError("session does not belong to the application workspace")
        if (
            summary.sandbox_profile is not None
            and summary.sandbox_profile is not self.config.sandbox_profile
        ):
            raise ConfigurationError("session sandbox profile does not match the active profile")

        if summary.provider in self.config.providers:
            import neuro_code.application.ports.configuration as configuration_ports

            selected = configuration_ports.override_provider(
                self.config,
                provider=summary.provider,
                model=summary.model,
            )
        elif summary.context_affinity is None:
            selected = self.config
        else:
            raise ConfigurationError("session provider profile is unavailable")

        if (
            summary.context_affinity is not None
            and selected.provider.context_affinity != summary.context_affinity
        ):
            raise ConfigurationError("session provider affinity is unavailable")
        return selected


__all__ = ["CompositionBindingMixin"]
