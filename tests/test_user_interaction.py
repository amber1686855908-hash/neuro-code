from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from neuro_code.application.permissions.policy import PermissionManager
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.user_interaction import (
    InteractionUnavailable,
    UserInputOption,
    UserInputRequest,
    UserInputResponse,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.finalization import Finalizer
from neuro_code.domain.conversation.events import (
    AgentEventKind,
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.tools.interaction import AskUserTool
from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry
from neuro_code.interfaces.tui.app import TuiUserInteraction
from neuro_code.shared.errors import ToolError
from tests.fakes import EmptyWorkspaceChangeObserver


class QueueInteraction:
    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.last_request: UserInputRequest | None = None
        self._answer: asyncio.Future[UserInputResponse] | None = None

    async def request(self, request: UserInputRequest) -> UserInputResponse:
        self.last_request = request
        self.requested.set()
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        return await self._answer

    def resolve(self, response: UserInputResponse) -> None:
        if self._answer is None:
            raise AssertionError("interaction request has not started")
        self._answer.set_result(response)


class ScriptedProvider:
    provider_name = "interaction-fixture"
    model_name = "interaction-model"
    context_affinity = "profile-v1:interaction"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent]]) -> None:
        self._scripts = list(scripts)
        self.calls = 0

    async def stream(
        self,
        context: object,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        self.calls += 1
        for event in self._scripts.pop(0):
            yield event


class MarkerTool:
    definition = ToolDefinition(
        name="marker",
        description="A test-only side-effect marker.",
        input_schema={"type": "object", "additionalProperties": False},
    )
    side_effecting = True

    def __init__(self) -> None:
        self.executed = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del arguments, context
        self.executed = True
        return ToolResult("marker executed")


class UserInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_user_emits_typed_events_and_returns_one_tool_result(self) -> None:
        interaction = QueueInteraction()
        emitted: list[tuple[AgentEventKind, dict[str, object]]] = []

        async def sink(kind: AgentEventKind, data: dict[str, object]) -> object:
            emitted.append((kind, data))
            return object()

        async def run_tool() -> ToolResult:
            return await AskUserTool().execute(
                {
                    "question": "Which compatibility policy should I use?",
                    "options": [
                        {"label": "Keep compatibility", "description": "Preserve old imports."},
                        {"label": "Break API"},
                    ],
                    "allow_free_text": False,
                },
                ToolContext(
                    Path("/workspace"),
                    user_interaction=interaction,
                    interaction_event_sink=sink,
                ),
            )

        task = asyncio.create_task(run_tool())
        await interaction.requested.wait()
        assert interaction.last_request is not None
        self.assertEqual(len(interaction.last_request.options), 2)
        interaction.resolve(
            UserInputResponse(interaction.last_request.request_id, selected_option="1")
        )
        result = await task

        self.assertEqual(result.content, "User selected: 1")
        self.assertEqual(
            [kind for kind, _ in emitted],
            [AgentEventKind.USER_INPUT_REQUESTED, AgentEventKind.USER_INPUT_RESOLVED],
        )

    async def test_ask_user_rejects_invalid_response_and_unavailable_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            UserInputResponse("request", selected_option="1", text="both")

        with self.assertRaises(InteractionUnavailable):
            await AskUserTool().execute(
                {"question": "Need a decision"},
                ToolContext(Path("/workspace")),
            )

        interaction = QueueInteraction()
        task = asyncio.create_task(
            AskUserTool().execute(
                {
                    "question": "Choose",
                    "options": [{"label": "Only option"}],
                    "allow_free_text": False,
                },
                ToolContext(Path("/workspace"), user_interaction=interaction),
            )
        )
        await interaction.requested.wait()
        assert interaction.last_request is not None
        interaction.resolve(
            UserInputResponse(interaction.last_request.request_id, text="not allowed")
        )
        with self.assertRaises(InteractionUnavailable):
            await task

        with self.assertRaises(ToolError):
            await AskUserTool().execute(
                {"question": "x" * 4_001},
                ToolContext(Path("/workspace"), user_interaction=QueueInteraction()),
            )

    async def test_same_process_ask_user_keeps_loop_and_budget(self) -> None:
        interaction = QueueInteraction()
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(
                        ToolCall(
                            "ask-1",
                            "ask_user",
                            {"question": "Continue?", "allow_free_text": True},
                        )
                    ),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("continued"), ModelCompleted("stop")),
            )
        )
        finalizer_called = False

        def fail_finalizer(
            provider: object,
            max_attempts: int,
            redaction_values: tuple[str, ...],
        ) -> Finalizer:
            del provider, max_attempts, redaction_values
            nonlocal finalizer_called
            finalizer_called = True
            raise AssertionError("finalizer must not run for a normal interaction turn")

        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=ToolRegistry((AskUserTool(),)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory), user_interaction=interaction),
                max_steps=4,
                finalizer_factory=fail_finalizer,
            )
            run_task = asyncio.create_task(runtime.run("Need clarification"))
            await interaction.requested.wait()
            assert interaction.last_request is not None
            interaction.resolve(
                UserInputResponse(interaction.last_request.request_id, text="yes, continue")
            )
            result = await run_task

        self.assertEqual(provider.calls, 2)
        self.assertEqual(result.response, "continued")
        self.assertFalse(finalizer_called)
        kinds = [event.kind for event in result.events]
        self.assertIn(AgentEventKind.USER_INPUT_REQUESTED, kinds)
        self.assertIn(AgentEventKind.USER_INPUT_RESOLVED, kinds)
        self.assertEqual(
            kinds.index(AgentEventKind.USER_INPUT_REQUESTED),
            kinds.index(AgentEventKind.TOOL_STARTED) + 1,
        )

    async def test_mixed_interaction_batch_is_rejected_before_side_effect(self) -> None:
        marker = MarkerTool()
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("ask-1", "ask_user", {"question": "Continue?"})),
                    ModelToolCall(ToolCall("marker-1", "marker", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("recovered"), ModelCompleted("stop")),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=ToolRegistry((AskUserTool(), marker)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                max_steps=4,
            )
            result = await runtime.run("Need a safe interaction")

        self.assertFalse(marker.executed)
        self.assertEqual(result.response, "recovered")
        failed = [event for event in result.events if event.kind is AgentEventKind.TOOL_FAILED]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all(event.data.get("control_batch_rejected") for event in failed))

    async def test_cancellation_while_waiting_does_not_finalize_or_resolve(self) -> None:
        interaction = QueueInteraction()
        provider = ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("ask-1", "ask_user", {"question": "Continue?"})),
                    ModelCompleted("tool_calls"),
                ),
            )
        )
        finalizer_called = False

        def fail_finalizer(
            provider: object,
            max_attempts: int,
            redaction_values: tuple[str, ...],
        ) -> Finalizer:
            del provider, max_attempts, redaction_values
            nonlocal finalizer_called
            finalizer_called = True
            raise AssertionError("finalizer must not run after cancellation")

        events: list[object] = []
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=ToolRegistry((AskUserTool(),)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory), user_interaction=interaction),
                max_steps=4,
                finalizer_factory=fail_finalizer,
            )
            task = asyncio.create_task(runtime.run("Need clarification", sink=events.append))
            await interaction.requested.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertFalse(finalizer_called)
        self.assertTrue(
            any(getattr(event, "kind", None) is AgentEventKind.TOOL_FAILED for event in events)
        )
        self.assertFalse(
            any(
                getattr(event, "kind", None) is AgentEventKind.USER_INPUT_RESOLVED
                for event in events
            )
        )

    async def test_tui_adapter_resolves_option_and_free_text(self) -> None:
        interaction = TuiUserInteraction()
        request = UserInputRequest(
            "request-1",
            "Choose",
            (UserInputOption("1", "Keep"), UserInputOption("2", "Break")),
            False,
        )
        task = asyncio.create_task(interaction.request(request))
        await asyncio.sleep(0)
        self.assertTrue(interaction.resolve(request.request_id, "2"))
        response = await task
        self.assertEqual(response.selected_option, "2")

        text_request = UserInputRequest("request-2", "Explain", allow_free_text=True)
        text_task = asyncio.create_task(interaction.request(text_request))
        await asyncio.sleep(0)
        interaction.resolve(text_request.request_id, "because it is safer")
        text_response = await text_task
        self.assertEqual(text_response.text, "because it is safer")

    async def test_cli_noninteractive_adapter_fails_closed(self) -> None:
        from neuro_code.interfaces.cli.app import CliUserInteraction

        with self.assertRaises(InteractionUnavailable):
            await CliUserInteraction(interactive=False).request(
                UserInputRequest("request", "Need input")
            )

    def test_ask_user_is_exposed_only_when_an_interaction_port_is_bound(self) -> None:
        self.assertNotIn("ask_user", default_tool_registry().names())
        self.assertIn(
            "ask_user",
            default_tool_registry(user_interaction=QueueInteraction()).names(),
        )


if __name__ == "__main__":
    unittest.main()
