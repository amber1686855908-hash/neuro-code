from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import closing, nullcontext, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, NoReturn, cast
from unittest.mock import patch

import pytest

from neuro_code.application.permissions.policy import PermissionMode
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider, ModelToolPolicy
from neuro_code.application.ports.result_adoption import ResultAdoptionRecord
from neuro_code.application.ports.ultracode import (
    UltracodeStore,
    UltracodeStoreError,
)
from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.conversation import AgentConversation
from neuro_code.application.sessions.profile_conversation import (
    ProfileConversationController,
    ProviderOption,
)
from neuro_code.application.sessions.turns import RunTurnRequest, SessionTurnService
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows.agent_swarm import RunAgentSwarmRequest
from neuro_code.application.workflows.result_adoption import ResultAdoptionApplicationService
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.application.workflows.ultracode import (
    UltracodeDelegationApplicationService,
    UltracodeDelegationPolicy,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.agent_swarm import (
    MAX_SWARM_OBJECTIVE_BYTES,
    AgentSwarmResult,
    AgentSwarmRun,
    AgentSwarmRunState,
    objective_fingerprint,
    terminal_result_fingerprint,
)
from neuro_code.domain.checkpoints import CheckpointId
from neuro_code.domain.conversation.events import (
    AgentEventKind,
    ModelCompleted,
    ModelEvent,
    ModelToolCall,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import Message, Role, ToolCall
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.conversation.request import context_fingerprints
from neuro_code.domain.execution import (
    TurnCancellationPolicy,
    TurnInput,
    TurnRecoveryAttempt,
    TurnRecoveryResolution,
    TurnSource,
    VerificationRequirement,
    VerificationRequirementsSnapshot,
)
from neuro_code.domain.result_adoption import (
    ResultAdoptionPlan,
    ResultAdoptionRequest,
    ResultAdoptionSource,
    ResultAdoptionState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagNodeState, TaskDagState
from neuro_code.domain.ultracode import (
    UltracodeDelegationDecision,
    UltracodeExecution,
    UltracodeExecutionState,
    ultracode_execution_id,
    ultracode_result_adoption_id,
    ultracode_result_fingerprint,
    ultracode_swarm_run_id,
)
from neuro_code.domain.worktree import WorktreeId, WorktreeRepositoryIdentity
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError
from tests.test_model_planning import (
    _make_repository as _make_production_repository,
)
from tests.test_model_planning import (
    _parent_capability as _production_parent_capability,
)
from tests.test_model_planning import (
    _ProductionPlanningProvider,
    _ProductionPlanningState,
    _run_git,
)
from tests.test_model_planning import (
    _write_fixture_config as _write_production_fixture_config,
)


class _Runtime:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.provider_name = "fixture-provider"
        self.model_name = "fixture-model"
        self.context_affinity = "fixture-context"
        self.sandbox_profile = SandboxProfile.OFF
        self.system_prompt = "fixture system prompt"
        self.reasoning_effort = ReasoningEffort.ULTRACODE
        self.interaction_mode = InteractionMode.NORMAL
        self.auto_mode_unrestricted = False
        self.plan = None
        self.plan_comments = ()

    def set_plan(self, plan: object) -> None:
        self.plan = plan

    def set_plan_comments(self, comments: object) -> None:
        self.plan_comments = comments

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self.reasoning_effort = effort

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        self.interaction_mode = mode


class _ParentRunner:
    def __init__(
        self,
        store: SqliteSessionStore,
        cwd: Path,
        response: str = "main answer",
        session_id: str | None = None,
    ) -> None:
        self._conversation = AgentConversation(
            runtime=_Runtime(cwd),
            store=store,
            session_id=session_id,
        )
        self.response = response
        self.run_calls = 0
        self.commit_calls = 0
        self.fail_main = False

    @property
    def session_id(self) -> str | None:
        return self._conversation.session_id

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._conversation.reasoning_effort

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._conversation.interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return self._conversation.auto_mode_unrestricted

    @property
    def items(self):
        return self._conversation.items

    async def ensure_persisted_session(self) -> str:
        return await self._conversation.ensure_persisted_session()

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self._conversation.set_reasoning_effort(effort)

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        self._conversation.set_interaction_mode(mode)

    async def run(
        self,
        prompt: str,
        *,
        sink=None,
        content_parts=(),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
        turn_id: str | None = None,
        ultracode_execution_id: str | None = None,
    ) -> AgentRunResult:
        del cancellation_policy, turn_source
        self.run_calls += 1
        if self.fail_main:
            raise RuntimeError("fixture main failure")
        if turn_id is None or ultracode_execution_id is None:
            raise AssertionError("the existing main path must receive exact Ultracode identity")
        return await self.commit_external_turn(
            prompt,
            response=self.response,
            turn_id=turn_id,
            execution_id=ultracode_execution_id,
            decision=UltracodeDelegationDecision.MAIN_MAX,
            content_parts=content_parts,
            sink=sink,
        )

    async def commit_external_turn(
        self,
        prompt: str,
        *,
        response: str,
        turn_id: str,
        execution_id: str,
        decision: UltracodeDelegationDecision,
        content_parts=(),
        sink=None,
    ) -> AgentRunResult:
        self.commit_calls += 1
        return await self._conversation.commit_external_turn(
            prompt,
            response=response,
            turn_id=turn_id,
            execution_id=execution_id,
            decision=decision,
            content_parts=content_parts,
            sink=sink,
        )


class _DynamicParentRunner(_ParentRunner):
    """Parent runner that exposes an ordinary path beside Ultracode."""

    def __init__(self, store: SqliteSessionStore, cwd: Path) -> None:
        super().__init__(store, cwd)
        self.ordinary_prompts: list[str] = []

    async def run(
        self,
        prompt: str,
        *,
        sink=None,
        content_parts=(),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
        turn_id: str | None = None,
        ultracode_execution_id: str | None = None,
    ) -> AgentRunResult:
        if ultracode_execution_id is not None:
            return await super().run(
                prompt,
                sink=sink,
                content_parts=content_parts,
                cancellation_policy=cancellation_policy,
                turn_source=turn_source,
                turn_id=turn_id,
                ultracode_execution_id=ultracode_execution_id,
            )
        del sink, content_parts, cancellation_policy, turn_source, turn_id
        self.ordinary_prompts.append(prompt)
        session_id = await self.ensure_persisted_session()
        return AgentRunResult(session_id, f"ordinary:{prompt}", (), (), (), 0)


def _binding(runner: _ParentRunner, cwd: Path) -> ConversationBinding:
    capabilities = SubagentCapabilitySet.from_runtime(
        tool_names=("read_file", "apply_patch"),
        cwd=cwd,
        sandbox_profile=SandboxProfile.OFF,
        enable_background_tasks=False,
        max_steps=4,
    )
    provider = cast(
        ModelProvider,
        SimpleNamespace(
            provider_name="fixture-provider",
            model_name="fixture-model",
            context_affinity="fixture-context",
        ),
    )
    return ConversationBinding(
        runner,
        provider,
        capabilities=capabilities,
        workspace_root=cwd,
    )


def _service(
    store: SqliteSessionStore,
    binding: ConversationBinding,
    swarm_factory,
    *,
    policy: UltracodeDelegationPolicy | None = None,
    owner_id: str = "fixture-ultracode-owner",
    result_adoption_factory: Any | None = None,
) -> UltracodeDelegationApplicationService:
    if result_adoption_factory is None:

        async def result_adoption_factory() -> _FixtureResultAdoption:
            return _FixtureResultAdoption(binding)

    return UltracodeDelegationApplicationService(
        cast(UltracodeStore, store),
        session_store=store,
        parent_binding=binding,
        swarm_factory=swarm_factory,
        result_adoption_factory=result_adoption_factory,
        policy=policy,
        owner_id=owner_id,
    )


class _CompletedSwarm:
    def __init__(self, result: AgentSwarmResult) -> None:
        self.result = result
        self.calls = 0
        self.close_calls = 0

    async def run(self, request: RunAgentSwarmRequest, *, sink=None) -> AgentSwarmResult:
        del sink
        self.calls += 1
        if request.swarm_run_id != self.result.swarm_run_id:
            raise AssertionError("the exact durable Swarm identity must be reused")
        return self.result

    async def close(self) -> None:
        self.close_calls += 1


class _FixtureResultAdoption:
    """Small canonical seam for unit tests that do not build worker evidence."""

    _records: ClassVar[dict[str, ResultAdoptionRecord]] = {}

    def __init__(self, binding: ConversationBinding) -> None:
        self._binding = binding

    async def get_result_adoption(self, adoption_id: str) -> ResultAdoptionRecord | None:
        return self._records.get(adoption_id)

    async def adopt(
        self,
        request: ResultAdoptionRequest,
        *,
        swarm_result: AgentSwarmResult,
    ) -> ResultAdoptionRecord:
        existing = self._records.get(request.adoption_id)
        if existing is not None:
            return existing
        parent_session_id = self._binding.runner.session_id
        if parent_session_id is None:
            raise AssertionError("fixture adoption requires a persisted parent session")
        root = self._binding.workspace_root
        if root is None:
            raise AssertionError("fixture adoption requires a parent root")
        repository = WorktreeRepositoryIdentity(
            common_dir=root,
            source_worktree=root,
            git_dir=root / ".git",
            head_sha="0" * 40,
        )
        source = ResultAdoptionSource(
            node_id="fixture-worker",
            parent_task_id="fixture-task",
            child_session_id="fixture-child",
            lease_id="fixture-lease",
            worktree_id=WorktreeId("fixture-worktree"),
            baseline_checkpoint_id=CheckpointId("cp-fixture-checkpoint"),
            base_commit_sha=repository.head_sha,
            final_workspace_fingerprint="1" * 64,
            capability_fingerprint="2" * 64,
            grant_fingerprint="3" * 64,
            parent_repository=repository,
        )
        now = datetime.now(UTC)
        plan = ResultAdoptionPlan(
            adoption_id=request.adoption_id,
            parent_session_id=parent_session_id,
            parent_workspace_root=root,
            parent_repository=repository,
            parent_head_sha=repository.head_sha,
            swarm_run_id=swarm_result.swarm_run_id,
            dag_id=swarm_result.dag.dag_id,
            dag_generation=swarm_result.dag.generation,
            dag_definition_fingerprint=swarm_result.dag.definition_fingerprint,
            sources=(source,),
            targets=(),
            created_at=now,
        )
        record = ResultAdoptionRecord(
            plan=plan,
            state=ResultAdoptionState.COMPLETED,
            owner_pid=os.getpid(),
            owner_token="fixture-adoption-token",
            lease_expires_at=now + timedelta(minutes=5),
            created_at=now,
            updated_at=now,
            targets=(),
        )
        self._records[request.adoption_id] = record
        return record


class _RecordingResultAdoption:
    def __init__(
        self,
        record: ResultAdoptionRecord,
        events: list[str] | None = None,
        adopt_result: ResultAdoptionRecord | None = None,
    ) -> None:
        self.record = record
        self.events = events if events is not None else []
        self.adopt_result = adopt_result or record
        self.get_calls = 0
        self.adopt_calls = 0

    async def get_result_adoption(self, adoption_id: str) -> ResultAdoptionRecord | None:
        self.get_calls += 1
        self.events.append("adoption_get")
        return self.record if adoption_id == self.record.adoption_id else None

    async def adopt(
        self,
        request: ResultAdoptionRequest,
        *,
        swarm_result: AgentSwarmResult,
    ) -> ResultAdoptionRecord:
        self.adopt_calls += 1
        self.events.append("adoption_adopt")
        if request.swarm_run_id != swarm_result.swarm_run_id:
            raise AssertionError("adoption must receive the exact Swarm result identity")
        return self.adopt_result


class _FailingSwarm:
    def __init__(self) -> None:
        self.calls = 0
        self.close_calls = 0

    async def run(self, request: RunAgentSwarmRequest, *, sink=None) -> AgentSwarmResult:
        del request, sink
        self.calls += 1
        raise RuntimeError("fixture Swarm failure")

    async def close(self) -> None:
        self.close_calls += 1


class _ProductionMainProvider:
    provider_name = "fixture-main"
    model_name = "fixture-main-model"
    context_affinity = "fixture-main-context"
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        context: Any,
        tools: Any,
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        self.calls += 1
        yield ModelCompleted("stop", response_text="production main answer")


class _DurableMainProvider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = "fixture-production-main"
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self, call_log: Path, phase: str) -> None:
        self._call_log = call_log
        self._phase = phase

    async def stream(
        self,
        context: Any,
        tools: Any,
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        _append_durable_json_line(
            self._call_log,
            {"branch": "main", "phase": self._phase, "pid": os.getpid()},
        )
        yield ModelCompleted("stop", response_text=_FRESH_MAIN_RESPONSE)


class _DurablePlanningProvider(_ProductionPlanningProvider):
    """Production Swarm fixture with durable provider-call provenance."""

    def __init__(self, state: _ProductionPlanningState, call_log: Path, phase: str) -> None:
        super().__init__(state)
        self._call_log = call_log
        self._phase = phase

    async def stream(
        self,
        context: Any,
        tools: Any,
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        contents = "\n".join(message.content for message in context.messages)
        if "bounded DAG Planner" in contents:
            kind = "planner"
            node_id = None
        elif "Leader decision authority" in contents:
            kind = "leader"
            node_id = None
        else:
            node_id = next(
                (
                    candidate
                    for candidate in ("a", "b", "c", "d")
                    if f"planning-node-{candidate}" in contents
                ),
                None,
            )
            kind = "worker"
        payload: dict[str, object] = {
            "branch": kind,
            "phase": self._phase,
            "pid": os.getpid(),
        }
        if node_id is not None:
            payload["node_id"] = node_id
        _append_durable_json_line(self._call_log, payload)

        release_task: asyncio.Task[None] | None = None
        if node_id in {"b", "c"}:

            async def release_fanout() -> None:
                await self._state.fanout_started.wait()
                self._state.release_fanout.set()

            release_task = asyncio.create_task(release_fanout())
        try:
            async for event in super().stream(context, tools, tool_policy=tool_policy):
                yield event
        finally:
            if release_task is not None and not release_task.done():
                release_task.cancel()
                with suppress(asyncio.CancelledError):
                    await release_task


class _ResultAdoptionPlanningProvider(_ProductionPlanningProvider):
    """Real DAG fixture whose preserved workers produce A.txt and C.txt."""

    def _worker_node_id(self, contents: str) -> str | None:
        return next(
            (
                candidate
                for candidate in ("a", "b", "c", "d")
                if f"planning-node-{candidate}" in contents
            ),
            None,
        )

    async def stream(
        self,
        context: Any,
        tools: Any,
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        contents = "\n".join(message.content for message in context.messages)
        if "bounded DAG Planner" in contents or "Leader decision authority" in contents:
            async for event in super().stream(context, tools, tool_policy=tool_policy):
                yield event
            return
        node_id = self._worker_node_id(contents)
        if node_id not in {"a", "b"}:
            async for event in super().stream(context, tools, tool_policy=tool_policy):
                yield event
            return
        if not tools:
            raise AssertionError(f"Result Adoption worker {node_id} received no tools")
        tool_result_seen = any(message.role is Role.TOOL for message in context.messages)
        if tool_result_seen:
            yield ModelCompleted("stop", response_text=f"production result {node_id}")
            async with self._state.lock:
                self._state.active -= 1
                self._state.timeline.append(f"complete:{node_id}")
            return
        self._state.worker_calls.append(node_id)
        async with self._state.lock:
            self._state.started.append(node_id)
            self._state.timeline.append(f"start:{node_id}")
            self._state.active += 1
            self._state.max_active = max(self._state.max_active, self._state.active)
            if {"b", "c"}.issubset(self._state.started):
                self._state.fanout_started.set()
        if node_id == "b":
            self._state.release_fanout.set()
            patch = "*** Begin Patch\n*** Add File: C.txt\n+worker-c\n*** End Patch"
        else:
            patch = "*** Begin Patch\n*** Update File: A.txt\n@@\n-base-a\n+worker-a\n*** End Patch"
        yield ModelToolCall(
            ToolCall(
                f"result-adoption-{node_id}",
                "apply_patch",
                {"patch": patch},
            )
        )
        yield ModelCompleted("tool_calls")


def _production_ultracode_settings(
    repository: Path,
    *,
    resume_id: str | None = None,
) -> ApplicationSettings:
    return ApplicationSettings(
        cwd=repository,
        sandbox="off",
        permission_mode=PermissionMode.BYPASS,
        max_steps=8,
        reasoning_effort=ReasoningEffort.ULTRACODE,
        resume_id=resume_id,
    )


def _durable_main_provider_factory(call_log: Path, phase: str) -> Any:
    def factory(_config: Any, _failover: bool) -> ModelProvider:
        return cast(ModelProvider, _DurableMainProvider(call_log, phase))

    return factory


def _durable_planning_provider_factory(
    state: _ProductionPlanningState,
    call_log: Path,
    phase: str,
) -> Any:
    def factory(_config: Any, _failover: bool) -> ModelProvider:
        return cast(ModelProvider, _DurablePlanningProvider(state, call_log, phase))

    return factory


def _result_adoption_provider_factory(state: _ProductionPlanningState) -> Any:
    def factory(_config: Any, _failover: bool) -> ModelProvider:
        return cast(ModelProvider, _ResultAdoptionPlanningProvider(state))

    return factory


def _composition_environment(root: Path, state_dir: Path) -> dict[str, str]:
    return {
        "HOME": str(root),
        "NEURO_CODE_HOME": str(state_dir),
        "FIXTURE_KEY": "fixture-key",
    }


def _spawn_production_main_crash(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    provider_call_log: str,
    prompt: str,
    turn_id: str,
) -> NoReturn:
    async def run() -> NoReturn:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        call_log = Path(provider_call_log)
        application: ApplicationComposition | None = None
        binding: ConversationBinding | None = None
        with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
            try:
                application = await ApplicationComposition.open(
                    _production_ultracode_settings(repository),
                    provider_factory=_durable_main_provider_factory(call_log, "l1"),
                )
                binding = await application.create_binding(
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                )
                store = cast(SqliteSessionStore, application.store)
                service = await application.create_ultracode_delegation_service(
                    parent_binding=binding,
                )
                original = store.compare_and_transition_ultracode_execution

                async def hooked(
                    proposed: UltracodeExecution,
                    *,
                    expected_generation: int,
                    expected_state: UltracodeExecutionState,
                ) -> UltracodeExecution:
                    if (
                        proposed.decision is UltracodeDelegationDecision.MAIN_MAX
                        and expected_state is UltracodeExecutionState.MAIN_MAX_RUNNING
                        and proposed.state is UltracodeExecutionState.COMPLETED
                    ):
                        completed = [
                            event
                            for event in await store.load_events(proposed.parent_session_id)
                            if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
                            and isinstance(event.get("data"), dict)
                            and event["data"].get("turn_id") == turn_id
                            and event["data"].get("ultracode_execution_id") == proposed.execution_id
                        ]
                        assert len(completed) == 1
                        os._exit(91)
                    return await original(
                        proposed,
                        expected_generation=expected_generation,
                        expected_state=expected_state,
                    )

                with patch.object(
                    store,
                    "compare_and_transition_ultracode_execution",
                    new=hooked,
                ):
                    await service.run_turn(RunTurnRequest(prompt, turn_id=turn_id))
                raise AssertionError("MAIN_MAX production process did not crash at the boundary")
            finally:
                if binding is not None:
                    await binding.close()
                if application is not None:
                    await application.close()

    asyncio.run(run())


def _spawn_production_swarm_crash(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    provider_call_log: str,
    prompt: str,
    turn_id: str,
) -> NoReturn:
    async def run() -> NoReturn:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        call_log = Path(provider_call_log)
        state = _ProductionPlanningState()
        application: ApplicationComposition | None = None
        binding: ConversationBinding | None = None
        with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
            try:
                application = await ApplicationComposition.open(
                    _production_ultracode_settings(repository),
                    provider_factory=_durable_planning_provider_factory(state, call_log, "l1"),
                )
                binding = await application.create_binding(
                    capabilities=_production_parent_capability(repository),
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                )
                store = cast(SqliteSessionStore, application.store)
                service = await application.create_ultracode_delegation_service(
                    parent_binding=binding,
                )
                original = AgentConversation.commit_external_turn
                expected_turn_id = turn_id

                async def hooked(
                    conversation: AgentConversation,
                    external_prompt: str,
                    *,
                    response: str,
                    turn_id: str,
                    execution_id: str,
                    decision: UltracodeDelegationDecision,
                    content_parts=(),
                    sink=None,
                ) -> AgentRunResult:
                    if decision is UltracodeDelegationDecision.BOUNDED_SWARM:
                        execution = await store.get_ultracode_execution(execution_id)
                        lower = await store.get_swarm_run(ultracode_swarm_run_id(execution_id))
                        assert execution is not None
                        assert execution.state is UltracodeExecutionState.FINALIZING
                        assert lower is not None
                        assert lower.state is AgentSwarmRunState.COMPLETED
                        assert turn_id == expected_turn_id
                        os._exit(92)
                    return await original(
                        conversation,
                        external_prompt,
                        response=response,
                        turn_id=turn_id,
                        execution_id=execution_id,
                        decision=decision,
                        content_parts=content_parts,
                        sink=sink,
                    )

                with patch.object(AgentConversation, "commit_external_turn", new=hooked):
                    await service.run_turn(RunTurnRequest(prompt, turn_id=turn_id))
                raise AssertionError("Swarm production process did not crash at the boundary")
            finally:
                if binding is not None:
                    await binding.close()
                if application is not None:
                    await application.close()

    asyncio.run(run())


class _UnexpectedPolicy(UltracodeDelegationPolicy):
    def decide(self, prompt: str) -> UltracodeDelegationDecision:
        del prompt
        raise AssertionError("a durable Ultracode decision must be reused on recovery")


def _completed_swarm_result(swarm_run_id: str, parent_session_id: str) -> AgentSwarmResult:
    now = datetime.now(UTC)
    dag = TaskDag.create(
        dag_id=f"dag-{swarm_run_id}",
        parent_session_id=parent_session_id,
        nodes=(TaskDagNode("worker", 0, "bounded worker"),),
        created_at=now,
        max_parallel=1,
    )
    completed_node = replace(dag.nodes[0], state=TaskDagNodeState.COMPLETED, generation=1)
    completed_dag = replace(
        dag,
        nodes=(completed_node,),
        state=TaskDagState.COMPLETED,
        generation=1,
        updated_at=now,
    )
    response = "bounded swarm answer"
    run = AgentSwarmRun(
        swarm_run_id=swarm_run_id,
        parent_session_id=parent_session_id,
        objective_fingerprint=objective_fingerprint("parallel objective"),
        planning_id=f"planning-{swarm_run_id}",
        state=AgentSwarmRunState.COMPLETED,
        generation=1,
        owner_id="fixture-swarm-owner",
        owner_pid=os.getpid(),
        owner_token="fixture-swarm-token",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
        root_dag_id=completed_dag.dag_id,
        current_dag_id=completed_dag.dag_id,
        current_dag_generation=completed_dag.generation,
        current_dag_definition_fingerprint=completed_dag.definition_fingerprint,
        final_response=response,
        final_result_fingerprint=terminal_result_fingerprint(
            swarm_run_id,
            completed_dag.dag_id,
            completed_dag.generation,
            completed_dag.definition_fingerprint,
            response,
        ),
    )
    return AgentSwarmResult(run, completed_dag)


async def _fixture_adoption_record(
    binding: ConversationBinding,
    result: AgentSwarmResult,
    *,
    execution_id: str,
    state: ResultAdoptionState = ResultAdoptionState.COMPLETED,
) -> ResultAdoptionRecord:
    request = ResultAdoptionRequest(
        ultracode_result_adoption_id(execution_id, result.swarm_run_id),
        result.swarm_run_id,
    )
    record = await _FixtureResultAdoption(binding).adopt(
        request,
        swarm_result=result,
    )
    return replace(record, state=state, error_kind=(state.value if state.terminal else None))


async def _store(tmp_path: Path) -> SqliteSessionStore:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    return store


def _row_count(database: Path, table: str) -> int:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row is not None else 0


def _append_durable_json_line(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_durable_json_lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        cast(dict[str, object], json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


_ULTRACODE_RESOURCE_TABLES = (
    "session_tasks",
    "subagent_links",
    "writable_subagent_leases",
    "parent_context_relays",
    "task_dag_dependency_relays",
    "task_dags",
    "leader_attempts",
    "leader_decisions",
    "orchestration_planning_attempts",
    "orchestration_plan_proposals",
    "orchestration_dag_replan_attempts",
    "orchestration_dag_replan_proposals",
    "orchestration_swarm_runs",
)


def _resource_counts(state_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    database = state_dir / "sessions.db"
    if database.exists():
        with closing(sqlite3.connect(database)) as connection:
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table in _ULTRACODE_RESOURCE_TABLES:
                if table in available:
                    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    counts[table] = int(row[0]) if row is not None else 0
    for filename, table in (
        ("worktrees.db", "managed_worktrees"),
        ("checkpoints.db", "checkpoints"),
    ):
        database = state_dir / filename
        if not database.exists():
            continue
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(row[0]) if row is not None else 0
    return counts


def _ultracode_identity_by_turn(database: Path, turn_id: str) -> tuple[str, str]:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT execution_id, parent_session_id "
            "FROM orchestration_ultracode_executions WHERE parent_turn_id = ?",
            (turn_id,),
        ).fetchone()
    if row is None:
        raise AssertionError(f"no Ultracode execution was published for {turn_id}")
    return str(row[0]), str(row[1])


_FRESH_MAIN_RESPONSE = "fresh-process main output"
_FRESH_SWARM_RESPONSE = "fresh-process swarm output"


def _fresh_ultracode_candidate(
    session_id: str,
    turn_id: str,
    prompt: str,
    decision: UltracodeDelegationDecision,
) -> UltracodeExecution:
    now = datetime.now(UTC)
    turn_input = TurnInput(prompt, source=TurnSource.USER)
    execution_id = ultracode_execution_id(session_id, turn_id)
    downstream_id = (
        turn_id
        if decision is UltracodeDelegationDecision.MAIN_MAX
        else ultracode_swarm_run_id(execution_id)
    )
    return UltracodeExecution(
        execution_id=execution_id,
        parent_session_id=session_id,
        parent_turn_id=turn_id,
        input_fingerprint=turn_input.fingerprint,
        context_fingerprint=context_fingerprints(()).context,
        decision=decision,
        downstream_id=downstream_id,
        provider_name="fixture-provider",
        model_name="fixture-model",
        context_affinity="fixture-context",
        state=UltracodeExecutionState.DECIDED,
        generation=0,
        owner_id="fresh-process-seed-owner",
        owner_pid=os.getpid(),
        owner_token="fresh-process-seed-token",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )


async def _fresh_ultracode_transition(
    store: SqliteSessionStore,
    run: UltracodeExecution,
    state: UltracodeExecutionState,
    **changes: object,
) -> UltracodeExecution:
    now = datetime.now(UTC)
    proposed = replace(
        run,
        **cast(dict[str, Any], changes),
        state=state,
        generation=run.generation + 1,
        lease_expires_at=now + timedelta(minutes=5),
        updated_at=now,
    )
    return await store.compare_and_transition_ultracode_execution(
        proposed,
        expected_generation=run.generation,
        expected_state=run.state,
    )


async def _fresh_seed_completed_swarm(
    store: SqliteSessionStore,
    result: AgentSwarmResult,
) -> None:
    await store.insert_task_dag(result.dag)
    claim = await store.claim_swarm_run(
        result.run,
        now=datetime.now(UTC),
        owner_is_alive=lambda _pid: False,
    )
    assert claim.acquired is True


async def _fresh_commit_parent_result(
    store: SqliteSessionStore,
    candidate: UltracodeExecution,
    prompt: str,
    response: str,
) -> None:
    conversation = AgentConversation(
        runtime=_Runtime(Path(store.database_path).parent),
        store=store,
        session_id=candidate.parent_session_id,
    )
    await conversation.commit_external_turn(
        prompt,
        response=response,
        turn_id=candidate.parent_turn_id,
        execution_id=candidate.execution_id,
        decision=candidate.decision,
    )


def _spawn_ultracode_crash(
    database_path: str,
    candidate: UltracodeExecution,
    prompt: str,
    stage: str,
) -> None:
    async def run() -> None:
        store = SqliteSessionStore(Path(database_path))
        await store.initialize()
        now = datetime.now(UTC)
        owned_candidate = replace(
            candidate,
            owner_id=f"fresh-process-owner-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=f"fresh-process-token-{os.getpid()}",
            lease_expires_at=now + timedelta(minutes=5),
            created_at=now,
            updated_at=now,
        )
        claim = await store.claim_ultracode_execution(
            owned_candidate,
            now=now,
            owner_is_alive=lambda _pid: False,
        )
        assert claim.acquired is True
        run = claim.execution
        if stage == "A":
            os._exit(81)
        if stage == "B":
            run = await _fresh_ultracode_transition(
                store,
                run,
                UltracodeExecutionState.MAIN_MAX_RUNNING,
            )
            del run
            await _fresh_commit_parent_result(store, candidate, prompt, _FRESH_MAIN_RESPONSE)
            os._exit(82)
        if stage == "C":
            result = _completed_swarm_result(run.downstream_id, run.parent_session_id)
            await _fresh_seed_completed_swarm(store, result)
            os._exit(83)
        if stage == "D":
            run = await _fresh_ultracode_transition(
                store,
                run,
                UltracodeExecutionState.BOUNDED_SWARM_RUNNING,
            )
            await store.start_turn_attempt(
                TurnRecoveryAttempt.create(
                    turn_id=run.parent_turn_id,
                    session_id=run.parent_session_id,
                    input=TurnInput(prompt, source=TurnSource.USER),
                    accepted_at=datetime.now(UTC),
                )
            )
            result = _completed_swarm_result(run.downstream_id, run.parent_session_id)
            await _fresh_seed_completed_swarm(store, result)
            await _fresh_ultracode_transition(
                store,
                run,
                UltracodeExecutionState.FINALIZING,
                final_response=result.final_response,
                final_result_fingerprint=ultracode_result_fingerprint(
                    run.execution_id,
                    result.final_response,
                ),
            )
            os._exit(84)
        if stage == "E":
            await _fresh_ultracode_transition(
                store,
                run,
                UltracodeExecutionState.BOUNDED_SWARM_RUNNING,
            )
            await _fresh_commit_parent_result(store, candidate, prompt, _FRESH_SWARM_RESPONSE)
            os._exit(85)
        raise AssertionError(f"unknown Ultracode fresh-process stage: {stage}")

    asyncio.run(run())


async def _join_ultracode_process(process: Any, expected_exit_code: int) -> None:
    await asyncio.to_thread(process.join, 120)
    if process.is_alive():
        process.terminate()
        await asyncio.to_thread(process.join, 15)
    try:
        assert process.exitcode == expected_exit_code
    finally:
        process.close()


@pytest.mark.asyncio
async def test_simple_ultracode_uses_existing_main_path_once_and_replays_exact_result() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        factory_calls = 0

        async def swarm_factory():
            nonlocal factory_calls
            factory_calls += 1
            raise AssertionError("simple Ultracode must not construct a Swarm")

        service = _service(store, binding, swarm_factory)
        request = RunTurnRequest("summarize this local file", turn_id="simple-turn")
        progress: list[AgentEventKind] = []

        async def sink(event) -> None:
            progress.append(event.kind)

        first = await service.run_turn(request, sink=sink)
        execution_id = ultracode_execution_id(runner.session_id or "", "simple-turn")

        assert first.response == "main answer"
        assert runner.run_calls == 1
        assert factory_calls == 0
        assert progress[0] is AgentEventKind.ULTRACODE_DELEGATION_PROGRESS
        assert progress.count(AgentEventKind.ULTRACODE_DELEGATION_PROGRESS) == 2
        assert AgentEventKind.TURN_COMPLETED in progress
        execution = await store.get_ultracode_execution(execution_id)
        assert execution is not None
        assert execution.decision is UltracodeDelegationDecision.MAIN_MAX
        assert execution.downstream_id == "simple-turn"
        assert execution.state is UltracodeExecutionState.COMPLETED
        assert await store.get_swarm_run(execution.downstream_id) is None
        messages = await store.load_messages(runner.session_id or "")
        assert [message for message in messages if message.role is Role.ASSISTANT] == [
            Message(Role.ASSISTANT, "main answer")
        ]

        replay = await _service(
            store,
            binding,
            swarm_factory,
            owner_id="new-process-owner",
        ).run_turn(request)
        assert replay.response == first.response
        assert runner.run_calls == 1
        messages = await store.load_messages(runner.session_id or "")
        assert len([message for message in messages if message.role is Role.ASSISTANT]) == 1

        events = await store.load_events(runner.session_id or "")
        completed = [
            event
            for event in events
            if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
            and isinstance(event.get("data"), dict)
            and event["data"].get("ultracode_execution_id") == execution_id
        ]
        assert len(completed) == 1


@pytest.mark.asyncio
async def test_decomposable_ultracode_uses_existing_swarm_and_replays_without_rerun() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        session_id = await runner.ensure_persisted_session()
        turn_id = "swarm-turn"
        execution_id = ultracode_execution_id(session_id, turn_id)
        swarm_id = ultracode_swarm_run_id(execution_id)
        swarm = _CompletedSwarm(_completed_swarm_result(swarm_id, session_id))
        factory_calls = 0

        async def swarm_factory():
            nonlocal factory_calls
            factory_calls += 1
            return swarm

        service = _service(store, binding, swarm_factory)
        request = RunTurnRequest("research these independent tasks in parallel", turn_id=turn_id)
        first = await service.run_turn(request)
        assert first.response == "bounded swarm answer"
        assert runner.run_calls == 0
        assert swarm.calls == 1
        assert swarm.close_calls == 1
        assert factory_calls == 1

        execution = await store.get_ultracode_execution(execution_id)
        assert execution is not None
        assert execution.decision is UltracodeDelegationDecision.BOUNDED_SWARM
        assert execution.downstream_id == swarm_id
        assert execution.state is UltracodeExecutionState.COMPLETED

        replay = await _service(
            store,
            binding,
            swarm_factory,
            policy=_UnexpectedPolicy(),
            owner_id="new-process-owner",
        ).run_turn(request)
        assert replay.response == first.response
        assert runner.run_calls == 0
        assert swarm.calls == 1
        assert factory_calls == 1
        messages = await store.load_messages(session_id)
        assert len([message for message in messages if message.role is Role.ASSISTANT]) == 1


@pytest.mark.asyncio
async def test_swarm_failure_is_indeterminate_and_never_falls_back_to_main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        swarm = _FailingSwarm()

        async def swarm_factory():
            return swarm

        request = RunTurnRequest("research independent tasks in parallel", turn_id="failed-turn")
        with pytest.raises(RuntimeError, match="fixture Swarm failure"):
            await _service(store, binding, swarm_factory).run_turn(request)

        execution_id = ultracode_execution_id(runner.session_id or "", "failed-turn")
        execution = await store.get_ultracode_execution(execution_id)
        assert execution is not None
        assert execution.decision is UltracodeDelegationDecision.BOUNDED_SWARM
        assert execution.state is UltracodeExecutionState.INDETERMINATE
        assert runner.run_calls == 0
        assert swarm.calls == 1
        with pytest.raises(ConfigurationError, match="automatic replay is disabled"):
            await _service(store, binding, swarm_factory, owner_id="recovery-owner").run_turn(
                request
            )
        assert runner.run_calls == 0
        assert swarm.calls == 1


@pytest.mark.asyncio
async def test_ultracode_store_uses_insert_once_and_generation_owner_fence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        session_id = await store.create_session(str(cwd), "fixture-provider", "fixture-model")
        now = datetime.now(UTC)
        turn_input = TurnInput("one exact input", source=TurnSource.USER)
        candidate = UltracodeExecution(
            execution_id="ultracode-cas",
            parent_session_id=session_id,
            parent_turn_id="cas-turn",
            input_fingerprint=turn_input.fingerprint,
            context_fingerprint=hashlib.sha256(b"context").hexdigest(),
            decision=UltracodeDelegationDecision.MAIN_MAX,
            downstream_id="cas-turn",
            provider_name="fixture-provider",
            model_name="fixture-model",
            context_affinity=None,
            state=UltracodeExecutionState.DECIDED,
            generation=0,
            owner_id="owner-one",
            owner_pid=os.getpid(),
            owner_token="token-one",
            lease_expires_at=now + timedelta(minutes=5),
            created_at=now,
            updated_at=now,
        )
        first = await store.claim_ultracode_execution(
            candidate,
            now=now,
            owner_is_alive=lambda _pid: True,
        )
        assert first.acquired

        other = replace(candidate, owner_id="owner-two", owner_token="token-two")
        blocked = await store.claim_ultracode_execution(
            other,
            now=now,
            owner_is_alive=lambda _pid: True,
        )
        assert not blocked.acquired
        assert blocked.execution.owner_id == "owner-one"

        takeover = await store.claim_ultracode_execution(
            other,
            now=now,
            owner_is_alive=lambda _pid: False,
        )
        assert takeover.acquired
        assert takeover.execution.generation == 1
        assert takeover.execution.owner_id == "owner-two"

        stale = replace(
            first.execution,
            state=UltracodeExecutionState.MAIN_MAX_RUNNING,
            generation=1,
            updated_at=now + timedelta(seconds=1),
        )
        with pytest.raises(UltracodeStoreError, match="lifecycle snapshot is stale"):
            await store.compare_and_transition_ultracode_execution(
                stale,
                expected_generation=0,
                expected_state=UltracodeExecutionState.DECIDED,
            )


@pytest.mark.asyncio
async def test_schema_27_to_29_migration_creates_ultracode_projection_without_loss() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        database = cwd / "sessions.db"
        store = await _store(cwd)
        session_id = await store.create_session(str(cwd), "fixture-provider", "fixture-model")
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TABLE orchestration_ultracode_executions")
            connection.execute("UPDATE schema_meta SET version = 27 WHERE singleton = 1")
        await store.initialize()

        assert SCHEMA_VERSION == 29
        assert await store.get_session(session_id) is not None
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone() == (29,)
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'orchestration_ultracode_executions'"
            ).fetchone() == ("orchestration_ultracode_executions",)


@pytest.mark.asyncio
async def test_turn_service_requires_explicit_ultracode_entry_and_never_routes_other_efforts() -> (
    None
):
    class _TurnRunner:
        def __init__(self, effort: ReasoningEffort) -> None:
            self.session_id = None
            self.reasoning_effort = effort
            self.calls = 0
            self.last_kwargs: dict[str, Any] = {}

        async def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
            del prompt
            self.calls += 1
            self.last_kwargs = kwargs
            return cast(AgentRunResult, SimpleNamespace(response="normal"))

    ultracode_runner = _TurnRunner(ReasoningEffort.ULTRACODE)
    with pytest.raises(ConfigurationError, match="entry is not configured"):
        await SessionTurnService(cast(Any, ultracode_runner)).run_turn(
            RunTurnRequest("explicit ultracode")
        )
    assert ultracode_runner.calls == 0

    delegate_calls = 0

    async def delegate(request: RunTurnRequest, sink) -> AgentRunResult:
        nonlocal delegate_calls
        del request, sink
        delegate_calls += 1
        return cast(AgentRunResult, SimpleNamespace(response="delegated"))

    result = await SessionTurnService(
        cast(Any, ultracode_runner),
        ultracode_delegate=delegate,
    ).run_turn(RunTurnRequest("explicit ultracode"))
    assert result.response == "delegated"
    assert delegate_calls == 1
    assert ultracode_runner.calls == 0

    for effort in (
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH,
        ReasoningEffort.MAX,
    ):
        runner = _TurnRunner(effort)
        result = await SessionTurnService(
            cast(Any, runner),
            ultracode_delegate=delegate,
        ).run_turn(RunTurnRequest("ordinary turn", turn_id="ordinary-turn"))
        assert result.response == "normal"
        assert runner.calls == 1
        assert runner.last_kwargs["turn_id"] == "ordinary-turn"


@pytest.mark.asyncio
async def test_long_lived_turn_service_switches_between_max_and_ultracode_without_rebinding() -> (
    None
):
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _DynamicParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        option = ProviderOption(
            "fixture-provider",
            "openai-chat",
            "fixture-model",
            True,
            True,
            default=True,
            context_window_tokens=100_000,
        )
        controller = ProfileConversationController(
            options=(option,),
            selected_profile=option.name,
            binding=binding,
            binding_factory=lambda _name: _unreachable_binding(),
            reasoning_effort=ReasoningEffort.MAX,
        )
        delegation_calls: list[tuple[str | None, ReasoningEffort]] = []

        async def delegate(request: RunTurnRequest, sink) -> AgentRunResult:
            delegation_calls.append((request.turn_id, controller.reasoning_effort))
            service = _service(store, controller.binding, _unexpected_swarm_factory)
            return await service.run_turn(request, sink=sink)

        turn_service = SessionTurnService(controller, ultracode_delegate=delegate)
        service_identity = id(turn_service)

        first_ordinary = await turn_service.run_turn(
            RunTurnRequest("ordinary before Ultracode", turn_id="ordinary-before")
        )
        selection = await controller.set_reasoning_effort(ReasoningEffort.ULTRACODE)
        first_ultracode = await turn_service.run_turn(
            RunTurnRequest("fix the first local typo", turn_id="ultracode-first")
        )
        await controller.set_reasoning_effort(ReasoningEffort.MAX)
        second_ordinary = await turn_service.run_turn(
            RunTurnRequest("ordinary after Ultracode", turn_id="ordinary-after")
        )
        await controller.set_reasoning_effort(ReasoningEffort.ULTRACODE)
        second_ultracode = await turn_service.run_turn(
            RunTurnRequest("fix the second local typo", turn_id="ultracode-second")
        )

        assert id(turn_service) == service_identity
        assert selection.workflow_orchestration_active is True
        assert first_ordinary.response == "ordinary:ordinary before Ultracode"
        assert second_ordinary.response == "ordinary:ordinary after Ultracode"
        assert first_ultracode.response == second_ultracode.response == "main answer"
        assert runner.ordinary_prompts == [
            "ordinary before Ultracode",
            "ordinary after Ultracode",
        ]
        assert runner.run_calls == 2
        assert runner.commit_calls == 2
        assert delegation_calls == [
            ("ultracode-first", ReasoningEffort.ULTRACODE),
            ("ultracode-second", ReasoningEffort.ULTRACODE),
        ]

        session_id = runner.session_id
        assert session_id is not None
        executions = [
            await store.get_ultracode_execution(ultracode_execution_id(session_id, turn_id))
            for turn_id in ("ultracode-first", "ultracode-second")
        ]
        assert all(execution is not None for execution in executions)
        assert [execution.execution_id for execution in executions if execution is not None] == [
            ultracode_execution_id(session_id, "ultracode-first"),
            ultracode_execution_id(session_id, "ultracode-second"),
        ]
        assert all(
            execution is not None
            and execution.decision is UltracodeDelegationDecision.MAIN_MAX
            and execution.state is UltracodeExecutionState.COMPLETED
            for execution in executions
        )
        messages = await store.load_messages(session_id)
        assert [message.content for message in messages if message.role is Role.ASSISTANT] == [
            "main answer",
            "main answer",
        ]
        completed = [
            event
            for event in await store.load_events(session_id)
            if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
            and isinstance(event.get("data"), dict)
            and event["data"].get("ultracode_execution_id")
            in {
                ultracode_execution_id(session_id, "ultracode-first"),
                ultracode_execution_id(session_id, "ultracode-second"),
            }
        ]
        assert len(completed) == 2
        assert len({event["data"]["turn_id"] for event in completed}) == 2


async def _unreachable_binding() -> ConversationBinding:
    raise AssertionError("the dynamic effort test must not replace its binding")


async def _unexpected_swarm_factory() -> Any:
    raise AssertionError("MAIN_MAX dynamic turns must not construct a Swarm")


def test_run_turn_request_rejects_noncanonical_values() -> None:
    with pytest.raises(ValueError, match="prompt must be a string"):
        RunTurnRequest(cast(Any, None))
    with pytest.raises(ValueError, match="content_parts"):
        RunTurnRequest("prompt", content_parts=(cast(Any, object()),))
    with pytest.raises(ValueError, match="cancellation_policy"):
        RunTurnRequest("prompt", cancellation_policy=cast(Any, "retain"))
    with pytest.raises(ValueError, match="turn_source"):
        RunTurnRequest("prompt", turn_source=cast(Any, "user"))
    with pytest.raises(ValueError, match="expected_session_id"):
        RunTurnRequest("prompt", expected_session_id=" ")
    with pytest.raises(ValueError, match="turn_id"):
        RunTurnRequest("prompt", turn_id=" ")
    with pytest.raises(ValueError, match="verification_requirements"):
        RunTurnRequest("prompt", verification_requirements=cast(Any, object()))


@pytest.mark.asyncio
async def test_structured_ultracode_request_fails_before_session_or_execution_claim() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        snapshot = VerificationRequirementsSnapshot.create(
            (VerificationRequirement.create(criterion="run the relevant checks"),)
        )
        swarm_calls = 0

        async def swarm_factory() -> Any:
            nonlocal swarm_calls
            swarm_calls += 1
            raise AssertionError("structured requirements must fail before Swarm creation")

        service = _service(store, binding, swarm_factory)
        request = RunTurnRequest(
            "research these independent tasks in parallel",
            turn_id="structured-ultracode",
            verification_requirements=snapshot,
        )

        with pytest.raises(ConfigurationError, match="structured verification requirements"):
            await service.run_turn(request)

        assert runner.session_id is None
        assert await store.list_sessions() == []
        assert swarm_calls == 0


def test_ultracode_execution_rejects_invalid_or_incomplete_terminal_identity() -> None:
    candidate = _fresh_ultracode_candidate(
        "validation-session",
        "validation-turn",
        "one exact input",
        UltracodeDelegationDecision.MAIN_MAX,
    )
    with pytest.raises(ValueError, match="safe identifier"):
        replace(candidate, execution_id="\x00")
    with pytest.raises(ValueError, match="fingerprint"):
        replace(candidate, input_fingerprint="x" * 64)
    with pytest.raises(ValueError, match="bounded safe text"):
        replace(candidate, final_response="\x01")
    with pytest.raises(ValueError, match="final response must not be empty"):
        ultracode_result_fingerprint(candidate.execution_id, " ")
    with pytest.raises(ValueError, match="decision must be canonical"):
        replace(candidate, decision=cast(Any, "main_max"))
    with pytest.raises(ValueError, match="state must be canonical"):
        replace(candidate, state=cast(Any, "decided"))
    with pytest.raises(ValueError, match="generation must be non-negative"):
        replace(candidate, generation=-1)
    with pytest.raises(ValueError, match="owner PID must be positive"):
        replace(candidate, owner_pid=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(candidate, lease_expires_at=datetime.fromisoformat("2026-01-01T00:00:00"))
    with pytest.raises(ValueError, match="must not precede creation"):
        replace(candidate, updated_at=candidate.created_at - timedelta(seconds=1))
    with pytest.raises(ValueError, match="requires a response"):
        replace(candidate, final_result_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="inconsistent"):
        replace(candidate, final_response="response", final_result_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="completed Ultracode execution"):
        replace(candidate, state=UltracodeExecutionState.COMPLETED)
    with pytest.raises(ValueError, match="finalizing Ultracode execution"):
        replace(candidate, state=UltracodeExecutionState.FINALIZING)


@pytest.mark.asyncio
async def test_ultracode_entry_validates_configuration_and_parent_guardrails() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)

        async def swarm_factory() -> _CompletedSwarm:
            raise AssertionError("configuration tests must not start a Swarm")

        with pytest.raises(ConfigurationError, match="parent binding"):
            UltracodeDelegationApplicationService(
                cast(UltracodeStore, store),
                session_store=store,
                parent_binding=cast(Any, object()),
                swarm_factory=swarm_factory,
            )
        with pytest.raises(ConfigurationError, match="store is invalid"):
            UltracodeDelegationApplicationService(
                cast(Any, object()),
                session_store=store,
                parent_binding=binding,
                swarm_factory=swarm_factory,
            )
        with pytest.raises(ConfigurationError, match="session store is invalid"):
            UltracodeDelegationApplicationService(
                cast(UltracodeStore, store),
                session_store=cast(Any, object()),
                parent_binding=binding,
                swarm_factory=swarm_factory,
            )
        with pytest.raises(ConfigurationError, match="factory is required"):
            UltracodeDelegationApplicationService(
                cast(UltracodeStore, store),
                session_store=store,
                parent_binding=binding,
                swarm_factory=cast(Any, object()),
            )
        with pytest.raises(ConfigurationError, match="lease duration"):
            UltracodeDelegationApplicationService(
                cast(UltracodeStore, store),
                session_store=store,
                parent_binding=binding,
                swarm_factory=swarm_factory,
                lease_seconds=0,
            )
        with pytest.raises(ConfigurationError, match="owner identity"):
            UltracodeDelegationApplicationService(
                cast(UltracodeStore, store),
                session_store=store,
                parent_binding=binding,
                swarm_factory=swarm_factory,
                owner_id=" ",
            )

        service = _service(store, binding, swarm_factory)
        with pytest.raises(ValueError, match="canonical"):
            await service.run_turn(cast(Any, object()))
        with pytest.raises(ConfigurationError, match="only for user turns"):
            await service.run_turn(
                RunTurnRequest(
                    "background request",
                    turn_source=TurnSource.BACKGROUND_TASK_AUTO_WAKE,
                )
            )
        runner._conversation._runtime.reasoning_effort = ReasoningEffort.MAX
        with pytest.raises(ConfigurationError, match="effort=ultracode"):
            await service.run_turn(RunTurnRequest("wrong effort", turn_id="wrong-effort"))
        runner._conversation._runtime.reasoning_effort = ReasoningEffort.ULTRACODE
        session_id = await runner.ensure_persisted_session()
        with pytest.raises(ConfigurationError, match="does not match"):
            await service.run_turn(
                RunTurnRequest(
                    "stale session request",
                    turn_id="stale-session",
                    expected_session_id=f"{session_id}-other",
                )
            )

        bad_runner_binding = ConversationBinding(cast(Any, object()), binding.provider)
        bad_service = _service(store, bad_runner_binding, swarm_factory)
        with pytest.raises(ConfigurationError, match="required seam"):
            await bad_service.run_turn(RunTurnRequest("missing runner seam"))


@pytest.mark.asyncio
async def test_ultracode_recovery_reuses_running_branch_and_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        next_swarm: _CompletedSwarm | None = None

        async def swarm_factory() -> _CompletedSwarm:
            if next_swarm is None:
                raise AssertionError("the recovery case has no expected Swarm")
            return next_swarm

        service = _service(store, binding, swarm_factory, owner_id="recovery-owner")
        session_id = await runner.ensure_persisted_session()

        async def seed_running(
            turn_id: str,
            prompt: str,
            decision: UltracodeDelegationDecision,
        ) -> UltracodeExecution:
            candidate = _fresh_ultracode_candidate(session_id, turn_id, prompt, decision)
            owned = replace(
                candidate,
                owner_id=service.owner_id,
                owner_pid=os.getpid(),
                owner_token=service._owner_token,
            )
            claim = await store.claim_ultracode_execution(
                owned,
                now=datetime.now(UTC),
                owner_is_alive=lambda _pid: True,
            )
            assert claim.acquired is True
            return await service._transition(
                claim.execution,
                (
                    UltracodeExecutionState.MAIN_MAX_RUNNING
                    if decision is UltracodeDelegationDecision.MAIN_MAX
                    else UltracodeExecutionState.BOUNDED_SWARM_RUNNING
                ),
            )

        main_prompt = "recover a normal main turn"
        main_run = await seed_running(
            "recovery-main-no-attempt",
            main_prompt,
            UltracodeDelegationDecision.MAIN_MAX,
        )
        result = await service.run_turn(
            RunTurnRequest(main_prompt, turn_id=main_run.parent_turn_id)
        )
        assert result.response == "main answer"
        assert runner.run_calls == 1

        open_prompt = "recover an open main attempt"
        open_run = await seed_running(
            "recovery-main-open-attempt",
            open_prompt,
            UltracodeDelegationDecision.MAIN_MAX,
        )
        open_input = TurnInput(open_prompt, source=TurnSource.USER)
        await store.start_turn_attempt(
            TurnRecoveryAttempt.create(
                turn_id=open_run.parent_turn_id,
                session_id=session_id,
                input=open_input,
                accepted_at=datetime.now(UTC),
            )
        )
        with pytest.raises(ConfigurationError, match="open parent attempt"):
            await service.run_turn(RunTurnRequest(open_prompt, turn_id=open_run.parent_turn_id))
        persisted_open = await store.get_ultracode_execution(open_run.execution_id)
        assert persisted_open is not None
        assert persisted_open.state is UltracodeExecutionState.INDETERMINATE
        await runner._conversation.abandon_recovery(open_run.parent_turn_id)

        missing_attempt_prompt = "recover a Swarm without an exact attempt"
        missing_attempt_run = await seed_running(
            "recovery-swarm-missing-attempt",
            missing_attempt_prompt,
            UltracodeDelegationDecision.BOUNDED_SWARM,
        )
        with pytest.raises(ConfigurationError, match="no exact parent attempt"):
            await service.run_turn(
                RunTurnRequest(missing_attempt_prompt, turn_id=missing_attempt_run.parent_turn_id)
            )

        no_lower_prompt = "recover a Swarm whose lower identity is absent"
        no_lower_run = await seed_running(
            "recovery-swarm-no-lower",
            no_lower_prompt,
            UltracodeDelegationDecision.BOUNDED_SWARM,
        )
        no_lower_input = TurnInput(no_lower_prompt, source=TurnSource.USER)
        await store.start_turn_attempt(
            TurnRecoveryAttempt.create(
                turn_id=no_lower_run.parent_turn_id,
                session_id=session_id,
                input=no_lower_input,
                accepted_at=datetime.now(UTC),
            )
        )
        with pytest.raises(ConfigurationError, match="replay is disabled"):
            await service.run_turn(
                RunTurnRequest(no_lower_prompt, turn_id=no_lower_run.parent_turn_id)
            )
        await runner._conversation.abandon_recovery(no_lower_run.parent_turn_id)

        recover_prompt = "recover an existing bounded Swarm"
        recover_run = await seed_running(
            "recovery-swarm-existing-lower",
            recover_prompt,
            UltracodeDelegationDecision.BOUNDED_SWARM,
        )
        recover_input = TurnInput(recover_prompt, source=TurnSource.USER)
        await store.start_turn_attempt(
            TurnRecoveryAttempt.create(
                turn_id=recover_run.parent_turn_id,
                session_id=session_id,
                input=recover_input,
                accepted_at=datetime.now(UTC),
            )
        )
        lower_result = _completed_swarm_result(recover_run.downstream_id, session_id)
        await _fresh_seed_completed_swarm(store, lower_result)
        next_swarm = _CompletedSwarm(lower_result)
        recovered = await service.run_turn(
            RunTurnRequest(recover_prompt, turn_id=recover_run.parent_turn_id)
        )
        assert recovered.response == lower_result.final_response
        assert next_swarm.calls == 0


@pytest.mark.asyncio
async def test_ultracode_rejects_noncanonical_or_mismatched_swarm_results() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        swarm_results: list[Any] = [object()]

        class _ArbitrarySwarm:
            def __init__(self, result: Any) -> None:
                self.result = result
                self.closed = False

            async def run(self, request: RunAgentSwarmRequest, *, sink=None) -> Any:
                del request, sink
                return self.result

            async def close(self) -> None:
                self.closed = True

        swarms: list[_ArbitrarySwarm] = []

        async def swarm_factory() -> _ArbitrarySwarm:
            swarm = _ArbitrarySwarm(swarm_results.pop(0))
            swarms.append(swarm)
            return swarm

        service = _service(store, binding, swarm_factory)
        with pytest.raises(ConfigurationError, match="non-canonical result"):
            await service.run_turn(
                RunTurnRequest("research independent tasks in parallel", turn_id="bad-result")
            )
        assert swarms[0].closed is True

        session_id = runner.session_id
        assert session_id is not None
        await runner._conversation.abandon_recovery("bad-result")
        swarm_results.append(_completed_swarm_result("wrong-swarm-id", session_id))
        with pytest.raises(ConfigurationError, match="does not match"):
            await service.run_turn(
                RunTurnRequest("research another independent task in parallel", turn_id="bad-id")
            )
        assert swarms[1].closed is True


@pytest.mark.asyncio
async def test_ultracode_transition_and_parent_result_guards_are_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)

        async def swarm_factory() -> _CompletedSwarm:
            raise AssertionError("guard tests must not start a Swarm")

        service = _service(store, binding, swarm_factory, owner_id="guard-owner")
        session_id = await runner.ensure_persisted_session()
        candidate = _fresh_ultracode_candidate(
            session_id,
            "guard-turn",
            "guard input",
            UltracodeDelegationDecision.MAIN_MAX,
        )
        candidate = replace(
            candidate,
            owner_id=service.owner_id,
            owner_pid=os.getpid(),
            owner_token=service._owner_token,
        )
        claim = await store.claim_ultracode_execution(
            candidate,
            now=datetime.now(UTC),
            owner_is_alive=lambda _pid: True,
        )
        assert claim.acquired is True
        running = await service._transition(
            claim.execution, UltracodeExecutionState.MAIN_MAX_RUNNING
        )
        with pytest.raises(ConfigurationError, match="invalid Ultracode lifecycle transition"):
            await service._transition(running, UltracodeExecutionState.MAIN_MAX_RUNNING)
        with pytest.raises(ConfigurationError, match="lifecycle fence was lost"):
            await service._transition(
                claim.execution,
                UltracodeExecutionState.MAIN_MAX_RUNNING,
            )
        with pytest.raises(ConfigurationError, match="missing its exact response"):
            await service._require_parent_result(session_id, "missing-turn", candidate.execution_id)

        open_attempt = TurnRecoveryAttempt.create(
            turn_id="guard-open",
            session_id=session_id,
            input=TurnInput("different prompt", source=TurnSource.USER),
            accepted_at=datetime.now(UTC),
        )
        await store.start_turn_attempt(open_attempt)
        guard_run = replace(
            candidate,
            parent_turn_id="guard-other",
            input_fingerprint=TurnInput("guard input", source=TurnSource.USER).fingerprint,
        )
        with pytest.raises(ConfigurationError, match="another unresolved turn"):
            await service._ensure_parent_attempt(
                guard_run,
                RunTurnRequest("guard input", turn_id="guard-other"),
            )

        resolved = replace(
            TurnRecoveryAttempt.create(
                turn_id=candidate.parent_turn_id,
                session_id=session_id,
                input=TurnInput("guard input", source=TurnSource.USER),
                accepted_at=datetime.now(UTC),
            ),
            resolution=TurnRecoveryResolution.ABANDONED,
            resolution_at=datetime.now(UTC),
        )

        async def load_resolved(_session_id: str) -> list[TurnRecoveryAttempt]:
            return [resolved]

        fake_session_store = SimpleNamespace(
            load_events=store.load_events,
            load_turn_attempts=load_resolved,
            start_turn_attempt=store.start_turn_attempt,
        )
        resolved_service = UltracodeDelegationApplicationService(
            cast(UltracodeStore, store),
            session_store=cast(Any, fake_session_store),
            parent_binding=binding,
            swarm_factory=swarm_factory,
            owner_id="resolved-guard-owner",
        )
        with pytest.raises(ConfigurationError, match="already resolved"):
            await resolved_service._ensure_parent_attempt(
                candidate,
                RunTurnRequest("guard input", turn_id=candidate.parent_turn_id),
            )


def test_policy_is_local_deterministic_and_bounded() -> None:
    policy = UltracodeDelegationPolicy()
    assert policy.decide("fix one local typo") is UltracodeDelegationDecision.MAIN_MAX
    assert (
        policy.decide("拆分多个文件并行研究独立任务") is UltracodeDelegationDecision.BOUNDED_SWARM
    )
    marker = "拆分多个文件并行研究独立任务"
    within_limit = marker + ("x" * (MAX_SWARM_OBJECTIVE_BYTES - len(marker.encode("utf-8"))))
    assert len(within_limit.encode("utf-8")) == MAX_SWARM_OBJECTIVE_BYTES
    assert policy.decide(within_limit) is UltracodeDelegationDecision.BOUNDED_SWARM
    assert policy.decide(within_limit + "x") is UltracodeDelegationDecision.MAIN_MAX
    with pytest.raises(ValueError, match="prompt is invalid"):
        policy.decide("\x00")


@pytest.mark.asyncio
async def test_oversized_decomposable_prompt_claims_main_before_swarm() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        marker = "拆分多个文件并行研究独立任务"
        prompt = marker + ("x" * (MAX_SWARM_OBJECTIVE_BYTES - len(marker.encode("utf-8")))) + "x"
        swarm_calls = 0

        async def swarm_factory() -> Any:
            nonlocal swarm_calls
            swarm_calls += 1
            raise AssertionError("an oversized objective must not construct a Swarm")

        request = RunTurnRequest(prompt, turn_id="oversized-main-turn")
        result = await _service(store, binding, swarm_factory).run_turn(request)

        assert result.response == "main answer"
        assert runner.run_calls == 1
        assert swarm_calls == 0
        execution = await store.get_ultracode_execution(
            ultracode_execution_id(runner.session_id or "", "oversized-main-turn")
        )
        assert execution is not None
        assert execution.decision is UltracodeDelegationDecision.MAIN_MAX
        assert execution.downstream_id == "oversized-main-turn"


def test_result_identity_is_exact_and_not_text_based() -> None:
    execution_id = "ultracode-result"
    first = ultracode_result_fingerprint(execution_id, "same text")
    second = ultracode_result_fingerprint(execution_id, "same text")
    assert first == second
    assert first != ultracode_result_fingerprint(execution_id, "different text")


def test_result_adoption_identity_is_deterministic_and_bound_to_both_runs() -> None:
    first = ultracode_result_adoption_id("execution-a", "swarm-a")
    assert first == ultracode_result_adoption_id("execution-a", "swarm-a")
    assert first != ultracode_result_adoption_id("execution-b", "swarm-a")
    assert first != ultracode_result_adoption_id("execution-a", "swarm-b")
    assert first.startswith("adopt-")


@pytest.mark.asyncio
async def test_main_max_performs_zero_result_adoption_activity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        adoption_calls = 0

        async def forbidden_adoption_factory() -> Any:
            nonlocal adoption_calls
            adoption_calls += 1
            raise AssertionError("MAIN_MAX must not construct Result Adoption")

        async def forbidden_swarm_factory() -> Any:
            raise AssertionError("MAIN_MAX must not construct a Swarm")

        result = await _service(
            store,
            binding,
            forbidden_swarm_factory,
            result_adoption_factory=forbidden_adoption_factory,
        ).run_turn(RunTurnRequest("fix one local typo", turn_id="main-only-adoption"))
        assert result.response == "main answer"
        assert adoption_calls == 0


@pytest.mark.asyncio
async def test_bounded_swarm_adopts_exact_result_before_parent_commit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        events: list[str] = []

        class OrderedRunner(_ParentRunner):
            async def commit_external_turn(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                events.append("parent_commit")
                return await super().commit_external_turn(prompt, **kwargs)

        runner = OrderedRunner(store, cwd)
        binding = _binding(runner, cwd)
        session_id = await runner.ensure_persisted_session()
        turn_id = "ordered-adoption-turn"
        execution_id = ultracode_execution_id(session_id, turn_id)
        result = _completed_swarm_result(ultracode_swarm_run_id(execution_id), session_id)
        record = await _fixture_adoption_record(
            binding,
            result,
            execution_id=execution_id,
            state=ResultAdoptionState.CLAIMED,
        )
        completed_record = replace(record, state=ResultAdoptionState.COMPLETED, error_kind=None)
        adapter = _RecordingResultAdoption(record, events, adopt_result=completed_record)

        class OrderedSwarm(_CompletedSwarm):
            async def run(self, request: RunAgentSwarmRequest, *, sink=None) -> AgentSwarmResult:
                events.append("swarm_completed")
                return await super().run(request, sink=sink)

        swarm = OrderedSwarm(result)

        async def swarm_factory() -> OrderedSwarm:
            return swarm

        response = await _service(
            store,
            binding,
            swarm_factory,
            result_adoption_factory=lambda: _completed_awaitable(adapter),
        ).run_turn(
            RunTurnRequest(
                "research these independent tasks in parallel",
                turn_id=turn_id,
            )
        )
        assert response.response == result.final_response
        assert events == ["swarm_completed", "adoption_get", "adoption_adopt", "parent_commit"]
        assert adapter.adopt_calls == 1


async def _completed_awaitable(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_adoption_conflict_is_parent_visible_and_never_falls_back() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        session_id = await runner.ensure_persisted_session()
        turn_id = "conflicting-adoption-turn"
        execution_id = ultracode_execution_id(session_id, turn_id)
        result = _completed_swarm_result(ultracode_swarm_run_id(execution_id), session_id)
        record = await _fixture_adoption_record(
            binding,
            result,
            execution_id=execution_id,
            state=ResultAdoptionState.CONFLICT,
        )
        adapter = _RecordingResultAdoption(record)
        swarm = _CompletedSwarm(result)

        async def swarm_factory() -> _CompletedSwarm:
            return swarm

        response = await _service(
            store,
            binding,
            swarm_factory,
            result_adoption_factory=lambda: _completed_awaitable(adapter),
        ).run_turn(
            RunTurnRequest(
                "research these independent tasks in parallel",
                turn_id=turn_id,
            )
        )
        assert "terminal_state: conflict" in response.response
        assert "adoption_id:" in response.response
        assert runner.run_calls == 0
        assert runner.commit_calls == 1
        assert adapter.adopt_calls == 0
        execution = await store.get_ultracode_execution(execution_id)
        assert execution is not None
        assert execution.state is UltracodeExecutionState.INDETERMINATE


async def _seed_bounded_recovery_case(
    store: SqliteSessionStore,
    service: UltracodeDelegationApplicationService,
    session_id: str,
    prompt: str,
    turn_id: str,
) -> tuple[UltracodeExecution, AgentSwarmResult]:
    candidate = _fresh_ultracode_candidate(
        session_id,
        turn_id,
        prompt,
        UltracodeDelegationDecision.BOUNDED_SWARM,
    )
    owned = replace(
        candidate,
        owner_id=service.owner_id,
        owner_pid=os.getpid(),
        owner_token=service._owner_token,
    )
    claim = await store.claim_ultracode_execution(
        owned,
        now=datetime.now(UTC),
        owner_is_alive=lambda _pid: True,
    )
    assert claim.acquired is True
    run = await service._transition(
        claim.execution,
        UltracodeExecutionState.BOUNDED_SWARM_RUNNING,
    )
    await store.start_turn_attempt(
        TurnRecoveryAttempt.create(
            turn_id=turn_id,
            session_id=session_id,
            input=TurnInput(prompt, source=TurnSource.USER),
            accepted_at=datetime.now(UTC),
        )
    )
    result = _completed_swarm_result(run.downstream_id, session_id)
    await _fresh_seed_completed_swarm(store, result)
    return run, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "error_pattern"),
    [
        ("missing-lower", "no exact completed Swarm evidence"),
        ("mismatched-result", "result integrity verification failed"),
    ],
)
async def test_legacy_finalizing_without_adoption_fails_closed_on_incomplete_evidence(
    tmp_path: Path,
    case: str,
    error_pattern: str,
) -> None:
    store = await _store(tmp_path)
    runner = _ParentRunner(store, tmp_path)
    binding = _binding(runner, tmp_path)
    session_id = await runner.ensure_persisted_session()
    prompt = "research these independent tasks in parallel"
    turn_id = f"legacy-finalizing-{case}"
    candidate = _fresh_ultracode_candidate(
        session_id,
        turn_id,
        prompt,
        UltracodeDelegationDecision.BOUNDED_SWARM,
    )

    class MissingAdoption:
        def __init__(self) -> None:
            self.adopt_calls = 0

        async def get_result_adoption(
            self,
            _adoption_id: str,
        ) -> ResultAdoptionRecord | None:
            return None

        async def adopt(
            self,
            _request: ResultAdoptionRequest,
            *,
            swarm_result: AgentSwarmResult,
        ) -> ResultAdoptionRecord:
            del swarm_result
            self.adopt_calls += 1
            raise AssertionError("incomplete legacy evidence must not start adoption")

    adoption = MissingAdoption()
    swarm_factory_calls = 0

    async def forbidden_swarm_factory() -> Any:
        nonlocal swarm_factory_calls
        swarm_factory_calls += 1
        raise AssertionError("legacy FINALIZING recovery must not replay the Swarm")

    service = _service(
        store,
        binding,
        forbidden_swarm_factory,
        owner_id="legacy-incomplete-owner",
        result_adoption_factory=lambda: _completed_awaitable(adoption),
    )
    owned = replace(
        candidate,
        owner_id=service.owner_id,
        owner_pid=os.getpid(),
        owner_token=service._owner_token,
    )
    claim = await store.claim_ultracode_execution(
        owned,
        now=datetime.now(UTC),
        owner_is_alive=lambda _pid: True,
    )
    assert claim.acquired is True
    running = await service._transition(
        claim.execution,
        UltracodeExecutionState.BOUNDED_SWARM_RUNNING,
    )
    await store.start_turn_attempt(
        TurnRecoveryAttempt.create(
            turn_id=turn_id,
            session_id=session_id,
            input=TurnInput(prompt, source=TurnSource.USER),
            accepted_at=datetime.now(UTC),
        )
    )
    if case == "mismatched-result":
        result = _completed_swarm_result(running.downstream_id, session_id)
        await _fresh_seed_completed_swarm(store, result)
    projected_response = "legacy projected response"
    await service._transition(
        running,
        UltracodeExecutionState.FINALIZING,
        final_response=projected_response,
        final_result_fingerprint=ultracode_result_fingerprint(
            running.execution_id,
            projected_response,
        ),
    )

    with pytest.raises(ConfigurationError, match=error_pattern):
        await service.run_turn(RunTurnRequest(prompt, turn_id=turn_id))

    assert swarm_factory_calls == 0
    assert adoption.adopt_calls == 0
    assert runner.commit_calls == 0
    persisted = await store.get_ultracode_execution(running.execution_id)
    assert persisted is not None
    assert persisted.state is UltracodeExecutionState.INDETERMINATE
    messages = await store.load_messages(session_id)
    assert [message.role for message in messages].count(Role.ASSISTANT) == 0
    completed = [
        event
        for event in await store.load_events(session_id)
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
    ]
    assert completed == []


@pytest.mark.asyncio
async def test_completed_adoption_recovers_without_swarm_or_adoption_writes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        session_id = await runner.ensure_persisted_session()

        async def forbidden_swarm_factory() -> Any:
            raise AssertionError("completed Swarm recovery must not construct the lower workflow")

        service = _service(store, binding, forbidden_swarm_factory)
        run, result = await _seed_bounded_recovery_case(
            store,
            service,
            session_id,
            "recover a completed bounded result",
            "adoption-recovery-completed",
        )
        record = await _fixture_adoption_record(
            binding,
            result,
            execution_id=run.execution_id,
        )
        adapter = _RecordingResultAdoption(record)
        recovery = _service(
            store,
            binding,
            forbidden_swarm_factory,
            result_adoption_factory=lambda: _completed_awaitable(adapter),
            owner_id="adoption-recovery-owner",
            policy=_UnexpectedPolicy(),
        )
        with patch(
            "neuro_code.application.workflows.ultracode.owner_is_alive",
            return_value=False,
        ):
            response = await recovery.run_turn(
                RunTurnRequest(
                    "recover a completed bounded result",
                    turn_id=run.parent_turn_id,
                )
            )
        assert response.response == result.final_response
        assert adapter.get_calls == 1
        assert adapter.adopt_calls == 0
        persisted = await store.get_ultracode_execution(run.execution_id)
        assert persisted is not None
        assert persisted.state is UltracodeExecutionState.COMPLETED
        assert runner.commit_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [ResultAdoptionState.INDETERMINATE, ResultAdoptionState.CONFLICT],
)
async def test_terminal_adoption_recovery_is_bounded_and_never_replays_swarm(
    terminal_state: ResultAdoptionState,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        store = await _store(cwd)
        runner = _ParentRunner(store, cwd)
        binding = _binding(runner, cwd)
        session_id = await runner.ensure_persisted_session()
        service = _service(
            store,
            binding,
            lambda: _never_construct_swarm(),
        )
        run, result = await _seed_bounded_recovery_case(
            store,
            service,
            session_id,
            "recover a terminal adoption result",
            f"adoption-recovery-{terminal_state.value}",
        )
        record = await _fixture_adoption_record(
            binding,
            result,
            execution_id=run.execution_id,
            state=terminal_state,
        )
        adapter = _RecordingResultAdoption(record)
        recovery = _service(
            store,
            binding,
            lambda: _never_construct_swarm(),
            result_adoption_factory=lambda: _completed_awaitable(adapter),
            owner_id=f"terminal-recovery-{terminal_state.value}",
            policy=_UnexpectedPolicy(),
        )
        with patch(
            "neuro_code.application.workflows.ultracode.owner_is_alive",
            return_value=False,
        ):
            response = await recovery.run_turn(
                RunTurnRequest(
                    "recover a terminal adoption result",
                    turn_id=run.parent_turn_id,
                )
            )
        assert f"terminal_state: {terminal_state.value}" in response.response
        assert adapter.adopt_calls == 0
        assert adapter.record == record
        persisted = await store.get_ultracode_execution(run.execution_id)
        assert persisted is not None
        assert persisted.state is UltracodeExecutionState.INDETERMINATE


async def _never_construct_swarm() -> Any:
    raise AssertionError("recovery must not construct a Swarm")


@pytest.mark.asyncio
async def test_real_composition_ultracode_simple_task_uses_main_without_orchestration() -> None:
    with tempfile.TemporaryDirectory(prefix="neuro-ultracode-main-production-") as directory:
        root = Path(directory)
        repository = _make_production_repository(root)
        state_dir = root / "state"
        _write_production_fixture_config(state_dir)
        provider = _ProductionMainProvider()
        environment = {
            "HOME": str(root),
            "NEURO_CODE_HOME": str(state_dir),
            "FIXTURE_KEY": "fixture-key",
        }
        with patch.dict("os.environ", environment, clear=False):
            application = await ApplicationComposition.open(
                ApplicationSettings(
                    cwd=repository,
                    sandbox="off",
                    permission_mode=PermissionMode.BYPASS,
                    max_steps=4,
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                ),
                provider_factory=lambda _config, _failover: cast(ModelProvider, provider),
            )
            binding = None
            try:
                binding = await application.create_binding(
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                )
                service = await application.create_ultracode_delegation_service(
                    parent_binding=binding,
                )
                request = RunTurnRequest("fix one local typo", turn_id="production-main-turn")
                first = await service.run_turn(request)
                replay = await (
                    await application.create_ultracode_delegation_service(
                        parent_binding=binding,
                    )
                ).run_turn(request)

                session_id = first.session_id
                execution_id = ultracode_execution_id(session_id, request.turn_id or "")
                store = cast(SqliteSessionStore, application.store)
                execution = await store.get_ultracode_execution(execution_id)
                assert execution is not None
                assert execution.decision is UltracodeDelegationDecision.MAIN_MAX
                assert execution.state is UltracodeExecutionState.COMPLETED
                assert first.response == replay.response == "production main answer"
                assert provider.calls == 1
                messages = await store.load_messages(session_id)
                assert [message.role for message in messages].count(Role.ASSISTANT) == 1
                assert await store.get_swarm_run(execution.downstream_id) is None
                assert not (state_dir / "worktrees.db").exists()
                assert _row_count(state_dir / "sessions.db", "orchestration_planning_attempts") == 0
                assert _row_count(state_dir / "sessions.db", "task_dags") == 0
                assert _row_count(state_dir / "sessions.db", "writable_subagent_leases") == 0
            finally:
                if binding is not None:
                    await binding.close()
                await application.close()


@pytest.mark.asyncio
async def test_real_composition_main_max_recovers_after_parent_commit_before_terminal_projection(
    tmp_path: Path,
) -> None:
    context = mp.get_context("spawn")
    root = tmp_path
    repository = _make_production_repository(root)
    state_dir = root / "state"
    _write_production_fixture_config(state_dir)
    database = state_dir / "sessions.db"
    call_log = root / "main-provider-calls.jsonl"
    prompt = "fix one local typo"
    turn_id = "production-main-process-turn"
    process = context.Process(
        target=_spawn_production_main_crash,
        args=(str(root), str(repository), str(state_dir), str(call_log), prompt, turn_id),
    )
    process.start()
    await _join_ultracode_process(process, 91)

    execution_id, session_id = _ultracode_identity_by_turn(database, turn_id)
    observer = SqliteSessionStore(database)
    await observer.initialize()
    crashed = await observer.get_ultracode_execution(execution_id)
    assert crashed is not None
    assert crashed.parent_session_id == session_id
    assert crashed.parent_turn_id == turn_id
    assert crashed.decision is UltracodeDelegationDecision.MAIN_MAX
    assert crashed.downstream_id == turn_id
    assert crashed.state is UltracodeExecutionState.MAIN_MAX_RUNNING
    main_calls = _read_durable_json_lines(call_log)
    assert len(main_calls) == 1
    assert main_calls[0]["branch"] == "main"
    assert main_calls[0]["phase"] == "l1"
    events_before = [
        event
        for event in await observer.load_events(session_id)
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
        and event["data"].get("ultracode_execution_id") == execution_id
    ]
    assert len(events_before) == 1
    resources_before = _resource_counts(state_dir)

    with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
        application = await ApplicationComposition.open(
            _production_ultracode_settings(repository, resume_id=session_id),
            provider_factory=_durable_main_provider_factory(call_log, "l2"),
        )
    binding = None
    try:
        binding = await application.create_binding(
            resume_id=session_id,
            reasoning_effort=ReasoningEffort.ULTRACODE,
        )
        service = await application.create_ultracode_delegation_service(
            parent_binding=binding,
        )
        result = await service.run_turn(RunTurnRequest(prompt, turn_id=turn_id))
    finally:
        if binding is not None:
            await binding.close()
        await application.close()

    assert result.response == _FRESH_MAIN_RESPONSE
    assert _read_durable_json_lines(call_log)[0]["phase"] == "l1"
    assert len(_read_durable_json_lines(call_log)) == 1
    recovered = await observer.get_ultracode_execution(execution_id)
    assert recovered is not None
    assert recovered.decision is UltracodeDelegationDecision.MAIN_MAX
    assert recovered.downstream_id == turn_id
    assert recovered.state is UltracodeExecutionState.COMPLETED
    assert recovered.final_response == _FRESH_MAIN_RESPONSE
    assert recovered.final_result_fingerprint == ultracode_result_fingerprint(
        execution_id,
        _FRESH_MAIN_RESPONSE,
    )
    assert await observer.get_swarm_run(turn_id) is None
    assert _resource_counts(state_dir) == resources_before
    assert not (state_dir / "worktrees.db").exists()
    assert _row_count(database, "orchestration_planning_attempts") == 0
    assert _row_count(database, "task_dags") == 0
    assert _row_count(database, "writable_subagent_leases") == 0
    messages = await observer.load_messages(session_id)
    assert [message.content for message in messages if message.role is Role.ASSISTANT] == [
        _FRESH_MAIN_RESPONSE
    ]
    completed_after = [
        event
        for event in await observer.load_events(session_id)
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
        and event["data"].get("ultracode_execution_id") == execution_id
    ]
    assert len(completed_after) == 1


@pytest.mark.asyncio
async def test_real_composition_ultracode_decomposable_task_uses_existing_bounded_swarm() -> None:
    with tempfile.TemporaryDirectory(prefix="neuro-ultracode-swarm-production-") as directory:
        root = Path(directory)
        repository = _make_production_repository(root)
        state_dir = root / "state"
        _write_production_fixture_config(state_dir)
        state = _ProductionPlanningState()
        environment = {
            "HOME": str(root),
            "NEURO_CODE_HOME": str(state_dir),
            "FIXTURE_KEY": "fixture-key",
        }

        def provider_factory(_config: Any, _failover: bool) -> ModelProvider:
            return cast(ModelProvider, _ProductionPlanningProvider(state))

        with patch.dict("os.environ", environment, clear=False):
            application = await ApplicationComposition.open(
                ApplicationSettings(
                    cwd=repository,
                    sandbox="off",
                    permission_mode=PermissionMode.BYPASS,
                    max_steps=8,
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                ),
                provider_factory=provider_factory,
            )
            binding = None
            running: asyncio.Task[AgentRunResult] | None = None
            try:
                binding = await application.create_binding(
                    capabilities=_production_parent_capability(repository),
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                )
                service = await application.create_ultracode_delegation_service(
                    parent_binding=binding,
                )
                request = RunTurnRequest(
                    "research these independent tasks in parallel",
                    turn_id="production-swarm-turn",
                )
                running = asyncio.create_task(service.run_turn(request))
                await asyncio.wait_for(state.fanout_started.wait(), timeout=90)
                session_id = binding.runner.session_id
                assert session_id is not None
                execution_id = ultracode_execution_id(session_id, request.turn_id or "")
                store = cast(SqliteSessionStore, application.store)
                in_flight = await store.get_ultracode_execution(execution_id)
                assert in_flight is not None
                assert in_flight.decision is UltracodeDelegationDecision.BOUNDED_SWARM
                assert in_flight.downstream_id == ultracode_swarm_run_id(execution_id)
                assert in_flight.state is UltracodeExecutionState.BOUNDED_SWARM_RUNNING
                assert state.max_active == 2
                assert state.planner_calls == 1
                assert state.leader_calls == 2
                assert state.worker_calls[0] == "a"
                assert set(state.worker_calls[1:3]) == {"b", "c"}
                assert "d" not in state.worker_calls
                state.release_fanout.set()
                first = await asyncio.wait_for(running, timeout=180)
                running = None

                async def replay_swarm_factory() -> Any:
                    return await application.create_agent_swarm_service(
                        parent_binding=binding,
                    )

                replay_service = UltracodeDelegationApplicationService(
                    cast(UltracodeStore, store),
                    session_store=store,
                    parent_binding=binding,
                    swarm_factory=replay_swarm_factory,
                    policy=_UnexpectedPolicy(),
                    owner_id="production-replay-owner",
                )
                replay = await replay_service.run_turn(request)
                assert first.response == replay.response == "planned DAG completed"
                assert state.planner_calls == 1
                assert state.leader_calls == 4
                assert len(state.worker_calls) == 4
                persisted = await store.get_ultracode_execution(execution_id)
                assert persisted is not None
                assert persisted.state is UltracodeExecutionState.COMPLETED
                swarm = await store.get_swarm_run(persisted.downstream_id)
                assert swarm is not None
                assert swarm.state is AgentSwarmRunState.COMPLETED
                leases = await store.list_writable_subagent_leases(
                    parent_session_id=session_id,
                )
                assert len(leases) == 4
                messages = await store.load_messages(session_id)
                assert [message.role for message in messages].count(Role.ASSISTANT) == 1
            finally:
                state.release_fanout.set()
                if running is not None and not running.done():
                    running.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await running
                if binding is not None:
                    await binding.close()
                await application.close()


def _spawn_integrated_result_adoption_boundary(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    turn_id: str,
    stage: str,
    exit_code: int,
) -> NoReturn:
    async def run() -> NoReturn:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        state = _ProductionPlanningState()
        application: ApplicationComposition | None = None
        binding: ConversationBinding | None = None
        with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
            try:
                application = await ApplicationComposition.open(
                    _production_ultracode_settings(repository),
                    provider_factory=_result_adoption_provider_factory(state),
                )
                binding = await application.create_binding(
                    capabilities=_production_parent_capability(repository),
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                )
                store = cast(SqliteSessionStore, application.store)
                delegation = await application.create_ultracode_delegation_service(
                    parent_binding=binding,
                )
                turn_service = SessionTurnService(
                    binding.runner,
                    ultracode_delegate=lambda request, sink: delegation.run_turn(
                        request,
                        sink=sink,
                    ),
                )
                if stage == "B":
                    original_commit = AgentConversation.commit_external_turn

                    async def hooked_commit(
                        conversation: AgentConversation,
                        external_prompt: str,
                        *,
                        response: str,
                        turn_id: str,
                        execution_id: str,
                        decision: UltracodeDelegationDecision,
                        content_parts=(),
                        sink=None,
                    ) -> AgentRunResult:
                        if decision is UltracodeDelegationDecision.BOUNDED_SWARM:
                            execution = await store.get_ultracode_execution(execution_id)
                            assert execution is not None
                            adoption = await store.get_result_adoption(
                                ultracode_result_adoption_id(
                                    execution_id,
                                    execution.downstream_id,
                                )
                            )
                            assert execution.state is UltracodeExecutionState.FINALIZING
                            assert adoption is not None
                            assert adoption.state is ResultAdoptionState.COMPLETED
                            assert turn_id == "result-adoption-recovery-turn"
                            os._exit(exit_code)
                        return await original_commit(
                            conversation,
                            external_prompt,
                            response=response,
                            turn_id=turn_id,
                            execution_id=execution_id,
                            decision=decision,
                            content_parts=content_parts,
                            sink=sink,
                        )

                    commit_patch = patch.object(
                        AgentConversation,
                        "commit_external_turn",
                        new=hooked_commit,
                    )
                else:
                    commit_patch = nullcontext()
                if stage in {"A", "C", "D"}:
                    original_adopt = ResultAdoptionApplicationService.adopt

                    async def hooked_adopt(
                        adoption_service: ResultAdoptionApplicationService,
                        request: ResultAdoptionRequest,
                        *,
                        swarm_result: AgentSwarmResult | None = None,
                    ) -> ResultAdoptionRecord:
                        prepared = await adoption_service.prepare(
                            request,
                            swarm_result=swarm_result,
                        )
                        assert prepared.state is ResultAdoptionState.CLAIMED
                        if stage == "A":
                            os._exit(exit_code)
                        if stage == "C":
                            await adoption_service._transition_adoption(
                                prepared,
                                ResultAdoptionState.INDETERMINATE,
                                error_kind="process_crash",
                            )
                            os._exit(exit_code)
                        (repository / "A.txt").write_text(
                            "parent-conflict\n",
                            encoding="utf-8",
                        )
                        result = await original_adopt(
                            adoption_service,
                            request,
                            swarm_result=swarm_result,
                        )
                        assert result.state is ResultAdoptionState.CONFLICT
                        os._exit(exit_code)

                    adopt_patch = patch.object(
                        ResultAdoptionApplicationService,
                        "adopt",
                        new=hooked_adopt,
                    )
                else:
                    adopt_patch = nullcontext()
                with commit_patch, adopt_patch:
                    await turn_service.run_turn(
                        RunTurnRequest(
                            "research these independent tasks in parallel",
                            turn_id=turn_id,
                        )
                    )
                raise AssertionError("integrated Result Adoption process did not crash")
            finally:
                if binding is not None:
                    await binding.close()
                if application is not None:
                    await application.close()

    asyncio.run(run())


def _spawn_legacy_pre_adoption_finalizing(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    prompt: str,
    turn_id: str,
    committed: bool,
    exit_code: int,
) -> NoReturn:
    """Persist the exact pre-integration #71 boundary through durable APIs."""

    async def run() -> NoReturn:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        state = _ProductionPlanningState()
        application: ApplicationComposition | None = None
        binding: ConversationBinding | None = None
        with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
            try:
                application = await ApplicationComposition.open(
                    _production_ultracode_settings(repository),
                    provider_factory=_result_adoption_provider_factory(state),
                )
                binding = await application.create_binding(
                    capabilities=_production_parent_capability(repository),
                    reasoning_effort=ReasoningEffort.ULTRACODE,
                )
                session_id = await binding.runner.ensure_persisted_session()
                store = cast(SqliteSessionStore, application.store)
                candidate = _fresh_ultracode_candidate(
                    session_id,
                    turn_id,
                    prompt,
                    UltracodeDelegationDecision.BOUNDED_SWARM,
                )
                now = datetime.now(UTC)
                candidate = replace(
                    candidate,
                    owner_id=f"legacy-ultracode-owner-{os.getpid()}",
                    owner_pid=os.getpid(),
                    owner_token=f"legacy-ultracode-token-{os.getpid()}",
                    lease_expires_at=now + timedelta(minutes=5),
                    created_at=now,
                    updated_at=now,
                )
                claim = await store.claim_ultracode_execution(
                    candidate,
                    now=now,
                    owner_is_alive=lambda _pid: False,
                )
                assert claim.acquired is True
                execution = await _fresh_ultracode_transition(
                    store,
                    claim.execution,
                    UltracodeExecutionState.BOUNDED_SWARM_RUNNING,
                )
                await store.start_turn_attempt(
                    TurnRecoveryAttempt.create(
                        turn_id=turn_id,
                        session_id=session_id,
                        input=TurnInput(prompt, source=TurnSource.USER),
                        accepted_at=datetime.now(UTC),
                    )
                )

                swarm = await application.create_agent_swarm_service(
                    parent_binding=binding,
                )
                try:
                    swarm_result = await swarm.run(
                        RunAgentSwarmRequest(execution.downstream_id, prompt),
                    )
                finally:
                    await swarm.close()
                assert swarm_result.run.state is AgentSwarmRunState.COMPLETED
                assert swarm_result.dag.state is TaskDagState.COMPLETED
                execution = await _fresh_ultracode_transition(
                    store,
                    execution,
                    UltracodeExecutionState.FINALIZING,
                    final_response=swarm_result.final_response,
                    final_result_fingerprint=ultracode_result_fingerprint(
                        execution.execution_id,
                        swarm_result.final_response,
                    ),
                )
                adoption_id = ultracode_result_adoption_id(
                    execution.execution_id,
                    execution.downstream_id,
                )
                assert await store.get_result_adoption(adoption_id) is None
                if committed:
                    await binding.runner.commit_external_turn(
                        prompt,
                        response=swarm_result.final_response,
                        turn_id=turn_id,
                        execution_id=execution.execution_id,
                        decision=UltracodeDelegationDecision.BOUNDED_SWARM,
                    )
                attempts = await store.load_turn_attempts(session_id)
                exact = next(item for item in attempts if item.turn_id == turn_id)
                assert (exact.resolution is TurnRecoveryResolution.COMMITTED) is committed
                os._exit(exit_code)
            finally:
                if binding is not None:
                    await binding.close()
                if application is not None:
                    await application.close()

    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("committed", "exit_code"),
    [(False, 121), (True, 122)],
    ids=("uncommitted-adopts-before-parent-success", "committed-preserves-history"),
)
async def test_real_composition_fresh_process_upgrades_legacy_finalizing_without_adoption(
    tmp_path: Path,
    committed: bool,
    exit_code: int,
) -> None:
    context = mp.get_context("spawn")
    root = tmp_path
    repository = _make_production_repository(root)
    (repository / "A.txt").write_text("base-a\n", encoding="utf-8")
    (repository / "B.txt").write_text("base-b\n", encoding="utf-8")
    _run_git(repository, "add", "A.txt", "B.txt")
    _run_git(repository, "commit", "-qm", "add legacy adoption fixtures")
    (repository / "U.txt").write_text("unrelated dirty\n", encoding="utf-8")
    head_before = _run_git(repository, "rev-parse", "HEAD").decode().strip()
    state_dir = root / "state"
    _write_production_fixture_config(state_dir)
    database = state_dir / "sessions.db"
    prompt = "research these independent tasks in parallel"
    turn_id = (
        "legacy-finalizing-committed-turn" if committed else "legacy-finalizing-uncommitted-turn"
    )
    process = context.Process(
        target=_spawn_legacy_pre_adoption_finalizing,
        args=(
            str(root),
            str(repository),
            str(state_dir),
            prompt,
            turn_id,
            committed,
            exit_code,
        ),
    )
    process.start()
    await _join_ultracode_process(process, exit_code)

    execution_id, session_id = _ultracode_identity_by_turn(database, turn_id)
    observer = SqliteSessionStore(database)
    await observer.initialize()
    execution_before = await observer.get_ultracode_execution(execution_id)
    assert execution_before is not None
    assert execution_before.decision is UltracodeDelegationDecision.BOUNDED_SWARM
    assert execution_before.state is UltracodeExecutionState.FINALIZING
    assert execution_before.final_response == "planned DAG completed"
    swarm_before = await observer.get_swarm_run(execution_before.downstream_id)
    assert swarm_before is not None
    assert swarm_before.state is AgentSwarmRunState.COMPLETED
    assert swarm_before.current_dag_id is not None
    dag_before = await observer.get_task_dag(swarm_before.current_dag_id)
    assert dag_before is not None
    assert dag_before.state is TaskDagState.COMPLETED
    adoption_id = ultracode_result_adoption_id(
        execution_id,
        execution_before.downstream_id,
    )
    assert await observer.get_result_adoption(adoption_id) is None
    assert _row_count(database, "result_adoptions") == 0
    attempts_before = await observer.load_turn_attempts(session_id)
    exact_attempt = next(item for item in attempts_before if item.turn_id == turn_id)
    assert (exact_attempt.resolution is TurnRecoveryResolution.COMMITTED) is committed
    messages_before = await observer.load_messages(session_id)
    assert [message.role for message in messages_before].count(Role.ASSISTANT) == int(committed)
    events_before = await observer.load_events(session_id)
    completed_before = [
        event
        for event in events_before
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
        and event["data"].get("ultracode_execution_id") == execution_id
    ]
    assert len(completed_before) == int(committed)
    resources_before = _resource_counts(state_dir)
    parent_before = {
        "A": (repository / "A.txt").read_bytes(),
        "B": (repository / "B.txt").read_bytes(),
        "C": (repository / "C.txt").read_bytes() if (repository / "C.txt").exists() else None,
        "U": (repository / "U.txt").read_bytes(),
    }

    fresh_state = _ProductionPlanningState()
    with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
        application = await ApplicationComposition.open(
            _production_ultracode_settings(repository, resume_id=session_id),
            provider_factory=_result_adoption_provider_factory(fresh_state),
        )
    binding = None
    try:
        binding = await application.create_binding(
            resume_id=session_id,
            capabilities=_production_parent_capability(repository),
            reasoning_effort=ReasoningEffort.ULTRACODE,
        )
        delegation = await application.create_ultracode_delegation_service(
            parent_binding=binding,
        )
        recovered = await delegation.run_turn(
            RunTurnRequest(prompt, turn_id=turn_id),
        )
    finally:
        if binding is not None:
            await binding.close()
        await application.close()

    assert recovered.response == "planned DAG completed"
    assert fresh_state.planner_calls == 0
    assert fresh_state.leader_calls == 0
    assert fresh_state.worker_calls == []
    assert fresh_state.zero_tool_calls == 0
    assert _resource_counts(state_dir) == resources_before
    assert _run_git(repository, "rev-parse", "HEAD").decode().strip() == head_before
    assert (repository / "B.txt").read_bytes() == parent_before["B"]
    assert (repository / "U.txt").read_bytes() == parent_before["U"]
    swarm_after = await observer.get_swarm_run(execution_before.downstream_id)
    assert swarm_after == swarm_before
    execution_after = await observer.get_ultracode_execution(execution_id)
    assert execution_after is not None
    assert execution_after.state is UltracodeExecutionState.COMPLETED
    assert execution_after.final_response == "planned DAG completed"

    adoption_after = await observer.get_result_adoption(adoption_id)
    if committed:
        assert adoption_after is None
        assert _row_count(database, "result_adoptions") == 0
        assert (repository / "A.txt").read_bytes() == parent_before["A"]
        if parent_before["C"] is None:
            assert not (repository / "C.txt").exists()
        else:
            assert (repository / "C.txt").read_bytes() == parent_before["C"]
    else:
        assert adoption_after is not None
        assert adoption_after.state is ResultAdoptionState.COMPLETED
        assert _row_count(database, "result_adoptions") == 1
        assert [target.target.path for target in adoption_after.targets] == ["A.txt", "C.txt"]
        assert all(target.state.value == "applied" for target in adoption_after.targets)
        assert (repository / "A.txt").read_text(encoding="utf-8") == "worker-a\n"
        assert (repository / "C.txt").read_text(encoding="utf-8") == "worker-c\n"

    messages_after = await observer.load_messages(session_id)
    assert [message.role for message in messages_after].count(Role.USER) == 1
    assert [message.role for message in messages_after].count(Role.ASSISTANT) == 1
    assert [message.content for message in messages_after if message.role is Role.ASSISTANT] == [
        "planned DAG completed"
    ]
    events_after = await observer.load_events(session_id)
    completed_after = [
        event
        for event in events_after
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
        and event["data"].get("ultracode_execution_id") == execution_id
    ]
    assert len(completed_after) == 1
    terminal_progress = [
        event
        for event in events_after
        if event.get("kind") == AgentEventKind.ULTRACODE_DELEGATION_PROGRESS.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("ultracode_execution_id") == execution_id
        and event["data"].get("state") == UltracodeExecutionState.COMPLETED.value
    ]
    assert len(terminal_progress) == 1


@pytest.mark.asyncio
async def test_real_composition_ultracode_swarm_adopts_worker_changes_safely() -> None:
    with tempfile.TemporaryDirectory(
        prefix="neuro-ultracode-result-adoption-production-"
    ) as directory:
        root = Path(directory)
        repository = _make_production_repository(root)
        (repository / "A.txt").write_text("base-a\n", encoding="utf-8")
        (repository / "B.txt").write_text("base-b\n", encoding="utf-8")
        _run_git(repository, "add", "A.txt", "B.txt")
        _run_git(repository, "commit", "-qm", "add adoption fixtures")
        (repository / "U.txt").write_text("unrelated dirty\n", encoding="utf-8")
        head_before = _run_git(repository, "rev-parse", "HEAD").decode().strip()
        state_dir = root / "state"
        _write_production_fixture_config(state_dir)
        state = _ProductionPlanningState()
        environment = _composition_environment(root, state_dir)
        progress: list[dict[str, object]] = []

        with patch.dict("os.environ", environment, clear=False):
            application = await ApplicationComposition.open(
                _production_ultracode_settings(repository),
                provider_factory=_result_adoption_provider_factory(state),
            )
        binding = None
        try:
            binding = await application.create_binding(
                capabilities=_production_parent_capability(repository),
                reasoning_effort=ReasoningEffort.ULTRACODE,
            )
            service = await application.create_ultracode_delegation_service(
                parent_binding=binding,
            )

            async def sink(event: Any) -> None:
                if event.kind is AgentEventKind.ULTRACODE_DELEGATION_PROGRESS:
                    data = event.data
                    if isinstance(data, dict):
                        progress.append(dict(data))

            result = await service.run_turn(
                RunTurnRequest(
                    "research these independent tasks in parallel",
                    turn_id="production-result-adoption-turn",
                ),
                sink=sink,
            )
            store = cast(SqliteSessionStore, application.store)
            session_id = result.session_id
            execution_id = ultracode_execution_id(
                session_id,
                "production-result-adoption-turn",
            )
            execution = await store.get_ultracode_execution(execution_id)
            assert execution is not None
            assert execution.state is UltracodeExecutionState.COMPLETED
            assert execution.decision is UltracodeDelegationDecision.BOUNDED_SWARM
            adoption_id = ultracode_result_adoption_id(execution_id, execution.downstream_id)
            adoption = await store.get_result_adoption(adoption_id)
            assert adoption is not None
            assert adoption.state is ResultAdoptionState.COMPLETED
            assert [target.target.path for target in adoption.targets] == ["A.txt", "C.txt"]
            assert all(target.state.value == "applied" for target in adoption.targets)
            assert result.response == "planned DAG completed"
            assert (repository / "A.txt").read_text(encoding="utf-8") == "worker-a\n"
            assert (repository / "B.txt").read_text(encoding="utf-8") == "base-b\n"
            assert (repository / "C.txt").read_text(encoding="utf-8") == "worker-c\n"
            assert (repository / "U.txt").read_text(encoding="utf-8") == "unrelated dirty\n"
            assert _run_git(repository, "rev-parse", "HEAD").decode().strip() == head_before

            leases = await store.list_writable_subagent_leases(parent_session_id=session_id)
            assert len(leases) == 4
            assert all(lease.state.value == "preserved" for lease in leases)
            with closing(sqlite3.connect(state_dir / "worktrees.db")) as connection:
                worktree_states = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT state FROM managed_worktrees ORDER BY worktree_id"
                    ).fetchall()
                ]
            assert worktree_states == ["ready"] * 4
            with closing(sqlite3.connect(state_dir / "checkpoints.db")) as connection:
                checkpoint_states = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT state FROM checkpoints ORDER BY checkpoint_id"
                    ).fetchall()
                ]
            assert checkpoint_states == ["ready"] * 4

            messages = await store.load_messages(session_id)
            assert [message.role for message in messages].count(Role.ASSISTANT) == 1
            completed = [
                event
                for event in await store.load_events(session_id)
                if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
                and isinstance(event.get("data"), dict)
                and event["data"].get("turn_id") == "production-result-adoption-turn"
            ]
            assert len(completed) == 1
            persisted_progress = [
                event
                for event in await store.load_events(session_id)
                if event.get("kind") == AgentEventKind.ULTRACODE_DELEGATION_PROGRESS.value
                and isinstance(event.get("data"), dict)
            ]
            stages = [
                str(cast(dict[str, object], event["data"]).get("stage"))
                for event in persisted_progress
            ]
            assert stages.index("swarm_completed") < stages.index("adoption_preparing")
            assert stages.index("adoption_preparing") < stages.index("adoption_applying")
            assert stages.index("adoption_applying") < stages.index("adoption_completed")
        finally:
            if binding is not None:
                await binding.close()
            await application.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "exit_code"),
    [("A", 101), ("B", 102), ("C", 103), ("D", 104)],
)
async def test_real_composition_fresh_process_result_adoption_recovery_matrix(
    tmp_path: Path,
    stage: str,
    exit_code: int,
) -> None:
    context = mp.get_context("spawn")
    root = tmp_path
    repository = _make_production_repository(root)
    (repository / "A.txt").write_text("base-a\n", encoding="utf-8")
    (repository / "B.txt").write_text("base-b\n", encoding="utf-8")
    _run_git(repository, "add", "A.txt", "B.txt")
    _run_git(repository, "commit", "-qm", "add adoption recovery fixtures")
    (repository / "U.txt").write_text("unrelated dirty\n", encoding="utf-8")
    head_before = _run_git(repository, "rev-parse", "HEAD").decode().strip()
    state_dir = root / "state"
    _write_production_fixture_config(state_dir)
    database = state_dir / "sessions.db"
    turn_id = "result-adoption-recovery-turn"
    process = context.Process(
        target=_spawn_integrated_result_adoption_boundary,
        args=(str(root), str(repository), str(state_dir), turn_id, stage, exit_code),
    )
    process.start()
    await _join_ultracode_process(process, exit_code)

    execution_id, session_id = _ultracode_identity_by_turn(database, turn_id)
    observer = SqliteSessionStore(database)
    await observer.initialize()
    execution_before = await observer.get_ultracode_execution(execution_id)
    assert execution_before is not None
    assert execution_before.downstream_id == ultracode_swarm_run_id(execution_id)
    lower_before = await observer.get_swarm_run(execution_before.downstream_id)
    assert lower_before is not None
    assert lower_before.state is AgentSwarmRunState.COMPLETED
    adoption_id = ultracode_result_adoption_id(execution_id, execution_before.downstream_id)
    adoption_before = await observer.get_result_adoption(adoption_id)
    assert adoption_before is not None
    expected_adoption_state = {
        "A": ResultAdoptionState.CLAIMED,
        "B": ResultAdoptionState.COMPLETED,
        "C": ResultAdoptionState.INDETERMINATE,
        "D": ResultAdoptionState.CONFLICT,
    }[stage]
    assert adoption_before.state is expected_adoption_state
    expected_execution_state = (
        UltracodeExecutionState.FINALIZING
        if stage == "B"
        else UltracodeExecutionState.BOUNDED_SWARM_RUNNING
    )
    assert execution_before.state is expected_execution_state
    assert [target.target.path for target in adoption_before.targets] == ["A.txt", "C.txt"]
    assert _resource_counts(state_dir)["writable_subagent_leases"] == 4
    parent_before_recovery = {
        "A": (repository / "A.txt").read_bytes(),
        "B": (repository / "B.txt").read_bytes(),
        "C": (repository / "C.txt").read_bytes() if (repository / "C.txt").exists() else None,
        "U": (repository / "U.txt").read_bytes(),
    }
    resources_before_recovery = _resource_counts(state_dir)

    fresh_state = _ProductionPlanningState()
    with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
        application = await ApplicationComposition.open(
            _production_ultracode_settings(repository, resume_id=session_id),
            provider_factory=_result_adoption_provider_factory(fresh_state),
        )
    binding = None
    try:
        binding = await application.create_binding(
            resume_id=session_id,
            capabilities=_production_parent_capability(repository),
            reasoning_effort=ReasoningEffort.ULTRACODE,
        )
        delegation = await application.create_ultracode_delegation_service(
            parent_binding=binding,
        )
        turn_service = SessionTurnService(
            binding.runner,
            ultracode_delegate=lambda request, sink: delegation.run_turn(
                request,
                sink=sink,
            ),
        )
        recovered = await turn_service.run_turn(
            RunTurnRequest(
                "research these independent tasks in parallel",
                turn_id=turn_id,
            )
        )
    finally:
        if binding is not None:
            await binding.close()
        await application.close()

    assert fresh_state.planner_calls == 0
    assert fresh_state.leader_calls == 0
    assert fresh_state.worker_calls == []
    assert fresh_state.zero_tool_calls == 0
    assert _resource_counts(state_dir) == resources_before_recovery
    assert _run_git(repository, "rev-parse", "HEAD").decode().strip() == head_before
    assert (repository / "B.txt").read_bytes() == parent_before_recovery["B"]
    assert (repository / "U.txt").read_bytes() == parent_before_recovery["U"]
    lower_after = await observer.get_swarm_run(execution_before.downstream_id)
    assert lower_after == lower_before

    execution_after = await observer.get_ultracode_execution(execution_id)
    adoption_after = await observer.get_result_adoption(adoption_id)
    assert execution_after is not None
    assert adoption_after is not None
    if stage == "A":
        assert recovered.response == "planned DAG completed"
        assert execution_after.state is UltracodeExecutionState.COMPLETED
        assert adoption_after.state is ResultAdoptionState.COMPLETED
        assert adoption_after.version > adoption_before.version
        assert all(target.state.value == "applied" for target in adoption_after.targets)
        assert (repository / "A.txt").read_text(encoding="utf-8") == "worker-a\n"
        assert (repository / "C.txt").read_text(encoding="utf-8") == "worker-c\n"
    elif stage == "B":
        assert recovered.response == "planned DAG completed"
        assert execution_after.state is UltracodeExecutionState.COMPLETED
        assert adoption_after == adoption_before
        assert (repository / "A.txt").read_bytes() == parent_before_recovery["A"]
        if parent_before_recovery["C"] is None:
            assert not (repository / "C.txt").exists()
        else:
            assert (repository / "C.txt").read_bytes() == parent_before_recovery["C"]
    else:
        assert f"terminal_state: {expected_adoption_state.value}" in recovered.response
        assert execution_after.state is UltracodeExecutionState.INDETERMINATE
        assert adoption_after == adoption_before
        assert (repository / "A.txt").read_bytes() == parent_before_recovery["A"]
        if parent_before_recovery["C"] is None:
            assert not (repository / "C.txt").exists()
        else:
            assert (repository / "C.txt").read_bytes() == parent_before_recovery["C"]

    leases = await observer.list_writable_subagent_leases(parent_session_id=session_id)
    assert len(leases) == 4
    assert all(lease.state.value == "preserved" for lease in leases)
    with closing(sqlite3.connect(state_dir / "worktrees.db")) as connection:
        assert [
            str(row[0])
            for row in connection.execute(
                "SELECT state FROM managed_worktrees ORDER BY worktree_id"
            ).fetchall()
        ] == ["ready"] * 4
    with closing(sqlite3.connect(state_dir / "checkpoints.db")) as connection:
        assert [
            str(row[0])
            for row in connection.execute(
                "SELECT state FROM checkpoints ORDER BY checkpoint_id"
            ).fetchall()
        ] == ["ready"] * 4
    messages = await observer.load_messages(session_id)
    assert [message.role for message in messages].count(Role.ASSISTANT) == 1
    completed_events = [
        event
        for event in await observer.load_events(session_id)
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
        and event["data"].get("ultracode_execution_id") == execution_id
    ]
    assert len(completed_events) == 1


