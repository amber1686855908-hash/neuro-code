from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.application.ports.tools import Tool, ToolCollection, ToolContext
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeObserver,
    WorkspaceChangeReport,
    WorkspaceFileChange,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.finalization import (
    FinalizationAttempt,
    FinalizationEvidence,
    FinalizationResult,
    FinalizationStatus,
)
from neuro_code.application.runtime.model_step import (
    MAX_BUFFERED_MODEL_STEP_TEXT_BYTES,
    MAX_BUFFERED_MODEL_STEP_TEXT_CHUNKS,
)
from neuro_code.application.runtime.verification import VerificationState
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.execution import (
    TurnRecoveryResolution,
    VerificationRequirement,
    VerificationRequirementsSnapshot,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import ProviderError


class _ScriptedProvider:
    provider_name = "gate-scripted"
    model_name = "gate-model"
    context_affinity = "profile-v1:gate"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent | BaseException]]) -> None:
        self.scripts = [tuple(script) for script in scripts]
        self.calls: list[ModelContext] = []
        self.tool_policies: list[ModelToolPolicy] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tools
        self.calls.append(context)
        self.tool_policies.append(tool_policy)
        script = self.scripts.pop(0)
        for event in script:
            if isinstance(event, BaseException):
                raise event
            yield event


class _BlockingCandidateProvider(_ScriptedProvider):
    def __init__(self) -> None:
        super().__init__(())
        self.candidate_started = asyncio.Event()

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        yield ModelTextDelta("buffered candidate")
        self.candidate_started.set()
        await asyncio.Event().wait()
        if False:
            yield ModelCompleted("stop")


class _Checkpoint(WorkspaceChangeCheckpoint):
    __slots__ = ("sequence",)

    def __init__(self, sequence: int) -> None:
        self.sequence = sequence


class _FixedWorkspaceObserver:
    def __init__(self, *, changed: bool = True) -> None:
        self._report = WorkspaceChangeReport(
            (
                WorkspaceFileChange(
                    "changed.txt",
                    "modified",
                    1,
                    0,
                    diff="+changed",
                    diff_truncated=False,
                ),
            )
            if changed
            else (),
            omitted_files=0,
            scan_limited=False,
        )
        self._sequence = 0

    def capture(self, root: Path, /) -> WorkspaceChangeCheckpoint:
        del root
        self._sequence += 1
        return _Checkpoint(self._sequence)

    def compare(
        self,
        before: WorkspaceChangeCheckpoint,
        after: WorkspaceChangeCheckpoint,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport:
        del before, after, explicit_redactions
        return self._report


class _Tools:
    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())


class _FixtureTool:
    def __init__(self, name: str, result: ToolResult, *, side_effecting: bool) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Fixture {name}",
            input_schema={"type": "object", "additionalProperties": False},
        )
        self.side_effecting = side_effecting
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del context
        self.calls.append(dict(arguments))
        return self._result


class _RecordingFinalizer:
    def __init__(self, response: str = "evidence-aware response") -> None:
        self.response = response
        self.calls: list[tuple[ModelContext, FinalizationEvidence]] = []

    async def finalize(
        self,
        context: ModelContext,
        evidence: FinalizationEvidence,
    ) -> FinalizationResult:
        self.calls.append((context, evidence))
        attempt = FinalizationAttempt(1, True, "stop", None, None, 0, len(self.response))
        return FinalizationResult(
            FinalizationStatus.COMPLETED,
            self.response,
            (attempt,),
            None,
            None,
            0,
            True,
            "stop",
        )


class _FailingFinalizer:
    async def finalize(
        self,
        context: ModelContext,
        evidence: FinalizationEvidence,
    ) -> FinalizationResult:
        del context, evidence
        raise ProviderError("finalizer unavailable")


class _RejectedFinalizer:
    async def finalize(
        self,
        context: ModelContext,
        evidence: FinalizationEvidence,
    ) -> FinalizationResult:
        del context, evidence
        attempt = FinalizationAttempt(1, True, "tool_calls", None, None, 1, 0)
        return FinalizationResult(
            FinalizationStatus.TOOL_CALL_REJECTED,
            "unsafe finalizer fallback",
            (attempt,),
            None,
            None,
            1,
            False,
            "tool_calls",
        )


