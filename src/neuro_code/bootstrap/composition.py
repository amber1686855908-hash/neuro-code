"""Public composition root for the Neuro Code process.

Neuro Code 进程的公共组合根.

The public ``ApplicationComposition`` identity remains stable while each
composition responsibility lives in one explicit bootstrap owner.  The class
below owns only shared state assembly; lifecycle, binding, capability,
workflow, and discovery methods are provided by cohesive mixins.
公共 ``ApplicationComposition`` 身份保持稳定;每项组合职责都由明确的 bootstrap owner
负责.下面的类只负责共享状态组装,生命周期、绑定、能力、工作流和发现方法由内聚 mixin 提供.
"""

from __future__ import annotations

from neuro_code.application.ports.background_tasks import BackgroundTaskSupervisor
from neuro_code.application.ports.configuration import AppConfig
from neuro_code.application.ports.instructions import InstructionDiscovery
from neuro_code.application.ports.skills import SkillDiscovery
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.sessions import SessionApplicationService
from neuro_code.application.sessions.summary import SessionSummaryQueryService
from neuro_code.application.settings import ApplicationSettings
from neuro_code.bootstrap.composition_bindings import CompositionBindingMixin
from neuro_code.bootstrap.composition_discovery import CompositionDiscoveryMixin
from neuro_code.bootstrap.composition_lifecycle import CompositionLifecycleMixin
from neuro_code.bootstrap.composition_services import CompositionServicesMixin
from neuro_code.bootstrap.composition_subagents import CompositionSubagentMixin
from neuro_code.bootstrap.composition_workflows import CompositionWorkflowMixin
from neuro_code.bootstrap.factories import (
    BackgroundSupervisorFactory,
    InstructionDiscoveryFactory,
    LocalProcessSandboxFactory,
    ProviderFactory,
    SessionStoreFactory,
    SkillDiscoveryFactory,
    WorkspaceChangeObserverFactory,
    _default_instruction_discovery_factory,
    _default_local_process_sandbox_factory,
    _default_skill_discovery_factory,
    _default_workspace_change_observer_factory,
)
from neuro_code.infrastructure.lsp.manager import LanguageServerManager
from neuro_code.infrastructure.workspace.paths import workspaces_match


class ApplicationComposition(
    CompositionBindingMixin,
    CompositionServicesMixin,
    CompositionSubagentMixin,
    CompositionWorkflowMixin,
    CompositionDiscoveryMixin,
    CompositionLifecycleMixin,
):
    """Own shared configuration, persistence, and conversation resources.

    管理共享的配置、持久化和会话资源.
    """

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
