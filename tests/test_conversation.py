from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    ProviderContextWindow,
)
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeGate,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeResult,
    ContextCompactionSafePoint,
    ContextCompactionTimeoutError,
    ContextCompactionTurnProjection,
)
from neuro_code.application.memory.compaction_service import ContextCompactionApplicationService
from neuro_code.application.memory.compaction_trigger import (
    ContextCompactionTriggerMode,
    ContextCompactionTriggerRequest,
    ContextCompactionTriggerService,
)
from neuro_code.application.permissions.policy import PermissionManager
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.sessions.conversation import (
    PLAN_EXECUTION_PROMPT,
    AgentConversation,
)
from neuro_code.domain.background_tasks import BackgroundWakeState
from neuro_code.domain.conversation.compaction import compute_compaction_source_fingerprint
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
)
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SessionExecutionRecord,
    SupervisorReasonCode,
    TurnCancellationPolicy,
)
from neuro_code.domain.plans import PlanComment, PlanStep, PlanStepStatus, SessionPlan
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_tasks import (
    MAX_QUEUED_SESSION_TASKS,
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.background_tasks import LocalBackgroundTaskManager
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.tools.registry import default_tool_registry
from neuro_code.shared.errors import ConfigurationError, ProviderError
from tests.fakes import EmptyWorkspaceChangeObserver, FakeWorkspaceIdentity


class ConversationProvider:
    provider_name = "conversation-fixture"
    model_name = "fixture-model"
    context_affinity = "profile-v1:conversation-fixture"

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.contexts: list[ModelContext] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tools, tool_policy
        self.contexts.append(context)
        response = self._responses.pop(0)
        yield ModelTextDelta(response)
        yield ModelCompleted("stop")


class FailOnceConversationProvider(ConversationProvider):
    def __init__(self) -> None:
        super().__init__(("recovered response",))
        self._failed = False

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        if not self._failed:
            self._failed = True
            self.contexts.append(context)
            raise ProviderError("fixture provider failure")
        async for event in super().stream(context, tools):
            yield event


class CancelOnceConversationProvider(ConversationProvider):
    def __init__(self) -> None:
        super().__init__(("recovered response",))
        self.started = asyncio.Event()
        self._blocked = False

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        if not self._blocked:
            self._blocked = True
            self.contexts.append(context)
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        async for event in super().stream(context, tools):
            yield event


class CancelAfterTextConversationProvider(ConversationProvider):
    def __init__(self) -> None:
        super().__init__(("recovered response",))
        self.started = asyncio.Event()

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tools, tool_policy
        self.contexts.append(context)
        yield ModelTextDelta("partial output")
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BlockingCompactionGate(ContextCompactionRuntimeGate):
    __slots__ = ("release", "started")

    def __init__(self, store: SqliteSessionStore, provider: ConversationProvider) -> None:
        super().__init__(
            ContextCompactionTriggerService(ContextCompactionApplicationService(store, provider))
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def trigger(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult:
        self.started.set()
        await self.release.wait()
        return await super().trigger(request)


class RaisingCompactionGate(ContextCompactionRuntimeGate):
    __slots__ = ("error",)

    def __init__(
        self, store: SqliteSessionStore, provider: ConversationProvider, error: BaseException
    ):
        super().__init__(
            ContextCompactionTriggerService(ContextCompactionApplicationService(store, provider))
        )
        self.error = error

    async def trigger(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult:
        del request
        raise self.error


def _disabled_compaction_request(
    *,
    session_id: str | None = None,
    mode: ContextCompactionTriggerMode = ContextCompactionTriggerMode.DISABLED,
) -> ContextCompactionRuntimeRequest:
    context = ModelContext.from_messages((Message(Role.USER, "immutable compaction snapshot"),))
    usage = CompactionContextUsage.from_provider_window(
        1,
        ProviderContextWindow(
            "conversation-fixture",
            "fixture-model",
            1_000,
            context_affinity="profile-v1:conversation-fixture",
        ),
        estimated=True,
    )
    return ContextCompactionRuntimeRequest(
        ContextCompactionTriggerRequest(
            context=context,
            usage=usage,
            mode=mode,
            session_id=session_id,
        ),
        ContextCompactionRuntimeBoundary(
            safe_point=ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
            model_step=0,
        ),
    )


def _explicit_compaction_request(
    service: ContextCompactionTriggerService,
    *,
    session_id: str,
    context: ModelContext,
    boundary: ContextCompactionRuntimeBoundary,
) -> ContextCompactionRuntimeRequest:
    usage = CompactionContextUsage.from_provider_window(
        9_500,
        ProviderContextWindow(
            "conversation-fixture",
            "fixture-model",
            10_000,
            context_affinity="profile-v1:conversation-fixture",
        ),
        estimated=True,
    )
    base = ContextCompactionTriggerRequest(
        context=context,
        usage=usage,
        mode=ContextCompactionTriggerMode.EXPLICIT,
        protected_item_count=0,
    )
    plan = service.assess(base).plan
    if plan.candidate_range is None:
        raise AssertionError("conversation fixture must produce an actionable compaction plan")
    trigger = ContextCompactionTriggerRequest(
        context=context,
        usage=usage,
        mode=ContextCompactionTriggerMode.EXPLICIT,
        protected_item_count=0,
        session_id=session_id,
        compaction_id="conversation-compaction-1",
        expected_source_fingerprint=compute_compaction_source_fingerprint(
            context.items,
            plan.candidate_range,
        ),
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )
    return ContextCompactionRuntimeRequest(trigger, boundary)


class AgentConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_compaction_command_builds_live_snapshot_and_runs_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
                "profile-v1:conversation-fixture",
            )
            provider = ConversationProvider(("summary",))
            service = ContextCompactionTriggerService(
                ContextCompactionApplicationService(store, provider)
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=ContextCompactionRuntimeGate(service),
            )
            original_items = tuple(Message(Role.USER, f"history-{index}") for index in range(12))
            conversation = AgentConversation(
                runtime=runtime,
                store=store,
                items=original_items,
                session_id=session_id,
                source_provider=provider.provider_name,
                source_model=provider.model_name,
                source_context_affinity=provider.context_affinity,
            )
            boundary = ContextCompactionRuntimeBoundary(
                ContextCompactionSafePoint.AFTER_TOOL_BATCH,
                3,
            )
            provider_window = ProviderContextWindow(
                provider.provider_name,
                provider.model_name,
                10_000,
                context_affinity=provider.context_affinity,
            )
            seen: list[ContextCompactionTurnProjection] = []

            async def owner(
                projection: ContextCompactionTurnProjection,
            ) -> ContextCompactionTurnProjection:
                seen.append(projection)
                return projection

            result = await conversation.run_explicit_context_compaction_with_owner(
                boundary=boundary,
                provider_window=provider_window,
                reported_input_tokens=9_500,
                owner=owner,
            )

            self.assertIs(result, seen[0])
            self.assertTrue(result.triggered)
            self.assertIsNotNone(result.compaction_item)
            self.assertEqual(len(await store.load_compaction_items(session_id)), 1)
            self.assertEqual(conversation.items, original_items)
            self.assertEqual(len(provider.contexts), 1)
            self.assertEqual(provider.contexts[0].source_provider, provider.provider_name)
            self.assertEqual(provider.contexts[0].source_model, provider.model_name)

    async def test_explicit_compaction_command_requires_persisted_session_before_building(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            provider = ConversationProvider(())
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=ContextCompactionRuntimeGate(
                    ContextCompactionTriggerService(
                        ContextCompactionApplicationService(store, provider)
                    )
                ),
            )
            conversation = AgentConversation(
                runtime=runtime,
                store=store,
                items=(Message(Role.USER, "unsaved"),),
            )

            async def owner(_: ContextCompactionTurnProjection) -> None:
                raise AssertionError("owner must not run for an unsaved session")

            with self.assertRaisesRegex(ConfigurationError, "persisted session"):
                await conversation.run_explicit_context_compaction_with_owner(
                    boundary=ContextCompactionRuntimeBoundary(
                        ContextCompactionSafePoint.AFTER_TOOL_BATCH,
                        0,
                    ),
                    provider_window=None,
                    owner=owner,
                )
            self.assertEqual(provider.contexts, [])

    async def test_explicit_compaction_uses_the_session_turn_lock_and_keeps_snapshot_immutable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
                "profile-v1:conversation-fixture",
            )
            provider = ConversationProvider(("turn response",))
            gate = BlockingCompactionGate(store, provider)
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=gate,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )
            request = _disabled_compaction_request(session_id=session_id)

            compaction_task = asyncio.create_task(conversation.trigger_context_compaction(request))
            await gate.started.wait()
            turn_task = asyncio.create_task(conversation.run("run after compaction"))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(turn_task), timeout=0.02)

            gate.release.set()
            compaction_result = await compaction_task
            turn_result = await turn_task

            self.assertFalse(compaction_result.trigger_result.triggered)
            self.assertEqual(
                request.trigger.context.messages[0].content, "immutable compaction snapshot"
            )
            self.assertEqual(turn_result.response, "turn response")
            self.assertEqual(len(provider.contexts), 1)

    async def test_explicit_compaction_rejects_a_request_bound_to_another_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            provider = ConversationProvider(())
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=BlockingCompactionGate(store, provider),
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )

            with self.assertRaisesRegex(ConfigurationError, "different session"):
                await conversation.trigger_context_compaction(
                    _disabled_compaction_request(
                        session_id="another-session",
                        mode=ContextCompactionTriggerMode.EXPLICIT,
                    )
                )

    async def test_compaction_owner_rejects_noop_without_calling_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            provider = ConversationProvider(())
            service = ContextCompactionTriggerService(
                ContextCompactionApplicationService(store, provider)
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=ContextCompactionRuntimeGate(service),
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )
            owner_called = False

            async def unexpected_owner(_: ContextCompactionTurnProjection) -> None:
                nonlocal owner_called
                owner_called = True

            with self.assertRaisesRegex(ConfigurationError, "triggered or controlled"):
                await conversation.run_context_compaction_with_owner(
                    _disabled_compaction_request(session_id=session_id),
                    unexpected_owner,
                )
            self.assertFalse(owner_called)
            self.assertEqual(provider.contexts, [])

    async def test_compaction_owner_runs_under_session_lock_and_receives_success_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
                "profile-v1:conversation-fixture",
            )
            provider = ConversationProvider(("summary", "turn response"))
            service = ContextCompactionTriggerService(
                ContextCompactionApplicationService(store, provider)
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=ContextCompactionRuntimeGate(service),
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )
            request = _explicit_compaction_request(
                service,
                session_id=session_id,
                context=ModelContext.from_messages((Message(Role.USER, "snapshot"),) * 12),
                boundary=ContextCompactionRuntimeBoundary(
                    ContextCompactionSafePoint.AFTER_TOOL_BATCH,
                    1,
                ),
            )
            owner_started = asyncio.Event()
            owner_release = asyncio.Event()

            async def owner(
                projection: ContextCompactionTurnProjection,
            ) -> ContextCompactionTurnProjection:
                self.assertTrue(projection.triggered)
                self.assertIsNotNone(projection.compaction_item)
                owner_started.set()
                await owner_release.wait()
                return projection

            compaction_task = asyncio.create_task(
                conversation.run_context_compaction_with_owner(request, owner)
            )
            await owner_started.wait()
            turn_task = asyncio.create_task(conversation.run("after compaction"))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(turn_task), timeout=0.02)

            owner_release.set()
            projection = await compaction_task
            result = await turn_task

            self.assertTrue(projection.triggered)
            self.assertEqual(result.response, "turn response")
            self.assertEqual(len(await store.load_compaction_items(session_id)), 1)

    async def test_compaction_owner_consumes_timeout_but_propagates_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "provider", "model")

            timeout_runtime = AgentRuntime(
                provider=ConversationProvider(()),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=RaisingCompactionGate(
                    store,
                    ConversationProvider(()),
                    ContextCompactionTimeoutError("secret timeout"),
                ),
            )
            timeout_conversation = await AgentConversation.open(
                runtime=timeout_runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )
            request = _disabled_compaction_request(session_id=session_id)
            seen: list[ContextCompactionTurnProjection] = []

            async def consume_timeout(
                projection: ContextCompactionTurnProjection,
            ) -> str:
                seen.append(projection)
                return projection.outcome.reason_code.value if projection.outcome else "missing"

            self.assertEqual(
                await timeout_conversation.run_context_compaction_with_owner(
                    request,
                    consume_timeout,
                ),
                "wall_time_budget",
            )
            self.assertEqual(len(seen), 1)
            self.assertTrue(seen[0].ready_for_turn_finalization)

            provider_error = ProviderError("provider failed")
            provider_runtime = AgentRuntime(
                provider=ConversationProvider(()),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=RaisingCompactionGate(
                    store,
                    ConversationProvider(()),
                    provider_error,
                ),
            )
            provider_conversation = await AgentConversation.open(
                runtime=provider_runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )
            owner_called = False

            async def unexpected_owner(_: ContextCompactionTurnProjection) -> None:
                nonlocal owner_called
                owner_called = True

            with self.assertRaisesRegex(ProviderError, "provider failed"):
                await provider_conversation.run_context_compaction_with_owner(
                    request,
                    unexpected_owner,
                )
            self.assertFalse(owner_called)

            cancelled_runtime = AgentRuntime(
                provider=ConversationProvider(()),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                compaction_runtime_gate=RaisingCompactionGate(
                    store,
                    ConversationProvider(()),
                    asyncio.CancelledError(),
                ),
            )
            cancelled_conversation = await AgentConversation.open(
                runtime=cancelled_runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )
            cancelled_owner_called = False

            async def unexpected_cancelled_owner(_: ContextCompactionTurnProjection) -> None:
                nonlocal cancelled_owner_called
                cancelled_owner_called = True

            with self.assertRaises(asyncio.CancelledError):
                await cancelled_conversation.run_context_compaction_with_owner(
                    request,
                    unexpected_cancelled_owner,
                )
            self.assertFalse(cancelled_owner_called)

    async def test_resume_loads_and_next_completion_replaces_the_durable_execution_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            paused_event = AgentEvent.create(
                1,
                AgentEventKind.TURN_COMPLETED,
                {"step": 1, "execution_status": "budget_limited"},
            )
            await store.append_event(session_id, paused_event)
            paused = SessionExecutionRecord(
                AgentExecutionOutcome(
                    AgentExecutionStatus.BUDGET_LIMITED,
                    SupervisorReasonCode.MODEL_STEP_LIMIT,
                    finalized=True,
                    recoverable=True,
                ),
                paused_event.sequence,
                paused_event.created_at,
            )
            await store.save_execution_record(session_id, paused)
            provider = ConversationProvider(("continued safely",))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )

            self.assertEqual(conversation.execution_record, paused)
            result = await conversation.run("continue with a new instruction")
            completed = next(
                event for event in result.events if event.kind is AgentEventKind.TURN_COMPLETED
            )
            self.assertEqual(
                conversation.execution_record,
                SessionExecutionRecord(
                    AgentExecutionOutcome(
                        AgentExecutionStatus.COMPLETED,
                        None,
                        finalized=False,
                        recoverable=False,
                    ),
                    completed.sequence,
                    completed.created_at,
                ),
            )

    async def test_resumed_conversation_loads_the_durable_plan_into_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            plan = SessionPlan(
                (PlanStep("Inspect the existing implementation", PlanStepStatus.IN_PROGRESS),),
                "Resume work without losing the agreed plan",
            )
            await store.save_session_plan(session_id, plan)
            comment = PlanComment(
                "plan-comment-resume",
                1,
                "Show the exact verification command in the revised plan.",
                datetime(2026, 7, 29, 13, tzinfo=UTC),
            )
            await store.add_plan_comment(session_id, plan, comment)
            provider = ConversationProvider(("resumed response",))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )
            result = await conversation.run("Continue the plan")

            self.assertEqual(conversation.plan, plan)
            self.assertEqual(conversation.plan_comments, (comment,))
            self.assertEqual(result.plan, plan)
            system = next(
                message for message in provider.contexts[0].messages if message.role is Role.SYSTEM
            )
            self.assertIn("Resume work without losing the agreed plan", system.content)
            self.assertIn("Inspect the existing implementation", system.content)
            self.assertIn("User comments on the current structured plan", system.content)
            self.assertIn("Show the exact verification command", system.content)

    async def test_adding_a_plan_comment_persists_without_starting_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            plan = SessionPlan(
                (PlanStep("Inspect the persisted feedback", PlanStepStatus.IN_PROGRESS),),
            )
            await store.save_session_plan(session_id, plan)
            provider = ConversationProvider(("not needed",))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )

            comment = await conversation.add_plan_comment(
                1,
                "Keep this plan reviewable before execution.",
            )

            self.assertEqual(conversation.plan_comments, (comment,))
            self.assertEqual(await conversation.list_plan_comments(), (comment,))
            self.assertEqual(provider.contexts, [])

    async def test_saved_plan_requires_an_explicit_execution_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            plan = SessionPlan(
                (PlanStep("Execute the approved change", PlanStepStatus.IN_PROGRESS),),
                "Make the saved plan actionable only after confirmation",
            )
            await store.save_session_plan(session_id, plan)
            provider = ConversationProvider(("executed response",))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )

            result = await conversation.execute_plan()

            self.assertEqual(result.response, "executed response")
            self.assertEqual(conversation.plan, plan)
            tasks = await conversation.list_session_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertIs(tasks[0].status, SessionTaskStatus.COMPLETED)
            self.assertEqual(await conversation.get_session_task(tasks[0].task_id), tasks[0])
            user = next(
                message for message in provider.contexts[0].messages if message.role is Role.USER
            )
            self.assertEqual(user.content, PLAN_EXECUTION_PROMPT)
            event_kinds = [event["kind"] for event in await store.load_events(session_id)]
            self.assertLess(
                event_kinds.index(AgentEventKind.PLAN_EXECUTION_REQUESTED.value),
                event_kinds.index(AgentEventKind.USER_MESSAGE.value),
            )

    async def test_schedule_plan_persists_without_model_execution_until_explicit_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            plan = SessionPlan(
                (PlanStep("Run the approved plan", PlanStepStatus.IN_PROGRESS),),
                "Keep this queued plan immutable until the user starts it.",
            )
            await store.save_session_plan(session_id, plan)
            provider = ConversationProvider(("queued response",))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )

            queued = await conversation.schedule_plan()

            self.assertIs(queued.status, SessionTaskStatus.QUEUED)
            self.assertEqual(queued.plan_snapshot, plan)
            self.assertEqual(provider.contexts, [])
            self.assertEqual(await conversation.list_session_tasks(), (queued,))

            result = await conversation.run_session_task(queued.task_id)

            self.assertEqual(result.response, "queued response")
            completed = await conversation.get_session_task(queued.task_id)
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertIs(completed.status, SessionTaskStatus.COMPLETED)
            self.assertEqual(len(provider.contexts), 1)

    async def test_schedule_plan_rejects_more_than_the_bounded_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "model")
            plan = SessionPlan((PlanStep("Keep queued"),))
            await store.save_session_plan(session_id, plan)
            conversation = await AgentConversation.open(
                runtime=AgentRuntime(
                    provider=ConversationProvider(()),
                    tools=default_tool_registry(),
                    workspace_change_observer=EmptyWorkspaceChangeObserver(),
                    permissions=PermissionManager(),
                    tool_context=ToolContext(root),
                    session_store=store,
                ),
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )

            for _ in range(MAX_QUEUED_SESSION_TASKS):
                await conversation.schedule_plan()
            with self.assertRaisesRegex(ConfigurationError, "at most 4 plan tasks"):
                await conversation.schedule_plan()

    async def test_schedule_plan_requires_saved_plan_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            conversation = await AgentConversation.open(
                runtime=AgentRuntime(
                    provider=ConversationProvider(()),
                    tools=default_tool_registry(),
                    workspace_change_observer=EmptyWorkspaceChangeObserver(),
                    permissions=PermissionManager(),
                    tool_context=ToolContext(root),
                    session_store=store,
                ),
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )

            with self.assertRaisesRegex(ConfigurationError, "has not been saved"):
                await conversation.schedule_plan()
            conversation._runtime.set_plan(SessionPlan((PlanStep("queued"),)))
            with self.assertRaisesRegex(ConfigurationError, "session is required"):
                await conversation.schedule_plan()

    async def test_plan_task_execution_requires_a_session_and_wake_state_is_empty_without_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            conversation = await AgentConversation.open(
                runtime=AgentRuntime(
                    provider=ConversationProvider(("unused",)),
                    tools=default_tool_registry(),
                    workspace_change_observer=EmptyWorkspaceChangeObserver(),
                    permissions=PermissionManager(),
                    tool_context=ToolContext(root),
                    session_store=store,
                ),
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )
            conversation._runtime.set_plan(SessionPlan((PlanStep("queued"),)))

            with self.assertRaisesRegex(ConfigurationError, "session is required"):
                await conversation.execute_plan(task_id="task-without-session")
            self.assertEqual(await conversation.load_background_wake_state(), BackgroundWakeState())
            state = BackgroundWakeState()
            await conversation.save_background_wake_state(state)

    async def test_run_session_task_rejects_unknown_kind_terminal_and_missing_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "model")
            plan = SessionPlan((PlanStep("Run the plan"),))
            await store.save_session_plan(session_id, plan)
            now = datetime(2026, 7, 29, 12, tzinfo=UTC)
            await store.create_session_task(
                session_id,
                SessionTask(
                    "task-subagent",
                    SessionTaskKind.SUBAGENT,
                    SessionTaskStatus.QUEUED,
                    now,
                ),
            )
            running = SessionTask(
                "task-completed",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.RUNNING,
                now,
            )
            await store.create_session_task(
                session_id,
                running.finish(SessionTaskStatus.COMPLETED, finished_at=now),
            )
            await store.create_session_task(
                session_id,
                SessionTask(
                    "task-no-snapshot",
                    SessionTaskKind.PLAN_EXECUTION,
                    SessionTaskStatus.QUEUED,
                    now,
                ),
            )
            conversation = await AgentConversation.open(
                runtime=AgentRuntime(
                    provider=ConversationProvider(()),
                    tools=default_tool_registry(),
                    workspace_change_observer=EmptyWorkspaceChangeObserver(),
                    permissions=PermissionManager(),
                    tool_context=ToolContext(root),
                    session_store=store,
                ),
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
                resume_id=session_id,
            )

            with self.assertRaisesRegex(ConfigurationError, "unknown queued plan task"):
                await conversation.run_session_task("missing")
            with self.assertRaisesRegex(ConfigurationError, "only plan execution"):
                await conversation.run_session_task("task-subagent")
            with self.assertRaisesRegex(ConfigurationError, "not queued"):
                await conversation.run_session_task("task-completed")
            with self.assertRaisesRegex(ConfigurationError, "no saved plan snapshot"):
                await conversation.run_session_task("task-no-snapshot")

    async def test_plan_execution_without_a_saved_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            runtime = AgentRuntime(
                provider=ConversationProvider(("unused",)),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )

            self.assertIsNone(await conversation.get_session_task("task-not-created"))
            with self.assertRaisesRegex(ConfigurationError, "has not been saved"):
                await conversation.execute_plan()

    async def test_failed_provider_attempt_does_not_consume_completion_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            manager = LocalBackgroundTaskManager()
            task = await manager.start_exec(
                sys.executable,
                ("-c", "pass"),
                display_command="fixture completion",
                cwd=root,
                env={},
                output_byte_limit=2_000,
                termination_grace_seconds=0.05,
            )
            await manager.get(task.task_id, wait_seconds=2)
            provider = FailOnceConversationProvider()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(enable_background_tasks=True),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root, background_tasks=manager),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )
            try:
                with self.assertRaisesRegex(ProviderError, "fixture provider failure"):
                    await conversation.run("failed prompt")
                self.assertEqual(
                    [item.task_id for item in await manager.pending_completions()],
                    [task.task_id],
                )

                await conversation.run("retry prompt")

                for context in provider.contexts:
                    reminder = "\n".join(
                        message.content
                        for message in context.messages
                        if "<background-task-completions>" in message.content
                    )
                    self.assertIn(task.task_id, reminder)
                self.assertEqual(await manager.pending_completions(), ())
            finally:
                await manager.shutdown()

    async def test_between_turn_completion_is_model_only_and_never_auto_wakes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            manager = LocalBackgroundTaskManager()
            provider = ConversationProvider(("first", "second", "third"))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(enable_background_tasks=True),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root, background_tasks=manager),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )
            try:
                await conversation.run("first prompt")
                task = await manager.start_exec(
                    sys.executable,
                    ("-c", "print('private completion output')"),
                    display_command="private completion command",
                    cwd=root,
                    env={},
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                await manager.get(task.task_id, wait_seconds=2)

                self.assertEqual(len(provider.contexts), 1)
                await conversation.run("second prompt")
                await conversation.run("third prompt")

                second_reminders = [
                    message.content
                    for message in provider.contexts[1].messages
                    if "<background-task-completions>" in message.content
                ]
                third_reminders = [
                    message.content
                    for message in provider.contexts[2].messages
                    if "<background-task-completions>" in message.content
                ]
                self.assertEqual(len(second_reminders), 1)
                self.assertIn(task.task_id, second_reminders[0])
                self.assertNotIn("private completion command", second_reminders[0])
                self.assertNotIn("private completion output", second_reminders[0])
                self.assertEqual(third_reminders, [])
                self.assertEqual(await manager.pending_completions(), ())
                self.assertNotIn(
                    "<background-task-completions>",
                    "\n".join(
                        item.content for item in conversation.items if isinstance(item, Message)
                    ),
                )
                assert conversation.session_id is not None
                persisted = await store.load_messages(conversation.session_id)
                self.assertNotIn(
                    "<background-task-completions>",
                    "\n".join(message.content for message in persisted),
                )
            finally:
                await manager.shutdown()

    async def test_explicit_background_wake_consumes_completion_without_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            manager = LocalBackgroundTaskManager()
            task = await manager.start_exec(
                sys.executable,
                ("-c", "print('wake completion output')"),
                display_command="fixture wake completion",
                cwd=root,
                env={},
                output_byte_limit=2_000,
                termination_grace_seconds=0.05,
            )
            await manager.get(task.task_id, wait_seconds=2)
            provider = ConversationProvider(("wake response", "next response"))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(enable_background_tasks=True),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root, background_tasks=manager),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )
            try:
                result = await conversation.run_background_wake()

                self.assertEqual(result.response, "wake response")
                self.assertNotIn(
                    AgentEventKind.USER_MESSAGE, [event.kind for event in result.events]
                )
                self.assertIn(
                    AgentEventKind.BACKGROUND_TASK_AUTO_WAKE_STARTED,
                    [event.kind for event in result.events],
                )
                reminder = next(
                    message
                    for message in provider.contexts[0].messages
                    if "<background-task-completions>" in message.content
                )
                self.assertIn(task.task_id, reminder.content)
                self.assertIn("wake completion output", reminder.content)
                self.assertEqual(await manager.pending_completions(), ())
                self.assertNotIn(
                    "wake response",
                    "\n".join(
                        item.content for item in conversation.items if isinstance(item, Message)
                    ),
                )
                persisted = await store.load_messages(conversation.session_id or "")
                self.assertNotIn(
                    "wake response", "\n".join(message.content for message in persisted)
                )

                await conversation.run("next prompt")
                self.assertIn(
                    "next prompt",
                    [
                        message.content
                        for message in provider.contexts[-1].messages
                        if message.role is Role.USER
                    ],
                )
            finally:
                await manager.shutdown()

    async def test_background_wake_requires_a_pending_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            runtime = AgentRuntime(
                provider=ConversationProvider(("unused",)),
                tools=default_tool_registry(enable_background_tasks=True),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root, background_tasks=manager),
            )
            conversation = AgentConversation(
                runtime=runtime, store=SqliteSessionStore(root / "sessions.db")
            )
            with self.assertRaisesRegex(ConfigurationError, "no pending background task"):
                await conversation.run_background_wake()
            await manager.shutdown()

    async def test_multiple_turns_reuse_session_and_provider_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            provider = ConversationProvider(("first response", "second response"))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )

            first = await conversation.run("first prompt")
            second = await conversation.run("second prompt")

            self.assertIsNotNone(first.session_id)
            self.assertEqual(second.session_id, first.session_id)
            self.assertEqual(conversation.session_id, first.session_id)
            self.assertEqual(len(await store.list_sessions()), 1)
            self.assertEqual(provider.contexts[1].source_provider, provider.provider_name)
            self.assertEqual(provider.contexts[1].source_model, provider.model_name)
            visible = [
                (message.role, message.content)
                for message in provider.contexts[1].messages
                if isinstance(message, Message) and message.role is not Role.SYSTEM
            ]
            self.assertEqual(
                visible,
                [
                    (Role.USER, "first prompt"),
                    (Role.ASSISTANT, "first response"),
                    (Role.USER, "second prompt"),
                ],
            )

    async def test_failed_turn_keeps_the_session_available_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            provider = FailOnceConversationProvider()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )

            with self.assertRaisesRegex(ProviderError, "fixture provider failure"):
                await conversation.run("failed prompt")
            failed_session_id = conversation.session_id
            recovered = await conversation.run("retry prompt")

            self.assertIsNotNone(failed_session_id)
            self.assertEqual(recovered.session_id, failed_session_id)
            self.assertEqual(len(await store.list_sessions()), 1)
            visible = [
                message.content
                for message in provider.contexts[1].messages
                if message.role is not Role.SYSTEM
            ]
            self.assertEqual(visible, ["failed prompt", "retry prompt"])

    async def test_cancelled_turn_reloads_durable_context_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            provider = CancelOnceConversationProvider()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )

            turn = asyncio.create_task(conversation.run("cancelled prompt"))
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            turn.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn
            cancelled_session_id = conversation.session_id
            recovered = await conversation.run("retry prompt")

            self.assertIsNotNone(cancelled_session_id)
            self.assertEqual(recovered.session_id, cancelled_session_id)
            self.assertEqual(len(await store.list_sessions()), 1)
            visible = [
                message.content
                for message in provider.contexts[1].messages
                if message.role is not Role.SYSTEM
            ]
            self.assertEqual(visible, ["cancelled prompt", "retry prompt"])
            assert cancelled_session_id is not None
            events = await store.load_events(cancelled_session_id)
            cancelled_failure = next(
                event
                for event in events
                if event["kind"] == "turn_failed" and event["data"].get("cancelled")
            )
            self.assertEqual(cancelled_failure["data"]["message"], "turn cancelled")

    async def test_pristine_cancel_rewinds_user_message_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            provider = CancelOnceConversationProvider()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )

            turn = asyncio.create_task(
                conversation.run(
                    "pristine prompt",
                    cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
                )
            )
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            turn.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn

            session_id = conversation.session_id
            self.assertIsNotNone(session_id)
            assert session_id is not None
            visible_after_cancel = [
                message.content
                for message in await store.load_messages(session_id)
                if message.role is not Role.SYSTEM
            ]
            self.assertEqual(visible_after_cancel, [])
            events = await store.load_events(session_id)
            cancelled_failure = next(
                event
                for event in events
                if event["kind"] == "turn_failed" and event["data"].get("cancelled")
            )
            self.assertTrue(cancelled_failure["data"]["pristine_rewound"])

            recovered = await conversation.run("retry prompt")
            self.assertEqual(
                [
                    message.content
                    for message in provider.contexts[1].messages
                    if message.role is not Role.SYSTEM
                ],
                ["retry prompt"],
            )
            self.assertEqual(recovered.response, "recovered response")

    async def test_pristine_cancel_retains_prompt_after_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            provider = CancelAfterTextConversationProvider()
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=FakeWorkspaceIdentity(),
            )

            turn = asyncio.create_task(
                conversation.run(
                    "output already started",
                    cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
                )
            )
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            turn.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn

            session_id = conversation.session_id
            self.assertIsNotNone(session_id)
            assert session_id is not None
            visible = [
                message.content
                for message in await store.load_messages(session_id)
                if message.role is not Role.SYSTEM
            ]
            self.assertEqual(visible, ["output already started"])
            events = await store.load_events(session_id)
            cancelled_failure = next(
                event
                for event in events
                if event["kind"] == "turn_failed" and event["data"].get("cancelled")
            )
            self.assertFalse(cancelled_failure["data"]["pristine_rewound"])

    async def test_resume_rejects_a_different_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as other_directory,
        ):
            root = Path(directory)
            other = Path(other_directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(other),
                "conversation-fixture",
                "fixture-model",
            )
            provider = ConversationProvider(("unused",))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            workspace_identity = FakeWorkspaceIdentity(matches_result=False)
            with (
                patch.object(
                    store,
                    "load_session_items",
                    side_effect=AssertionError("workspace mismatch must not load session items"),
                ),
                self.assertRaisesRegex(ConfigurationError, "session workspace"),
            ):
                await AgentConversation.open(
                    runtime=runtime,
                    store=store,
                    cwd=root,
                    workspace_identity=workspace_identity,
                    resume_id=session_id,
                )
            self.assertEqual(workspace_identity.calls, [(str(other), root)])

    async def test_resume_rejects_a_different_saved_sandbox_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
                sandbox_profile=SandboxProfile.STRICT,
            )
            runtime = AgentRuntime(
                provider=ConversationProvider(("unused",)),
                tools=default_tool_registry(SandboxProfile.WORKSPACE),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(
                    root,
                    sandbox_profile=SandboxProfile.WORKSPACE,
                ),
                session_store=store,
            )

            workspace_identity = FakeWorkspaceIdentity()
            with self.assertRaisesRegex(ConfigurationError, "not the active profile 'workspace'"):
                await AgentConversation.open(
                    runtime=runtime,
                    store=store,
                    cwd=root,
                    workspace_identity=workspace_identity,
                    resume_id=session_id,
                )
            self.assertEqual(workspace_identity.calls, [(str(root), root)])

    async def test_resume_accepts_an_injected_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            runtime = AgentRuntime(
                provider=ConversationProvider(("unused",)),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            workspace_identity = FakeWorkspaceIdentity()

            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
                workspace_identity=workspace_identity,
                resume_id=session_id,
            )

            self.assertEqual(conversation.session_id, session_id)
            self.assertEqual(workspace_identity.calls, [(str(root), root)])

    async def test_workspace_identity_exceptions_propagate_from_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(
                str(root),
                "conversation-fixture",
                "fixture-model",
            )
            runtime = AgentRuntime(
                provider=ConversationProvider(("unused",)),
                tools=default_tool_registry(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            workspace_identity = FakeWorkspaceIdentity(error=RuntimeError("identity failure"))

            with self.assertRaisesRegex(RuntimeError, "identity failure"):
                await AgentConversation.open(
                    runtime=runtime,
                    store=store,
                    cwd=root,
                    workspace_identity=workspace_identity,
                    resume_id=session_id,
                )
            self.assertEqual(workspace_identity.calls, [(str(root), root)])


if __name__ == "__main__":
    unittest.main()