class _BlockingFinalizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def finalize(
        self,
        context: ModelContext,
        evidence: FinalizationEvidence,
    ) -> FinalizationResult:
        del context, evidence
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _factory(finalizer: object):
    def create(
        provider: ModelProvider,
        attempts: int,
        redactions: tuple[str, ...],
    ) -> object:
        del provider, attempts, redactions
        return finalizer

    return create


def _mutation_call(call_id: str = "mutate") -> ModelToolCall:
    return ModelToolCall(ToolCall(call_id, "mutate", {}))


def _verification_call(call_id: str = "verify") -> ModelToolCall:
    return ModelToolCall(ToolCall(call_id, "bash", {"command": "pytest -q"}))


def _terminal_candidate(text: str = "tests passed") -> tuple[ModelTextDelta, ModelCompleted]:
    return ModelTextDelta(text), ModelCompleted("stop")


def _runtime(
    root: Path,
    provider: ModelProvider,
    tools: ToolCollection,
    observer: WorkspaceChangeObserver,
    *,
    finalizer: object | None = None,
    session_store: SqliteSessionStore | None = None,
) -> AgentRuntime:
    return AgentRuntime(
        provider=provider,
        tools=tools,
        workspace_change_observer=observer,
        permissions=PermissionManager(mode=PermissionMode.BYPASS),
        tool_context=ToolContext(root),
        session_store=session_store,
        finalizer_factory=_factory(finalizer) if finalizer is not None else None,
    )


def _text_events(events: Sequence[AgentEvent]) -> list[object]:
    return [event.data["text"] for event in events if event.kind is AgentEventKind.TEXT_DELTA]


@pytest.mark.asyncio
async def test_non_gated_terminal_text_streams_without_a_finalizer(tmp_path: Path) -> None:
    provider = _ScriptedProvider(((_terminal_candidate("visible")),))

    def unexpected_factory(*_args: object) -> object:
        raise AssertionError("non-gated turns must not invoke the finalizer")

    runtime = AgentRuntime(
        provider=provider,
        tools=_Tools(()),
        workspace_change_observer=_FixedWorkspaceObserver(changed=False),
        permissions=PermissionManager(mode=PermissionMode.BYPASS),
        tool_context=ToolContext(tmp_path),
        finalizer_factory=unexpected_factory,
    )

    result = await runtime.run("answer")

    assert result.response == "visible"
    assert _text_events(result.events) == ["visible"]
    assert not any(event.kind is AgentEventKind.FINALIZING_STARTED for event in result.events)


@pytest.mark.asyncio
async def test_structured_requirements_activate_the_gate_before_the_first_model_delta(
    tmp_path: Path,
) -> None:
    snapshot = VerificationRequirementsSnapshot.create(
        (VerificationRequirement.create(criterion="run the relevant checks"),)
    )
    finalizer = _RecordingFinalizer("structured safe response")
    provider = _ScriptedProvider(((_terminal_candidate("tests passed")),))

    result = await _runtime(
        tmp_path,
        provider,
        _Tools(()),
        _FixedWorkspaceObserver(changed=False),
        finalizer=finalizer,
    ).run("answer", verification_requirements=snapshot)

    assert result.verification is not None
    assert result.verification.requirements_fingerprint == snapshot.fingerprint
    assert result.verification.requirement_evaluations[0].requirement_id == (
        snapshot.requirements[0].requirement_id
    )
    assert finalizer.calls[0][1].verification_state is VerificationState.INCOMPLETE
    assert _text_events(result.events) == ["structured safe response"]
    assert "tests passed" not in repr(result.events)


@pytest.mark.asyncio
async def test_gated_text_buffer_byte_overflow_fails_closed_without_partial_text(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        (
            (
                ModelTextDelta("x" * MAX_BUFFERED_MODEL_STEP_TEXT_BYTES),
                ModelTextDelta("overflow"),
                ModelCompleted("stop"),
            ),
        )
    )
    events: list[AgentEvent] = []

    async def collect(event: AgentEvent) -> None:
        events.append(event)

    with pytest.raises(ProviderError, match="byte limit"):
        await _runtime(
            tmp_path,
            provider,
            _Tools(()),
            _FixedWorkspaceObserver(changed=False),
        ).run("bounded", sink=collect, verification_required=True)

    assert _text_events(events) == []


