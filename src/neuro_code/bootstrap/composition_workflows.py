"""Workflow and DAG factory composition owned by bootstrap.

工作流与 DAG 工厂的组合根 owner.

Application workflows retain orchestration, persistence, and recovery policy.
This module only assembles those owners with explicit stores and bindings.
应用工作流继续拥有编排、持久化和恢复策略;本模块只用显式 store 与 binding 组装它们.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import cast

from neuro_code.application.ports.agent_swarm import AgentSwarmStore
from neuro_code.application.ports.leader import LeaderStore
from neuro_code.application.ports.model_planning import ModelPlanningStore
from neuro_code.application.ports.parent_context_relay import ParentContextRelayStore
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.task_dag_recovery import TaskDagRecoveryClaimStore
from neuro_code.application.ports.task_dag_replan import TaskDagReplanStore
from neuro_code.application.ports.task_dag_result_relay import TaskDagDependencyResultRelayStore
from neuro_code.application.ports.ultracode import UltracodeResultAdoption, UltracodeStore
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.workflows.agent_swarm import (
    AgentSwarmApplicationService,
    AgentSwarmLeader,
    AgentSwarmPlanner,
    AgentSwarmReplanner,
)
from neuro_code.application.workflows.leader import LeaderApplicationService
from neuro_code.application.workflows.model_planning import ModelDagPlanningApplicationService
from neuro_code.application.workflows.task_dag import (
    TaskDagApplicationService,
    TaskDagWritableService,
    TaskDagWritableWorkerFactory,
)
from neuro_code.application.workflows.task_dag_replan import TaskDagReplanApplicationService
from neuro_code.application.workflows.ultracode import UltracodeDelegationApplicationService
from neuro_code.bootstrap.composition_contracts import CompositionRootMixin
from neuro_code.shared.errors import ConfigurationError


class CompositionTaskDagWritableWorkerFactory(TaskDagWritableWorkerFactory):
    """Create one fresh writable application owner per parallel DAG node."""

    __slots__ = ("_composition", "_parent_binding", "_timeout_seconds")

    def __init__(
        self,
        composition: CompositionRootMixin,
        parent_binding: ConversationBinding,
        timeout_seconds: float,
    ) -> None:
        self._composition = composition
        self._parent_binding = parent_binding
        self._timeout_seconds = timeout_seconds

    def create(self) -> TaskDagWritableService:
        return cast(
            TaskDagWritableService,
            self._composition.create_writable_subagent_service(
                parent_binding=self._parent_binding,
                timeout_seconds=self._timeout_seconds,
            ),
        )


class CompositionWorkflowMixin(CompositionRootMixin):
    """Assemble the bounded planning, DAG, swarm, and delegation workflows."""

    def create_task_dag_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> TaskDagApplicationService:
        """Create the explicit bounded Task DAG application slice."""

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("task DAG parent binding is required")
        writable = self.create_writable_subagent_service(
            parent_binding=parent_binding,
            timeout_seconds=timeout_seconds,
        )
        writable_worker_factory = CompositionTaskDagWritableWorkerFactory(
            self,
            parent_binding,
            timeout_seconds,
        )
        return TaskDagApplicationService(
            self.store,
            cast(TaskDagStore, self.store),
            writable,
            cast(WritableSubagentLeaseStore, self.store),
            cast(ParentContextRelayStore, self.store),
            parent_binding=parent_binding,
            dependency_relay_store=cast(TaskDagDependencyResultRelayStore, self.store),
            recovery_claim_store=cast(TaskDagRecoveryClaimStore, self.store),
            writable_worker_factory=writable_worker_factory,
            redaction_values=self.config.redaction_values(os.environ),
        )

    async def create_leader_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> LeaderApplicationService:
        """Create the bounded zero-tool Leader over one existing Task DAG."""

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("Leader parent binding is required")
        leader_config = replace(
            self.config,
            providers={
                name: replace(profile, builtin_tools=())
                for name, profile in self.config.providers.items()
            },
        )
        provider_profile = leader_config.provider
        leader_session_id = await self.store.create_session(
            str(leader_config.cwd),
            provider_profile.name,
            provider_profile.model,
            provider_profile.context_affinity,
            leader_config.sandbox_profile,
        )
        try:
            leader_binding = await self.create_binding(
                config=leader_config,
                resume_id=leader_session_id,
                max_steps=1,
                allowed_tool_names=(),
                enable_background_tasks=False,
            )
        except BaseException:
            await asyncio.shield(self.store.delete_session(leader_session_id))
            raise
        try:
            dag_service = self.create_task_dag_service(
                parent_binding=parent_binding,
                timeout_seconds=timeout_seconds,
            )
            return LeaderApplicationService(
                cast(LeaderStore, self.store),
                dag_service,
                parent_binding=parent_binding,
                leader_binding=leader_binding,
                session_store=self.store,
                redaction_values=leader_config.redaction_values(os.environ),
            )
        except BaseException:
            await asyncio.shield(leader_binding.close())
            await asyncio.shield(self.store.delete_session(leader_session_id))
            raise

    async def create_model_planning_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> ModelDagPlanningApplicationService:
        """Create the bounded zero-tool model-generated DAG planner."""

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("model planning parent binding is required")
        planner_config = replace(
            self.config,
            providers={
                name: replace(profile, builtin_tools=())
                for name, profile in self.config.providers.items()
            },
        )
        provider_profile = planner_config.provider
        planner_session_id = await self.store.create_session(
            str(planner_config.cwd),
            provider_profile.name,
            provider_profile.model,
            provider_profile.context_affinity,
            planner_config.sandbox_profile,
        )
        try:
            planner_binding = await self.create_binding(
                config=planner_config,
                resume_id=planner_session_id,
                max_steps=1,
                allowed_tool_names=(),
                enable_background_tasks=False,
            )
        except BaseException:
            await asyncio.shield(self.store.delete_session(planner_session_id))
            raise
        try:
            dag_service = self.create_task_dag_service(
                parent_binding=parent_binding,
                timeout_seconds=timeout_seconds,
            )
            return ModelDagPlanningApplicationService(
                cast(ModelPlanningStore, self.store),
                dag_service,
                parent_binding=parent_binding,
                planner_binding=planner_binding,
                session_store=self.store,
                redaction_values=planner_config.redaction_values(os.environ),
            )
        except BaseException:
            await asyncio.shield(planner_binding.close())
            await asyncio.shield(self.store.delete_session(planner_session_id))
            raise

    create_planner_service = create_model_planning_service

    async def create_task_dag_replan_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> TaskDagReplanApplicationService:
        """Create the explicit zero-tool bounded failed-DAG replan service."""

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("DAG replan parent binding is required")
        planner_config = replace(
            self.config,
            providers={
                name: replace(profile, builtin_tools=())
                for name, profile in self.config.providers.items()
            },
        )
        provider_profile = planner_config.provider
        planner_session_id = await self.store.create_session(
            str(planner_config.cwd),
            provider_profile.name,
            provider_profile.model,
            provider_profile.context_affinity,
            planner_config.sandbox_profile,
        )
        try:
            planner_binding = await self.create_binding(
                config=planner_config,
                resume_id=planner_session_id,
                max_steps=1,
                allowed_tool_names=(),
                enable_background_tasks=False,
            )
        except BaseException:
            await asyncio.shield(self.store.delete_session(planner_session_id))
            raise
        try:
            dag_service = self.create_task_dag_service(
                parent_binding=parent_binding,
                timeout_seconds=timeout_seconds,
            )
            return TaskDagReplanApplicationService(
                cast(TaskDagReplanStore, self.store),
                cast(TaskDagStore, self.store),
                dag_service,
                parent_binding=parent_binding,
                planner_binding=planner_binding,
                session_store=self.store,
                redaction_values=planner_config.redaction_values(os.environ),
            )
        except BaseException:
            await asyncio.shield(planner_binding.close())
            await asyncio.shield(self.store.delete_session(planner_session_id))
            raise

    create_dag_replan_service = create_task_dag_replan_service

    async def create_agent_swarm_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> AgentSwarmApplicationService:
        """Create the internal bounded Planner-to-Replan Swarm workflow."""

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("Swarm parent binding is required")

        async def planner_factory() -> AgentSwarmPlanner:
            return cast(
                AgentSwarmPlanner,
                await self.create_model_planning_service(
                    parent_binding=parent_binding,
                    timeout_seconds=timeout_seconds,
                ),
            )

        async def leader_factory() -> AgentSwarmLeader:
            return cast(
                AgentSwarmLeader,
                await self.create_leader_service(
                    parent_binding=parent_binding,
                    timeout_seconds=timeout_seconds,
                ),
            )

        async def replanner_factory() -> AgentSwarmReplanner:
            return cast(
                AgentSwarmReplanner,
                await self.create_task_dag_replan_service(
                    parent_binding=parent_binding,
                    timeout_seconds=timeout_seconds,
                ),
            )

        return AgentSwarmApplicationService(
            cast(AgentSwarmStore, self.store),
            cast(TaskDagStore, self.store),
            parent_binding=parent_binding,
            planner_factory=planner_factory,
            leader_factory=leader_factory,
            replanner_factory=replanner_factory,
            redaction_values=self.config.redaction_values(os.environ),
        )

    async def create_ultracode_delegation_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> UltracodeDelegationApplicationService:
        """Create the application-owned Ultracode branch router."""

        if not isinstance(parent_binding, ConversationBinding):
            raise ConfigurationError("Ultracode parent binding is required")
        if parent_binding.capabilities is None:
            raise ConfigurationError("Ultracode parent capability metadata is missing")

        async def swarm_factory() -> AgentSwarmApplicationService:
            return await self.create_agent_swarm_service(
                parent_binding=parent_binding,
                timeout_seconds=timeout_seconds,
            )

        async def result_adoption_factory() -> UltracodeResultAdoption:
            return cast(
                UltracodeResultAdoption,
                self.create_result_adoption_service(parent_binding=parent_binding),
            )

        return UltracodeDelegationApplicationService(
            cast(UltracodeStore, self.store),
            session_store=self.store,
            parent_binding=parent_binding,
            swarm_factory=swarm_factory,
            result_adoption_factory=result_adoption_factory,
        )

    create_swarm_service = create_agent_swarm_service


__all__ = [
    "CompositionTaskDagWritableWorkerFactory",
    "CompositionWorkflowMixin",
]
