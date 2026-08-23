"""Concrete process composition for Neuro Code.

定义 Neuro Code 的具体进程组合."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from neuro_code.application.checkpoints import WorkspaceCheckpointApplicationService
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
    BackgroundTaskSupervisor,
)
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.instructions import InstructionDiscovery
from neuro_code.application.ports.model import (
    CapabilityStatus,
    ModelCapability,
    ModelCapabilitySet,
    ModelProvider,
)
from neuro_code.application.ports.sandbox import LocalProcessSandbox
from neuro_code.application.ports.skills import SkillDiscovery
from neuro_code.application.ports.storage import SessionStore
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
from neuro_code.application.ports.workspace_changes import WorkspaceChangeObserver
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.providers.service import (
    ProviderChangeService,
    ProviderProfileController,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.sessions import (
    SessionApplicationService,
)
from neuro_code.application.sessions.binding import (
    ConversationBinding,
    ConversationBindingResourceScope,
)
from neuro_code.application.sessions.conversation import AgentConversation
from neuro_code.application.sessions.selection import (
    SessionSelectionController,
    SessionSelectionService,
)
from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipLifecycleService,
)
from neuro_code.application.sessions.subagent_queries import (
    SubagentRelationshipQueryService,
)
from neuro_code.application.sessions.summary import (
    GetSessionSummaryRequest,
    SessionSummaryQueryService,
)
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.tools.service import SessionToolOutputArtifactApplicationService
from neuro_code.application.web_fetch.service import WebFetchService
from neuro_code.application.web_search.service import WebSearchService
from neuro_code.application.workflows.plan_execution import (
    PlanExecutionController,
    PlanExecutionService,
)
from neuro_code.application.workflows.plan_scheduling import (
    PlanSchedulingController,
    PlanSchedulingService,
)
from neuro_code.application.workflows.session_task_execution import (
    QueuedPlanExecutionController,
    QueuedPlanExecutionService,
)
from neuro_code.application.workflows.subagent import (
    MAX_SUBAGENT_RESULT_BYTES,
    IsolatedSubagentExecutionService,
    ReadOnlySubagentApplicationService,
    SubagentExecutionService,
    SubagentExecutorFactory,
)
from neuro_code.application.workflows.subagent_capabilities import (
    MAX_SUBAGENT_CAPABILITY_STEPS,
    SubagentCapabilitySet,
)
from neuro_code.application.workflows.subagent_scheduler import (
    MAX_SUBAGENT_PARALLELISM,
    ScopedSubagentRuntimeFactory,
    SubagentScheduler,
)
from neuro_code.application.workflows.writable_subagent import (
    WritableSubagentApplicationService,
)
from neuro_code.application.worktrees import WorktreeApplicationService
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult
from neuro_code.infrastructure.background_tasks import LocalBackgroundTaskManager
from neuro_code.infrastructure.git.worktree import LocalGitWorktreeAdapter
from neuro_code.infrastructure.lsp.manager import LanguageServerManager
from neuro_code.infrastructure.persistence.checkpoint_artifacts import LocalCheckpointArtifactStore
from neuro_code.infrastructure.persistence.managed_worktrees import SqliteManagedWorktreeStore
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.persistence.workspace_checkpoints import (
    SqliteWorkspaceCheckpointStore,
)
from neuro_code.infrastructure.providers import create_routed_provider
from neuro_code.infrastructure.providers.hosted_web_search import (
    RoutedWebSearchBackendResolver,
)
from neuro_code.infrastructure.sandbox.linux_local_process import LinuxBubblewrapLocalProcessSandbox
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry
from neuro_code.infrastructure.tools.web_fetch import WebFetchTool
from neuro_code.infrastructure.tools.web_search import WebSearchTool
from neuro_code.infrastructure.tools.workspace_diff import WorkspaceMutationJournal
from neuro_code.infrastructure.web_fetch.local import LocalWebFetcher
from neuro_code.infrastructure.workspace.changes import (
    FilesystemWorkspaceChangeObserver,
    MultiRootWorkspaceChangeObserver,
)
from neuro_code.infrastructure.workspace.checkpoints import LocalWorkspaceStateAdapter
from neuro_code.infrastructure.workspace.instructions import FilesystemInstructionDiscovery
from neuro_code.infrastructure.workspace.paths import FilesystemWorkspaceIdentity, workspaces_match
from neuro_code.infrastructure.workspace.skills import FilesystemSkillDiscovery
from neuro_code.shared.errors import ConfigurationError, SandboxError

if TYPE_CHECKING:
    from neuro_code.configuration.app import AppConfig


ProviderFactory = Callable[["AppConfig", bool], ModelProvider]
LocalProcessSandboxFactory = Callable[[SandboxProfile, Path, Path], LocalProcessSandbox]
SessionStoreFactory = Callable[[Path], SessionStore]
BackgroundSupervisorFactory = Callable[[], BackgroundTaskSupervisor]
InstructionDiscoveryFactory = Callable[[], InstructionDiscovery]
SkillDiscoveryFactory = Callable[[], SkillDiscovery]
WorkspaceChangeObserverFactory = Callable[[], WorkspaceChangeObserver]


def _default_provider_factory(config: AppConfig, failover: bool) -> ModelProvider:
    return create_routed_provider(config, failover=failover)


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


def _default_local_process_sandbox_factory(
    profile: SandboxProfile,
    workspace: Path,
    state_dir: Path,
) -> LocalProcessSandbox:
    """Choose the canonical local-process launcher for one session binding.

    为一个会话绑定选择规范本地进程启动器.

    ``off`` preserves the existing owned-process bridge. Enabled profiles use
    the platform's child adapter on Linux, macOS, or Windows W3; unsupported
    platform/profile combinations fail closed. The controller is never
    re-executed inside a sandbox namespace.

    ``off`` 保留既有的受管进程桥接器.每个启用的 profile 都通过 Linux Bubblewrap、
    macOS Seatbelt 或 Windows W3 child adapter 创建边界;不支持的平台/profile
    组合失败关闭.controller 不会重新执行到沙箱中.
    """

    if not profile.enabled:
        return ProcessTreeLocalProcessSandbox()
    platform = _runtime_platform()
    if platform.startswith("linux"):
        return LinuxBubblewrapLocalProcessSandbox(profile, workspace, state_dir)
    if platform == "darwin":
        from neuro_code.infrastructure.sandbox.macos_local_process import (
            MacOSSeatbeltLocalProcessSandbox,
        )

        return MacOSSeatbeltLocalProcessSandbox(profile, workspace, state_dir)
    if platform.startswith("win"):
        return WindowsNativeLocalProcessSandbox(profile, workspace, state_dir)
    raise SandboxError(f"sandbox profile {profile.value!r} is not enforceable on {platform}")


def _runtime_platform() -> str:
    return sys.platform


def _default_session_store_factory(path: Path) -> SessionStore:
    return SqliteSessionStore(path)


def _default_background_supervisor_factory() -> BackgroundTaskSupervisor:
    return LocalBackgroundTaskManager()


def _default_instruction_discovery_factory() -> InstructionDiscovery:
    return FilesystemInstructionDiscovery()


def _default_skill_discovery_factory() -> SkillDiscovery:
    return FilesystemSkillDiscovery()


def _default_workspace_change_observer_factory() -> WorkspaceChangeObserver:
    return FilesystemWorkspaceChangeObserver()


class ApplicationComposition:
    """Own shared configuration, persistence, and conversation resources.

    管理共享的配置、持久化和会话资源."""

    def __init__(
        self,
        *,
        settings: ApplicationSettings,
        config: AppConfig,
        store: SessionStore,
        background_tasks: BackgroundTaskSupervisor,
        provider_factory: ProviderFactory,
        local_process_sandbox_factory: LocalProcessSandboxFactory = (
            _default_local_process_sandbox_factory
        ),
        instruction_discovery: InstructionDiscovery | None = None,
        skill_discovery: SkillDiscovery | None = None,
        workspace_change_observer_factory: WorkspaceChangeObserverFactory = (
            _default_workspace_change_observer_factory
        ),
    ) -> None:
        self.settings = settings
        self.config = config
        self.store = store
        self._session_service = SessionApplicationService(
            store,
            workspace_matcher=workspaces_match,
        )
        self._session_summary_queries = SessionSummaryQueryService(store)
        self.background_tasks = background_tasks
        self._provider_factory = provider_factory
        self._local_process_sandbox_factory = local_process_sandbox_factory
        self._instruction_discovery = (
            instruction_discovery
            if instruction_discovery is not None
            else _default_instruction_discovery_factory()
        )
        self._skill_discovery = (
            skill_discovery if skill_discovery is not None else _default_skill_discovery_factory()
        )
        self._workspace_change_observer_factory = workspace_change_observer_factory
        self._lsp_services: set[LanguageServerManager] = set()
        self._closed = False

    def create_local_process_sandbox(
        self,
        *,
        config: AppConfig | None = None,
    ) -> LocalProcessSandbox:
        """Create a composition-owned local process launcher for one config.

        为一个配置创建由组合根拥有的本地进程启动器.

        Bootstrap adapters use this narrow factory when a session-scoped
        process, such as an ACP stdio MCP server, starts outside a conversation
        binding. The launcher is still selected by the same composition path as
        Bash and background tasks.

        当会话范围的进程(例如 ACP stdio MCP server)在 conversation binding 之外
        启动时,bootstrap 适配器使用这个精简工厂.启动器仍由与 Bash 和后台任务
        相同的组合路径选择.
        """

        selected_config = config or self.config
        return self._local_process_sandbox_factory(
            selected_config.sandbox_profile,
            selected_config.cwd,
            selected_config.state_dir,
        )

    def create_worktree_service(self) -> WorktreeApplicationService:
        """Create the application-owned local Git worktree capability.

        The service is explicit and not automatically exposed to ordinary
        model-facing tools.  Its database and managed root are both owned by
        the configured state directory; callers must await ``initialize``
        before using lifecycle operations.

        创建应用拥有的本地 Git worktree 能力.该服务是显式的,不会自动暴露给普通模型工具.
        database 和 managed root 都属于配置的 state directory;调用方必须先 await
        ``initialize`` 再使用生命周期操作.
        """

        return WorktreeApplicationService(
            git=LocalGitWorktreeAdapter(hooks_directory=self.config.state_dir / "git-hooks"),
            store=SqliteManagedWorktreeStore(self.config.state_dir / "worktrees.db"),
            managed_root=self.config.state_dir / "worktrees",
        )

    def create_workspace_checkpoint_service(self) -> WorkspaceCheckpointApplicationService:
        """Create the internal managed-workspace checkpoint capability.

        The capability is intentionally explicit and is not registered as a
        model-facing tool.  Callers must await ``initialize`` before use.
        """

        git = LocalGitWorktreeAdapter(hooks_directory=self.config.state_dir / "git-hooks")
        return WorkspaceCheckpointApplicationService(
            git=git,
            workspace_git=git,
            worktrees=SqliteManagedWorktreeStore(self.config.state_dir / "worktrees.db"),
            state=LocalWorkspaceStateAdapter(git=git, workspace_git=git),
            checkpoints=SqliteWorkspaceCheckpointStore(self.config.state_dir / "checkpoints.db"),
            artifacts=LocalCheckpointArtifactStore(self.config.state_dir),
        )

    @classmethod
    async def open(
        cls,
        settings: ApplicationSettings,
        *,
        provider_factory: ProviderFactory = _default_provider_factory,
        local_process_sandbox_factory: LocalProcessSandboxFactory = (
            _default_local_process_sandbox_factory
        ),
        store_factory: SessionStoreFactory = _default_session_store_factory,
        background_supervisor_factory: BackgroundSupervisorFactory = _default_background_supervisor_factory,
        instruction_discovery_factory: InstructionDiscoveryFactory = _default_instruction_discovery_factory,
        skill_discovery_factory: SkillDiscoveryFactory = _default_skill_discovery_factory,
        workspace_change_observer_factory: WorkspaceChangeObserverFactory = (
            _default_workspace_change_observer_factory
        ),
    ) -> ApplicationComposition:
        background_tasks = background_supervisor_factory()
        try:
            from neuro_code.configuration.app import (
                load_config,
                override_provider,
                override_sandbox,
                pin_resumed_sandbox,
            )

            config = load_config(settings.cwd)
            config = override_sandbox(config, settings.sandbox)
            config = override_provider(
                config,
                provider=settings.provider,
                model=settings.model,
                base_url=settings.base_url,
            )
            store = store_factory(config.state_dir / "sessions.db")
            if settings.resume_id is not None:
                saved_profile = await store.peek_session_sandbox_profile(settings.resume_id)
                config = pin_resumed_sandbox(config, saved_profile)
            await store.initialize()
            return cls(
                settings=settings,
                config=config,
                store=store,
                background_tasks=background_tasks,
                provider_factory=provider_factory,
                local_process_sandbox_factory=local_process_sandbox_factory,
                instruction_discovery=instruction_discovery_factory(),
                skill_discovery=skill_discovery_factory(),
                workspace_change_observer_factory=workspace_change_observer_factory,
            )
        except BaseException:
            await asyncio.shield(background_tasks.shutdown())
            raise

    async def create_binding(
        self,
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
    ) -> ConversationBinding:
        if self._closed:
            raise RuntimeError("application composition is closed")
        selected_config = config or self.config
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
                redaction_values=selected_config.redaction_values(),
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
                                    redaction_values=selected_config.redaction_values(),
                                ),
                                redaction_values=selected_config.redaction_values(),
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
                                redaction_values=selected_config.redaction_values(),
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
                redaction_values=selected_config.redaction_values(),
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
                    redaction_values=selected_config.redaction_values(),
                    background_tasks=task_scope if enable_background_tasks else None,
                    instruction_tracker=tracker,
                    skill_tracker=skill_tracker,
                    client_file_system=client_file_system,
                    client_terminal=client_terminal,
                    output_artifact_store=FileToolOutputArtifactStore(
                        selected_config.state_dir / "tool-output",
                        redaction_values=selected_config.redaction_values(),
                    ),
                    workspace_change_journal=workspace_change_journal,
                    user_interaction=user_interaction,
                ),
                approver=approval_service,
                session_store=self.store,
                execution_budget=selected_execution_budget,
                reasoning_effort=reasoning_effort or self.settings.reasoning_effort,
                execution_control_mode=self.settings.execution_control_mode,
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
            )
        except BaseException:
            self._lsp_services.discard(lsp_service)
            await asyncio.shield(lsp_service.close())
            if task_scope is not None:
                await asyncio.shield(task_scope.shutdown())
            raise

    async def config_for_session_resume(self, session_id: str) -> AppConfig:
        """Select a safe application configuration for a persisted session.

        为持久化会话选择安全的应用配置."""

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
            from neuro_code.configuration.app import override_provider

            selected = override_provider(
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

    @property
    def session_service(self) -> SessionApplicationService:
        """Return the application session use-case facade for this composition.

        返回当前组合使用的应用会话用例门面."""

        return self._session_service

    @property
    def session_summary_queries(self) -> SessionSummaryQueryService:
        """Return the canonical read-only session-summary query owner.

        返回规范的只读会话摘要查询 owner.
        """

        return self._session_summary_queries

    def create_tool_output_artifact_service(
        self,
        *,
        config: AppConfig | None = None,
    ) -> SessionToolOutputArtifactApplicationService:
        """Create the session-scoped tool-output read boundary for an interface.

        为入站接口创建会话作用域的工具输出读取边界.

        The returned service exposes only event-associated opaque handles.  The
        composition root supplies the existing state directory and redaction
        boundary; interfaces never receive a filesystem path or artifact store.

        返回的服务只暴露与事件关联的不透明句柄.组合根提供现有状态目录和脱敏边界;
        入站接口不会收到文件系统路径或 artifact 存储器.
        """

        selected_config = config or self.config
        reader = FileToolOutputArtifactStore(
            selected_config.state_dir / "tool-output",
            redaction_values=selected_config.redaction_values(),
        )
        return SessionToolOutputArtifactApplicationService(
            self.store,
            reader,
            garbage_collector=reader,
        )

    def bind_provider_controller(
        self,
        controller: ProviderProfileController,
    ) -> ProviderChangeService:
        """Bind the existing profile owner to the ChangeProvider application seam.

        The controller continues to own provider construction, conversation
        replacement, turn locking, and background-task cleanup.  The returned
        facade is a non-owning inbound application boundary for interfaces.

        将现有配置档案所有者绑定到 ChangeProvider 应用接缝. 控制器继续负责 Provider 创建、会话替换、回合锁和后台任务清理.
        """

        return ProviderChangeService(controller)

    def bind_session_selection_controller(
        self,
        controller: SessionSelectionController,
    ) -> SessionSelectionService:
        """Bind session listing, selection, and rename to an inbound seam.

        会话列表、选择和重命名绑定到入站应用接缝.

        The profile controller remains the owner of locking, binding
        replacement, workspace validation, and resume lifecycle.

        profile 控制器仍负责锁、绑定替换、工作区校验和恢复生命周期.
        """

        return SessionSelectionService(controller)

    def bind_plan_execution_controller(
        self,
        controller: PlanExecutionController,
    ) -> PlanExecutionService:
        """Bind the existing plan owner to the ExecutePlan application seam.

        The controller continues to own the turn lock, plan validation,
        SessionTask lifecycle, permissions, event sink, and cancellation.
        The returned facade is only an inbound typed intent boundary.

        将现有计划所有者绑定到 ExecutePlan 应用接缝. 控制器继续负责回合锁、计划验证、任务生命周期、权限、事件和取消.
        """

        return PlanExecutionService(controller)

    def bind_plan_scheduling_controller(
        self,
        controller: PlanSchedulingController,
    ) -> PlanSchedulingService:
        """Bind the existing plan owner to the scheduling application seam.

        将现有计划所有者绑定到计划调度应用接缝."""

        return PlanSchedulingService(controller)

    def bind_queued_plan_execution_controller(
        self,
        controller: QueuedPlanExecutionController,
    ) -> QueuedPlanExecutionService:
        """Bind the existing queued-task owner to its application seam.

        将现有排队任务所有者绑定到对应的应用接缝."""

        return QueuedPlanExecutionService(controller)

    def bind_subagent_executor(
        self,
        executor_factory: SubagentExecutorFactory,
        *,
        _test_only: bool = False,
    ) -> SubagentExecutionService:
        """Bind the legacy executor seam for tests/internal compatibility only.

        This seam accepts an arbitrary executor and therefore is not a
        capability boundary.  Normal production entrypoints must use the
        capability-aware read-only service or scheduler.  The explicit flag
        keeps the compatibility API available without presenting it as a
        production security guarantee.

        仅为测试/内部兼容绑定旧版执行器接缝. 该接缝接受任意执行器,因此不是 capability
        边界.正常生产入口必须使用 capability-aware 只读服务或 scheduler.
        """

        if _test_only is not True:
            raise ConfigurationError("legacy subagent executor binding is test-only")
        return SubagentExecutionService(self.store, executor_factory)

    def subagent_global_policy(self) -> SubagentCapabilitySet:
        """Return the one composition-owned child capability ceiling.

        The parent binding remains the primary authority.  This manifest is
        only the process-wide ceiling used by both the canonical scheduler and
        the explicit read-only workflow.
        """

        config = self.config
        tools = default_tool_registry(
            config.sandbox_profile,
            enable_background_tasks=True,
        )
        return SubagentCapabilitySet.from_runtime(
            tool_names=tools.names(),
            provider_tool_names=config.provider.builtin_tools,
            cwd=config.cwd,
            sandbox_profile=config.sandbox_profile,
            enable_background_tasks=True,
            max_steps=MAX_SUBAGENT_CAPABILITY_STEPS,
        )

    def create_read_only_subagent_service(
        self,
        *,
        timeout_seconds: float = 120.0,
    ) -> IsolatedSubagentExecutionService:
        """Create an explicit read-only subagent application workflow.

        创建一个显式的只读子代理应用工作流.

        The concrete child runtime is assembled here so capability selection
        remains a composition-root concern.  No caller is automatically
        scheduled and the returned service is not used by normal sessions.
        具体子运行时在组合根创建,确保能力选择属于组合根职责. 不会自动调度调用方,
        返回的服务也不会被普通会话使用.
        """

        from neuro_code.bootstrap.subagent import CompositionReadOnlySubagentRuntimeFactory

        factory = CompositionReadOnlySubagentRuntimeFactory(self)
        return IsolatedSubagentExecutionService(
            self.store,
            factory,
            global_policy=self.subagent_global_policy(),
            requested_capability_factory=factory.requested_capabilities,
            timeout_seconds=timeout_seconds,
        )

    def create_writable_subagent_service(
        self,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> WritableSubagentApplicationService:
        """Create the explicit internal serialized writable-subagent slice.

        The returned service is not connected to CLI, TUI, ACP, or the normal
        ``/subagent`` command.  Its parent authority is captured from the
        actual active ``ConversationBinding`` at this composition boundary.
        """

        from neuro_code.bootstrap.subagent import CompositionWritableSubagentRuntimeFactory

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("writable subagent parent binding is required")
        if not isinstance(parent_binding.capabilities, SubagentCapabilitySet):
            raise ConfigurationError(
                "writable subagent parent binding capability metadata is missing"
            )
        parent_session_id = parent_binding.runner.session_id
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ConfigurationError("writable subagent parent binding session identity is missing")

        worktrees = self.create_worktree_service()
        checkpoints = self.create_workspace_checkpoint_service()
        factory = CompositionWritableSubagentRuntimeFactory(self)
        return WritableSubagentApplicationService(
            self.store,
            cast(WritableSubagentLeaseStore, self.store),
            worktrees,
            checkpoints,
            factory,
            parent_binding=parent_binding,
            global_policy=self.subagent_global_policy(),
            timeout_seconds=timeout_seconds,
        )

    def create_subagent_scheduler(
        self,
        factory: ScopedSubagentRuntimeFactory,
        *,
        parent_capabilities: SubagentCapabilitySet | None = None,
        parent_binding: ConversationBinding | None = None,
        global_policy: SubagentCapabilitySet | None = None,
        max_parallel: int = MAX_SUBAGENT_PARALLELISM,
        max_retries: int = 0,
        timeout_seconds: float | None = None,
    ) -> SubagentScheduler:
        """Create an opt-in scheduler with explicit capability authorities."""

        if parent_binding is not None:
            if parent_binding.capabilities is None:
                raise ConfigurationError("parent binding capability metadata is missing")
            if (
                parent_capabilities is not None
                and parent_capabilities != parent_binding.capabilities
            ):
                raise ConfigurationError("parent capability metadata conflicts with binding")
            parent_capabilities = parent_binding.capabilities
        if parent_capabilities is None:
            raise ConfigurationError("parent subagent capability metadata is required")
        owned_global_policy = self.subagent_global_policy()
        if global_policy is not None and global_policy != owned_global_policy:
            raise ConfigurationError("global subagent capability policy is not composition-owned")

        return SubagentScheduler(
            factory,
            parent_capabilities=parent_capabilities,
            global_policy=owned_global_policy,
            max_parallel=max_parallel,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )

    def create_read_only_subagent_application_service(
        self,
        *,
        timeout_seconds: float = 120.0,
        max_result_bytes: int = MAX_SUBAGENT_RESULT_BYTES,
    ) -> ReadOnlySubagentApplicationService:
        """Create the safe, explicit read-only subagent result boundary.

        创建安全且明确的只读子代理结果边界.

        The returned application service projects the child run into bounded,
        redacted metadata and response text.  It does not add anything to the
        parent transcript or expose child events to the caller.
        返回的应用服务将子运行投影为有界、脱敏的元数据和响应文本,不会追加父 transcript 或暴露子事件.
        """

        return ReadOnlySubagentApplicationService(
            self.create_read_only_subagent_service(timeout_seconds=timeout_seconds),
            redaction_values=self.config.redaction_values(),
            max_result_bytes=max_result_bytes,
        )

    def create_subagent_relationship_query_service(self) -> SubagentRelationshipQueryService:
        """Create the read-only parent/child relationship projection service.

        创建只读父子子代理关系投影服务.

        The service reads bounded lifecycle metadata through the session store;
        it never starts, resumes, forks, or deletes a child session.
        该服务通过会话存储读取有界生命周期元数据,不会启动、恢复、分叉或删除子会话.
        """

        return SubagentRelationshipQueryService(self.store)

    def create_subagent_relationship_lifecycle_service(
        self,
    ) -> SubagentRelationshipLifecycleService:
        """Create the explicit parent-owned child lifecycle boundary.

        创建显式且由父会话拥有的子会话生命周期边界.

        The returned service validates the existing relationship before
        delegating resume preparation, fork, or delete.  It never starts a
        model turn or exposes SQLite to an interface.
        返回的服务会在委托恢复准备、分叉或删除前校验既有关联,不会启动模型回合或向接口暴露 SQLite.
        """

        return SubagentRelationshipLifecycleService(self.store, self.session_service)

    @property
    def instruction_result(self) -> InstructionDiscoveryResult | None:
        """Return a fresh instruction discovery result for the application workspace.

        This uses the same ``InstructionDiscovery`` adapter that per-binding
        trackers use, but with a fresh target of the application CWD.  CLI
        ``inspect`` uses this to render what would be discovered at the
        workspace root level.

        返回一个新的指令发现结果用于该应用工作区.
        """
        return self._instruction_discovery.discover(self.config.cwd, target=self.config.cwd)

    @property
    def skill_result(self) -> SkillDiscoveryResult | None:
        """Return a fresh skill discovery result for the application workspace.

        This uses the same ``SkillDiscovery`` adapter that per-binding
        trackers use.  CLI ``inspect`` uses this to render what would be
        discovered at the workspace root level.

        返回一个新的技能发现结果用于该应用工作区.
        """
        return self._skill_discovery.discover(self.config.cwd)

    @staticmethod
    def default_instruction_discovery() -> InstructionDiscovery:
        """Return the default instruction discovery adapter.

        This is the same factory default that ``ApplicationComposition.open()``
        uses when no explicit ``instruction_discovery_factory`` is provided.
        CLI ``inspect`` calls this so that it uses the same discovery
        implementation and port contract as a full application session,
        without the overhead of opening a store or background task scope.

        返回默认的指令发现适配器. 该工厂与完整 ApplicationComposition 使用的默认实现和端口契约保持一致.
        """
        return _default_instruction_discovery_factory()

    @staticmethod
    def default_skill_discovery() -> SkillDiscovery:
        """Return the default skill discovery adapter.

        This is the same factory default that ``ApplicationComposition.open()``
        uses when no explicit ``skill_discovery_factory`` is provided.
        CLI ``inspect`` calls this so that it uses the same discovery
        implementation and port contract as a full application session.

        返回默认的技能发现适配器,与完整应用会话使用的实现保持一致.
        """
        return _default_skill_discovery_factory()

    def rediscover_instructions(self, cwd: Path | None = None) -> InstructionDiscoveryResult:
        """Re-run instruction discovery, detecting changes since the last pass.

        重新运行指令发现,并检测上一次发现之后的变化."""
        workspace = cwd or self.config.cwd
        return self._instruction_discovery.discover(workspace, target=workspace)

    def rediscover_skills(self, cwd: Path | None = None) -> SkillDiscoveryResult:
        """Re-run skill discovery, detecting changes since the last pass.

        重新运行技能发现,并检测上一次发现之后的变化."""
        workspace = cwd or self.config.cwd
        return self._skill_discovery.discover(workspace, target=workspace)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        lsp_services = tuple(self._lsp_services)
        self._lsp_services.clear()
        await asyncio.gather(
            *(service.close() for service in lsp_services),
            return_exceptions=True,
        )
        await self.background_tasks.shutdown()


__all__ = [
    "ApplicationComposition",
    "BackgroundSupervisorFactory",
    "InstructionDiscoveryFactory",
    "LocalProcessSandboxFactory",
    "ProviderFactory",
    "SessionStoreFactory",
    "SkillDiscoveryFactory",
    "WorkspaceChangeObserverFactory",
]
