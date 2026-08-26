from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from neuro_code.application.permissions.policy import PermissionMode
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider, ModelToolPolicy
from neuro_code.application.ports.model_planning import ModelPlanningStoreError
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows.leader import RunLeaderRequest
from neuro_code.application.workflows.model_planning import (
    ModelDagPlanningApplicationService,
    RunModelDagPlanningRequest,
)
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.application.workflows.task_dag import CreateTaskDagRequest
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.conversation.events import ModelCompleted, ModelEvent
from neuro_code.domain.conversation.messages import (
    ContentPart,
    Message,
    Role,
    ToolCall,
)
from neuro_code.domain.model_planning import (
    MAX_MODEL_PLANNING_RESPONSE_BYTES,
    MAX_PLANNING_CONTEXT_ITEM_BYTES,
    MAX_PLANNING_CONTEXT_ITEMS,
    ModelDagProposal,
    ModelDagProposalNode,
    PlanningAttempt,
    PlanningAttemptState,
    PlanningContextEnvelope,
    PlanningContextItem,
    PlanningProposalRecord,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.task_dag import MAX_TASK_DAG_NODES, TaskDag, TaskDagNode
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _proposal_json(*, dependency_order: tuple[str, ...] = ("a",)) -> str:
    nodes = [
        {"id": "a", "prompt": "research", "depends_on": []},
        {"id": "b", "prompt": "implement", "depends_on": list(dependency_order)},
    ]
    return json.dumps(
        {"nodes": nodes, "max_parallel": 2, "reason": "bounded"},
        ensure_ascii=False,
    )


class _Runner:
    def __init__(
        self,
        session_id: str,
        response: str,
        *,
        items: tuple[Message, ...] = (),
        delay: asyncio.Event | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.session_id = session_id
        self.response = response
        self.items = items
        self.delay = delay
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []
        self.turn_ids: list[str | None] = []

    async def run(self, prompt: str, *, turn_id=None, turn_source=None, **kwargs):
        del kwargs
        if turn_source is not None and turn_source.value != "user":
            raise AssertionError("planner must use the user turn source")
        if self.delay is not None:
            await self.delay.wait()
        self.calls += 1
        self.prompts.append(prompt)
        self.turn_ids.append(turn_id)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(response=self.response)


def _binding(runner: _Runner, *, zero_tools: bool = True) -> ConversationBinding:
    capabilities = SubagentCapabilitySet.from_runtime(
        tool_names=() if zero_tools else ("read_file",),
        cwd=Path.cwd(),
        sandbox_profile=SandboxProfile.OFF,
        enable_background_tasks=False,
        max_steps=1,
    )
    return ConversationBinding(
        cast(ConversationRunner, runner),
        cast(ModelProvider, object()),
        capabilities=capabilities,
    )


class _DagService:
    def __init__(self, store: SqliteSessionStore, parent_session_id: str) -> None:
        self.store = store
        self.parent_session_id = parent_session_id
        self.calls = 0

    async def create_task_dag(self, request: CreateTaskDagRequest) -> TaskDag:
        self.calls += 1
        dag = TaskDag.create(
            dag_id=request.dag_id,
            parent_session_id=self.parent_session_id,
            nodes=request.nodes,
            created_at=datetime.now(UTC),
            max_parallel=request.max_parallel,
        )
        return await self.store.insert_task_dag(dag)


async def _store_with_sessions(
    directory: str,
) -> tuple[SqliteSessionStore, str, str]:
    store = SqliteSessionStore(Path(directory) / "sessions.db")
    await store.initialize()
    parent = await store.create_session(directory, "fixture", "fixture-model")
    planner = await store.create_session(directory, "fixture", "fixture-model")
    return store, parent, planner


def test_model_dag_proposal_is_strict_and_canonical() -> None:
    first = ModelDagProposal.parse(_proposal_json())
    second = ModelDagProposal.parse(
        '{"reason":"bounded","max_parallel":2,"nodes":['
        '{"depends_on":[],"prompt":"research","id":"a"},'
        '{"depends_on":["a"],"prompt":"implement","id":"b"}]}'
    )
    assert first.canonical_json == second.canonical_json
    assert first.fingerprint == second.fingerprint
    with pytest.raises(ValueError, match="unknown fields"):
        ModelDagProposal.parse(
            '{"nodes":[{"id":"a","prompt":"x","depends_on":[]}],"max_parallel":1,"tools":[]}'
        )
    with pytest.raises(ValueError, match="strict JSON"):
        ModelDagProposal.parse("not-json")
    with pytest.raises(ValueError, match="node ids must be unique"):
        ModelDagProposal.parse(
            '{"nodes":[{"id":"a","prompt":"x","depends_on":[]},'
            '{"id":"a","prompt":"y","depends_on":[]}],"max_parallel":1}'
        )
    with pytest.raises(ValueError, match="canonical node order"):
        ModelDagProposal(
            (
                ModelDagProposalNode("a", "a"),
                ModelDagProposalNode("b", "b"),
                ModelDagProposalNode("c", "c", ("b", "a")),
            ),
            1,
        )


def test_model_dag_proposal_bounds_and_canonical_task_dag_validation() -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        ModelDagProposal.parse(
            json.dumps(
                {
                    "nodes": [
                        {"id": "a", "prompt": "x", "depends_on": []},
                    ],
                    "max_parallel": 5,
                }
            )
        )
    with pytest.raises(ValueError, match="prompt"):
        ModelDagProposalNode("a", "x" * (8 * 1024 + 1))
    unknown = ModelDagProposal(
        (ModelDagProposalNode("a", "a"), ModelDagProposalNode("b", "b", ("missing",))),
        1,
    )
    with pytest.raises(ValueError, match="unknown dependencies"):
        TaskDag.create(
            dag_id="dag",
            parent_session_id="parent",
            nodes=unknown.to_task_dag_nodes(),
            created_at=datetime.now(UTC),
        )
    self_dependency = ModelDagProposal(
        (ModelDagProposalNode("a", "a", ("a",)),),
        1,
    )
    with pytest.raises(ValueError, match="itself"):
        TaskDag.create(
            dag_id="dag-self",
            parent_session_id="parent",
            nodes=self_dependency.to_task_dag_nodes(),
            created_at=datetime.now(UTC),
        )
    cycle = ModelDagProposal(
        (
            ModelDagProposalNode("a", "a", ("b",)),
            ModelDagProposalNode("b", "b", ("a",)),
        ),
        1,
    )
    with pytest.raises(ValueError, match="cycle"):
        TaskDag.create(
            dag_id="dag-cycle",
            parent_session_id="parent",
            nodes=cycle.to_task_dag_nodes(),
            created_at=datetime.now(UTC),
        )


def test_model_dag_parser_rejects_malformed_shapes_and_keeps_frozen_graph_bounds() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        ModelDagProposal.parse("[]")
    with pytest.raises(ValueError, match="nodes must be a list"):
        ModelDagProposal.parse('{"nodes":{},"max_parallel":1}')
    with pytest.raises(ValueError, match="max_parallel must be an integer"):
        ModelDagProposal.parse('{"nodes":[],"max_parallel":true}')
    with pytest.raises(ValueError, match="reason must be text"):
        ModelDagProposal.parse('{"nodes":[],"max_parallel":1,"reason":false}')
    with pytest.raises(ValueError, match="node must be an object"):
        ModelDagProposal.parse('{"nodes":["node"],"max_parallel":1}')
    with pytest.raises(ValueError, match="unknown or missing fields"):
        ModelDagProposal.parse('{"nodes":[{"id":"a","prompt":"x"}],"max_parallel":1}')
    with pytest.raises(ValueError, match="id and prompt must be text"):
        ModelDagProposal.parse('{"nodes":[{"id":1,"prompt":"x","depends_on":[]}],"max_parallel":1}')
    with pytest.raises(ValueError, match="list of strings"):
        ModelDagProposal.parse(
            '{"nodes":[{"id":"a","prompt":"x","depends_on":[1]}],"max_parallel":1}'
        )
    with pytest.raises(ValueError, match="dependencies must be unique"):
        ModelDagProposal.parse(
            '{"nodes":[{"id":"a","prompt":"x","depends_on":["a","a"]}],"max_parallel":1}'
        )
    with pytest.raises(ValueError, match="too many nodes"):
        ModelDagProposal.parse(
            json.dumps(
                {
                    "nodes": [
                        {"id": str(index), "prompt": "x", "depends_on": []}
                        for index in range(MAX_TASK_DAG_NODES + 1)
                    ],
                    "max_parallel": 1,
                }
            )
        )
    with pytest.raises(ValueError, match="too many dependencies"):
        ModelDagProposal(
            (ModelDagProposalNode("a", "x", ("b", "c", "d", "e", "f")),),
            1,
        ).to_task_dag_nodes()
    with pytest.raises(ValueError, match="too many edges"):
        TaskDag.create(
            dag_id="edge-bound",
            parent_session_id="parent",
            nodes=(
                TaskDagNode("a", 0, "x"),
                TaskDagNode("b", 1, "x", ("a",)),
                TaskDagNode("c", 2, "x", ("a", "b")),
                TaskDagNode("d", 3, "x", ("a", "b", "c")),
                TaskDagNode("e", 4, "x", ("a", "b", "c", "d")),
                TaskDagNode("f", 5, "x", ("b", "c", "d", "e")),
                TaskDagNode("g", 6, "x", ("c", "d", "e", "f")),
                TaskDagNode("h", 7, "x", ("d", "e", "f", "g")),
            ),
            created_at=datetime.now(UTC),
        )


def test_planning_context_envelope_is_bounded_and_deterministic() -> None:
    item = PlanningContextItem(1, Role.USER, "safe")
    envelope = PlanningContextEnvelope("parent", 3, (item,))
    assert envelope.fingerprint == PlanningContextEnvelope("parent", 3, (item,)).fingerprint
    assert envelope.render().startswith("Bounded parent planning context:")
    with pytest.raises(ValueError, match="too many items"):
        PlanningContextEnvelope(
            "parent",
            MAX_PLANNING_CONTEXT_ITEMS + 1,
            tuple(
                PlanningContextItem(index, Role.USER, "x")
                for index in range(MAX_PLANNING_CONTEXT_ITEMS + 1)
            ),
        )
    with pytest.raises(ValueError, match="role"):
        PlanningContextItem(0, Role.SYSTEM, "not allowed")
    with pytest.raises(ValueError, match="text"):
        PlanningContextItem(0, Role.USER, "x" * (MAX_PLANNING_CONTEXT_ITEM_BYTES + 1))
    with pytest.raises(ValueError, match="source index"):
        PlanningContextItem(-1, Role.USER, "x")
    with pytest.raises(TypeError, match="truncated"):
        PlanningContextItem(0, Role.USER, "x", truncated=1)
    with pytest.raises(ValueError, match="source item count"):
        PlanningContextEnvelope("parent", -1, ())
    with pytest.raises(TypeError, match="items must be a tuple"):
        PlanningContextEnvelope("parent", 0, [])
    with pytest.raises(TypeError, match="items must be canonical"):
        PlanningContextEnvelope("parent", 1, (object(),))
    with pytest.raises(ValueError, match="source order"):
        PlanningContextEnvelope(
            "parent",
            2,
            (PlanningContextItem(1, Role.USER, "one"), PlanningContextItem(0, Role.USER, "zero")),
        )
    with pytest.raises(TypeError, match="truncated"):
        PlanningContextEnvelope("parent", 0, (), truncated=1)


def test_model_planning_domain_rejects_invalid_durable_contract_values() -> None:
    now = datetime.now(UTC)
    context_fingerprint = PlanningContextEnvelope("parent", 0, ()).fingerprint
    with pytest.raises(ValueError, match="planning id"):
        PlanningAttempt(
            "",
            "parent",
            _sha("objective"),
            context_fingerprint,
            "planner",
            "turn",
            "dag",
            PlanningAttemptState.CLAIMED,
            "owner",
            now,
        )
    with pytest.raises(ValueError, match="fingerprint"):
        PlanningAttempt(
            "planning",
            "parent",
            "not-a-fingerprint",
            context_fingerprint,
            "planner",
            "turn",
            "dag",
            PlanningAttemptState.CLAIMED,
            "owner",
            now,
        )
    with pytest.raises(TypeError, match="state"):
        PlanningAttempt(
            "planning",
            "parent",
            _sha("objective"),
            context_fingerprint,
            "planner",
            "turn",
            "dag",
            "claimed",
            "owner",
            now,
        )
    with pytest.raises(ValueError, match="timezone"):
        PlanningAttempt(
            "planning",
            "parent",
            _sha("objective"),
            context_fingerprint,
            "planner",
            "turn",
            "dag",
            PlanningAttemptState.CLAIMED,
            "owner",
            now.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="updated_at"):
        PlanningAttempt(
            "planning",
            "parent",
            _sha("objective"),
            context_fingerprint,
            "planner",
            "turn",
            "dag",
            PlanningAttemptState.CLAIMED,
            "owner",
            now,
            created_at=now,
            updated_at=now - timedelta(seconds=1),
        )
    with pytest.raises(TypeError, match="dependencies must be a tuple"):
        ModelDagProposalNode("node", "prompt", [])
    with pytest.raises(TypeError, match="nodes must be canonical"):
        ModelDagProposal((object(),), 1)
    parsed = ModelDagProposal.parse(_proposal_json())
    with pytest.raises(TypeError, match="proposal must be canonical"):
        PlanningProposalRecord(
            "proposal",
            "planning",
            "parent",
            "dag",
            _sha("objective"),
            context_fingerprint,
            object(),
            now,
        )
    with pytest.raises(ValueError, match="created_at"):
        PlanningProposalRecord(
            "proposal",
            "planning",
            "parent",
            "dag",
            _sha("objective"),
            context_fingerprint,
            parsed,
            now.replace(tzinfo=None),
        )


@pytest.mark.asyncio
async def test_model_planning_contract_validates_identity_and_configuration() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        planner = _binding(_Runner(planner_id, _proposal_json()))
        dag_service = _DagService(store, parent_id)
        service = ModelDagPlanningApplicationService(
            store,
            dag_service,
            parent_binding=parent,
            planner_binding=planner,
            session_store=store,
            owner_id="contract-owner",
        )
        assert service.planning_session_id == planner_id
        assert service.owner_id == "contract-owner"

        with pytest.raises(ValueError, match="planning id"):
            RunModelDagPlanningRequest("", "objective")
        with pytest.raises(ValueError, match="objective"):
            RunModelDagPlanningRequest("contract-invalid", " ")
        with pytest.raises(ValueError, match="canonical"):
            await service.run(cast(RunModelDagPlanningRequest, object()))

        with pytest.raises(ConfigurationError, match="parent binding is required"):
            ModelDagPlanningApplicationService(
                store,
                dag_service,
                parent_binding=None,
                planner_binding=planner,
            )
        with pytest.raises(ConfigurationError, match="binding is required"):
            ModelDagPlanningApplicationService(
                store,
                dag_service,
                parent_binding=parent,
                planner_binding=None,
            )
        with pytest.raises(ConfigurationError, match="Task DAG service is invalid"):
            ModelDagPlanningApplicationService(
                store,
                None,
                parent_binding=parent,
                planner_binding=planner,
            )
        with pytest.raises(ConfigurationError, match="parent session identity"):
            ModelDagPlanningApplicationService(
                store,
                dag_service,
                parent_binding=_binding(_Runner("", "unused"), zero_tools=False),
                planner_binding=planner,
            )
        with pytest.raises(ConfigurationError, match="session identity"):
            ModelDagPlanningApplicationService(
                store,
                dag_service,
                parent_binding=parent,
                planner_binding=_binding(_Runner("", _proposal_json())),
            )
        with pytest.raises(ConfigurationError, match="lease duration"):
            ModelDagPlanningApplicationService(
                store,
                dag_service,
                parent_binding=parent,
                planner_binding=planner,
                lease_seconds=0,
            )
        with pytest.raises(ConfigurationError, match="owner identity"):
            ModelDagPlanningApplicationService(
                store,
                dag_service,
                parent_binding=parent,
                planner_binding=planner,
                owner_id="",
            )


@pytest.mark.asyncio
async def test_model_planning_uses_actual_parent_and_publishes_exact_dag() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        parent_runner = _Runner(
            parent_id,
            "unused",
            items=(
                Message(Role.SYSTEM, "hidden system"),
                Message(Role.USER, 'api_key="secret-value" visible objective context'),
                Message(Role.ASSISTANT, "safe assistant context"),
                Message(Role.TOOL, "tool output must not appear"),
                Message(
                    Role.ASSISTANT,
                    "tool call payload must not appear",
                    tool_calls=(ToolCall("call", "bash", {"command": "cat /etc/passwd"}),),
                ),
                Message(Role.ASSISTANT, "reasoning hidden", reasoning_content="hidden reasoning"),
                Message(Role.USER, content_parts=(ContentPart.from_image("x"),)),
            ),
        )
        planner_runner = _Runner(planner_id, _proposal_json())
        parent = _binding(parent_runner, zero_tools=False)
        planner = _binding(planner_runner)
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=planner,
            session_store=store,
            redaction_values=("secret-value",),
        )
        result = await service.run(RunModelDagPlanningRequest("planning-context", "make a plan"))
        assert result.dag.parent_session_id == parent_id
        assert result.dag.dag_id == result.attempt.intended_dag_id
        assert result.dag.max_parallel == 2
        assert [node.node_id for node in result.dag.nodes] == ["a", "b"]
        assert result.dag.node("b").dependencies == ("a",)
        assert planner_runner.calls == 1
        assert planner_runner.turn_ids[0] == result.attempt.planner_turn_id
        prompt = planner_runner.prompts[0]
        assert "visible objective context" in prompt
        assert "safe assistant context" in prompt
        assert "hidden system" not in prompt
        assert "tool output must not appear" not in prompt
        assert "cat /etc/passwd" not in prompt
        assert "hidden reasoning" not in prompt
        assert "[REDACTED]" in prompt
        persisted = await store.get_model_planning_attempt("planning-context")
        assert persisted is not None
        assert persisted.state is PlanningAttemptState.COMPLETED


@pytest.mark.asyncio
async def test_invalid_observable_planning_output_is_stale_and_not_retried() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        runner = _Runner(planner_id, '{"nodes":[],"max_parallel":1}')
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(runner),
            session_store=store,
        )
        with pytest.raises(Exception, match="valid immutable DAG proposal"):
            await service.run(RunModelDagPlanningRequest("invalid-output", "objective"))
        assert runner.calls == 1
        attempt = await store.get_model_planning_attempt("invalid-output")
        assert attempt is not None
        assert attempt.state is PlanningAttemptState.STALE
        with pytest.raises(ConfigurationError, match=r"stale|recovery"):
            await service.run(RunModelDagPlanningRequest("invalid-output", "objective"))
        assert runner.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "state", "message"),
    [
        ("", PlanningAttemptState.INDETERMINATE, "empty response"),
        ("x" * (MAX_MODEL_PLANNING_RESPONSE_BYTES + 1), PlanningAttemptState.STALE, "exceeded"),
    ],
)
async def test_invalid_or_unbounded_observable_output_is_classified_without_replay(
    response: str,
    state: PlanningAttemptState,
    message: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        runner = _Runner(planner_id, response)
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(runner),
            session_store=store,
        )
        with pytest.raises(ConfigurationError, match=message):
            await service.run(RunModelDagPlanningRequest("bounded-output", "objective"))
        assert runner.calls == 1
        attempt = await store.get_model_planning_attempt("bounded-output")
        assert attempt is not None
        assert attempt.state is state
        with pytest.raises(ConfigurationError, match=r"stale|recovery"):
            await service.run(RunModelDagPlanningRequest("bounded-output", "objective"))
        assert runner.calls == 1