@pytest.mark.asyncio
async def test_gated_text_buffer_chunk_overflow_fails_closed_without_partial_text(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        (
            (
                *(ModelTextDelta("x") for _ in range(MAX_BUFFERED_MODEL_STEP_TEXT_CHUNKS)),
                ModelTextDelta("overflow"),
                ModelCompleted("stop"),
            ),
        )
    )
    events: list[AgentEvent] = []

    async def collect(event: AgentEvent) -> None:
        events.append(event)

    with pytest.raises(ProviderError, match="chunk limit"):
        await _runtime(
            tmp_path,
            provider,
            _Tools(()),
            _FixedWorkspaceObserver(changed=False),
        ).run("bounded", sink=collect, verification_required=True)

    assert _text_events(events) == []


@pytest.mark.asyncio
async def test_mutation_candidate_is_not_public_or_durable(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    finalizer = _RecordingFinalizer("safe committed response")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("tests passed"),
        )
    )
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
        session_store=store,
    )

    result = await runtime.run("change files")

    assert result.response == "safe committed response"
    assert result.response_contract is not None
    assert result.response_contract.source.value == "evidence_aware_finalizer"
    assert _text_events(result.events) == ["safe committed response"]
    assert "tests passed" not in repr(result.messages)
    assert "tests passed" not in repr(result.items)
    assert finalizer.calls[0][1].verification_state is VerificationState.INCOMPLETE
    assert result.session_id is not None
    completed = next(
        event for event in result.events if event.kind is AgentEventKind.TURN_COMPLETED
    )
    assert completed.data["execution_status"] == "completed"
    assert completed.data["finalized"] is False
    assert completed.data["recoverable"] is False
    assert completed.data["response_committed"] is True
    assert completed.data["response_source"] == "evidence_aware_finalizer"
    persisted_events = await store.load_events(result.session_id)
    persisted_items = await store.load_session_items(result.session_id)
    assert "tests passed" not in repr(persisted_events)
    assert "tests passed" not in repr(persisted_items)
    assert not any(
        event["kind"] == AgentEventKind.TURN_COMPLETED.value
        and event["data"].get("response") == "tests passed"
        for event in persisted_events
    )


@pytest.mark.asyncio
async def test_finalizer_response_is_delivered_after_atomic_completion_commit(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    finalizer = _RecordingFinalizer("safe committed response")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("PROVISIONAL_FALSE_SUCCESS_SENTINEL"),
        )
    )
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
        session_store=store,
    )
    session_id = await store.create_session(str(tmp_path), "gate-scripted", "gate-model")
    delivered: list[AgentEvent] = []

    async def sink(event: AgentEvent) -> None:
        delivered.append(event)
        if event.kind is AgentEventKind.TEXT_DELTA:
            assert event.data["text"] == "safe committed response"
            persisted = await store.load_events(session_id)
            kinds = [item["kind"] for item in persisted]
            assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(persisted)
            assert AgentEventKind.TEXT_DELTA.value in kinds
            assert kinds[-1] == AgentEventKind.TURN_COMPLETED.value

    result = await runtime.run("change files", session_id=session_id, sink=sink)

    assert result.response == "safe committed response"
    assert [event.kind for event in delivered if event.kind is AgentEventKind.TEXT_DELTA] == [
        AgentEventKind.TEXT_DELTA
    ]
    persisted_events = await store.load_events(session_id)
    response_sequence = next(
        item["sequence"]
        for item in persisted_events
        if item["kind"] == AgentEventKind.TEXT_DELTA.value
    )
    completed_sequence = next(
        item["sequence"]
        for item in persisted_events
        if item["kind"] == AgentEventKind.TURN_COMPLETED.value
    )
    assert response_sequence < completed_sequence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "delivered_kinds"),
    [
        (AgentEventKind.TEXT_DELTA, [AgentEventKind.TEXT_DELTA]),
        (
            AgentEventKind.TURN_COMPLETED,
            [AgentEventKind.TEXT_DELTA, AgentEventKind.TURN_COMPLETED],
        ),
    ],
)
async def test_post_commit_sink_failure_does_not_reclassify_committed_turn(
    tmp_path: Path,
    failure_kind: AgentEventKind,
    delivered_kinds: list[AgentEventKind],
) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    finalizer = _RecordingFinalizer("safe committed response")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("PROVISIONAL_FALSE_SUCCESS_SENTINEL"),
        )
    )
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
        session_store=store,
    )
    session_id = await store.create_session(str(tmp_path), "gate-scripted", "gate-model")
    delivered: list[AgentEventKind] = []

    async def sink(event: AgentEvent) -> None:
        delivered.append(event.kind)
        if event.kind is failure_kind:
            raise RuntimeError("sink disconnected")

    with pytest.raises(RuntimeError, match="sink disconnected"):
        await runtime.run("change files", session_id=session_id, sink=sink)

    persisted_events = await store.load_events(session_id)
    persisted_items = await store.load_session_items(session_id)
    terminal_delivered = [
        kind
        for kind in delivered
        if kind in {AgentEventKind.TEXT_DELTA, AgentEventKind.TURN_COMPLETED}
    ]
    assert terminal_delivered == delivered_kinds
    assert finalizer.calls
    assert len(finalizer.calls) == 1
    assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(persisted_events)
    assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(persisted_items)
    assert any(
        event["kind"] == AgentEventKind.TEXT_DELTA.value
        and event["data"].get("text") == "safe committed response"
        for event in persisted_events
    )
    assert any(event["kind"] == AgentEventKind.TURN_COMPLETED.value for event in persisted_events)
    assert not any(event["kind"] == AgentEventKind.TURN_FAILED.value for event in persisted_events)
    attempts = await store.load_turn_attempts(session_id)
    assert len(attempts) == 1
    assert attempts[0].resolution is TurnRecoveryResolution.COMMITTED
    assert await store.load_open_turn_attempts(session_id) == []


