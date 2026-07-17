from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from neuro_code.adapters.sqlite_session import SqliteSessionStore
from neuro_code.domain.messages import Message, Role
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelCompleted, ModelEvent, ModelTextDelta
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ConfigurationError, ProviderError
from neuro_code.permissions import PermissionManager
from neuro_code.ports.tools import ToolContext
from neuro_code.runtime import AgentConversation, AgentRuntime
from neuro_code.tools import default_tool_registry


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
    async def test_multiple_turns_reuse_session_and_provider_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / ".state" / "sessions.db")
            await store.initialize()
            provider = ConversationProvider(("first response", "second response"))
            runtime = AgentRuntime(
                provider=provider,
                tools=default_tool_registry(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
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
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
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
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )
            conversation = await AgentConversation.open(
                runtime=runtime,
                store=store,
                cwd=root,
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
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
            )

            with self.assertRaisesRegex(ConfigurationError, "session workspace"):
                await AgentConversation.open(
                    runtime=runtime,
                    store=store,
                    cwd=root,
                    resume_id=session_id,
                )


if __name__ == "__main__":
    unittest.main()