@pytest.mark.asyncio
async def test_planner_provider_failure_is_indeterminate_and_not_replayed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        runner = _Runner(planner_id, "unused", error=RuntimeError("provider down"))
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(runner),
            session_store=store,
        )
        with pytest.raises(ConfigurationError, match="automatic provider replay"):
            await service.run(RunModelDagPlanningRequest("provider-failure", "objective"))
        assert runner.calls == 1
        attempt = await store.get_model_planning_attempt("provider-failure")
        assert attempt is not None
        assert attempt.state is PlanningAttemptState.INDETERMINATE


@pytest.mark.asyncio
async def test_planner_requires_a_zero_tool_binding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        with pytest.raises(ConfigurationError, match="exactly zero tools"):
            ModelDagPlanningApplicationService(
                store,
                _DagService(store, parent_id),
                parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
                planner_binding=_binding(_Runner(planner_id, _proposal_json()), zero_tools=False),
                session_store=store,
            )


@pytest.mark.asyncio
async def test_planner_takes_over_only_an_expired_claim_without_turn_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, old_planner_id = await _store_with_sessions(directory)
        fresh_planner_id = await store.create_session(directory, "fixture", "fixture-model")
        now = datetime.now(UTC)
        claimed = PlanningAttempt(
            "takeover",
            parent_id,
            _sha("objective"),
            PlanningContextEnvelope(parent_id, 0, ()).fingerprint,
            old_planner_id,
            "old-turn",
            "old-dag",
            PlanningAttemptState.CLAIMED,
            "old-owner",
            now - timedelta(seconds=1),
        )
        await store.claim_model_planning_attempt(claimed, now=now)
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        runner = _Runner(fresh_planner_id, _proposal_json())
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(runner),
            session_store=store,
            owner_id="fresh-owner",
        )
        result = await service.run(RunModelDagPlanningRequest("takeover", "objective"))
        assert runner.calls == 1
        assert result.attempt.owner_id == "fresh-owner"
        assert result.attempt.planner_session_id == fresh_planner_id
        assert result.attempt.intended_dag_id == "old-dag"


