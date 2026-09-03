"""Application-capability bindings owned by the composition root.

组合根负责的应用能力绑定.

These methods assemble concrete workspace, checkpoint, artifact, and inbound
application facades.  They do not implement those capabilities; the
application services remain their canonical owners.
"""

from __future__ import annotations

import os
from typing import cast

from neuro_code.application.checkpoints import WorkspaceCheckpointApplicationService
from neuro_code.application.ports.agent_swarm import AgentSwarmStore
from neuro_code.application.ports.configuration import AppConfig
from neuro_code.application.ports.result_adoption import ResultAdoptionStore
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.providers.service import (
    ProviderChangeService,
    ProviderProfileController,
)
from neuro_code.application.sessions import SessionApplicationService
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.selection import (
    SessionSelectionController,
    SessionSelectionService,
)
from neuro_code.application.sessions.summary import SessionSummaryQueryService
from neuro_code.application.tools.service import SessionToolOutputArtifactApplicationService
from neuro_code.application.workflows.plan_execution import (
    PlanExecutionController,
    PlanExecutionService,
)
from neuro_code.application.workflows.plan_scheduling import (
    PlanSchedulingController,
    PlanSchedulingService,
)
from neuro_code.application.workflows.result_adoption import ResultAdoptionApplicationService
from neuro_code.application.workflows.session_task_execution import (
    QueuedPlanExecutionController,
    QueuedPlanExecutionService,
)
from neuro_code.application.worktrees import WorktreeApplicationService
from neuro_code.bootstrap.composition_contracts import CompositionRootMixin
from neuro_code.infrastructure.git.worktree import LocalGitWorktreeAdapter
from neuro_code.infrastructure.persistence.checkpoint_artifacts import LocalCheckpointArtifactStore
from neuro_code.infrastructure.persistence.managed_worktrees import SqliteManagedWorktreeStore
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.infrastructure.persistence.workspace_checkpoints import (
    SqliteWorkspaceCheckpointStore,
)
from neuro_code.infrastructure.workspace.checkpoints import LocalWorkspaceStateAdapter
from neuro_code.infrastructure.workspace.projection import LocalParentWorkspaceProjectionReader
from neuro_code.shared.errors import ConfigurationError


class CompositionServicesMixin(CompositionRootMixin):
    """Assemble application service facades and their concrete adapters."""

    def create_worktree_service(
        self: CompositionRootMixin,
    ) -> WorktreeApplicationService:
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

    def create_workspace_checkpoint_service(
        self: CompositionRootMixin,
    ) -> WorkspaceCheckpointApplicationService:
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

    def create_result_adoption_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
    ) -> ResultAdoptionApplicationService:
        """Create the explicit durable parent-result adoption capability.

        The service is assembled from the active composition and binding; it
        is not a model-facing tool or a second provider path.

        创建显式且持久化的父结果采纳能力.服务由当前组合根和绑定组装,不是模型工具或第二条 provider 路径.
        """

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("result adoption parent binding is required")
        if parent_binding.workspace_mutation is None:
            raise ConfigurationError("result adoption parent mutation authority is missing")
        git = LocalGitWorktreeAdapter(hooks_directory=self.config.state_dir / "git-hooks")
        managed_worktrees = SqliteManagedWorktreeStore(self.config.state_dir / "worktrees.db")
        checkpoint_store = SqliteWorkspaceCheckpointStore(self.config.state_dir / "checkpoints.db")
        state = LocalWorkspaceStateAdapter(git=git, workspace_git=git)
        worktrees = WorktreeApplicationService(
            git=git,
            store=managed_worktrees,
            managed_root=self.config.state_dir / "worktrees",
        )
        checkpoints = WorkspaceCheckpointApplicationService(
            git=git,
            workspace_git=git,
            worktrees=managed_worktrees,
            state=state,
            checkpoints=checkpoint_store,
            artifacts=LocalCheckpointArtifactStore(self.config.state_dir),
        )
        parent_reader = LocalParentWorkspaceProjectionReader(git=git, state=state)
        return ResultAdoptionApplicationService(
            store=cast(ResultAdoptionStore, self.store),
            swarms=cast(AgentSwarmStore, self.store),
            dags=cast(TaskDagStore, self.store),
            leases=cast(WritableSubagentLeaseStore, self.store),
            worktrees=worktrees,
            checkpoints=checkpoints,
            parent_reader=parent_reader,
            mutation=parent_binding.workspace_mutation,
            parent_binding=parent_binding,
        )

    def create_tool_output_artifact_service(
        self: CompositionRootMixin,
        *,
        config: AppConfig | None = None,
    ) -> SessionToolOutputArtifactApplicationService:
        """Create the session-scoped tool-output read boundary for an interface.

        The returned service exposes only event-associated opaque handles.  The
        composition root supplies the existing state directory and redaction
        boundary; interfaces never receive a filesystem path or artifact store.

        为入站接口创建会话作用域的工具输出读取边界.返回的服务只暴露与事件关联的不透明句柄;
        组合根提供既有 state directory 和脱敏边界,接口不会收到文件系统路径或 artifact 存储器.
        """

        selected_config = config or self.config
        reader = FileToolOutputArtifactStore(
            selected_config.state_dir / "tool-output",
            redaction_values=selected_config.redaction_values(os.environ),
        )
        return SessionToolOutputArtifactApplicationService(
            self.store,
            reader,
            garbage_collector=reader,
        )

    def bind_provider_controller(
        self: CompositionRootMixin,
        controller: ProviderProfileController,
    ) -> ProviderChangeService:
        """Bind the existing profile owner to the ChangeProvider application seam."""

        return ProviderChangeService(controller)

    def bind_session_selection_controller(
        self: CompositionRootMixin,
        controller: SessionSelectionController,
    ) -> SessionSelectionService:
        """Bind session listing, selection, and rename to an inbound seam."""

        return SessionSelectionService(controller)

    def bind_plan_execution_controller(
        self: CompositionRootMixin,
        controller: PlanExecutionController,
    ) -> PlanExecutionService:
        """Bind the existing plan owner to the ExecutePlan application seam."""

        return PlanExecutionService(controller)

    def bind_plan_scheduling_controller(
        self: CompositionRootMixin,
        controller: PlanSchedulingController,
    ) -> PlanSchedulingService:
        """Bind the existing plan owner to the scheduling application seam."""

        return PlanSchedulingService(controller)

    def bind_queued_plan_execution_controller(
        self: CompositionRootMixin,
        controller: QueuedPlanExecutionController,
    ) -> QueuedPlanExecutionService:
        """Bind the existing queued-task owner to its application seam."""

        return QueuedPlanExecutionService(controller)

    @property
    def session_service(
        self: CompositionRootMixin,
    ) -> SessionApplicationService:
        """Return the application session use-case facade for this composition."""

        return self._session_service

    @property
    def session_summary_queries(
        self: CompositionRootMixin,
    ) -> SessionSummaryQueryService:
        """Return the canonical read-only session-summary query owner."""

        return self._session_summary_queries


__all__ = ["CompositionServicesMixin"]
