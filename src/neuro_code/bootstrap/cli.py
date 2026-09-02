"""Concrete CLI-side bootstrap services.

This module owns the concrete services selected for inbound CLI and TUI
commands. Process entrypoints remain in ``bootstrap.entrypoints`` and ACP
composition adapters live in ``bootstrap.acp``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from neuro_code.application.acp.contracts import AcpSessionMetadata
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.ports.configuration import AppConfig, override_provider
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.providers.contracts import ProviderOption
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.lifecycle import RenameSessionRequest
from neuro_code.application.sessions.profile_conversation import ProfileConversationController
from neuro_code.application.sessions.service import ResumeSessionRequest, SessionApplicationService
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.tools.service import SessionToolOutputArtifactApplicationService
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.bootstrap.acp import (
    _BootstrapMcpToolFactory,
    _BootstrapWorkspaceValidator,
    _CompositionAcpBindingFactory,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.bootstrap.configuration import load_config
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.sessions.search import SessionSearchHit
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.infrastructure.persistence.rust_session import load_rust_session
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.persistence.ui_preferences import JsonUiPreferencesStore
from neuro_code.infrastructure.providers.binding import (
    resolve_provider_binding,
    socks_support_available,
)
from neuro_code.infrastructure.providers.catalog_cache import PersistentProviderCatalog
from neuro_code.infrastructure.providers.provider_catalog import HttpProviderCatalog
from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore
from neuro_code.infrastructure.workspace.paths import workspaces_match
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
    from neuro_code.domain.workspace.skills import SkillDiscoveryResult
    from neuro_code.infrastructure.persistence.rust_session import RustSessionImport


class BootstrapCliServices:
    """Concrete infrastructure choices used by the CLI command dispatcher.

    表示 CLI 命令分发器使用的具体基础设施选择."""

    async def open_application(self, settings: ApplicationSettings) -> ApplicationComposition:
        return await ApplicationComposition.open(settings)

    def load_config(self, cwd: Path | None) -> AppConfig:
        return load_config(cwd)

    def discover_instructions(self, cwd: Path) -> InstructionDiscoveryResult:
        return ApplicationComposition.default_instruction_discovery().discover(cwd)

    def discover_skills(self, cwd: Path) -> SkillDiscoveryResult:
        return ApplicationComposition.default_skill_discovery().discover(cwd)

    async def create_session_store(self, config: AppConfig) -> SessionStore:
        store = SqliteSessionStore(config.state_dir / "sessions.db")
        await store.initialize()
        return store

    def create_tool_output_artifact_service(
        self,
        config: AppConfig,
        store: SessionStore,
    ) -> SessionToolOutputArtifactApplicationService:
        """Build the CLI's session-scoped bounded artifact read boundary.

        构建 CLI 使用的会话作用域有界 artifact 读取边界.

        The CLI receives only the application service; the state directory and
        filesystem reader remain bootstrap-owned infrastructure details.

        CLI 只接收应用服务;状态目录和文件读取器仍属于 bootstrap 基础设施细节.
        """

        reader = FileToolOutputArtifactStore(
            config.state_dir / "tool-output",
            redaction_values=config.redaction_values(os.environ),
        )
        return SessionToolOutputArtifactApplicationService(
            store,
            reader,
            garbage_collector=reader,
        )

    async def load_rust_session(self, source: Path) -> RustSessionImport:
        return await run_blocking(load_rust_session, source)

    def workspaces_match(self, left: str | Path, right: str | Path) -> bool:
        return workspaces_match(left, right)

    def create_acp_service(self, application: ApplicationComposition) -> AcpApplicationService:
        """Assemble ACP's narrow application service for one open process.

        为一个已打开的进程组装 ACP 精简应用服务."""
        config = application.config
        return AcpApplicationService(
            metadata=AcpSessionMetadata(
                workspace=config.cwd,
                protected_environment_variables=config.protected_environment_variables,
                context_window_tokens=config.provider.context_window_tokens,
            ),
            store=application.store,
            bindings=_CompositionAcpBindingFactory(application),
            mcp_tools=_BootstrapMcpToolFactory(
                local_process_sandbox_factory=lambda: application.create_local_process_sandbox(
                    config=config,
                ),
                sandbox_profile=config.sandbox_profile,
            ),
            workspace=_BootstrapWorkspaceValidator(config.cwd, config.sandbox_profile),
            sessions=application.session_service,
            summary_queries=application.session_summary_queries,
            artifacts=application.create_tool_output_artifact_service(config=config),
            subagents=application.create_read_only_subagent_application_service(),
            subagent_lifecycle=application.create_subagent_relationship_lifecycle_service(),
        )

    async def run_acp(
        self,
        args: argparse.Namespace,
        settings: ApplicationSettings,
    ) -> int:
        """Open the process composition and serve ACP with injected dependencies.

        打开进程组合,使用注入的依赖提供 ACP 服务."""
        from neuro_code.interfaces.acp.agent import serve_acp, serve_acp_websocket

        application = await self.open_application(settings)
        try:
            service = self.create_acp_service(application)
            if getattr(args, "transport", "stdio") == "websocket":
                await serve_acp_websocket(
                    service,
                    host=getattr(args, "host", "127.0.0.1"),
                    port=getattr(args, "port", 0),
                )
            else:
                await serve_acp(service)
            return 0
        finally:
            await asyncio.shield(application.close())

    async def run_tui(
        self,
        args: argparse.Namespace,
        settings: ApplicationSettings,
    ) -> int:
        try:
            from neuro_code.interfaces.tui.app import NeuroCodeApp
            from neuro_code.interfaces.tui.interaction import TuiUserInteraction
            from neuro_code.interfaces.tui.screens.provider import ProviderSetupApp
            from neuro_code.interfaces.tui.state import TUI_RELOAD_PROVIDER_SETTINGS
        except ModuleNotFoundError as error:
            if error.name in {"rich", "textual"}:
                raise ConfigurationError(
                    "interactive TUI dependencies are missing; reinstall neuro-code"
                ) from error
            raise

        initial_config = load_config(args.cwd)
        provider_settings_store = JsonProviderSettingsStore(initial_config.state_dir)
        provider_catalog = PersistentProviderCatalog(
            HttpProviderCatalog(),
            initial_config.state_dir / "model-catalog-cache.json",
        )
        ui_preferences = JsonUiPreferencesStore(initial_config.state_dir / "ui-preferences.json")
        explicit_provider_override = any(
            value is not None for value in (args.provider, args.model, args.base_url)
        )
        while True:
            preflight_config = load_config(args.cwd)
            if explicit_provider_override:
                preflight_config = override_provider(
                    preflight_config,
                    provider=args.provider,
                    model=args.model,
                    base_url=args.base_url,
                )
            managed_provider_settings = await provider_settings_store.load()
            selected_profile = (
                preflight_config.providers.get(preflight_config.selected_provider)
                if preflight_config.selected_provider is not None
                else None
            )
            selected_ready = (
                selected_profile is not None
                and selected_profile.available
                and selected_profile.redacted_dict(os.environ).get("credential_configured") is True
            )
            startup_error: str | None = None
            if selected_ready:
                assert selected_profile is not None
                try:
                    resolve_provider_binding(selected_profile)
                except ConfigurationError as error:
                    recoverable = (
                        managed_provider_settings.profile(selected_profile.name) is not None
                    )
                    if explicit_provider_override or not recoverable:
                        raise
                    startup_error = str(error)
            if not selected_ready or startup_error is not None:
                setup = ProviderSetupApp(
                    provider_settings=managed_provider_settings,
                    provider_settings_store=provider_settings_store,
                    provider_catalog=provider_catalog,
                    socks_supported=socks_support_available(),
                    language=await ui_preferences.load_language(),
                    first_run=not managed_provider_settings.profiles,
                    initial_profile=(
                        preflight_config.selected_provider
                        if managed_provider_settings.profile(
                            preflight_config.selected_provider or ""
                        )
                        is not None
                        else None
                    ),
                    initial_error=startup_error,
                )
                configured = await setup.run_async()
                if not configured:
                    return 0
                continue

            application = await self.open_application(settings)
            try:
                user_interaction = TuiUserInteraction()
                if args.resume is not None:
                    await application.session_service.prepare_resume(
                        ResumeSessionRequest(args.resume)
                    )
                approvals = SessionApprovalBroker()
                config = application.config
                session_service = application.session_service
                managed_provider_settings = await provider_settings_store.load()
                language = await ui_preferences.load_language()
                saved_reasoning_effort = await ui_preferences.load_reasoning_effort()
                saved_interaction_mode = await ui_preferences.load_interaction_mode()
                reasoning_effort = (
                    ReasoningEffort(args.effort)
                    if getattr(args, "effort", None) is not None
                    else saved_reasoning_effort
                )
                interaction_mode = (
                    InteractionMode.AUTO if args.always_approve else saved_interaction_mode
                )

                async def compose_scoped(
                    selected_config: AppConfig,
                    resume_id: str | None,
                    *,
                    application_: ApplicationComposition = application,
                    approvals_: SessionApprovalBroker = approvals,
                    reasoning_effort_: ReasoningEffort = reasoning_effort,
                    user_interaction_: TuiUserInteraction = user_interaction,
                ) -> ConversationBinding:
                    return await application_.create_binding(
                        config=selected_config,
                        approver=approvals_,
                        resume_id=resume_id,
                        reasoning_effort=reasoning_effort_,
                        user_interaction=user_interaction_,
                    )

                binding = await compose_scoped(config, args.resume)
                selected_profile_name = config.selected_provider
                if selected_profile_name is None:
                    raise ConfigurationError("no provider profile is selected")

                async def bind_profile(
                    profile_name: str,
                    *,
                    config_: AppConfig = config,
                    compose_scoped_: Callable[
                        [AppConfig, str | None], Awaitable[ConversationBinding]
                    ] = compose_scoped,
                ) -> ConversationBinding:
                    selected_config = override_provider(config_, provider=profile_name)
                    return await compose_scoped_(
                        selected_config,
                        None,
                    )

                async def list_workspace_sessions(
                    *,
                    session_service_: SessionApplicationService = session_service,
                    config_: AppConfig = config,
                ) -> tuple[SessionSummary, ...]:
                    return await session_service_.list_sessions_in_workspace(
                        config_.cwd.as_posix(),
                        limit=1000,
                        result_limit=50,
                    )

                async def search_workspace_sessions(
                    query: str,
                    *,
                    session_service_: SessionApplicationService = session_service,
                    config_: AppConfig = config,
                ) -> tuple[SessionSearchHit, ...]:
                    return await session_service_.search_sessions_in_workspace(
                        query,
                        config_.cwd.as_posix(),
                        limit=1000,
                        result_limit=50,
                    )

                async def bind_session(
                    profile_name: str,
                    session_id: str,
                    *,
                    config_: AppConfig = config,
                    compose_scoped_: Callable[
                        [AppConfig, str | None], Awaitable[ConversationBinding]
                    ] = compose_scoped,
                    application_: ApplicationComposition = application,
                ) -> ConversationBinding:
                    await application_.session_service.prepare_resume(
                        ResumeSessionRequest(session_id)
                    )
                    selected_config = override_provider(config_, provider=profile_name)
                    return await compose_scoped_(
                        selected_config,
                        session_id,
                    )

                async def rename_workspace_session(
                    session_id: str,
                    title: str,
                    *,
                    session_service_: SessionApplicationService = session_service,
                    config_: AppConfig = config,
                ) -> SessionSummary:
                    inspection = await session_service_.inspect_session(session_id)
                    if not workspaces_match(inspection.summary.cwd, config_.cwd):
                        raise ConfigurationError(
                            f"session does not exist in the current workspace: {session_id}"
                        )
                    return await session_service_.rename_session(
                        RenameSessionRequest(session_id, title)
                    )

                controller = ProfileConversationController(
                    options=_provider_options(config),
                    selected_profile=selected_profile_name,
                    binding=binding,
                    binding_factory=bind_profile,
                    session_catalog=list_workspace_sessions,
                    session_search=search_workspace_sessions,
                    session_binding_factory=bind_session,
                    session_rename=rename_workspace_session,
                    sandbox_profile=config.sandbox_profile,
                    reasoning_effort=reasoning_effort,
                    interaction_mode=interaction_mode,
                )
                provider_service = application.bind_provider_controller(controller)
                session_selection_service = application.bind_session_selection_controller(
                    controller
                )
                plan_execution_service = application.bind_plan_execution_controller(controller)
                plan_scheduling_service = application.bind_plan_scheduling_controller(controller)
                queued_plan_execution_service = application.bind_queued_plan_execution_controller(
                    controller
                )

                async def ultracode_delegate(
                    request: RunTurnRequest,
                    sink: EventSink | None,
                    application_: ApplicationComposition = application,
                    controller_: ProfileConversationController = controller,
                ) -> AgentRunResult:
                    service = await application_.create_ultracode_delegation_service(
                        parent_binding=controller_.binding,
                    )
                    return await service.run_turn(request, sink=sink)

                # Keep the application-level delegation entry dormant until the
                # long-lived controller reports ULTRACODE for a user turn.  The
                # controller may change effort after this binding is created;
                # rebuilding the turn service would leave a stale entry seam.
                turn_service = application.session_service.bind_runner(
                    controller,
                    ultracode_delegate=ultracode_delegate,
                )
                tool_output_artifact_service = application.create_tool_output_artifact_service(
                    config=config
                )
                read_only_subagent_service = (
                    application.create_read_only_subagent_application_service()
                )
                subagent_relationship_query = (
                    application.create_subagent_relationship_query_service()
                )
                subagent_relationship_lifecycle = (
                    application.create_subagent_relationship_lifecycle_service()
                )

                def subagent_parent_capability_provider(
                    controller_: ProfileConversationController = controller,
                ) -> SubagentCapabilitySet:
                    return controller_.capabilities

                app = NeuroCodeApp(
                    controller,
                    turn_service=turn_service,
                    approval_controller=approvals,
                    provider_controller=provider_service,
                    reasoning_controller=controller,
                    interaction_mode_controller=controller,
                    session_controller=controller,
                    session_selection_service=session_selection_service,
                    task_controller=controller,
                    session_task_controller=controller,
                    plan_controller=controller,
                    plan_execution_service=plan_execution_service,
                    plan_scheduling_service=plan_scheduling_service,
                    queued_plan_execution_service=queued_plan_execution_service,
                    tool_output_artifact_service=tool_output_artifact_service,
                    read_only_subagent_service=read_only_subagent_service,
                    subagent_parent_capability_provider=subagent_parent_capability_provider,
                    subagent_relationship_query=subagent_relationship_query,
                    subagent_relationship_lifecycle=subagent_relationship_lifecycle,
                    ui_preferences=ui_preferences,
                    provider_settings_store=provider_settings_store,
                    provider_catalog=provider_catalog,
                    managed_provider_settings=managed_provider_settings,
                    language=language,
                    initial_items=controller.items,
                    provider_name=controller.provider_name,
                    model_name=controller.model_name,
                    cwd=config.cwd,
                    user_interaction=user_interaction,
                    socks_supported=socks_support_available(),
                )
                await app.run_async()
                return_code = app.return_code if app.return_code is not None else 0
            finally:
                await asyncio.shield(application.close())
            if return_code != TUI_RELOAD_PROVIDER_SETTINGS:
                return return_code


def _provider_options(config: AppConfig) -> tuple[ProviderOption, ...]:
    options: list[ProviderOption] = []
    for name, profile in config.providers.items():
        redacted = profile.redacted_dict(os.environ)
        credential_configured = redacted.get("credential_configured") is True
        options.append(
            ProviderOption(
                name=name,
                protocol=profile.protocol,
                model=profile.model,
                available=profile.available,
                credential_configured=credential_configured,
                default=name == config.default_provider,
                context_window_tokens=profile.context_window_tokens,
            )
        )
    return tuple(options)