async def _prepare_committed_attempt(
    store: SqliteSessionStore,
    parent_id: str,
    planner_id: str,
    planning_id: str,
    response: str,
) -> PlanningAttempt:
    now = datetime.now(UTC)
    attempt = PlanningAttempt(
        planning_id,
        parent_id,
        _sha("objective"),
        PlanningContextEnvelope(parent_id, 0, ()).fingerprint,
        planner_id,
        f"turn-{planning_id}",
        f"dag-{planning_id}",
        PlanningAttemptState.CLAIMED,
        "owner-a",
        now + timedelta(minutes=5),
    )
    claimed = await store.claim_model_planning_attempt(attempt, now=now)
    assert claimed.acquired
    await store.fence_model_planning_attempt(
        planning_id,
        owner_id="owner-a",
        planner_session_id=planner_id,
        planner_turn_id=attempt.planner_turn_id,
        updated_at=now,
    )
    return await store.mark_model_planning_model_committed(
        planning_id,
        owner_id="owner-a",
        planner_session_id=planner_id,
        planner_turn_id=attempt.planner_turn_id,
        model_response=response,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_durable_model_output_and_proposal_recovery_do_not_replay_provider() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        response = _proposal_json()
        committed = await _prepare_committed_attempt(
            store, parent_id, planner_id, "reuse-model", response
        )
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        runner = _Runner(planner_id, "this provider must not be called")
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(runner),
            session_store=store,
        )
        result = await service.run(RunModelDagPlanningRequest("reuse-model", "objective"))
        assert result.attempt.state is PlanningAttemptState.COMPLETED
        assert runner.calls == 0
        proposal = await store.get_model_planning_proposal("reuse-model")
        assert proposal is not None
        assert proposal.proposal_fingerprint == ModelDagProposal.parse(response).fingerprint
        assert committed.planner_turn_id == result.attempt.planner_turn_id

        proposal_attempt = await _prepare_committed_attempt(
            store, parent_id, planner_id, "reuse-proposal", response
        )
        proposal = PlanningProposalRecord(
            "proposal-old",
            proposal_attempt.planning_id,
            parent_id,
            proposal_attempt.intended_dag_id,
            proposal_attempt.objective_fingerprint,
            proposal_attempt.context_fingerprint,
            ModelDagProposal.parse(response),
            datetime.now(UTC),
        )
        await store.publish_model_planning_proposal(proposal, owner_id="owner-a")
        runner2 = _Runner(planner_id, "must not call")
        service2 = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(runner2),
            session_store=store,
        )
        result2 = await service2.run(RunModelDagPlanningRequest("reuse-proposal", "objective"))
        assert result2.dag.dag_id == proposal_attempt.intended_dag_id
        assert runner2.calls == 0