@pytest.mark.asyncio
async def test_post_commit_cancellation_does_not_persist_failure_or_retry_finalization(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    finalizer = _RecordingFinalizer("safe committed response")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("PROVISIONAL_FALSE_SUCCESS_SENTINEL"),
        )
    )
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
        session_store=store,
    )
    session_id = await store.create_session(str(tmp_path), "gate-scripted", "gate-model")
    delivered: list[AgentEventKind] = []

    async def sink(event: AgentEvent) -> None:
        delivered.append(event.kind)
        if event.kind is AgentEventKind.TURN_COMPLETED:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await runtime.run("change files", session_id=session_id, sink=sink)

    persisted_events = await store.load_events(session_id)
    assert [
        kind
        for kind in delivered
        if kind in {AgentEventKind.TEXT_DELTA, AgentEventKind.TURN_COMPLETED}
    ] == [AgentEventKind.TEXT_DELTA, AgentEventKind.TURN_COMPLETED]
    assert finalizer.calls
    assert len(finalizer.calls) == 1
    assert not any(event["kind"] == AgentEventKind.TURN_FAILED.value for event in persisted_events)
    assert any(event["kind"] == AgentEventKind.TURN_COMPLETED.value for event in persisted_events)
    assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(persisted_events)
    attempts = await store.load_turn_attempts(session_id)
    assert len(attempts) == 1
    assert attempts[0].resolution is TurnRecoveryResolution.COMMITTED


@pytest.mark.asyncio
async def test_cancellation_after_storage_commit_does_not_reclassify_completion(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    finalizer = _RecordingFinalizer("safe committed response")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("PROVISIONAL_FALSE_SUCCESS_SENTINEL"),
        )
    )
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
        session_store=store,
    )
    session_id = await store.create_session(str(tmp_path), "gate-scripted", "gate-model")
    original_finalize = store.finalize_turn
    committed = asyncio.Event()
    release = asyncio.Event()

    async def delayed_finalize(*args: object, **kwargs: object) -> None:
        await original_finalize(*args, **kwargs)
        committed.set()
        await release.wait()

    with patch.object(store, "finalize_turn", new=delayed_finalize):
        task = asyncio.create_task(runtime.run("change files", session_id=session_id))
        await committed.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    persisted_events = await store.load_events(session_id)
    assert finalizer.calls
    assert len(finalizer.calls) == 1
    assert any(event["kind"] == AgentEventKind.TURN_COMPLETED.value for event in persisted_events)
    assert not any(event["kind"] == AgentEventKind.TURN_FAILED.value for event in persisted_events)
    assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(persisted_events)
    attempts = await store.load_turn_attempts(session_id)
    assert len(attempts) == 1
    assert attempts[0].resolution is TurnRecoveryResolution.COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verification_result", "expected_state"),
    [
        (ToolResult("2 passed"), VerificationState.PASS),
        (ToolResult("1 failed", is_error=True), VerificationState.FAIL),
    ],
)
async def test_gated_terminal_candidate_uses_authoritative_verification_state(
    tmp_path: Path,
    verification_result: ToolResult,
    expected_state: VerificationState,
) -> None:
    finalizer = _RecordingFinalizer("verified-aware response")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            (_verification_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("tests passed"),
        )
    )
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools(
            (
                _FixtureTool("mutate", ToolResult("changed"), side_effecting=True),
                _FixtureTool("bash", verification_result, side_effecting=False),
            )
        ),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
    )

    result = await runtime.run("change and verify")

    assert result.verification is not None
    assert result.verification.state is expected_state
    assert finalizer.calls[0][1].verification_state is expected_state
    assert result.response == "verified-aware response"
    assert "tests passed" not in repr(result.events)


