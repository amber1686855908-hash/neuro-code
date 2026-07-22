from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.adapters.instruction_discovery import FilesystemInstructionDiscovery
from neuro_code.adapters.sandbox import create_shell_sandbox, enforce_configured_sandbox
from neuro_code.adapters.skill_discovery import FilesystemSkillDiscovery
from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.config import (
    AppConfig,
    load_config,
    override_provider,
    override_sandbox,
    pin_resumed_sandbox,
)
from neuro_code.domain.instructions import InstructionDiscoveryResult
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.skills import SkillDiscoveryResult
from neuro_code.errors import ConfigurationError
from neuro_code.permissions import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)
from neuro_code.ports.approval import PermissionApprover
from neuro_code.ports.background_tasks import BackgroundTaskSupervisor
from neuro_code.ports.instructions import InstructionDiscovery
from neuro_code.ports.model import ModelProvider
from neuro_code.ports.sandbox import ShellSandbox
from neuro_code.ports.skills import SkillDiscovery
from neuro_code.ports.tools import Tool, ToolContext
from neuro_code.providers import create_routed_provider
from neuro_code.runtime import AgentConversation, AgentRuntime, ConversationBinding
from neuro_code.runtime.instruction_tracker import InstructionTracker
from neuro_code.runtime.skill_tracker import SkillTracker
from neuro_code.tools import default_tool_registry
from neuro_code.workspace import workspaces_match

ProviderFactory = Callable[[AppConfig, bool], ModelProvider]
ShellSandboxFactory = Callable[[SandboxProfile, Path, Path], ShellSandbox | None]
ProcessSandboxEnforcer = Callable[[SandboxProfile, Path, Path, Sequence[str]], None]
SessionStoreFactory = Callable[[Path], SqliteSessionStore]
BackgroundSupervisorFactory = Callable[[], BackgroundTaskSupervisor]
InstructionDiscoveryFactory = Callable[[], InstructionDiscovery]
SkillDiscoveryFactory = Callable[[], SkillDiscovery]


def _default_provider_factory(config: AppConfig, failover: bool) -> ModelProvider:
    return create_routed_provider(config, failover=failover)


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """Interface-neutral settings for composing one Neuro Code process."""

    cwd: Path | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    sandbox: str | None = None
    failover: bool = True
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    permission_rules: tuple[PermissionRule, ...] = ()
    max_steps: int = 24
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    resume_id: str | None = None
    launch_command: tuple[str, ...] = ()


class ApplicationComposition:
    """Own shared configuration, persistence, and conversation resources."""

    def __init__(
        self,
        *,
        settings: ApplicationSettings,
        config: AppConfig,
        store: SqliteSessionStore,
        background_tasks: BackgroundTaskSupervisor,
        provider_factory: ProviderFactory,
        shell_sandbox_factory: ShellSandboxFactory,
        instruction_discovery: InstructionDiscovery | None = None,
        skill_discovery: SkillDiscovery | None = None,
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
            else FilesystemInstructionDiscovery()
        )
        self._skill_discovery = (
            skill_discovery if skill_discovery is not None else FilesystemSkillDiscovery()
        )
        self._closed = False

    @classmethod
    async def open(
        cls,
        settings: ApplicationSettings,
        *,
        provider_factory: ProviderFactory = _default_provider_factory,
        shell_sandbox_factory: ShellSandboxFactory = create_shell_sandbox,
        process_sandbox_enforcer: ProcessSandboxEnforcer = enforce_configured_sandbox,
        store_factory: SessionStoreFactory = SqliteSessionStore,
        background_supervisor_factory: BackgroundSupervisorFactory = (LocalBackgroundTaskManager),
        instruction_discovery_factory: InstructionDiscoveryFactory = FilesystemInstructionDiscovery,
        skill_discovery_factory: SkillDiscoveryFactory = FilesystemSkillDiscovery,
    ) -> ApplicationComposition:
        background_tasks = background_supervisor_factory()
        try:
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
    ) -> ConversationBinding:
        if self._closed:
            raise RuntimeError("application composition is closed")
        selected_config = config or self.config
        tools = default_tool_registry(
            selected_config.sandbox_profile,
            enable_background_tasks=True,
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
            runtime = AgentRuntime(
                provider=provider,
                tools=tools,
                permissions=permissions,
                tool_context=ToolContext(
                    selected_config.cwd,
                    sandbox_profile=selected_config.sandbox_profile,
                    shell_sandbox=shell_sandbox,
                    protected_environment_variables=(
                        selected_config.protected_environment_variables
                    ),
                    background_tasks=task_scope,
                    instruction_tracker=tracker,
                    skill_tracker=skill_tracker,
                ),
                approver=approver,
                session_store=self.store,
                max_steps=self.settings.max_steps,
                reasoning_effort=reasoning_effort or self.settings.reasoning_effort,
                instruction_provider=instruction_provider,
                skill_provider=skill_provider,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=self.store,
                cwd=selected_config.cwd,
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
        return FilesystemInstructionDiscovery()

    @staticmethod
    def default_skill_discovery() -> SkillDiscovery:
        """Return the default skill discovery adapter.

        This is the same factory default that ``ApplicationComposition.open()``
        uses when no explicit ``skill_discovery_factory`` is provided.
        CLI ``inspect`` calls this so that it uses the same discovery
        implementation and port contract as a full application session.
        """
        return FilesystemSkillDiscovery()

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
    "ApplicationSettings",
    "BackgroundSupervisorFactory",
    "InstructionDiscoveryFactory",
    "ProcessSandboxEnforcer",
    "ProviderFactory",
    "SessionStoreFactory",
    "ShellSandboxFactory",
    "SkillDiscoveryFactory",
]