@pytest.mark.asyncio
async def test_planning_store_lifecycle_is_idempotent_and_dag_identity_is_exact() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        response = _proposal_json()
        now = datetime.now(UTC)
        fence_attempt = PlanningAttempt(
            "fence-idempotence",
            parent_id,
            _sha("objective"),
            PlanningContextEnvelope(parent_id, 0, ()).fingerprint,
            planner_id,
            "fence-turn",
            "fence-dag",
            PlanningAttemptState.CLAIMED,
            "owner-a",
            now + timedelta(minutes=5),
        )
        await store.claim_model_planning_attempt(fence_attempt, now=now)
        fenced = await store.fence_model_planning_attempt(
            fence_attempt.planning_id,
            owner_id="owner-a",
            planner_session_id=planner_id,
            planner_turn_id=fence_attempt.planner_turn_id,
            updated_at=now,
        )
        fenced_again = await store.fence_model_planning_attempt(
            fence_attempt.planning_id,
            owner_id="owner-a",
            planner_session_id=planner_id,
            planner_turn_id=fence_attempt.planner_turn_id,
            updated_at=datetime.now(UTC),
        )
        assert fenced.state is PlanningAttemptState.PROVIDER_FENCED
        assert fenced_again == fenced
        committed = await _prepare_committed_attempt(
            store, parent_id, planner_id, "store-lifecycle", response
        )
        committed_again = await store.mark_model_planning_model_committed(
            committed.planning_id,
            owner_id="owner-a",
            planner_session_id=planner_id,
            planner_turn_id=committed.planner_turn_id,
            model_response=response,
            updated_at=datetime.now(UTC),
        )
        assert committed_again == committed
        with pytest.raises(ModelPlanningStoreError, match="conflicts"):
            await store.mark_model_planning_model_committed(
                committed.planning_id,
                owner_id="owner-a",
                planner_session_id=planner_id,
                planner_turn_id=committed.planner_turn_id,
                model_response='{"nodes":[],"max_parallel":1}',
                updated_at=datetime.now(UTC),
            )
        parsed = ModelDagProposal.parse(response)
        proposal = PlanningProposalRecord(
            "store-proposal",
            committed.planning_id,
            parent_id,
            committed.intended_dag_id,
            committed.objective_fingerprint,
            committed.context_fingerprint,
            parsed,
            datetime.now(UTC),
        )
        await store.publish_model_planning_proposal(proposal, owner_id="owner-a")
        with pytest.raises(ModelPlanningStoreError, match="intended DAG"):
            await store.mark_model_planning_dag_published(
                committed.planning_id,
                owner_id="owner-a",
                dag_id="other-dag",
                proposal_fingerprint=proposal.proposal_fingerprint,
                updated_at=datetime.now(UTC),
            )
        dag = TaskDag.create(
            dag_id=committed.intended_dag_id,
            parent_session_id=parent_id,
            nodes=parsed.to_task_dag_nodes(),
            created_at=datetime.now(UTC),
            max_parallel=parsed.max_parallel,
        )
        await store.insert_task_dag(dag)
        published = await store.mark_model_planning_dag_published(
            committed.planning_id,
            owner_id="owner-a",
            dag_id=dag.dag_id,
            proposal_fingerprint=proposal.proposal_fingerprint,
            updated_at=datetime.now(UTC),
        )
        published_again = await store.mark_model_planning_dag_published(
            committed.planning_id,
            owner_id="owner-a",
            dag_id=dag.dag_id,
            proposal_fingerprint=proposal.proposal_fingerprint,
            updated_at=datetime.now(UTC),
        )
        assert published.state is PlanningAttemptState.DAG_PUBLISHED
        assert published_again == published
        completed = await store.transition_model_planning_attempt(
            committed.planning_id,
            expected_state=PlanningAttemptState.DAG_PUBLISHED,
            state=PlanningAttemptState.COMPLETED,
            updated_at=datetime.now(UTC),
        )
        assert (
            await store.transition_model_planning_attempt(
                committed.planning_id,
                expected_state=PlanningAttemptState.COMPLETED,
                state=PlanningAttemptState.COMPLETED,
                updated_at=datetime.now(UTC),
            )
        ) == completed