@pytest.mark.asyncio
async def test_explicit_verification_evidence_activates_gate_without_mutation(
    tmp_path: Path,
) -> None:
    finalizer = _RecordingFinalizer("evidence-only response")
    provider = _ScriptedProvider(
        (
            (_verification_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("tests passed"),
        )
    )
    result = await _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("bash", ToolResult("2 passed"), side_effecting=False),)),
        _FixedWorkspaceObserver(changed=False),
        finalizer=finalizer,
    ).run("verify", verification_required=False)

    assert result.verification is not None
    assert result.verification.state is VerificationState.PASS
    assert result.verification.workspace_generation == 0
    assert finalizer.calls[0][1].verification_state is VerificationState.PASS
    assert result.response == "evidence-only response"
    assert "tests passed" not in repr(result.events)


@pytest.mark.asyncio
async def test_mutation_without_verification_commits_only_incomplete_summary(
    tmp_path: Path,
) -> None:
    finalizer = _RecordingFinalizer("cannot claim verified")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("tests passed"),
        )
    )
    result = await _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
    ).run("change")

    assert result.verification is not None
    assert result.verification.state is VerificationState.INCOMPLETE
    assert result.response == "cannot claim verified"
    assert "tests passed" not in result.response


@pytest.mark.asyncio
async def test_pass_becomes_incomplete_after_a_later_mutation(tmp_path: Path) -> None:
    finalizer = _RecordingFinalizer("stale evidence response")
    provider = _ScriptedProvider(
        (
            (_mutation_call("mutate-1"), ModelCompleted("tool_calls")),
            (_verification_call(), ModelCompleted("tool_calls")),
            (_mutation_call("mutate-2"), ModelCompleted("tool_calls")),
            _terminal_candidate("tests passed"),
        )
    )
    result = await _runtime(
        tmp_path,
        provider,
        _Tools(
            (
                _FixtureTool("mutate", ToolResult("changed"), side_effecting=True),
                _FixtureTool("bash", ToolResult("2 passed"), side_effecting=False),
            )
        ),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
    ).run("change twice")

    assert result.verification is not None
    assert result.verification.state is VerificationState.INCOMPLETE
    assert result.verification.workspace_generation == 2
    assert finalizer.calls[0][1].verification_state is VerificationState.INCOMPLETE
    assert "tests passed" not in repr(result.events)


@pytest.mark.asyncio
async def test_gated_tool_step_releases_intermediate_text_before_tool_execution(
    tmp_path: Path,
) -> None:
    finalizer = _RecordingFinalizer("final response")
    provider = _ScriptedProvider(
        (
            (
                ModelTextDelta("intermediate"),
                ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                ModelCompleted("tool_calls"),
            ),
            _terminal_candidate("candidate"),
        )
    )
    result = await _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("inspect", ToolResult("evidence"), side_effecting=False),)),
        _FixedWorkspaceObserver(changed=False),
        finalizer=finalizer,
    ).run("inspect", verification_required=True)

    kinds = [event.kind for event in result.events]
    intermediate_index = next(
        index
        for index, event in enumerate(result.events)
        if event.kind is AgentEventKind.TEXT_DELTA and event.data["text"] == "intermediate"
    )
    tool_request_index = kinds.index(AgentEventKind.TOOL_REQUESTED)
    assert intermediate_index < tool_request_index
    assert _text_events(result.events) == ["intermediate", "final response"]
    assert "candidate" not in repr(result.events)