@pytest.mark.asyncio
async def test_real_composition_swarm_recovers_after_lower_completion_before_parent_commit(
    tmp_path: Path,
) -> None:
    context = mp.get_context("spawn")
    root = tmp_path
    repository = _make_production_repository(root)
    state_dir = root / "state"
    _write_production_fixture_config(state_dir)
    database = state_dir / "sessions.db"
    call_log = root / "swarm-provider-calls.jsonl"
    prompt = "research these independent tasks in parallel"
    turn_id = "production-swarm-process-turn"
    process = context.Process(
        target=_spawn_production_swarm_crash,
        args=(str(root), str(repository), str(state_dir), str(call_log), prompt, turn_id),
    )
    process.start()
    await _join_ultracode_process(process, 92)

    execution_id, session_id = _ultracode_identity_by_turn(database, turn_id)
    swarm_id = ultracode_swarm_run_id(execution_id)
    observer = SqliteSessionStore(database)
    await observer.initialize()
    crashed = await observer.get_ultracode_execution(execution_id)
    assert crashed is not None
    assert crashed.parent_session_id == session_id
    assert crashed.parent_turn_id == turn_id
    assert crashed.decision is UltracodeDelegationDecision.BOUNDED_SWARM
    assert crashed.downstream_id == swarm_id
    assert crashed.state is UltracodeExecutionState.FINALIZING
    lower = await observer.get_swarm_run(swarm_id)
    assert lower is not None
    assert lower.state is AgentSwarmRunState.COMPLETED
    l1_calls = _read_durable_json_lines(call_log)
    assert len(l1_calls) == 9
    assert sum(record["branch"] == "planner" for record in l1_calls) == 1
    assert sum(record["branch"] == "leader" for record in l1_calls) == 4
    assert sum(record["branch"] == "worker" for record in l1_calls) == 4
    resources_before = _resource_counts(state_dir)

    state = _ProductionPlanningState()
    with patch.dict("os.environ", _composition_environment(root, state_dir), clear=False):
        application = await ApplicationComposition.open(
            _production_ultracode_settings(repository, resume_id=session_id),
            provider_factory=_durable_planning_provider_factory(state, call_log, "l2"),
        )
    binding = None
    try:
        binding = await application.create_binding(
            resume_id=session_id,
            capabilities=_production_parent_capability(repository),
            reasoning_effort=ReasoningEffort.ULTRACODE,
        )
        service = await application.create_ultracode_delegation_service(
            parent_binding=binding,
        )
        result = await service.run_turn(RunTurnRequest(prompt, turn_id=turn_id))
    finally:
        if binding is not None:
            await binding.close()
        await application.close()

    assert result.response == "planned DAG completed"
    calls_after = _read_durable_json_lines(call_log)
    assert calls_after == l1_calls
    recovered = await observer.get_ultracode_execution(execution_id)
    assert recovered is not None
    assert recovered.decision is UltracodeDelegationDecision.BOUNDED_SWARM
    assert recovered.downstream_id == swarm_id
    assert recovered.state is UltracodeExecutionState.COMPLETED
    assert recovered.final_response == "planned DAG completed"
    recovered_lower = await observer.get_swarm_run(swarm_id)
    assert recovered_lower is not None
    assert recovered_lower.state is AgentSwarmRunState.COMPLETED
    assert _resource_counts(state_dir) == resources_before
    messages = await observer.load_messages(session_id)
    assert [message.content for message in messages if message.role is Role.ASSISTANT] == [
        "planned DAG completed"
    ]
    completed = [
        event
        for event in await observer.load_events(session_id)
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
        and event["data"].get("ultracode_execution_id") == execution_id
    ]
    assert len(completed) == 1
    assert _row_count(database, "orchestration_planning_attempts") == 1
    assert _row_count(database, "orchestration_plan_proposals") == 1
    assert _row_count(database, "task_dags") == 1
    assert _row_count(database, "writable_subagent_leases") == 4
    assert _row_count(database, "orchestration_swarm_runs") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "exit_code"),
    [("A", 81), ("B", 82), ("C", 83), ("D", 84), ("E", 85)],
)
async def test_ultracode_fresh_process_recovery_matrix(
    tmp_path: Path,
    stage: str,
    exit_code: int,
) -> None:
    """Prove exact branch and result recovery across an actual process death."""

    context = mp.get_context("spawn")
    database = tmp_path / "sessions.db"
    store = SqliteSessionStore(database)
    await store.initialize()
    session_id = await store.create_session(
        str(tmp_path),
        "fixture-provider",
        "fixture-model",
        context_affinity="fixture-context",
    )
    prompt = "research these independent tasks in parallel"
    decision = (
        UltracodeDelegationDecision.MAIN_MAX
        if stage in {"A", "B"}
        else UltracodeDelegationDecision.BOUNDED_SWARM
    )
    turn_id = f"fresh-process-{stage}-turn"
    candidate = _fresh_ultracode_candidate(session_id, turn_id, prompt, decision)
    process = context.Process(
        target=_spawn_ultracode_crash,
        args=(str(database), candidate, prompt, stage),
    )
    process.start()
    await _join_ultracode_process(process, exit_code)

    expected_response = {
        "A": "main answer",
        "B": _FRESH_MAIN_RESPONSE,
        "C": "bounded swarm answer",
        "D": "bounded swarm answer",
        "E": _FRESH_SWARM_RESPONSE,
    }[stage]
    runner = _ParentRunner(
        store,
        tmp_path,
        response=expected_response,
        session_id=session_id,
    )
    binding = _binding(runner, tmp_path)
    factory_calls = 0
    completed_swarm = _completed_swarm_result(candidate.downstream_id, session_id)
    swarm = _CompletedSwarm(completed_swarm)

    async def swarm_factory() -> _CompletedSwarm:
        nonlocal factory_calls
        factory_calls += 1
        if stage != "C":
            raise AssertionError(f"fresh-process stage {stage} must not start a new Swarm")
        return swarm

    service = _service(
        store,
        binding,
        swarm_factory,
        policy=_UnexpectedPolicy(),
        owner_id=f"fresh-parent-owner-{stage}",
    )
    request = RunTurnRequest(prompt, turn_id=turn_id)
    result = await service.run_turn(request)

    execution = await store.get_ultracode_execution(candidate.execution_id)
    assert execution is not None
    assert execution.decision is decision
    assert execution.downstream_id == candidate.downstream_id
    assert execution.state is UltracodeExecutionState.COMPLETED
    assert execution.final_response == expected_response
    assert result.response == expected_response
    assert runner.run_calls == (1 if stage == "A" else 0)
    assert runner.commit_calls == 1
    assert factory_calls == (1 if stage == "C" else 0)
    assert swarm.calls == (1 if stage == "C" else 0)

    lower = await store.get_swarm_run(candidate.downstream_id)
    if stage in {"C", "D"}:
        assert lower is not None
        assert lower.state is AgentSwarmRunState.COMPLETED
    else:
        assert lower is None

    messages = await store.load_messages(session_id)
    assert [message.content for message in messages if message.role is Role.ASSISTANT] == [
        expected_response
    ]
    completed_events = [
        event
        for event in await store.load_events(session_id)
        if event.get("kind") == AgentEventKind.TURN_COMPLETED.value
        and isinstance(event.get("data"), dict)
        and event["data"].get("turn_id") == turn_id
        and event["data"].get("ultracode_execution_id") == candidate.execution_id
    ]
    assert len(completed_events) == 1