@pytest.mark.asyncio
async def test_proposal_is_insert_only_and_tamper_is_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        committed = await _prepare_committed_attempt(
            store, parent_id, planner_id, "tamper", _proposal_json()
        )
        parsed = ModelDagProposal.parse(_proposal_json())
        first = PlanningProposalRecord(
            "proposal-one",
            committed.planning_id,
            parent_id,
            committed.intended_dag_id,
            committed.objective_fingerprint,
            committed.context_fingerprint,
            parsed,
            datetime.now(UTC),
        )
        await store.publish_model_planning_proposal(first, owner_id="owner-a")
        second = replace(first, proposal_id="proposal-two", created_at=datetime.now(UTC))
        persisted = await store.publish_model_planning_proposal(second, owner_id="owner-b")
        assert persisted.proposal_id == "proposal-one"
        with sqlite3.connect(Path(directory) / "sessions.db") as connection:
            connection.execute(
                "UPDATE orchestration_plan_proposals SET canonical_json = ? WHERE planning_id = ?",
                ('{"nodes":[],"max_parallel":1,"reason":"tampered"}', committed.planning_id),
            )
            connection.commit()
        with pytest.raises(ModelPlanningStoreError, match="invalid"):
            await store.get_model_planning_proposal(committed.planning_id)


