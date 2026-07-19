from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.adapters.sandbox import create_shell_sandbox, enforce_configured_sandbox
from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.config import (
    AppConfig,
    load_config,
    override_provider,
    override_sandbox,
    pin_resumed_sandbox,
)
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.errors import ConfigurationError
from neuro_code.permissions import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)
from neuro_code.ports.approval import PermissionApprover
from neuro_code.ports.background_tasks import BackgroundTaskSupervisor
from neuro_code.ports.model import ModelProvider
from neuro_code.ports.sandbox import ShellSandbox
from neuro_code.ports.tools import Tool, ToolContext
from neuro_code.providers import create_routed_provider
from neuro_code.runtime import AgentConversation, AgentRuntime, ConversationBinding
from neuro_code.tools import default_tool_registry
from neuro_code.workspace import workspaces_match

ProviderFactory = Callable[[AppConfig, bool], ModelProvider]
ShellSandboxFactory = Callable[[SandboxProfile, Path, Path], ShellSandbox | None]
ProcessSandboxEnforcer = Callable[[SandboxProfile, Path, Path, Sequence[str]], None]
SessionStoreFactory = Callable[[Path], SqliteSessionStore]
BackgroundSupervisorFactory = Callable[[], BackgroundTaskSupervisor]


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
    ) -> None:
        self.settings = settings
        self.config = config
        self.store = store
        self.background_tasks = background_tasks
        self._provider_factory = provider_factory
        self._shell_sandbox_factory = shell_sandbox_factory
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
                ),
                approver=approver,
                session_store=self.store,
                max_steps=self.settings.max_steps,
                reasoning_effort=reasoning_effort or self.settings.reasoning_effort,
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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.background_tasks.shutdown()


__all__ = [
    "ApplicationComposition",
    "ApplicationSettings",
    "BackgroundSupervisorFactory",
    "ProcessSandboxEnforcer",
    "ProviderFactory",
    "SessionStoreFactory",
    "ShellSandboxFactory",
]
