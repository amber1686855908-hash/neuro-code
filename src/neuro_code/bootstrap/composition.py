"""Concrete process composition for Neuro Code."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.adapters.instruction_discovery import FilesystemInstructionDiscovery
from neuro_code.adapters.sandbox import create_shell_sandbox, enforce_configured_sandbox
from neuro_code.adapters.skill_discovery import FilesystemSkillDiscovery
from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.background_tasks import BackgroundTaskSupervisor
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.instructions import InstructionDiscovery
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.sandbox import ShellSandbox
from neuro_code.application.ports.skills import SkillDiscovery
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.application.ports.workspace_changes import WorkspaceChangeObserver
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.conversation import AgentConversation
from neuro_code.application.runtime.instruction_tracker import InstructionTracker
from neuro_code.application.runtime.profile_conversation import ConversationBinding
from neuro_code.application.runtime.skill_tracker import SkillTracker
from neuro_code.application.settings import ApplicationSettings
from neuro_code.domain.instructions import InstructionDiscoveryResult
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.skills import SkillDiscoveryResult
from neuro_code.permissions import (
    PermissionEffect,
    PermissionManager,
    PermissionRule,
)
from neuro_code.providers import create_routed_provider
from neuro_code.shared.errors import ConfigurationError
from neuro_code.tools import default_tool_registry
from neuro_code.workspace import FilesystemWorkspaceIdentity, workspaces_match
from neuro_code.workspace_changes import (
    FilesystemWorkspaceChangeObserver,
    MultiRootWorkspaceChangeObserver,
)

if TYPE_CHECKING:
    from neuro_code.config import AppConfig


ProviderFactory = Callable[["AppConfig", bool], ModelProvider]
ShellSandboxFactory = Callable[[SandboxProfile, Path, Path], ShellSandbox | None]
ProcessSandboxEnforcer = Callable[[SandboxProfile, Path, Path, Sequence[str]], None]
SessionStoreFactory = Callable[[Path], SessionStore]
BackgroundSupervisorFactory = Callable[[], BackgroundTaskSupervisor]
InstructionDiscoveryFactory = Callable[[], InstructionDiscovery]
SkillDiscoveryFactory = Callable[[], SkillDiscovery]
WorkspaceChangeObserverFactory = Callable[[], WorkspaceChangeObserver]


def _default_provider_factory(config: AppConfig, failover: bool) -> ModelProvider:
    return create_routed_provider(config, failover=failover)


def _default_shell_sandbox_factory(
    profile: SandboxProfile,
    cwd: Path,
    state_dir: Path,
) -> ShellSandbox | None:
    return create_shell_sandbox(profile, cwd, state_dir)


def _default_process_sandbox_enforcer(
    profile: SandboxProfile,
    cwd: Path,
    state_dir: Path,
    launch_command: Sequence[str],
) -> None:
    enforce_configured_sandbox(profile, cwd, state_dir, launch_command)


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
    """Own shared configuration, persistence, and conversation resources."""

    def __init__(
        self,
        *,
        settings: ApplicationSettings,
        config: AppConfig,
        store: SessionStore,
        background_tasks: BackgroundTaskSupervisor,
        provider_factory: ProviderFactory,
        shell_sandbox_factory: ShellSandboxFactory,
        instruction_discovery: InstructionDiscovery | None = None,
        skill_discovery: SkillDiscovery | None = None,
        workspace_change_observer_factory: WorkspaceChangeObserverFactory = (
            _default_workspace_change_observer_factory
        ),
    ) -> None:
        self.settings = settings
        self.config = config
        self.store = store
        self.background_tasks = background_tasks
        self._provider_factory = provider_factory
        self._shell_sandbox_factory = shell_sandbox_factory
        self._instruction_discovery = (
            instruction_discovery
            if instruction_discovery is not None
            else _default_instruction_discovery_factory()
        )
        self._skill_discovery = (
            skill_discovery if skill_discovery is not None else _default_skill_discovery_factory()
        )
        self._workspace_change_observer_factory = workspace_change_observer_factory
        self._closed = False

    @classmethod
    async def open(
        cls,
        settings: ApplicationSettings,
        *,
        provider_factory: ProviderFactory = _default_provider_factory,
        shell_sandbox_factory: ShellSandboxFactory = _default_shell_sandbox_factory,
        process_sandbox_enforcer: ProcessSandboxEnforcer = _default_process_sandbox_enforcer,
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
            from neuro_code.config import (
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
            if config.sandbox_profile.enabled and not settings.launch_command:
                raise ValueError("a launch command is required for an enabled process sandbox")
            process_sandbox_enforcer(
                config.sandbox_profile,
                config.cwd,
                config.state_dir,
                settings.launch_command,
            )
            await store.initialize()
            return cls(
                settings=settings,
                config=config,
                store=store,
                background_tasks=background_tasks,
                provider_factory=provider_factory,
                shell_sandbox_factory=shell_sandbox_factory,
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
    ) -> ConversationBinding:
        if self._closed:
            raise RuntimeError("application composition is closed")
        selected_config = config or self.config
        tools = default_tool_registry(
            selected_config.sandbox_profile,
            enable_background_tasks=True,
            client_file_system=client_file_system,
            client_terminal=client_terminal,
        )
        for tool in additional_tools:
            tools.register(tool)
        task_scope = self.background_tasks.open_scope()
        try:
            provider = self._provider_factory(selected_config, self.settings.failover)
            shell_sandbox = self._shell_sandbox_factory(
                selected_config.sandbox_profile,
                selected_config.cwd,
                selected_config.state_dir,
            )
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

            permissions = PermissionManager(
                mode=self.settings.permission_mode,
                rules=(
                    *self.settings.permission_rules,
                    *(
                        PermissionRule(PermissionEffect.ASK, tool.definition.name)
                        for tool in additional_tools
                    ),
                ),
                interactive=approver is not None,
            )
            workspace_change_observer = self._workspace_change_observer_factory()
            if additional_workspace_roots:
                workspace_change_observer = MultiRootWorkspaceChangeObserver(
                    workspace_change_observer,
                    additional_workspace_roots,
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
                    shell_sandbox=shell_sandbox,
                    protected_environment_variables=(
                        selected_config.protected_environment_variables
                    ),
                    redaction_values=selected_config.redaction_values(),
                    background_tasks=task_scope,
                    instruction_tracker=tracker,
                    skill_tracker=skill_tracker,
                    client_file_system=client_file_system,
                    client_terminal=client_terminal,
                ),
                approver=approver,
                session_store=self.store,
                max_steps=self.settings.max_steps,
                reasoning_effort=reasoning_effort or self.settings.reasoning_effort,
                execution_control_mode=self.settings.execution_control_mode,
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
            return ConversationBinding(conversation, provider, task_scope)
        except BaseException:
            await asyncio.shield(task_scope.shutdown())
            raise

    async def config_for_session_resume(self, session_id: str) -> AppConfig:
        """Select a safe application configuration for a persisted session."""

        if self._closed:
            raise RuntimeError("application composition is closed")
        summary = await self.store.get_session(session_id)
        if not workspaces_match(summary.cwd, self.config.cwd):
            raise ConfigurationError("session does not belong to the application workspace")
        if (
            summary.sandbox_profile is not None
            and summary.sandbox_profile is not self.config.sandbox_profile
        ):
            raise ConfigurationError("session sandbox profile does not match the active profile")

        if summary.provider in self.config.providers:
            from neuro_code.config import override_provider

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
    def instruction_result(self) -> InstructionDiscoveryResult | None:
        """Return a fresh instruction discovery result for the application workspace.

        This uses the same ``InstructionDiscovery`` adapter that per-binding
        trackers use, but with a fresh target of the application CWD.  CLI
        ``inspect`` uses this to render what would be discovered at the
        workspace root level.
        """
        return self._instruction_discovery.discover(self.config.cwd, target=self.config.cwd)

    @property
    def skill_result(self) -> SkillDiscoveryResult | None:
        """Return a fresh skill discovery result for the application workspace.

        This uses the same ``SkillDiscovery`` adapter that per-binding
        trackers use.  CLI ``inspect`` uses this to render what would be
        discovered at the workspace root level.
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
        """
        return _default_instruction_discovery_factory()

    @staticmethod
    def default_skill_discovery() -> SkillDiscovery:
        """Return the default skill discovery adapter.

        This is the same factory default that ``ApplicationComposition.open()``
        uses when no explicit ``skill_discovery_factory`` is provided.
        CLI ``inspect`` calls this so that it uses the same discovery
        implementation and port contract as a full application session.
        """
        return _default_skill_discovery_factory()

    def rediscover_instructions(self, cwd: Path | None = None) -> InstructionDiscoveryResult:
        """Re-run instruction discovery, detecting changes since the last pass."""
        workspace = cwd or self.config.cwd
        return self._instruction_discovery.discover(workspace, target=workspace)

    def rediscover_skills(self, cwd: Path | None = None) -> SkillDiscoveryResult:
        """Re-run skill discovery, detecting changes since the last pass."""
        workspace = cwd or self.config.cwd
        return self._skill_discovery.discover(workspace, target=workspace)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.background_tasks.shutdown()


__all__ = [
    "ApplicationComposition",
    "BackgroundSupervisorFactory",
    "InstructionDiscoveryFactory",
    "ProcessSandboxEnforcer",
    "ProviderFactory",
    "SessionStoreFactory",
    "ShellSandboxFactory",
    "SkillDiscoveryFactory",
    "WorkspaceChangeObserverFactory",
]
