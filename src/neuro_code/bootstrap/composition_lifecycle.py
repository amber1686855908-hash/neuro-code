"""Resource lifecycle owned by the bootstrap composition root.

组合根负责的共享资源生命周期.

This module contains only process-level acquisition and release.  Per-binding
resources remain owned by ``composition_bindings`` and close through the
``ConversationBinding`` resource scope.
"""

from __future__ import annotations

import asyncio
from typing import Any, Self, cast

import neuro_code.application.ports.configuration as configuration_ports
import neuro_code.bootstrap.configuration as bootstrap_configuration
from neuro_code.application.settings import ApplicationSettings
from neuro_code.bootstrap.composition_contracts import CompositionRootMixin
from neuro_code.bootstrap.factories import (
    BackgroundSupervisorFactory,
    InstructionDiscoveryFactory,
    LocalProcessSandboxFactory,
    ProviderFactory,
    SessionStoreFactory,
    SkillDiscoveryFactory,
    WorkspaceChangeObserverFactory,
    _default_background_supervisor_factory,
    _default_instruction_discovery_factory,
    _default_local_process_sandbox_factory,
    _default_provider_factory,
    _default_session_store_factory,
    _default_skill_discovery_factory,
    _default_workspace_change_observer_factory,
)


class CompositionLifecycleMixin(CompositionRootMixin):
    """Own process resource acquisition and shutdown ordering."""

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
        background_supervisor_factory: BackgroundSupervisorFactory = (
            _default_background_supervisor_factory
        ),
        instruction_discovery_factory: InstructionDiscoveryFactory = (
            _default_instruction_discovery_factory
        ),
        skill_discovery_factory: SkillDiscoveryFactory = _default_skill_discovery_factory,
        workspace_change_observer_factory: WorkspaceChangeObserverFactory = (
            _default_workspace_change_observer_factory
        ),
    ) -> Self:
        """Acquire the shared resources in their established order.

        以既有顺序获取共享资源.

        The background supervisor is the first acquired resource and is
        always unwound if configuration or session-store initialization fails.
        The constructor receives fully initialized resources; it does not
        create process-wide state at import time.
        """

        background_tasks = background_supervisor_factory()
        try:
            config = bootstrap_configuration.load_config(settings.cwd)
            config = configuration_ports.override_sandbox(config, settings.sandbox)
            config = configuration_ports.override_provider(
                config,
                provider=settings.provider,
                model=settings.model,
                base_url=settings.base_url,
            )
            store = store_factory(config.state_dir / "sessions.db")
            if settings.resume_id is not None:
                saved_profile = await store.peek_session_sandbox_profile(settings.resume_id)
                config = configuration_ports.pin_resumed_sandbox(config, saved_profile)
            await store.initialize()
            return cast(
                Self,
                cast(Any, cls)(
                    settings=settings,
                    config=config,
                    store=store,
                    background_tasks=background_tasks,
                    provider_factory=provider_factory,
                    local_process_sandbox_factory=local_process_sandbox_factory,
                    instruction_discovery=instruction_discovery_factory(),
                    skill_discovery=skill_discovery_factory(),
                    workspace_change_observer_factory=workspace_change_observer_factory,
                ),
            )
        except BaseException:
            await asyncio.shield(background_tasks.shutdown())
            raise

    async def close(self: CompositionRootMixin) -> None:
        """Release bindings' remaining process resources in the old order.

        以原有顺序释放绑定之外仍存活的进程资源.

        Binding scopes remove their LSP service before closing it.  The root
        therefore only closes idle services still registered here, then shuts
        down the shared background supervisor.  Gathering with
        ``return_exceptions=True`` preserves the existing best-effort cleanup
        contract while allowing every LSP resource to be released.
        """

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


__all__ = ["CompositionLifecycleMixin"]
