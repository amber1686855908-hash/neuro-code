"""Subagent capability and relationship composition owners.

子代理能力与关系的组合根 owner.

The application workflows own subagent execution and lifecycle policy.  This
module only constructs the concrete child runtime factories and bounded
application facades from the active composition.
"""

from __future__ import annotations

import os
from typing import cast

from neuro_code.application.ports.parent_context_relay import ParentContextRelayStore
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipLifecycleService,
)
from neuro_code.application.sessions.subagent_queries import SubagentRelationshipQueryService
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
from neuro_code.bootstrap.composition_contracts import CompositionRootMixin
from neuro_code.bootstrap.subagent import (
    CompositionReadOnlySubagentRuntimeFactory,
    CompositionWritableSubagentRuntimeFactory,
)
from neuro_code.infrastructure.tools.registry import default_tool_registry
from neuro_code.shared.errors import ConfigurationError


class CompositionSubagentMixin(CompositionRootMixin):
    """Assemble bounded subagent runtimes and relationship facades."""

    def subagent_global_policy(
        self: CompositionRootMixin,
    ) -> SubagentCapabilitySet:
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
        self: CompositionRootMixin,
        *,
        timeout_seconds: float = 120.0,
    ) -> IsolatedSubagentExecutionService:
        """Create the explicit read-only subagent application workflow.

        The concrete child runtime is assembled by the canonical composition
        factory.  No caller is automatically scheduled and the returned
        service is not used by normal sessions.

        创建一个显式的只读子代理应用工作流.具体子运行时由规范组合根工厂组装,不会自动调度调用方.
        """

        factory = CompositionReadOnlySubagentRuntimeFactory(self)
        return IsolatedSubagentExecutionService(
            self.store,
            factory,
            global_policy=self.subagent_global_policy(),
            requested_capability_factory=factory.requested_capabilities,
            timeout_seconds=timeout_seconds,
        )

    def create_writable_subagent_service(
        self: CompositionRootMixin,
        *,
        parent_binding: ConversationBinding,
        timeout_seconds: float = 120.0,
    ) -> WritableSubagentApplicationService:
        """Create the explicit internal serialized writable-subagent slice."""

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
            relay_store=cast(ParentContextRelayStore, self.store),
            redaction_values=self.config.redaction_values(os.environ),
            timeout_seconds=timeout_seconds,
        )

    def create_subagent_scheduler(
        self: CompositionRootMixin,
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
        self: CompositionRootMixin,
        *,
        timeout_seconds: float = 120.0,
        max_result_bytes: int = MAX_SUBAGENT_RESULT_BYTES,
    ) -> ReadOnlySubagentApplicationService:
        """Create the safe, explicit read-only subagent result boundary."""

        return ReadOnlySubagentApplicationService(
            self.create_read_only_subagent_service(timeout_seconds=timeout_seconds),
            redaction_values=self.config.redaction_values(os.environ),
            max_result_bytes=max_result_bytes,
        )

    def create_subagent_relationship_query_service(
        self: CompositionRootMixin,
    ) -> SubagentRelationshipQueryService:
        """Create the read-only parent/child relationship projection service."""

        return SubagentRelationshipQueryService(self.store)

    def create_subagent_relationship_lifecycle_service(
        self: CompositionRootMixin,
    ) -> SubagentRelationshipLifecycleService:
        """Create the explicit parent-owned child lifecycle boundary."""

        return SubagentRelationshipLifecycleService(self.store, self.session_service)

    def bind_subagent_executor(
        self: CompositionRootMixin,
        executor_factory: SubagentExecutorFactory,
        *,
        _test_only: bool = False,
    ) -> SubagentExecutionService:
        """Bind the legacy executor seam for tests/internal compatibility only."""

        if _test_only is not True:
            raise ConfigurationError("legacy subagent executor binding is test-only")
        return SubagentExecutionService(self.store, executor_factory)


__all__ = ["CompositionSubagentMixin"]