@pytest.mark.asyncio
async def test_gated_finalizer_failure_uses_deterministic_fallback_not_candidate(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("tests passed"),
        )
    )
    result = await _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=_FailingFinalizer(),
    ).run("change")

    assert result.response_contract is not None
    assert result.response_contract.source.value == "deterministic_fallback"
    assert "tests passed" not in result.response
    assert "Recorded verification state: incomplete." in result.response
    assert _text_events(result.events) == [result.response]


@pytest.mark.asyncio
async def test_gated_non_completed_finalizer_result_uses_deterministic_fallback(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("tests passed"),
        )
    )
    result = await _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=_RejectedFinalizer(),
    ).run("change")

    assert result.response_contract is not None
    assert result.response_contract.source.value == "deterministic_fallback"
    assert "unsafe finalizer fallback" not in result.response
    assert "Recorded verification state: incomplete." in result.response
    assert _text_events(result.events) == [result.response]


@pytest.mark.asyncio
async def test_cancellation_during_candidate_buffering_does_not_emit_candidate(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    session_id = await store.create_session(str(tmp_path), "gate-scripted", "gate-model")
    provider = _BlockingCandidateProvider()
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools(()),
        _FixedWorkspaceObserver(changed=False),
        session_store=store,
    )
    task = asyncio.create_task(
        runtime.run("cancel", session_id=session_id, verification_required=True)
    )
    await provider.candidate_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    persisted_events = await store.load_events(session_id)
    assert "buffered candidate" not in repr(persisted_events)
    assert not any(
        event["kind"] == AgentEventKind.TURN_COMPLETED.value for event in persisted_events
    )
    attempts = await store.load_turn_attempts(session_id)
    assert len(attempts) == 1
    assert attempts[0].output_started is True
    assert attempts[0].resolution is TurnRecoveryResolution.CANCELLED
    assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(attempts[0].safe_projection())
    assert any(event["kind"] == AgentEventKind.TURN_FAILED.value for event in persisted_events)


@pytest.mark.asyncio
async def test_cancellation_during_finalization_does_not_commit_candidate(tmp_path: Path) -> None:
    finalizer = _BlockingFinalizer()
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("buffered candidate"),
        )
    )
    runtime = _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
    )
    task = asyncio.create_task(runtime.run("cancel finalizer"))
    await finalizer.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_failure_before_turn_commit_never_replays_terminal_candidate(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    finalizer = _RecordingFinalizer("committed final")
    provider = _ScriptedProvider(
        (
            (_mutation_call(), ModelCompleted("tool_calls")),
            _terminal_candidate("PROVISIONAL_FALSE_SUCCESS_SENTINEL"),
        )
    )
    session_id = await store.create_session(str(tmp_path), "gate-scripted", "gate-model")

    runtime = _runtime(
        tmp_path,
        provider,
        _Tools((_FixtureTool("mutate", ToolResult("changed"), side_effecting=True),)),
        _FixedWorkspaceObserver(),
        finalizer=finalizer,
        session_store=store,
    )

    with (
        patch.object(
            store,
            "finalize_turn",
            new=AsyncMock(side_effect=RuntimeError("simulated failure before turn commit")),
        ),
        pytest.raises(RuntimeError, match="simulated failure"),
    ):
        await runtime.run("fail before commit", session_id=session_id)

    persisted_events = await store.load_events(session_id)
    persisted_items = await store.load_session_items(session_id)
    assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(persisted_events)
    assert "committed final" not in repr(persisted_events)
    assert "committed final" not in repr(persisted_items)
    assert not any(
        event["kind"] == AgentEventKind.TURN_COMPLETED.value for event in persisted_events
    )
    attempts = await store.load_turn_attempts(session_id)
    assert len(attempts) == 1
    assert attempts[0].output_started is True
    assert attempts[0].resolution is TurnRecoveryResolution.FAILED
    assert "PROVISIONAL_FALSE_SUCCESS_SENTINEL" not in repr(attempts[0].safe_projection())
