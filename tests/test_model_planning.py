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
from contextlib import closing
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
    MAX_MODEL_PLANNING_PROMPT_BYTES,
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
from neuro_code.domain.execution import TurnRecoveryFactKind
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
        result3 = await service2.run(RunModelDagPlanningRequest("reuse-proposal", "objective"))
        assert result3.attempt.state is PlanningAttemptState.COMPLETED
        assert result3.dag == result2.dag
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
        with closing(sqlite3.connect(Path(directory) / "sessions.db")) as connection, connection:
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
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TABLE orchestration_plan_proposals")
            connection.execute("DROP TABLE orchestration_planning_attempts")
            connection.execute("UPDATE schema_meta SET version = 24 WHERE singleton = 1")
            connection.commit()
        await store.initialize()
        assert SCHEMA_VERSION == 25
        assert (
            await store.get_task_dag("preexisting")
        ).definition_fingerprint == dag.definition_fingerprint
        with closing(sqlite3.connect(database)) as connection:
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
async def test_model_planning_recovery_requires_a_durable_proposal() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        committed = await _prepare_committed_attempt(
            store, parent_id, planner_id, "missing-proposal", _proposal_json()
        )
        await store.transition_model_planning_attempt(
            committed.planning_id,
            expected_state=PlanningAttemptState.MODEL_COMMITTED,
            state=PlanningAttemptState.PROPOSAL_PUBLISHED,
            updated_at=datetime.now(UTC),
        )
        runner = _Runner(planner_id, "provider must not be replayed")
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )
        with pytest.raises(ConfigurationError, match="proposal is missing"):
            await service.run(RunModelDagPlanningRequest("missing-proposal", "objective"))
        assert runner.calls == 0


@pytest.mark.asyncio
async def test_model_planning_provider_cancellation_is_indeterminate_without_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        runner = _Runner(planner_id, "unused", error=asyncio.CancelledError())
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )
        with pytest.raises(asyncio.CancelledError):
            await service.run(RunModelDagPlanningRequest("cancelled", "objective"))
        assert runner.calls == 1
        attempt = await store.get_model_planning_attempt("cancelled")
        assert attempt is not None
        assert attempt.state is PlanningAttemptState.INDETERMINATE


@pytest.mark.asyncio
async def test_model_planning_output_durability_failure_does_not_replay_provider() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        runner = _Runner(planner_id, _proposal_json())
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )

        async def fail_commit(*args: object, **kwargs: object) -> PlanningAttempt:
            del args, kwargs
            raise ModelPlanningStoreError("durable output unavailable")

        with (
            patch.object(store, "mark_model_planning_model_committed", new=fail_commit),
            pytest.raises(ConfigurationError, match="output durability failed"),
        ):
            await service.run(RunModelDagPlanningRequest("commit-failure", "objective"))
        assert runner.calls == 1
        attempt = await store.get_model_planning_attempt("commit-failure")
        assert attempt is not None
        assert attempt.state is PlanningAttemptState.PROVIDER_FENCED


@pytest.mark.asyncio
async def test_model_planning_context_projection_caps_items_and_projected_bytes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        parent_runner = _Runner(
            parent_id,
            "unused",
            items=tuple(Message(Role.USER, "item") for _ in range(MAX_PLANNING_CONTEXT_ITEMS + 2)),
        )
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(parent_runner, zero_tools=False),
            planner_binding=_binding(_Runner(planner_id, _proposal_json())),
            session_store=store,
        )
        item_capped = service._project_context(parent_id)
        assert item_capped.truncated
        assert len(item_capped.items) == MAX_PLANNING_CONTEXT_ITEMS

        parent_runner.items = tuple(
            Message(Role.USER, "large-" + "x" * 3_500) for _ in range(MAX_PLANNING_CONTEXT_ITEMS)
        )
        byte_capped = service._project_context(parent_id)
        assert byte_capped.truncated
        assert len(byte_capped.items) < MAX_PLANNING_CONTEXT_ITEMS