@pytest.mark.asyncio
async def test_two_planning_controllers_share_one_durable_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        planner2_id = await store.create_session(directory, "fixture", "fixture-model")
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        release = asyncio.Event()
        first_runner = _Runner(planner_id, _proposal_json(), delay=release)
        second_runner = _Runner(planner2_id, _proposal_json())
        first = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(first_runner),
            session_store=store,
            owner_id="owner-first",
        )
        second = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(second_runner),
            session_store=store,
            owner_id="owner-second",
        )
        first_task = asyncio.create_task(first.run(RunModelDagPlanningRequest("race", "objective")))
        await asyncio.sleep(0.02)
        second_task = asyncio.create_task(
            second.run(RunModelDagPlanningRequest("race", "objective"))
        )
        await asyncio.sleep(0.02)
        release.set()
        second_result = await asyncio.gather(second_task, return_exceptions=True)
        first_result = await first_task
        assert first_runner.calls == 1
        assert second_runner.calls == 0
        assert isinstance(second_result[0], Exception)
        assert first_result.dag.dag_id == first_result.attempt.intended_dag_id


@pytest.mark.asyncio
async def test_schema_24_to_25_preserves_existing_task_dag() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, _planner_id = await _store_with_sessions(directory)
        dag = TaskDag.create(
            dag_id="preexisting",
            parent_session_id=parent_id,
            nodes=(TaskDagNode("a", 0, "existing"),),
            created_at=datetime.now(UTC),
            max_parallel=2,
        )
        await store.insert_task_dag(dag)
        database = Path(directory) / "sessions.db"
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE orchestration_plan_proposals")
            connection.execute("DROP TABLE orchestration_planning_attempts")
            connection.execute("UPDATE schema_meta SET version = 24 WHERE singleton = 1")
            connection.commit()
        await store.initialize()
        assert SCHEMA_VERSION == 25
        assert (
            await store.get_task_dag("preexisting")
        ).definition_fingerprint == dag.definition_fingerprint
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert {
                "orchestration_planning_attempts",
                "orchestration_plan_proposals",
            }.issubset(tables)
            assert connection.execute("SELECT version FROM schema_meta").fetchone() == (25,)


@pytest.mark.asyncio
async def test_crash_safe_turn_identity_is_bound_before_provider_request() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        runner = _Runner(planner_id, _proposal_json())
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=_binding(runner),
            session_store=store,
        )
        result = await service.run(RunModelDagPlanningRequest("identity", "objective"))
        attempt = await store.get_model_planning_attempt("identity")
        assert attempt is not None
        assert attempt.planner_turn_id == runner.turn_ids[0]
        assert attempt.dag_id == result.dag.dag_id
        assert len(attempt.model_response.encode()) <= MAX_MODEL_PLANNING_RESPONSE_BYTES


