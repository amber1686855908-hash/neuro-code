from __future__ import annotations

import asyncio
import inspect
import unittest
from dataclasses import replace
from pathlib import Path

from textual.widgets import Button, Input

from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.permissions import (
    PermissionApproval,
    PermissionApprovalKind,
    build_permission_request,
)
from neuro_code.runtime import SessionApprovalBroker
from neuro_code.runtime.agent import AgentRunResult, EventSink
from neuro_code.runtime.profile_conversation import ProviderOption, ProviderSelectionResult
from neuro_code.tui import (
    NeuroCodeApp,
    PermissionApprovalScreen,
    ProviderSelectionScreen,
)


class TuiConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.prompts: list[str] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        self.prompts.append(prompt)
        self._session_id = "session-fixture"
        events = (
            AgentEvent.create(
                1,
                AgentEventKind.SESSION_STARTED,
                {"session_id": self._session_id},
            ),
            AgentEvent.create(2, AgentEventKind.REASONING_DELTA, {"text": "private"}),
            AgentEvent.create(
                3,
                AgentEventKind.TOOL_REQUESTED,
                {"id": "read", "name": "read_file", "arguments": {"path": "README.md"}},
            ),
            AgentEvent.create(
                4,
                AgentEventKind.TOOL_PERMISSION,
                {
                    "id": "read",
                    "name": "read_file",
                    "effect": "ask",
                    "reason": "fixture approval",
                },
            ),
            AgentEvent.create(
                5,
                AgentEventKind.TOOL_APPROVAL_REQUESTED,
                {
                    "id": "read",
                    "name": "read_file",
                    "summary": "private approval summary",
                    "reason": "fixture approval",
                },
            ),
            AgentEvent.create(
                6,
                AgentEventKind.TOOL_APPROVAL_RESOLVED,
                {
                    "id": "read",
                    "name": "read_file",
                    "effect": "allow",
                    "outcome": "allow_once",
                    "reason": "approved once",
                },
            ),
            AgentEvent.create(
                7,
                AgentEventKind.TOOL_COMPLETED,
                {"id": "read", "name": "read_file", "content": "not rendered"},
            ),
            AgentEvent.create(8, AgentEventKind.TEXT_DELTA, {"text": "fixture "}),
            AgentEvent.create(9, AgentEventKind.TEXT_DELTA, {"text": "response"}),
            AgentEvent.create(10, AgentEventKind.TURN_COMPLETED, {"step": 1}),
        )
        if sink is not None:
            for event in events:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
        return AgentRunResult(
            self._session_id,
            "fixture response",
            (),
            (),
            events,
            1,
        )


class ApprovalTuiConversation:
    def __init__(self, broker: SessionApprovalBroker) -> None:
        self._broker = broker
        self._session_id: str | None = None
        self.approvals: list[PermissionApproval] = []
        self.executed = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del prompt, sink
        request = build_permission_request(
            "edit",
            "search_replace",
            {"path": "note.txt", "old": "private-old", "new": "private-new"},
            "interactive approval required",
        )
        approval = await self._broker.request(request)
        self.approvals.append(approval)
        self.executed = approval.allowed
        self._session_id = "approval-session"
        response = "approved" if approval.allowed else "denied"
        return AgentRunResult(self._session_id, response, (), (), (), 1)


class CancellableTuiConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.prompts: list[str] = []
        self.started = asyncio.Event()
        self.cancelled = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del sink
        self.prompts.append(prompt)
        self._session_id = "cancel-session"
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class ProfileTuiController:
    def __init__(self) -> None:
        self._selected_profile = "first"
        self.selections: list[str] = []
        self._options = (
            ProviderOption(
                "first",
                "openai-chat",
                "first-model",
                True,
                True,
                default=True,
            ),
            ProviderOption("second", "anthropic-messages", "second-model", True, True),
            ProviderOption("missing", "openai-chat", "missing-model", True, False),
        )

    @property
    def profiles(self) -> tuple[ProviderOption, ...]:
        return tuple(
            replace(option, selected=option.name == self._selected_profile)
            for option in self._options
        )

    @property
    def selected_profile(self) -> str:
        return self._selected_profile

    async def select_profile(self, name: str) -> ProviderSelectionResult:
        self.selections.append(name)
        changed = name != self._selected_profile
        self._selected_profile = name
        model = next(option.model for option in self._options if option.name == name)
        return ProviderSelectionResult(
            name,
            name,
            model,
            "old-session" if changed else None,
            changed,
        )


class NeuroCodeAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_streams_events_and_commits_response(self) -> None:
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "inspect the repository"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break

            self.assertFalse(prompt.disabled)
            self.assertEqual(runner.prompts, ["inspect the repository"])
            entries = [(entry.category, entry.text) for entry in app.entries]
            self.assertIn(("user", "inspect the repository"), entries)
            self.assertIn(("status", "Reasoning…"), entries)
            self.assertIn(("tool", "Tool read_file requested."), entries)
            self.assertIn(("status", "Tool read_file is waiting for approval."), entries)
            self.assertIn(
                ("status", "Tool read_file approval resolved: allow_once."),
                entries,
            )
            self.assertIn(("tool", "Tool read_file completed."), entries)
            self.assertIn(("assistant", "fixture response"), entries)
            self.assertNotIn("private", "\n".join(text for _, text in entries))
            self.assertNotIn("not rendered", "\n".join(text for _, text in entries))

    async def test_local_slash_commands_do_not_call_the_model(self) -> None:
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/help"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("/status", app.entries[-1].text)
            self.assertIn("/cancel", app.entries[-1].text)

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("fixture/fixture-model", app.entries[-1].text)

            prompt.value = "/clear"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual([entry.text for entry in app.entries], ["Transcript cleared."])
            self.assertEqual(runner.prompts, [])

    async def test_permission_modal_blocks_until_allow_once_is_selected(self) -> None:
        broker = SessionApprovalBroker()
        runner = ApprovalTuiConversation(broker)
        app = NeuroCodeApp(
            runner,
            approval_controller=broker,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "edit the file"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, PermissionApprovalScreen):
                    break

            self.assertIsInstance(app.screen, PermissionApprovalScreen)
            self.assertFalse(runner.executed)
            approval_screen = app.screen
            assert isinstance(approval_screen, PermissionApprovalScreen)
            self.assertNotIn("private-old", approval_screen.request.summary)
            self.assertNotIn("private-new", approval_screen.request.summary)

            clicked = await pilot.click("#approval-allow-once")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.approvals:
                    break

            self.assertTrue(runner.executed)
            self.assertEqual(runner.approvals[0].kind, PermissionApprovalKind.ALLOW_ONCE)

    async def test_permission_modal_defaults_to_deny(self) -> None:
        broker = SessionApprovalBroker()
        runner = ApprovalTuiConversation(broker)
        app = NeuroCodeApp(
            runner,
            approval_controller=broker,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "edit the file"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, PermissionApprovalScreen):
                    break

            self.assertEqual(app.focused.id if app.focused is not None else None, "approval-deny")
            await pilot.press("ctrl+c")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.approvals:
                    break

            self.assertFalse(runner.executed)
            self.assertEqual(runner.approvals[0].kind, PermissionApprovalKind.DENY)

    async def test_ctrl_c_cancels_a_running_turn_and_keeps_input_available(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            self.assertFalse(prompt.disabled)

            await pilot.press("ctrl+c")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.cancelled and any(
                    entry.text == "Turn cancelled." for entry in app.entries
                ):
                    break

            self.assertTrue(runner.cancelled)
            self.assertIn("Cancellation requested.", [entry.text for entry in app.entries])
            self.assertIn("Turn cancelled.", [entry.text for entry in app.entries])
            self.assertEqual(runner.prompts, ["long turn"])

    async def test_cancel_command_cancels_without_starting_another_turn(self) -> None:
        runner = CancellableTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)

            prompt.value = "/cancel"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if runner.cancelled:
                    break

            self.assertTrue(runner.cancelled)
            self.assertEqual(runner.prompts, ["long turn"])

    async def test_provider_picker_switches_profile_without_rendering_credentials(self) -> None:
        runner = TuiConversation()
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/provider"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ProviderSelectionScreen):
                    break

            self.assertIsInstance(app.screen, ProviderSelectionScreen)
            missing = app.screen.query_one("#provider-choice-2", Button)
            self.assertTrue(missing.disabled)
            rendered_labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            self.assertIn("credential missing", rendered_labels)
            clicked = await pilot.click("#provider-choice-1")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if profiles.selections:
                    break

            self.assertEqual(profiles.selections, ["second"])
            self.assertIn("previous session old-session remains saved", app.entries[-1].text)

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("second/second-model", app.entries[-1].text)
            self.assertIn("Profile: second", app.entries[-1].text)

            await pilot.press("ctrl+p")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ProviderSelectionScreen):
                    break
            self.assertIsInstance(app.screen, ProviderSelectionScreen)
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ProviderSelectionScreen)
            self.assertEqual(profiles.selections, ["second"])

    async def test_model_alias_selects_directly_and_switch_is_blocked_mid_turn(self) -> None:
        profiles = ProfileTuiController()
        direct_runner = TuiConversation()
        direct_app = NeuroCodeApp(
            direct_runner,
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with direct_app.run_test(size=(100, 30)) as pilot:
            prompt = direct_app.query_one("#prompt", Input)
            prompt.value = "/model second"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(profiles.selections, ["second"])

        blocking_runner = CancellableTuiConversation()
        blocking_profiles = ProfileTuiController()
        blocking_app = NeuroCodeApp(
            blocking_runner,
            provider_controller=blocking_profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with blocking_app.run_test(size=(100, 30)) as pilot:
            prompt = blocking_app.query_one("#prompt", Input)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(blocking_runner.started.wait(), timeout=1)
            prompt.value = "/provider second"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(blocking_profiles.selections, [])
            self.assertEqual(
                blocking_app.entries[-1].text,
                "Cannot switch provider while a turn is running.",
            )
            await pilot.press("ctrl+c")


if __name__ == "__main__":
    unittest.main()