@pytest.mark.asyncio
async def test_model_planning_prompt_size_is_bounded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(_Runner(planner_id, _proposal_json())),
            session_store=store,
        )
        with pytest.raises(ConfigurationError, match="prompt exceeds its bounded size"):
            service._prompt(
                "x" * MAX_MODEL_PLANNING_PROMPT_BYTES,
                PlanningContextEnvelope(parent_id, 0, ()),
            )


@pytest.mark.asyncio
async def test_model_planning_rejects_a_durable_identity_conflict_without_provider_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        await _prepare_committed_attempt(
            store, parent_id, planner_id, "identity-conflict", _proposal_json()
        )
        runner = _Runner(planner_id, "provider must not be called")
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )
        with pytest.raises(ConfigurationError, match="identity conflicts"):
            await service.run(RunModelDagPlanningRequest("identity-conflict", "different"))
        assert runner.calls == 0


@pytest.mark.asyncio
async def test_model_planning_requires_recovery_inspection_before_expired_takeover() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        now = datetime.now(UTC)
        expired = PlanningAttempt(
            "inspection-unavailable",
            parent_id,
            _sha("objective"),
            PlanningContextEnvelope(parent_id, 0, ()).fingerprint,
            planner_id,
            "expired-turn",
            "expired-dag",
            PlanningAttemptState.CLAIMED,
            "expired-owner",
            now - timedelta(seconds=1),
        )
        await store.claim_model_planning_attempt(expired, now=now)
        runner = _Runner(planner_id, _proposal_json())
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
        )
        with pytest.raises(ConfigurationError, match="inspection is unavailable"):
            await service.run(RunModelDagPlanningRequest("inspection-unavailable", "objective"))
        assert runner.calls == 0


@pytest.mark.asyncio
async def test_model_planning_rejects_mismatched_published_dag_without_provider_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        dag_service = _DagService(store, parent_id)
        runner = _Runner(planner_id, _proposal_json())
        service = ModelDagPlanningApplicationService(
            store,
            dag_service,
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )

        async def return_mismatched_dag(request: CreateTaskDagRequest) -> TaskDag:
            return TaskDag.create(
                dag_id="mismatched-dag",
                parent_session_id=parent_id,
                nodes=request.nodes,
                created_at=datetime.now(UTC),
                max_parallel=request.max_parallel,
            )

        with (
            patch.object(dag_service, "create_task_dag", new=return_mismatched_dag),
            pytest.raises(ConfigurationError, match="does not match"),
        ):
            await service.run(RunModelDagPlanningRequest("dag-mismatch", "objective"))
        assert runner.calls == 1


@pytest.mark.asyncio
async def test_model_planning_rejects_task_dag_publication_without_provider_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        dag_service = _DagService(store, parent_id)
        runner = _Runner(planner_id, _proposal_json())
        service = ModelDagPlanningApplicationService(
            store,
            dag_service,
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )

        async def reject_dag(request: CreateTaskDagRequest) -> TaskDag:
            del request
            raise ValueError("canonical DAG rejected")

        with (
            patch.object(dag_service, "create_task_dag", new=reject_dag),
            pytest.raises(ConfigurationError, match="canonical Task DAG validation"),
        ):
            await service.run(RunModelDagPlanningRequest("dag-rejected", "objective"))
        assert runner.calls == 1
        attempt = await store.get_model_planning_attempt("dag-rejected")
        assert attempt is not None
        assert attempt.state is PlanningAttemptState.STALE
        assert await store.get_model_planning_proposal("dag-rejected") is not None


@pytest.mark.asyncio
async def test_model_planning_provider_fence_loss_fails_closed_before_provider_call() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        runner = _Runner(planner_id, _proposal_json())
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )

        async def lose_fence(*args: object, **kwargs: object) -> PlanningAttempt:
            del args, kwargs
            raise ModelPlanningStoreError("fence changed")

        with (
            patch.object(store, "fence_model_planning_attempt", new=lose_fence),
            pytest.raises(ConfigurationError, match="provider fence was lost"),
        ):
            await service.run(RunModelDagPlanningRequest("fence-loss", "objective"))
        assert runner.calls == 0
        attempt = await store.get_model_planning_attempt("fence-loss")
        assert attempt is not None
        assert attempt.state is PlanningAttemptState.CLAIMED