def _spawn_planning_crash(
    database: str,
    parent_id: str,
    planner_id: str,
    planning_id: str,
    stage: str,
) -> None:
    async def run() -> None:
        store = SqliteSessionStore(Path(database))
        await store.initialize()
        now = datetime.now(UTC)
        attempt = PlanningAttempt(
            planning_id,
            parent_id,
            _sha("objective"),
            PlanningContextEnvelope(parent_id, 0, ()).fingerprint,
            planner_id,
            f"turn-{planning_id}",
            f"dag-{planning_id}",
            PlanningAttemptState.CLAIMED,
            "crash-owner",
            now + timedelta(minutes=5),
        )
        await store.claim_model_planning_attempt(attempt, now=now)
        await store.fence_model_planning_attempt(
            planning_id,
            owner_id="crash-owner",
            planner_session_id=planner_id,
            planner_turn_id=attempt.planner_turn_id,
            updated_at=now,
        )
        committed = await store.mark_model_planning_model_committed(
            planning_id,
            owner_id="crash-owner",
            planner_session_id=planner_id,
            planner_turn_id=attempt.planner_turn_id,
            model_response=_proposal_json(),
            updated_at=now,
        )
        if stage == "after-output":
            os._exit(74)
        proposal = PlanningProposalRecord(
            f"proposal-{planning_id}",
            planning_id,
            parent_id,
            committed.intended_dag_id,
            committed.objective_fingerprint,
            committed.context_fingerprint,
            ModelDagProposal.parse(_proposal_json()),
            now,
        )
        await store.publish_model_planning_proposal(proposal, owner_id="crash-owner")
        if stage == "after-proposal":
            os._exit(74)
        dag = TaskDag.create(
            dag_id=committed.intended_dag_id,
            parent_session_id=parent_id,
            nodes=proposal.proposal.to_task_dag_nodes(),
            created_at=now,
            max_parallel=proposal.proposal.max_parallel,
        )
        await store.insert_task_dag(dag)
        if stage == "after-dag":
            os._exit(74)
        raise AssertionError(f"unknown crash stage: {stage}")

    asyncio.run(run())


@pytest.mark.asyncio
async def test_spawn_crash_matrix_reuses_output_proposal_and_dag_without_replay() -> None:
    context = mp.get_context("spawn")
    for stage in ("after-output", "after-proposal", "after-dag"):
        with tempfile.TemporaryDirectory(prefix=f"neuro-planning-crash-{stage}-") as directory:
            store, parent_id, planner_id = await _store_with_sessions(directory)
            process = context.Process(
                target=_spawn_planning_crash,
                args=(
                    str(Path(directory) / "sessions.db"),
                    parent_id,
                    planner_id,
                    f"crash-{stage}",
                    stage,
                ),
            )
            process.start()
            await asyncio.to_thread(process.join, 30)
            assert process.exitcode == 74
            process.close()
            parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
            runner = _Runner(planner_id, "provider must not be replayed")
            service = ModelDagPlanningApplicationService(
                store,
                _DagService(store, parent_id),
                parent_binding=parent,
                planner_binding=_binding(runner),
                session_store=store,
            )
            result = await service.run(RunModelDagPlanningRequest(f"crash-{stage}", "objective"))
            assert runner.calls == 0
            assert result.dag.dag_id == result.attempt.intended_dag_id
            assert result.attempt.state is PlanningAttemptState.COMPLETED


class _ProductionPlanningState:
    def __init__(self) -> None:
        self.planner_calls = 0
        self.leader_calls = 0
        self.zero_tool_calls = 0
        self.worker_calls: list[str] = []
        self.started: list[str] = []
        self.timeline: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()
        self.fanout_started = asyncio.Event()
        self.release_fanout = asyncio.Event()


