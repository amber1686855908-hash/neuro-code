from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.conversation import (
    PLAN_EXECUTION_PROMPT,
    AgentConversation,
)
from neuro_code.domain.events import AgentEventKind
from neuro_code.domain.messages import Message, Role
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelCompleted, ModelEvent, ModelTextDelta
from neuro_code.domain.plans import PlanComment, PlanStep, PlanStepStatus, SessionPlan
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_tasks import SessionTaskStatus
from neuro_code.domain.tools import ToolDefinition
from neuro_code.permissions import PermissionManager
from neuro_code.shared.errors import ConfigurationError, ProviderError
from neuro_code.tools import default_tool_registry
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
    ) -> AsyncIterator[ModelEvent]:
        del tools
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
    ) -> AsyncIterator[ModelEvent]:
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
    ) -> AsyncIterator[ModelEvent]:
        if not self._blocked:
            self._blocked = True
            self.contexts.append(context)
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        async for event in super().stream(context, tools):
            yield event


class AgentConversationTests(unittest.IsolatedAsyncioTestCase):
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