@pytest.mark.asyncio
async def test_model_planning_existing_provider_fence_requires_explicit_recovery() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id = await _store_with_sessions(directory)
        now = datetime.now(UTC)
        attempt = PlanningAttempt(
            "existing-fence",
            parent_id,
            _sha("objective"),
            PlanningContextEnvelope(parent_id, 0, ()).fingerprint,
            planner_id,
            "fenced-turn",
            "fenced-dag",
            PlanningAttemptState.CLAIMED,
            "owner-a",
            now + timedelta(minutes=5),
        )
        await store.claim_model_planning_attempt(attempt, now=now)
        await store.fence_model_planning_attempt(
            attempt.planning_id,
            owner_id="owner-a",
            planner_session_id=planner_id,
            planner_turn_id=attempt.planner_turn_id,
            updated_at=now,
        )
        runner = _Runner(planner_id, "provider must not be replayed")
        service = ModelDagPlanningApplicationService(
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(runner),
            session_store=store,
        )
        with pytest.raises(ConfigurationError, match="provider fence exists"):
            await service.run(RunModelDagPlanningRequest("existing-fence", "objective"))
        assert runner.calls == 0


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


def _write_durable_json(path: Path, payload: dict[str, object]) -> None:
    """Write one test-process fact before an intentional os._exit boundary."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_durable_json_line(path: Path, payload: dict[str, object]) -> None:
    """Append one cross-process test fact with a completed file-system flush."""

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


async def _record_real_planner_snapshot(
    application: ApplicationComposition,
    *,
    marker_path: Path,
    planning_id: str,
    provider_call_log: Path,
    stage: str,
) -> None:
    attempt = await application.store.get_model_planning_attempt(planning_id)
    assert attempt is not None
    proposal = await application.store.get_model_planning_proposal(planning_id)
    dag = await application.store.get_task_dag(attempt.intended_dag_id)
    turn_attempts = await application.store.load_turn_attempts(attempt.planner_session_id)
    turn = next(
        (candidate for candidate in turn_attempts if candidate.turn_id == attempt.planner_turn_id),
        None,
    )
    payload: dict[str, object] = {
        "stage": stage,
        "planning_id": attempt.planning_id,
        "parent_session_id": attempt.parent_session_id,
        "planner_session_id": attempt.planner_session_id,
        "planner_turn_id": attempt.planner_turn_id,
        "intended_dag_id": attempt.intended_dag_id,
        "state": attempt.state.value,
        "model_response": attempt.model_response,
        "proposal_id": proposal.proposal_id if proposal is not None else None,
        "proposal_fingerprint": (proposal.proposal_fingerprint if proposal is not None else None),
        "proposal_canonical_json": (
            proposal.proposal.canonical_json if proposal is not None else None
        ),
        "dag_id": dag.dag_id if dag is not None else None,
        "dag_definition_fingerprint": (dag.definition_fingerprint if dag is not None else None),
        "provider_call_count": len(_read_durable_json_lines(provider_call_log)),
        "turn_id": turn.turn_id if turn is not None else None,
        "turn_last_stage": turn.last_stage.value if turn is not None else None,
        "turn_request_started_count": turn.request_started_count if turn is not None else None,
        "turn_output_started": turn.output_started if turn is not None else None,
    }
    _write_durable_json(marker_path, payload)


def _production_planning_settings(repository: Path) -> ApplicationSettings:
    return ApplicationSettings(
        cwd=repository,
        sandbox="off",
        permission_mode=PermissionMode.BYPASS,
        max_steps=8,
    )


def _production_planning_environment(root: Path, state_dir: Path) -> dict[str, str]:
    return {
        "HOME": str(root),
        "NEURO_CODE_HOME": str(state_dir),
        "FIXTURE_KEY": "fixture-key",
    }


def _spawn_real_planner_crash(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    planning_id: str,
    objective: str,
    marker: str,
    provider_call_log: str,
    stage: str,
) -> None:
    async def run() -> None:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        marker_path = Path(marker)
        call_log = Path(provider_call_log)
        state = _ProductionPlanningState(provider_call_log=call_log)

        def provider_factory(_config, _failover):
            return cast(ModelProvider, _ProductionPlanningProvider(state))

        application = None
        parent_binding = None
        planner = None
        with patch.dict(
            "os.environ",
            _production_planning_environment(root, state_dir),
            clear=False,
        ):
            try:
                application = await ApplicationComposition.open(
                    _production_planning_settings(repository),
                    provider_factory=provider_factory,
                )
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

                if stage == "model-committed":
                    original = application.store.mark_model_planning_model_committed

                    async def crash_after_commit(planning_key: str, **kwargs: object):
                        await original(planning_key, **kwargs)
                        await _record_real_planner_snapshot(
                            application,
                            marker_path=marker_path,
                            planning_id=planning_id,
                            provider_call_log=call_log,
                            stage=stage,
                        )
                        os._exit(73)

                    with patch.object(
                        application.store,
                        "mark_model_planning_model_committed",
                        new=crash_after_commit,
                    ):
                        await planner.run(RunModelDagPlanningRequest(planning_id, objective))
                elif stage == "proposal-published":
                    original = application.store.publish_model_planning_proposal

                    async def crash_after_proposal(proposal, *, owner_id: str):
                        await original(proposal, owner_id=owner_id)
                        await _record_real_planner_snapshot(
                            application,
                            marker_path=marker_path,
                            planning_id=planning_id,
                            provider_call_log=call_log,
                            stage=stage,
                        )
                        os._exit(74)

                    with patch.object(
                        application.store,
                        "publish_model_planning_proposal",
                        new=crash_after_proposal,
                    ):
                        await planner.run(RunModelDagPlanningRequest(planning_id, objective))
                elif stage == "dag-inserted":
                    original = application.store.insert_task_dag

                    async def crash_after_dag(dag):
                        inserted = await original(dag)
                        del inserted
                        await _record_real_planner_snapshot(
                            application,
                            marker_path=marker_path,
                            planning_id=planning_id,
                            provider_call_log=call_log,
                            stage=stage,
                        )
                        os._exit(74)

                    with patch.object(application.store, "insert_task_dag", new=crash_after_dag):
                        await planner.run(RunModelDagPlanningRequest(planning_id, objective))
                elif stage == "provider-turn-evidence":
                    original = application.store.append_turn_recovery_fact

                    async def crash_after_turn_evidence(
                        session_id: str,
                        turn_id: str,
                        event,
                        fact,
                    ):
                        await original(session_id, turn_id, event, fact)
                        if fact.kind is TurnRecoveryFactKind.MODEL_OUTPUT_STARTED:
                            await _record_real_planner_snapshot(
                                application,
                                marker_path=marker_path,
                                planning_id=planning_id,
                                provider_call_log=call_log,
                                stage=stage,
                            )
                            os._exit(75)

                    with patch.object(
                        application.store,
                        "append_turn_recovery_fact",
                        new=crash_after_turn_evidence,
                    ):
                        await planner.run(RunModelDagPlanningRequest(planning_id, objective))
                else:
                    raise AssertionError(f"unknown production planner crash stage: {stage}")
                raise AssertionError(f"production planner did not crash at stage: {stage}")
            finally:
                if planner is not None:
                    await planner.close()
                if parent_binding is not None:
                    await parent_binding.close()
                if application is not None:
                    await application.close()

    asyncio.run(run())


def _spawn_real_planner_recovery(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    planning_id: str,
    objective: str,
    marker: str,
    provider_call_log: str,
    result_path: str,
) -> None:
    async def run() -> None:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        marker_data = cast(
            dict[str, object],
            json.loads(await asyncio.to_thread(Path(marker).read_text, encoding="utf-8")),
        )
        call_log = Path(provider_call_log)
        result_file = Path(result_path)
        state = _ProductionPlanningState(provider_call_log=call_log)

        def provider_factory(_config, _failover):
            return cast(ModelProvider, _ProductionPlanningProvider(state))

        payload: dict[str, object] = {
            "planner_session_id": None,
            "status": "error",
            "provider_call_count_before": len(_read_durable_json_lines(call_log)),
        }
        application = None
        parent_binding = None
        planner = None
        with patch.dict(
            "os.environ",
            _production_planning_environment(root, state_dir),
            clear=False,
        ):
            try:
                application = await ApplicationComposition.open(
                    _production_planning_settings(repository),
                    provider_factory=provider_factory,
                )
                parent_binding = await application.create_binding(
                    resume_id=str(marker_data["parent_session_id"]),
                    capabilities=_parent_capability(repository),
                )
                planner = await application.create_model_planning_service(
                    parent_binding=parent_binding,
                )
                payload["planner_session_id"] = planner.planning_session_id
                try:
                    result = await planner.run(RunModelDagPlanningRequest(planning_id, objective))
                except Exception as error:
                    payload.update(
                        {
                            "status": "error",
                            "error_type": type(error).__name__,
                            "error_message": str(error),
                        }
                    )
                else:
                    payload.update(
                        {
                            "status": "completed",
                            "planning_id": result.planning_id,
                            "attempt_state": result.attempt.state.value,
                            "historical_planner_session_id": result.attempt.planner_session_id,
                            "historical_planner_turn_id": result.attempt.planner_turn_id,
                            "intended_dag_id": result.attempt.intended_dag_id,
                            "attempt_dag_id": result.attempt.dag_id,
                            "model_response": result.attempt.model_response,
                            "proposal_id": result.proposal.proposal_id,
                            "proposal_fingerprint": result.proposal.proposal_fingerprint,
                            "proposal_canonical_json": result.proposal.proposal.canonical_json,
                            "dag_id": result.dag.dag_id,
                            "dag_definition_fingerprint": result.dag.definition_fingerprint,
                        }
                    )
            except Exception as error:
                payload.update(
                    {
                        "status": "error",
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                    }
                )
            finally:
                if planner is not None:
                    await planner.close()
                if parent_binding is not None:
                    await parent_binding.close()
                if application is not None:
                    await application.close()
        payload["provider_call_count_after"] = len(_read_durable_json_lines(call_log))
        _write_durable_json(result_file, payload)

    asyncio.run(run())


def _spawn_real_planner_controller(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    parent_session_id: str,
    planning_id: str,
    objective: str,
    provider_call_log: str,
    start_barrier,
    provider_started_event,
    release_provider_event,
    controller_finished_event,
    result_path: str,
) -> None:
    async def run() -> None:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        call_log = Path(provider_call_log)
        result_file = Path(result_path)
        state = _ProductionPlanningState(
            provider_call_log=call_log,
            provider_started_event=provider_started_event,
            provider_release_event=release_provider_event,
        )

        def provider_factory(_config, _failover):
            return cast(ModelProvider, _ProductionPlanningProvider(state))

        payload: dict[str, object] = {"status": "error"}
        application = None
        parent_binding = None
        planner = None
        try:
            with patch.dict(
                "os.environ",
                _production_planning_environment(root, state_dir),
                clear=False,
            ):
                application = await ApplicationComposition.open(
                    _production_planning_settings(repository),
                    provider_factory=provider_factory,
                )
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                planner = await application.create_model_planning_service(
                    parent_binding=parent_binding,
                )
                payload["planner_session_id"] = planner.planning_session_id
                await asyncio.to_thread(start_barrier.wait, 90)
                try:
                    result = await planner.run(RunModelDagPlanningRequest(planning_id, objective))
                except Exception as error:
                    payload.update(
                        {
                            "status": "error",
                            "error_type": type(error).__name__,
                            "error_message": str(error),
                        }
                    )
                else:
                    payload.update(
                        {
                            "status": "completed",
                            "attempt_state": result.attempt.state.value,
                            "historical_planner_session_id": result.attempt.planner_session_id,
                            "historical_planner_turn_id": result.attempt.planner_turn_id,
                            "intended_dag_id": result.attempt.intended_dag_id,
                            "proposal_fingerprint": result.proposal.proposal_fingerprint,
                            "dag_id": result.dag.dag_id,
                        }
                    )
        except Exception as error:
            payload.update(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                }
            )
        finally:
            if planner is not None:
                await planner.close()
            if parent_binding is not None:
                await parent_binding.close()
            if application is not None:
                await application.close()
        payload["provider_call_count"] = len(_read_durable_json_lines(call_log))
        _write_durable_json(result_file, payload)
        controller_finished_event.set()

    asyncio.run(run())


def _assert_planning_publication_counts(
    database: Path,
    *,
    expected_proposal_count: int,
    expected_dag_count: int,
) -> None:
    with closing(sqlite3.connect(database)) as connection:
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM orchestration_planning_attempts"
        ).fetchone()
        proposal_count = connection.execute(
            "SELECT COUNT(*) FROM orchestration_plan_proposals"
        ).fetchone()
        dag_count = connection.execute("SELECT COUNT(*) FROM task_dags").fetchone()
    assert attempt_count == (1,)
    assert proposal_count == (expected_proposal_count,)
    assert dag_count == (expected_dag_count,)


@pytest.mark.parametrize(
    ("stage", "exit_code"),
    [("model-committed", 73), ("proposal-published", 74), ("dag-inserted", 74)],
)
@pytest.mark.asyncio
async def test_fresh_composition_restart_reuses_exact_planning_identity_without_replay(
    stage: str,
    exit_code: int,
) -> None:
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix=f"neuro-planning-fresh-{stage}-") as directory:
        root = Path(directory)
        repository = _make_repository(root)
        state_dir = root / "state"
        _write_fixture_config(state_dir)
        planning_id = f"fresh-{stage}"
        objective = "decompose this objective"
        marker = root / "planner-a.json"
        result_file = root / "planner-b.json"
        call_log = root / "planner-calls.jsonl"
        database = state_dir / "sessions.db"

        process_a = context.Process(
            target=_spawn_real_planner_crash,
            args=(
                str(root),
                str(repository),
                str(state_dir),
                planning_id,
                objective,
                str(marker),
                str(call_log),
                stage,
            ),
        )
        process_a.start()
        await asyncio.to_thread(process_a.join, 90)
        assert process_a.exitcode == exit_code
        process_a.close()
        snapshot = cast(dict[str, object], json.loads(marker.read_text(encoding="utf-8")))
        call_records = _read_durable_json_lines(call_log)
        assert snapshot["state"] == (
            PlanningAttemptState.MODEL_COMMITTED.value
            if stage == "model-committed"
            else PlanningAttemptState.PROPOSAL_PUBLISHED.value
        )
        assert snapshot["planner_session_id"]
        assert snapshot["planner_turn_id"]
        assert snapshot["provider_call_count"] == 1
        assert len(call_records) == 1
        assert snapshot["model_response"] == call_records[0]["response"]

        process_b = context.Process(
            target=_spawn_real_planner_recovery,
            args=(
                str(root),
                str(repository),
                str(state_dir),
                planning_id,
                objective,
                str(marker),
                str(call_log),
                str(result_file),
            ),
        )
        process_b.start()
        await asyncio.to_thread(process_b.join, 90)
        assert process_b.exitcode == 0
        process_b.close()
        recovered = cast(dict[str, object], json.loads(result_file.read_text(encoding="utf-8")))
        assert recovered["status"] == "completed"
        assert recovered["planner_session_id"] != snapshot["planner_session_id"]
        assert recovered["historical_planner_session_id"] == snapshot["planner_session_id"]
        assert recovered["historical_planner_turn_id"] == snapshot["planner_turn_id"]
        assert recovered["intended_dag_id"] == snapshot["intended_dag_id"]
        assert recovered["attempt_dag_id"] == snapshot["intended_dag_id"]
        assert recovered["model_response"] == snapshot["model_response"]
        assert recovered["attempt_state"] == PlanningAttemptState.COMPLETED.value
        assert recovered["provider_call_count_before"] == 1
        assert recovered["provider_call_count_after"] == 1
        assert len(_read_durable_json_lines(call_log)) == 1
        assert (
            recovered["proposal_canonical_json"]
            == ModelDagProposal.parse(str(snapshot["model_response"])).canonical_json
        )
        assert (
            recovered["proposal_fingerprint"]
            == ModelDagProposal.parse(str(snapshot["model_response"])).fingerprint
        )
        if stage != "model-committed":
            assert recovered["proposal_id"] == snapshot["proposal_id"]
            assert recovered["proposal_fingerprint"] == snapshot["proposal_fingerprint"]
            assert recovered["proposal_canonical_json"] == snapshot["proposal_canonical_json"]
        if stage == "dag-inserted":
            assert recovered["dag_id"] == snapshot["dag_id"]
            assert recovered["dag_definition_fingerprint"] == snapshot["dag_definition_fingerprint"]
        _assert_planning_publication_counts(
            database,
            expected_proposal_count=1,
            expected_dag_count=1,
        )


@pytest.mark.asyncio
async def test_fresh_composition_provider_turn_evidence_fails_closed_without_replay() -> None:
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="neuro-planning-provider-evidence-") as directory:
        root = Path(directory)
        repository = _make_repository(root)
        state_dir = root / "state"
        _write_fixture_config(state_dir)
        planning_id = "fresh-provider-turn-evidence"
        objective = "decompose this objective"
        marker = root / "planner-a.json"
        result_file = root / "planner-b.json"
        call_log = root / "planner-calls.jsonl"
        database = state_dir / "sessions.db"

        process_a = context.Process(
            target=_spawn_real_planner_crash,
            args=(
                str(root),
                str(repository),
                str(state_dir),
                planning_id,
                objective,
                str(marker),
                str(call_log),
                "provider-turn-evidence",
            ),
        )
        process_a.start()
        await asyncio.to_thread(process_a.join, 90)
        assert process_a.exitcode == 75
        process_a.close()
        snapshot = cast(dict[str, object], json.loads(marker.read_text(encoding="utf-8")))
        assert snapshot["state"] == PlanningAttemptState.PROVIDER_FENCED.value
        assert snapshot["model_response"] is None
        assert snapshot["turn_id"] == snapshot["planner_turn_id"]
        assert snapshot["turn_last_stage"] == "model_output_started"
        assert snapshot["turn_request_started_count"] == 1
        assert snapshot["turn_output_started"] is True
        assert snapshot["provider_call_count"] == 1

        process_b = context.Process(
            target=_spawn_real_planner_recovery,
            args=(
                str(root),
                str(repository),
                str(state_dir),
                planning_id,
                objective,
                str(marker),
                str(call_log),
                str(result_file),
            ),
        )
        process_b.start()
        await asyncio.to_thread(process_b.join, 90)
        assert process_b.exitcode == 0
        process_b.close()
        recovered = cast(dict[str, object], json.loads(result_file.read_text(encoding="utf-8")))
        assert recovered["planner_session_id"] != snapshot["planner_session_id"]
        assert recovered["status"] == "error"
        assert recovered["error_type"] == "ConfigurationError"
        assert "observable provider-turn evidence" in str(recovered["error_message"])
        assert recovered["provider_call_count_before"] == 1
        assert recovered["provider_call_count_after"] == 1
        assert len(_read_durable_json_lines(call_log)) == 1

        observer = SqliteSessionStore(database)
        await observer.initialize()
        attempt = await observer.get_model_planning_attempt(planning_id)
        assert attempt is not None
        assert attempt.state is PlanningAttemptState.INDETERMINATE
        assert attempt.planner_session_id == snapshot["planner_session_id"]
        assert attempt.planner_turn_id == snapshot["planner_turn_id"]
        turns = await observer.load_turn_attempts(str(snapshot["planner_session_id"]))
        turn = next(item for item in turns if item.turn_id == snapshot["planner_turn_id"])
        assert turn.output_started is True
        assert turn.last_stage.value == "model_output_started"
        _assert_planning_publication_counts(
            database,
            expected_proposal_count=0,
            expected_dag_count=0,
        )


@pytest.mark.asyncio
async def test_fresh_composition_controllers_race_without_mutating_winner() -> None:
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="neuro-planning-controller-race-") as directory:
        root = Path(directory)
        repository = _make_repository(root)
        state_dir = root / "state"
        _write_fixture_config(state_dir)
        planning_id = "fresh-controller-race"
        objective = "decompose this objective"
        call_log = root / "planner-calls.jsonl"
        database = state_dir / "sessions.db"

        seed = SqliteSessionStore(database)
        await seed.initialize()
        parent_session_id = await seed.create_session(
            str(repository),
            "fixture",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )
        start_barrier = context.Barrier(3)
        provider_started_event = context.Event()
        release_provider_event = context.Event()
        controller_finished_event = context.Event()
        processes = [
            context.Process(
                target=_spawn_real_planner_controller,
                args=(
                    str(root),
                    str(repository),
                    str(state_dir),
                    parent_session_id,
                    planning_id,
                    objective,
                    str(call_log),
                    start_barrier,
                    provider_started_event,
                    release_provider_event,
                    controller_finished_event,
                    str(root / f"controller-{index}.json"),
                ),
            )
            for index in (1, 2)
        ]
        for process in processes:
            process.start()
        try:
            await asyncio.to_thread(start_barrier.wait, 90)
            assert await asyncio.to_thread(provider_started_event.wait, 90)
            assert await asyncio.to_thread(controller_finished_event.wait, 90)
            release_provider_event.set()
            for process in processes:
                await asyncio.to_thread(process.join, 90)
                assert process.exitcode == 0
            outcomes = [
                cast(
                    dict[str, object],
                    json.loads((root / f"controller-{index}.json").read_text(encoding="utf-8")),
                )
                for index in (1, 2)
            ]
        finally:
            release_provider_event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 15)
                process.close()

        assert sum(outcome["status"] == "completed" for outcome in outcomes) == 1
        winner = next(outcome for outcome in outcomes if outcome["status"] == "completed")
        loser = next(outcome for outcome in outcomes if outcome["status"] == "error")
        assert winner["attempt_state"] == PlanningAttemptState.COMPLETED.value
        assert winner["historical_planner_session_id"] == winner["planner_session_id"]
        assert winner["intended_dag_id"]
        assert winner["dag_id"] == winner["intended_dag_id"]
        assert loser["error_type"] == "ConfigurationError"
        assert loser["planner_session_id"] != winner["planner_session_id"]
        assert len(_read_durable_json_lines(call_log)) == 1
        assert all(outcome["provider_call_count"] <= 1 for outcome in outcomes)
        assert sum(outcome["provider_call_count"] == 1 for outcome in outcomes) >= 1
        _assert_planning_publication_counts(
            database,
            expected_proposal_count=1,
            expected_dag_count=1,
        )


class _ProductionPlanningState:
    def __init__(
        self,
        *,
        provider_call_log: Path | None = None,
        provider_started_event=None,
        provider_release_event=None,
    ) -> None:
        self.planner_calls = 0
        self.leader_calls = 0
        self.zero_tool_calls = 0
        self.provider_call_log = provider_call_log
        self.provider_started_event = provider_started_event
        self.provider_release_event = provider_release_event
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
            response = json.dumps(
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
            )
            if self._state.provider_call_log is not None:
                _append_durable_json_line(
                    self._state.provider_call_log,
                    {"response": response},
                )
            if self._state.provider_started_event is not None:
                self._state.provider_started_event.set()
                await asyncio.to_thread(self._state.provider_release_event.wait, 90)
            yield ModelCompleted(
                "stop",
                response_text=response,
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