class _ProductionPlanningProvider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = "fixture-model-planning"
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self, state: _ProductionPlanningState) -> None:
        self._state = state

    async def stream(
        self,
        context,
        tools,
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        contents = "\n".join(message.content for message in context.messages)
        if "bounded DAG Planner" in contents:
            if tools:
                raise AssertionError("Planner received tools")
            self._state.planner_calls += 1
            self._state.zero_tool_calls += 1
            yield ModelCompleted(
                "stop",
                response_text=json.dumps(
                    {
                        "nodes": [
                            {"id": "a", "prompt": "planning-node-a", "depends_on": []},
                            {"id": "b", "prompt": "planning-node-b", "depends_on": ["a"]},
                            {"id": "c", "prompt": "planning-node-c", "depends_on": ["a"]},
                            {
                                "id": "d",
                                "prompt": "planning-node-d",
                                "depends_on": ["b", "c"],
                            },
                        ],
                        "max_parallel": 2,
                        "reason": "bounded production decomposition",
                    }
                ),
            )
            return
        if "Leader decision authority" in contents:
            if tools:
                raise AssertionError("Leader received tools")
            actions = (
                '{"action":"SELECT_NODE","node_id":"a"}',
                '{"action":"SELECT_NODES","node_ids":["b","c"]}',
                '{"action":"SELECT_NODE","node_id":"d"}',
                '{"action":"FINALIZE","summary":"planned DAG completed"}',
            )
            if self._state.leader_calls >= len(actions):
                raise AssertionError("Leader fixture received too many decisions")
            self._state.leader_calls += 1
            self._state.zero_tool_calls += 1
            yield ModelCompleted("stop", response_text=actions[self._state.leader_calls - 1])
            return
        node_id = next(
            (
                candidate
                for candidate in ("a", "b", "c", "d")
                if f"planning-node-{candidate}" in contents
            ),
            None,
        )
        if node_id is None:
            raise AssertionError("worker fixture could not identify its node")
        if not tools:
            raise AssertionError("Writable worker unexpectedly received no tools")
        self._state.worker_calls.append(node_id)
        async with self._state.lock:
            self._state.started.append(node_id)
            self._state.timeline.append(f"start:{node_id}")
            self._state.active += 1
            self._state.max_active = max(self._state.max_active, self._state.active)
            if {"b", "c"}.issubset(self._state.started):
                self._state.fanout_started.set()
        try:
            if node_id in {"b", "c"}:
                await self._state.release_fanout.wait()
            yield ModelCompleted("stop", response_text=f"production result {node_id}")
        finally:
            async with self._state.lock:
                self._state.active -= 1
                self._state.timeline.append(f"complete:{node_id}")


def _run_git(directory: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return result.stdout


def _make_repository(root: Path) -> Path:
    repository = root / "parent-repository"
    repository.mkdir()
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository, "config", "user.name", "Neuro Code Tests")
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _run_git(repository, "add", "tracked.txt")
    _run_git(repository, "commit", "-qm", "initial")
    return repository


def _write_fixture_config(state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    (state_dir / "config.toml").write_text(
        """
[web_search]
mode = "disabled"

[web_fetch]
mode = "disabled"

[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
context_window_tokens = 131072
""",
        encoding="utf-8",
    )


def _parent_capability(repository: Path) -> SubagentCapabilitySet:
    return SubagentCapabilitySet.from_runtime(
        tool_names=(
            "read_file",
            "read_files",
            "list_dir",
            "list_tree",
            "glob",
            "grep",
            "grep_many",
            "skill",
            "search_replace",
            "apply_patch",
        ),
        cwd=repository,
        sandbox_profile=SandboxProfile.OFF,
        enable_background_tasks=False,
        max_steps=8,
    )


@pytest.mark.asyncio
async def test_real_planner_to_parallel_leader_to_writable_workers() -> None:
    with tempfile.TemporaryDirectory(prefix="neuro-model-planning-production-") as directory:
        root = Path(directory)
        repository = _make_repository(root)
        dirty_file = repository / "dirty-parent.txt"
        dirty_file.write_text("parent remains dirty\n", encoding="utf-8")
        before_status = _run_git(repository, "status", "--porcelain=v1")
        before_head = _run_git(repository, "rev-parse", "HEAD")
        state_dir = root / "state"
        _write_fixture_config(state_dir)
        state = _ProductionPlanningState()

        def provider_factory(_config, _failover):
            return cast(ModelProvider, _ProductionPlanningProvider(state))

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
                    max_steps=8,
                ),
                provider_factory=provider_factory,
            )
            planner = None
            leader = None
            parent_binding = None
            try:
                parent_session_id = await application.store.create_session(
                    str(repository),
                    "fixture",
                    "fixture-model",
                    sandbox_profile=SandboxProfile.OFF,
                )
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                planner = await application.create_model_planning_service(
                    parent_binding=parent_binding,
                )
                planned = await planner.run(
                    RunModelDagPlanningRequest("production-plan", "decompose this objective")
                )
                assert state.planner_calls == 1
                assert state.zero_tool_calls == 1
                assert [node.node_id for node in planned.dag.nodes] == ["a", "b", "c", "d"]
                assert planned.dag.node("b").dependencies == ("a",)
                assert planned.dag.node("c").dependencies == ("a",)
                assert planned.dag.node("d").dependencies == ("b", "c")
                assert planned.dag.max_parallel == 2

                leader = await application.create_leader_service(parent_binding=parent_binding)
                running = asyncio.create_task(
                    leader.run(RunLeaderRequest(planned.dag.dag_id, "execute the plan"))
                )
                await asyncio.wait_for(state.fanout_started.wait(), timeout=90)
                during = await application.store.get_task_dag(planned.dag.dag_id)
                assert during is not None
                assert during.running_node_ids == ("b", "c")
                assert state.max_active == 2
                assert "d" not in state.started
                state.release_fanout.set()
                result = await asyncio.wait_for(running, timeout=120)
                assert result.dag.state.terminal
                assert result.final_response == "planned DAG completed"
                assert state.worker_calls[0] == "a"
                assert set(state.worker_calls[1:3]) == {"b", "c"}
                assert state.worker_calls[3] == "d"
                assert state.timeline.index("complete:b") < state.timeline.index("start:d")
                assert state.timeline.index("complete:c") < state.timeline.index("start:d")
                assert state.zero_tool_calls == 5
                leases = await application.store.list_writable_subagent_leases(
                    parent_session_id=parent_session_id,
                )
                assert len(leases) == 4
                assert len({lease.worktree_id for lease in leases}) == 4
                assert len({lease.child_session_id for lease in leases}) == 4
                assert _run_git(repository, "status", "--porcelain=v1") == before_status
                assert _run_git(repository, "rev-parse", "HEAD") == before_head
                assert dirty_file.read_text(encoding="utf-8") == "parent remains dirty\n"
            finally:
                if leader is not None:
                    await leader.close()
                if planner is not None:
                    await planner.close()
                if parent_binding is not None:
                    await parent_binding.close()
                await application.close()
