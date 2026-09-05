"""Typing contract shared by the cohesive composition owners.

组合职责 owner 共享的类型契约.

The live state remains on ``ApplicationComposition``.  This type-only base
describes that state to the method-owning mixins without creating a second
runtime object or a generic service locator.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neuro_code.application.checkpoints import WorkspaceCheckpointApplicationService
    from neuro_code.application.ports.approval import PermissionApprover
    from neuro_code.application.ports.background_tasks import BackgroundTaskSupervisor
    from neuro_code.application.ports.client_filesystem import ClientFileSystem
    from neuro_code.application.ports.client_terminal import ClientTerminal
    from neuro_code.application.ports.configuration import AppConfig
    from neuro_code.application.ports.instructions import InstructionDiscovery
    from neuro_code.application.ports.skills import SkillDiscovery
    from neuro_code.application.ports.storage import SessionStore
    from neuro_code.application.ports.tools import Tool
    from neuro_code.application.ports.user_interaction import UserInteractionPort
    from neuro_code.application.sessions import SessionApplicationService
    from neuro_code.application.sessions.binding import ConversationBinding
    from neuro_code.application.sessions.summary import SessionSummaryQueryService
    from neuro_code.application.settings import ApplicationSettings
    from neuro_code.application.workflows.agent_swarm import AgentSwarmApplicationService
    from neuro_code.application.workflows.leader import LeaderApplicationService
    from neuro_code.application.workflows.model_planning import ModelDagPlanningApplicationService
    from neuro_code.application.workflows.result_adoption import ResultAdoptionApplicationService
    from neuro_code.application.workflows.subagent import (
        IsolatedSubagentExecutionService,
    )
    from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
    from neuro_code.application.workflows.task_dag import TaskDagApplicationService
    from neuro_code.application.workflows.task_dag_replan import TaskDagReplanApplicationService
    from neuro_code.application.workflows.writable_subagent import (
        WritableSubagentApplicationService,
    )
    from neuro_code.application.worktrees import WorktreeApplicationService
    from neuro_code.bootstrap.factories import (
        LocalProcessSandboxFactory,
        ProviderFactory,
        WorkspaceChangeObserverFactory,
    )
    from neuro_code.domain.conversation.reasoning import ReasoningEffort
    from neuro_code.domain.parent_context_relay import ParentContextRelay
    from neuro_code.domain.task_dag_result_relay import TaskDagDependencyResultRelay
    from neuro_code.infrastructure.lsp.manager import LanguageServerManager


class CompositionRootMixin:
    """Describe only state assembled by the public composition root."""

    if TYPE_CHECKING:
        settings: ApplicationSettings
        config: AppConfig
        store: SessionStore
        background_tasks: BackgroundTaskSupervisor
        _provider_factory: ProviderFactory
        _local_process_sandbox_factory: LocalProcessSandboxFactory
        _instruction_discovery: InstructionDiscovery
        _skill_discovery: SkillDiscovery
        _workspace_change_observer_factory: WorkspaceChangeObserverFactory
        _session_service: SessionApplicationService
        _session_summary_queries: SessionSummaryQueryService
        _lsp_services: set[LanguageServerManager]
        _closed: bool

        @property
        def session_service(self) -> SessionApplicationService: ...

        @property
        def session_summary_queries(self) -> SessionSummaryQueryService: ...

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
            parent_context_relay: ParentContextRelay | None = None,
            dag_result_relay: TaskDagDependencyResultRelay | None = None,
            final_output_gate_enabled: bool = True,
        ) -> ConversationBinding: ...

        def create_worktree_service(self) -> WorktreeApplicationService: ...

        def create_workspace_checkpoint_service(
            self,
        ) -> WorkspaceCheckpointApplicationService: ...

        def create_result_adoption_service(
            self,
            *,
            parent_binding: ConversationBinding,
        ) -> ResultAdoptionApplicationService: ...

        def subagent_global_policy(self) -> SubagentCapabilitySet: ...

        def create_read_only_subagent_service(
            self,
            *,
            timeout_seconds: float = 120.0,
        ) -> IsolatedSubagentExecutionService: ...

        def create_writable_subagent_service(
            self,
            *,
            parent_binding: ConversationBinding,
            timeout_seconds: float = 120.0,
        ) -> WritableSubagentApplicationService: ...

        def create_task_dag_service(
            self,
            *,
            parent_binding: ConversationBinding,
            timeout_seconds: float = 120.0,
        ) -> TaskDagApplicationService: ...

        async def create_leader_service(
            self,
            *,
            parent_binding: ConversationBinding,
            timeout_seconds: float = 120.0,
        ) -> LeaderApplicationService: ...

        async def create_model_planning_service(
            self,
            *,
            parent_binding: ConversationBinding,
            timeout_seconds: float = 120.0,
        ) -> ModelDagPlanningApplicationService: ...

        async def create_task_dag_replan_service(
            self,
            *,
            parent_binding: ConversationBinding,
            timeout_seconds: float = 120.0,
        ) -> TaskDagReplanApplicationService: ...

        async def create_agent_swarm_service(
            self,
            *,
            parent_binding: ConversationBinding,
            timeout_seconds: float = 120.0,
        ) -> AgentSwarmApplicationService: ...


__all__ = ["CompositionRootMixin"]
