from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.widgets import Button, Input, Label, Static

from neuro_code.adapters.provider_settings import JsonProviderSettingsStore
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    ToolCall,
)
from neuro_code.domain.provider_settings import ManagedProviderProfile, ManagedProviderSettings
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.ui_preferences import UiLanguage
from neuro_code.permissions import (
    PermissionApproval,
    PermissionApprovalKind,
    build_permission_request,
)
from neuro_code.runtime import SessionApprovalBroker
from neuro_code.runtime.agent import AgentRunResult, EventSink
from neuro_code.runtime.profile_conversation import (
    InteractionModeSelectionResult,
    ProviderOption,
    ProviderSelectionResult,
    ReasoningEffortSelectionResult,
    SessionOption,
    SessionSelectionResult,
)
from neuro_code.tui import (
    TUI_RELOAD_PROVIDER_SETTINGS,
    AssistantMarkdown,
    ConversationMessage,
    LanguageSettingsScreen,
    NeuroCodeApp,
    PermissionApprovalScreen,
    ProviderSelectionScreen,
    ProviderSettingsScreen,
    ProviderSetupApp,
    ReasoningEffortScreen,
    SessionSelectionScreen,
    SettingsScreen,
    ToolFeedbackMessage,
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
                AgentEventKind.MODEL_THINKING_COMPLETED,
                {"step": 1, "duration_seconds": 1.25},
            ),
            AgentEvent.create(
                4,
                AgentEventKind.TOOL_REQUESTED,
                {"id": "read", "name": "read_file", "arguments": {"path": "README.md"}},
            ),
            AgentEvent.create(
                5,
                AgentEventKind.TOOL_PERMISSION,
                {
                    "id": "read",
                    "name": "read_file",
                    "effect": "ask",
                    "reason": "fixture approval",
                },
            ),
            AgentEvent.create(
                6,
                AgentEventKind.TOOL_APPROVAL_REQUESTED,
                {
                    "id": "read",
                    "name": "read_file",
                    "summary": "private approval summary",
                    "reason": "fixture approval",
                },
            ),
            AgentEvent.create(
                7,
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
                8,
                AgentEventKind.TOOL_COMPLETED,
                {
                    "id": "read",
                    "name": "read_file",
                    "content": "1\tNeuro Code project\n2\tPython agent",
                    "metadata": {"path": "/workspace/README.md", "total_lines": 2},
                    "duration_seconds": 0.42,
                },
            ),
            AgentEvent.create(9, AgentEventKind.TEXT_DELTA, {"text": "fixture "}),
            AgentEvent.create(10, AgentEventKind.TEXT_DELTA, {"text": "response"}),
            AgentEvent.create(
                11,
                AgentEventKind.TURN_COMPLETED,
                {"step": 1, "duration_seconds": 2.75},
            ),
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


class StreamingTuiConversation:
    def __init__(self) -> None:
        self._session_id: str | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del prompt
        self._session_id = "streaming-session"
        first = AgentEvent.create(1, AgentEventKind.TEXT_DELTA, {"text": "partial"})
        if sink is not None:
            outcome = sink(first)
            if inspect.isawaitable(outcome):
                await outcome
        self.started.set()
        await self.release.wait()
        second = AgentEvent.create(2, AgentEventKind.TEXT_DELTA, {"text": " response"})
        if sink is not None:
            outcome = sink(second)
            if inspect.isawaitable(outcome):
                await outcome
        return AgentRunResult(
            self._session_id,
            "partial response",
            (),
            (),
            (first, second),
            1,
        )


class UiPreferencesFixture:
    def __init__(self) -> None:
        self.saved: list[UiLanguage] = []
        self.saved_efforts: list[ReasoningEffort] = []
        self.saved_modes: list[InteractionMode] = []

    async def load_language(self) -> UiLanguage:
        return UiLanguage.ENGLISH

    async def save_language(self, language: UiLanguage) -> None:
        self.saved.append(language)

    async def load_reasoning_effort(self) -> ReasoningEffort:
        return ReasoningEffort.HIGH

    async def save_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self.saved_efforts.append(effort)

    async def load_interaction_mode(self) -> InteractionMode:
        return InteractionMode.NORMAL

    async def save_interaction_mode(self, mode: InteractionMode) -> None:
        self.saved_modes.append(mode)


class ApprovalControllerFixture:
    def __init__(self) -> None:
        self.handlers: list[object | None] = []

    def set_handler(self, handler: object | None) -> None:
        self.handlers.append(handler)


class ProfileTuiController:
    def __init__(self) -> None:
        self._selected_profile = "first"
        self.selections: list[str] = []
        self.effort_selections: list[ReasoningEffort] = []
        self.mode_selections: list[InteractionMode] = []
        self._reasoning_effort = ReasoningEffort.HIGH
        self._interaction_mode = InteractionMode.NORMAL
        self._options = (
            ProviderOption(
                "first",
                "openai-chat",
                "first-model",
                True,
                True,
                default=True,
                context_window_tokens=1_000_000,
            ),
            ProviderOption(
                "second",
                "anthropic-messages",
                "second-model",
                True,
                True,
                context_window_tokens=200_000,
            ),
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

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort

    @property
    def effective_reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort.effective

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return False

    async def set_reasoning_effort(
        self,
        effort: ReasoningEffort,
    ) -> ReasoningEffortSelectionResult:
        changed = effort is not self._reasoning_effort
        self.effort_selections.append(effort)
        self._reasoning_effort = effort
        return ReasoningEffortSelectionResult(
            requested=effort,
            effective=effort.effective,
            changed=changed,
        )

    async def set_interaction_mode(
        self,
        mode: InteractionMode,
    ) -> InteractionModeSelectionResult:
        changed = mode is not self._interaction_mode
        self.mode_selections.append(mode)
        self._interaction_mode = mode
        return InteractionModeSelectionResult(
            requested=mode,
            changed=changed,
            auto_unrestricted=False,
        )

    async def select_profile(self, name: str) -> ProviderSelectionResult:
        self.selections.append(name)
        changed = name != self._selected_profile
        self._selected_profile = name
        model = next(option.model for option in self._options if option.name == name)
        context_window_tokens = next(
            option.context_window_tokens for option in self._options if option.name == name
        )
        return ProviderSelectionResult(
            name,
            name,
            model,
            "old-session" if changed else None,
            changed,
            context_window_tokens=context_window_tokens,
        )


def restored_history() -> tuple[SessionItem, ...]:
    return (
        Message(
            Role.USER,
            content_parts=(
                ContentPart.from_text("restored prompt"),
                ContentPart.from_image("data:image/png;base64,private-image"),
            ),
        ),
        PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "private-reasoning",
                "summary": [{"type": "summary_text", "text": "never render this"}],
            },
        ),
        Message(
            Role.ASSISTANT,
            tool_calls=(ToolCall("read-1", "read_file", {"path": "private.txt"}),),
            reasoning_content="private assistant reasoning",
        ),
        Message(
            Role.TOOL,
            "private tool output",
            name="read_file",
            tool_call_id="read-1",
        ),
        Message(Role.ASSISTANT, "restored response"),
    )


class SessionTuiController:
    def __init__(self, *, current_session: str = "current-session") -> None:
        self._session_id = current_session
        self.selected: list[str] = []
        self.renamed: list[str] = []
        self.queries: list[str | None] = []
        timestamp = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)
        self.options = (
            SessionOption(
                "current-session",
                "first",
                "first-model",
                timestamp,
                "first",
                current_session == "current-session",
                True,
                True,
                title="Current workspace session",
            ),
            SessionOption(
                "target-session-123456789",
                "second",
                "second-model",
                timestamp,
                "second",
                current_session == "target-session-123456789",
                True,
                True,
                title="Escaped quoted session",
                matched_fields=("title", "content"),
                snippet="[quoted] content from the restored conversation",
            ),
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del prompt, sink
        return AgentRunResult(self._session_id, "ok", (), (), (), 1)

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]:
        self.queries.append(query)
        if query is not None:
            return tuple(
                option
                for option in self.options
                if query.casefold() in f"{option.title or ''} {option.snippet or ''}".casefold()
            )
        return self.options

    async def select_session(self, session_id: str) -> SessionSelectionResult:
        self.selected.append(session_id)
        previous = self._session_id
        changed = session_id != previous
        self._session_id = session_id
        return SessionSelectionResult(
            session_id,
            "second",
            "second-model",
            "second",
            "second",
            "second-model",
            previous if changed else None,
            changed,
            True,
            restored_history(),
        )

    async def rename_session(self, title: str) -> SessionSummary:
        self.renamed.append(title)
        timestamp = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)
        return SessionSummary(
            self._session_id,
            "/workspace",
            "first",
            "first-model",
            timestamp,
            timestamp,
            title=title,
        )


class TaskTuiController:
    def __init__(self, snapshots: tuple[BackgroundTaskSnapshot, ...] = ()) -> None:
        self.snapshots = snapshots
        self.list_calls = 0

    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]:
        self.list_calls += 1
        return self.snapshots


def background_snapshot(
    task_id: str,
    status: BackgroundTaskStatus,
    *,
    exit_code: int | None = None,
) -> BackgroundTaskSnapshot:
    started_at = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)
    return BackgroundTaskSnapshot(
        task_id=task_id,
        command="curl -H 'secret: private-command' https://example.invalid",
        cwd="/workspace",
        status=status,
        output="private task output",
        total_output_bytes=19,
        truncated=False,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=started_at if status.terminal else None,
    )


class NeuroCodeAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_quit_skips_the_model_and_detaches_approval_handler(self) -> None:
        runner = TuiConversation()
        approvals = ApprovalControllerFixture()
        app = NeuroCodeApp(
            runner,
            approval_controller=approvals,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            self.assertTrue(approvals.handlers)
            self.assertIsNotNone(approvals.handlers[-1])
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/quit"
            await pilot.press("enter")

        self.assertEqual(runner.prompts, [])
        self.assertEqual(app.return_code, 0)
        self.assertIsNone(approvals.handlers[-1])

    async def test_neutral_theme_disables_the_builtin_emoji_command_palette(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            self.assertEqual(app.theme, "neuro-code-dark")
            self.assertEqual(app.screen.styles.background.hex, "#101214")
            self.assertFalse(app.ENABLE_COMMAND_PALETTE)
            self.assertEqual(app.query_one("HeaderIcon").styles.display, "none")
            entry_styles = {
                str(app._render_entry(category, "content").style)
                for category in ("assistant", "system", "tool", "user")
            }
            self.assertFalse(
                any(
                    color in style
                    for style in entry_styles
                    for color in ("cyan", "green", "magenta", "yellow")
                )
            )

    async def test_user_and_assistant_messages_use_distinct_unlabelled_blocks(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
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

            messages = list(app.query(ConversationMessage))
            user = next(message for message in messages if message.category == "user")
            assistant = next(message for message in messages if message.category == "assistant")
            user_text = str(user.renderable)
            assistant_text = str(assistant.renderable)
            self.assertTrue(user.has_class("message-user"))
            self.assertTrue(assistant.has_class("message-assistant"))
            self.assertTrue(user_text.startswith("inspect the repository"))
            self.assertIn("fixture response", assistant_text)
            self.assertNotIn("You:", user_text)
            self.assertNotIn("Assistant:", assistant_text)

    async def test_assistant_markdown_uses_semantic_styles_without_markup_injection(
        self,
    ) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)):
            rendered = app._render_entry(
                "assistant",
                "## Important\n\nUse **bold** and `code`.\n\n- [red]literal[/red]",
            )
            self.assertIsInstance(rendered, AssistantMarkdown)
            segments = list(app.console.render(rendered, app.console.options.update(width=80)))
            plain = "".join(segment.text for segment in segments)
            styled = {
                segment.text.strip(): str(segment.style)
                for segment in segments
                if segment.text.strip()
            }

            self.assertIn("Important", plain)
            self.assertIn("bold", plain)
            self.assertIn("code", plain)
            self.assertIn("[red]literal[/red]", plain)
            self.assertNotIn("**bold**", plain)
            self.assertIn("#9eafff", styled["Important"])
            self.assertIn("#aebcff", styled["bold"])
            self.assertIn("#9cc4cc", styled["code"])

    async def test_tool_notice_highlights_the_tool_name_in_an_aligned_gutter(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)):
            rendered = app._render_entry(
                "tool",
                "Tool read_file completed.",
                ui_key="tool.completed",
                ui_values=(("name", "read_file"),),
            )
            segments = list(app.console.render(rendered, app.console.options.update(width=60)))
            tool_segments = [segment for segment in segments if "read_file" in segment.text]

            self.assertTrue(tool_segments)
            self.assertIn("#8ed1e6", str(tool_segments[0].style))

    async def test_streaming_response_updates_one_stable_transcript_node(self) -> None:
        runner = StreamingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "stream the answer"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            await pilot.pause()

            transcript = app.query_one("#transcript", VerticalScroll)
            pending = app._pending_assistant
            assert pending is not None
            child_count = len(transcript.children)
            self.assertIs(transcript.children[-1], pending)
            self.assertEqual(prompt.value, "")
            self.assertIn("partial", str(pending.renderable))
            self.assertEqual(list(app.query("#stream")), [])

            runner.release.set()
            for _ in range(20):
                await pilot.pause(0.01)
                if any(entry.category == "assistant" for entry in app.entries):
                    break

            self.assertEqual(len(transcript.children), child_count)
            self.assertIs(app._entry_widgets[-1], pending)
            self.assertIs(pending.parent, transcript)
            self.assertIn("partial response", str(pending.renderable))

    async def test_waiting_model_uses_the_supplied_collapsing_pulse(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            app._begin_pending_assistant()
            pending = app._pending_assistant
            assert pending is not None
            first_frame = str(pending.renderable)
            self.assertIn("█", first_frame)
            self.assertIn("Thinking", first_frame)

            await pilot.pause(0.12)

            second_frame = str(pending.renderable)
            self.assertNotEqual(first_frame, second_frame)
            self.assertNotIn("█", str(app.query_one("#runtime-model", Static).renderable))
            await app._discard_pending_assistant()
            self.assertFalse(app._model_loading)

    async def test_settings_switches_and_persists_the_interface_language(self) -> None:
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            ui_preferences=preferences,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            app._write_entry("assistant", "literal model response")
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/setting"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SettingsScreen):
                    break

            self.assertIsInstance(app.screen, SettingsScreen)
            self.assertEqual(list(app.screen.query("#provider-settings-form")), [])
            self.assertEqual(list(app.screen.query("#settings-languages")), [])
            clicked = await pilot.click("#settings-category-language")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, LanguageSettingsScreen):
                    break

            self.assertIsInstance(app.screen, LanguageSettingsScreen)
            clicked = await pilot.click("#settings-language-zh-cn")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if preferences.saved:
                    break

            self.assertEqual(preferences.saved, [UiLanguage.SIMPLIFIED_CHINESE])
            self.assertEqual(app.sub_title, "终端编程智能体")
            self.assertIn("输入 /help", prompt.placeholder)
            self.assertIn("设置", str(app.query_one("#shortcut-bar", Static).renderable))
            self.assertTrue(app.entries[0].text.startswith("已就绪"))
            self.assertIn(
                "literal model response",
                [entry.text for entry in app.entries if entry.category == "assistant"],
            )
            self.assertIn("界面语言已切换", app.entries[-1].text)

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("供应商", app.entries[-1].text)
            self.assertIn("fixture/fixture-model", app.entries[-1].text)

    async def test_first_run_settings_save_a_provider_without_echoing_its_key(self) -> None:
        self.assertEqual(
            ProviderSettingsScreen._preset_for_profile(
                ManagedProviderProfile(
                    name="legacy-wrong-protocol",
                    protocol="openai-responses",
                    model="deepseek-v4-pro",
                    base_url="https://api.deepseek.com",
                )
            ),
            "deepseek",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            app = ProviderSetupApp(
                provider_settings=ManagedProviderSettings(),
                provider_settings_store=store,
            )

            async with app.run_test(size=(110, 40)) as pilot:
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                self.assertIsInstance(app.screen, ProviderSettingsScreen)
                clicked = await pilot.click("#provider-settings-preset-deepseek")
                self.assertTrue(clicked)
                self.assertEqual(
                    app.screen.query_one("#provider-settings-base-url", Input).value,
                    "https://api.deepseek.com",
                )
                self.assertIn(
                    "/chat/completions",
                    str(
                        app.screen.query_one(
                            "#provider-settings-protocol-hint",
                            Static,
                        ).renderable
                    ),
                )
                app.screen.query_one("#provider-settings-name", Input).value = "personal"
                app.screen.query_one("#provider-settings-model", Input).value = "deepseek-v4-pro"
                api_key = app.screen.query_one("#provider-settings-api-key", Input)
                api_key.value = "never-echo-this-key"
                self.assertTrue(api_key.password)

                clicked = await pilot.click("#provider-settings-save")
                self.assertTrue(clicked)
                for _ in range(20):
                    await pilot.pause(0.01)
                    if (await store.load()).profiles:
                        break

            saved = await store.load()
            self.assertEqual(saved.default_provider, "personal")
            self.assertEqual(saved.profiles[0].protocol, "openai-chat")
            self.assertEqual(saved.profiles[0].model, "deepseek-v4-pro")
            self.assertNotIn("never-echo-this-key", repr(saved))

    async def test_settings_edit_managed_provider_and_request_safe_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            settings = await store.save_profile(
                ManagedProviderProfile(
                    name="personal",
                    protocol="openai-responses",
                    model="old-model",
                    base_url="https://api.openai.com/v1",
                    api_key="saved-secret",
                )
            )
            app = NeuroCodeApp(
                TuiConversation(),
                provider_settings_store=store,
                managed_provider_settings=settings,
                provider_name="fixture",
                model_name="fixture-model",
                cwd=Path("/workspace"),
            )

            async with app.run_test(size=(110, 40)) as pilot:
                await app.action_open_settings()
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                await pilot.click("#settings-category-providers")
                for _ in range(20):
                    await pilot.pause(0.01)
                    if isinstance(app.screen, ProviderSettingsScreen):
                        break
                self.assertIsInstance(app.screen, ProviderSettingsScreen)
                await pilot.click("#provider-settings-profile-0")
                model = app.screen.query_one("#provider-settings-model", Input)
                model.value = "updated-model"
                self.assertEqual(
                    app.screen.query_one("#provider-settings-api-key", Input).value,
                    "",
                )
                await pilot.click("#provider-settings-save")
                for _ in range(20):
                    await pilot.pause(0.01)
                    if app.return_code is not None:
                        break

            updated = await store.load()
            self.assertEqual(updated.profiles[0].model, "updated-model")
            self.assertEqual(updated.profiles[0].api_key, "saved-secret")
            self.assertEqual(app.return_code, TUI_RELOAD_PROVIDER_SETTINGS)

    async def test_runtime_bar_shows_model_and_effort_and_localizes_labels(self) -> None:
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            TuiConversation(),
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
            context_window_tokens=1_000_000,
        )

        async with app.run_test(size=(80, 24)) as pilot:
            model = app.query_one("#runtime-model", Static)
            context = app.query_one("#runtime-context", Static)
            effort = app.query_one("#runtime-effort", Static)
            workspace = app.query_one("#runtime-workspace", Static)
            mode = app.query_one("#runtime-mode", Static)
            self.assertIn("MODEL", str(model.renderable))
            self.assertIn("first / first-model", str(model.renderable))
            self.assertIn("EFFORT", str(effort.renderable))
            self.assertIn("● high", str(effort.renderable))
            self.assertIn("CTX", str(context.renderable))
            self.assertIn("~0.0%", str(context.renderable))
            self.assertIn("CWD", str(workspace.renderable))
            self.assertIn(str(Path("/workspace")), str(workspace.renderable))
            self.assertIn("MODE", str(mode.renderable))
            self.assertIn("normal", str(mode.renderable))

            await app._language_settings_selected(UiLanguage.SIMPLIFIED_CHINESE)
            await pilot.pause()
            self.assertIn("模型", str(model.renderable))
            self.assertIn("上下文", str(context.renderable))
            self.assertIn("强度", str(effort.renderable))
            self.assertIn("工作区", str(workspace.renderable))
            self.assertIn("模式", str(mode.renderable))
            self.assertIn("first / first-model", str(model.renderable))

    async def test_shift_tab_cycles_modes_and_persists_safe_auto_preview(self) -> None:
        profiles = ProfileTuiController()
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            provider_controller=profiles,
            ui_preferences=preferences,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(profiles.mode_selections[-1], InteractionMode.ACCEPT_EDITS)
            self.assertEqual(preferences.saved_modes[-1], InteractionMode.ACCEPT_EDITS)
            self.assertIn(
                "accept-edits",
                str(app.query_one("#runtime-mode", Static).renderable),
            )

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/mode auto"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(profiles.mode_selections[-1], InteractionMode.AUTO)
            self.assertIn("safe preview", app.entries[-1].text)
            self.assertIn("auto", str(app.query_one("#runtime-mode", Static).renderable))

    async def test_context_bar_uses_provider_usage_and_status_reports_token_budget(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="deepseek",
            model_name="deepseek-v4-pro",
            cwd=Path("/workspace"),
            context_window_tokens=1_000_000,
        )

        async with app.run_test(size=(90, 24)) as pilot:
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.CONTEXT_USAGE_UPDATED,
                    {"used_tokens": 500_000, "estimated": False},
                )
            )
            await pilot.pause()

            context = app.query_one("#runtime-context", Static)
            self.assertIn("50.0%", str(context.renderable))
            self.assertNotIn("~", str(context.renderable))
            self.assertIn("500,000 / 1,000,000", str(context.tooltip))

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Context: 50.0% (500,000/1,000,000)", app.entries[-1].text)

    async def test_slash_commands_show_parameter_hints_and_tab_completes_first_option(
        self,
    ) -> None:
        runner = TuiConversation()
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(90, 24)) as pilot:
            prompt = app.query_one("#prompt", Input)
            hints = app.query_one("#command-hints", Static)
            prompt.value = "/eff"
            await pilot.pause()
            self.assertTrue(hints.display)
            self.assertIn("/effort LEVEL", str(hints.renderable))

            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/effort")
            self.assertIn("/effort low", str(hints.renderable))

            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/effort low")

            prompt.value = "/provider"
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/provider first")

            prompt.value = "/resume"
            await pilot.pause()
            self.assertIn("/resume SESSION_ID", str(hints.renderable))
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.value, "/resume ")

            prompt.value = "ordinary prompt"
            await pilot.pause()
            self.assertFalse(hints.display)
            self.assertEqual(runner.prompts, [])

            await pilot.press("ctrl+p")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ProviderSelectionScreen):
                    break
            self.assertIsInstance(app.screen, ProviderSelectionScreen)
            focused_before = app.focused.id if app.focused is not None else None
            await pilot.press("tab")
            await pilot.pause()
            self.assertNotEqual(
                app.focused.id if app.focused is not None else None,
                focused_before,
            )
            await pilot.press("ctrl+c")

    async def test_effort_picker_switches_all_levels_and_marks_ultracode_fallback(
        self,
    ) -> None:
        profiles = ProfileTuiController()
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            provider_controller=profiles,
            ui_preferences=preferences,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+e")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, ReasoningEffortScreen):
                    break

            self.assertIsInstance(app.screen, ReasoningEffortScreen)
            self.assertLessEqual(app.screen.query_one("#effort-dialog").region.bottom, 24)
            labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            for effort in ReasoningEffort:
                self.assertIn(effort.value, labels)
            clicked = await pilot.click("#effort-choice-3")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if profiles.effort_selections:
                    break

            self.assertEqual(profiles.effort_selections, [ReasoningEffort.XHIGH])
            self.assertEqual(preferences.saved_efforts, [ReasoningEffort.XHIGH])
            self.assertIn(
                "⬤ xhigh",
                str(app.query_one("#runtime-effort", Static).renderable),
            )

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/effort ultracode"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(profiles.effort_selections[-1], ReasoningEffort.ULTRACODE)
            self.assertIn("workflow orchestration is not implemented", app.entries[-1].text)
            self.assertIn(
                "⚡ ultracode → ⬤ xhigh",
                str(app.query_one("#runtime-effort", Static).renderable),
            )

            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Effort: ⚡ ultracode → ⬤ xhigh", app.entries[-1].text)

    async def test_effort_validation_and_running_turn_guard_do_not_change_policy(self) -> None:
        runner = CancellableTuiConversation()
        profiles = ProfileTuiController()
        app = NeuroCodeApp(
            runner,
            provider_controller=profiles,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/effort impossible"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Unknown effort", app.entries[-1].text)
            self.assertEqual(profiles.effort_selections, [])

            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)
            prompt.value = "/effort low"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(profiles.effort_selections, [])
            self.assertEqual(
                app.entries[-1].text,
                "Cannot change reasoning effort while a turn is running.",
            )
            await pilot.press("ctrl+c")

    async def test_narrow_runtime_bar_keeps_effort_visible_above_the_prompt(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="provider-with-a-very-long-name",
            model_name="model-with-a-very-long-name",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(52, 18)) as pilot:
            await pilot.pause()
            runtime_bar = app.query_one("#runtime-bar")
            context = app.query_one("#runtime-context", Static)
            effort = app.query_one("#runtime-effort", Static)
            mode = app.query_one("#runtime-mode", Static)
            prompt = app.query_one("#prompt", Input)
            self.assertLessEqual(runtime_bar.region.bottom, prompt.region.y)
            self.assertGreater(effort.region.width, 0)
            self.assertGreater(context.region.width, 0)
            self.assertGreater(mode.region.width, 0)
            self.assertIn("?", str(context.renderable))
            self.assertIn("● high", str(effort.renderable))
            self.assertIn("normal", str(mode.renderable))

    async def test_terminal_size_fallback_expands_the_full_screen_layout(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(80, 24)) as pilot:
            with patch("neuro_code.tui._read_terminal_size", return_value=Size(132, 41)):
                app._synchronize_terminal_size()
                await pilot.pause()

            self.assertEqual(app.screen.size, Size(132, 41))
            self.assertEqual(app.screen.region.size, Size(132, 41))
            transcript = app.query_one("#transcript", VerticalScroll)
            self.assertEqual(
                transcript.region.width + transcript.scrollbar_size_vertical,
                132,
            )
            prompt = app.query_one("#prompt", Input)
            self.assertEqual(prompt.region.right, 131)
            self.assertEqual(prompt.region.bottom, 40)

    async def test_tasks_command_lists_current_scope_without_rendering_command_or_output(
        self,
    ) -> None:
        runner = TuiConversation()
        tasks = TaskTuiController(
            (
                background_snapshot("task-running", BackgroundTaskStatus.RUNNING),
                background_snapshot(
                    "task-failed",
                    BackgroundTaskStatus.FAILED,
                    exit_code=7,
                ),
            )
        )
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/tasks"
            await pilot.press("enter")
            await pilot.pause()

            rendered = app.entries[-1].text
            self.assertIn("task-running · running", rendered)
            self.assertIn("task-failed · failed · exit 7", rendered)
            self.assertIn("19 output bytes", rendered)
            self.assertNotIn("private-command", rendered)
            self.assertNotIn("private task output", rendered)
            self.assertEqual(runner.prompts, [])

    async def test_terminal_task_notification_is_emitted_once_without_raw_output(
        self,
    ) -> None:
        runner = TuiConversation()
        tasks = TaskTuiController((background_snapshot("task-fast", BackgroundTaskStatus.RUNNING),))
        app = NeuroCodeApp(
            runner,
            task_controller=tasks,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)):
            await app._poll_background_tasks()
            tasks.snapshots = (
                background_snapshot(
                    "task-fast",
                    BackgroundTaskStatus.COMPLETED,
                    exit_code=0,
                ),
            )
            await app._poll_background_tasks()
            await app._poll_background_tasks()

            notifications = [
                entry.text for entry in app.entries if "Background task task-fast" in entry.text
            ]
            self.assertEqual(notifications, ["Background task task-fast completed (exit 0)."])
            self.assertNotIn("private task output", "\n".join(notifications))

    def test_session_picker_labels_saved_and_mismatched_sandbox_profiles(self) -> None:
        option = replace(
            SessionTuiController().options[1],
            selectable=False,
            sandbox_profile=SandboxProfile.STRICT,
            sandbox_profile_match=False,
        )

        label = SessionSelectionScreen._label(option)

        self.assertIn("sandbox strict", label)
        self.assertIn("restart required", label)
        self.assertIn("unavailable", label)

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
            self.assertIn(("status", "Reasoning..."), entries)
            self.assertIn(("status", "Thought for 1.2s · model step 1"), entries)
            tool_entries = [text for category, text in entries if category == "tool"]
            self.assertEqual(len(tool_entries), 1)
            self.assertEqual(tool_entries[0], "● Read README.md · 420ms")
            self.assertIn(("assistant", "fixture response"), entries)
            self.assertEqual(entries[-1], ("status", "Turn completed in 2.8s · 1 model step(s)"))
            self.assertNotIn("private", "\n".join(text for _, text in entries))

    async def test_tool_card_updates_in_place_and_renders_a_redacted_file_diff(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 32)) as pilot:
            transcript = app.query_one("#transcript", VerticalScroll)
            await app._handle_event(
                AgentEvent.create(
                    1,
                    AgentEventKind.TOOL_REQUESTED,
                    {
                        "id": "write",
                        "name": "bash",
                        "arguments": {
                            "command": "printf 'API_KEY=sk-fixturesecret123' > src/new.py"
                        },
                    },
                )
            )
            await pilot.pause()
            child_count = len(transcript.children)

            for event in (
                AgentEvent.create(
                    2,
                    AgentEventKind.TOOL_PERMISSION,
                    {
                        "id": "write",
                        "name": "bash",
                        "effect": "allow",
                        "reason": "fixture policy",
                    },
                ),
                AgentEvent.create(
                    3,
                    AgentEventKind.TOOL_STARTED,
                    {"id": "write", "name": "bash"},
                ),
                AgentEvent.create(
                    4,
                    AgentEventKind.TOOL_COMPLETED,
                    {
                        "id": "write",
                        "name": "bash",
                        "content": "",
                        "duration_seconds": 0.125,
                        "workspace_changes": {
                            "files": [
                                {
                                    "path": "src/new.py",
                                    "status": "created",
                                    "additions": 2,
                                    "deletions": 0,
                                    "diff": (
                                        "--- /dev/null\n"
                                        "+++ b/src/new.py\n"
                                        "@@ -0,0 +1,2 @@\n"
                                        '+API_KEY = "sk-fixturesecret123"\n'
                                        '+print("ready")'
                                    ),
                                    "diff_truncated": False,
                                }
                            ],
                            "omitted_files": 0,
                            "scan_limited": False,
                        },
                    },
                ),
            ):
                await app._handle_event(event)
                await pilot.pause()
                self.assertEqual(len(transcript.children), child_count)

            tool_entries = [entry for entry in app.entries if entry.category == "tool"]
            self.assertEqual(len(tool_entries), 1)
            card = tool_entries[0].text
            self.assertIn("● bash(", card)
            self.assertIn("├ Allowed · fixture policy", card)
            self.assertIn("├ Created src/new.py (+2)", card)
            self.assertIn("+++ b/src/new.py", card)
            self.assertIn('+API_KEY = "[REDACTED]"', card)
            self.assertIn('+print("ready")', card)
            self.assertIn("└ Completed · 125ms", card)
            self.assertNotIn("sk-fixturesecret123", card)

            rendered_segments = list(
                app.console.render(
                    app.query_one(ToolFeedbackMessage).renderable,
                    app.console.options.update(width=100),
                )
            )
            added_segments = [
                segment for segment in rendered_segments if '+print("ready")' in segment.text
            ]
            removed_or_header_segments = [
                segment for segment in rendered_segments if "--- /dev/null" in segment.text
            ]
            self.assertTrue(added_segments)
            self.assertIn("#b7f7ca", str(added_segments[0].style))
            self.assertIn("#213a2b", str(added_segments[0].style))
            self.assertTrue(removed_or_header_segments)
            self.assertIn("#ffb4ab", app._diff_line_style("-removed line"))
            self.assertIn("#4a221d", app._diff_line_style("-removed line"))

            card_widget = app.query_one(ToolFeedbackMessage)
            self.assertTrue(card_widget.can_focus)
            self.assertIn("Details shown", card)
            card_widget.focus()
            await pilot.press("enter")
            await pilot.pause()

            collapsed_card = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("Created src/new.py (+2)", collapsed_card)
            self.assertIn("Details hidden", collapsed_card)
            self.assertIn("Completed · 125ms", collapsed_card)
            self.assertNotIn("+++ b/src/new.py", collapsed_card)
            self.assertNotIn('+print("ready")', collapsed_card)
            self.assertNotIn("sk-fixturesecret123", collapsed_card)

            self.assertTrue(await pilot.click(card_widget, offset=(12, 0)))
            await pilot.pause()
            expanded_card = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("+++ b/src/new.py", expanded_card)
            self.assertIn('+print("ready")', expanded_card)
            self.assertNotIn("sk-fixturesecret123", expanded_card)

            await app._language_settings_selected(UiLanguage.SIMPLIFIED_CHINESE)
            await pilot.pause()
            localized_card = next(entry.text for entry in app.entries if entry.category == "tool")
            self.assertIn("已允许 · fixture policy", localized_card)
            self.assertIn("新建 src/new.py", localized_card)
            self.assertIn("+2", localized_card)
            self.assertIn("完成 · 125ms", localized_card)
            self.assertIn("已展开详细信息", localized_card)

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
            self.assertIn("/sessions", app.entries[-1].text)
            self.assertIn("/rename", app.entries[-1].text)
            self.assertIn("/tasks", app.entries[-1].text)

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
            self.assertIn(
                "second / second-model",
                str(app.query_one("#runtime-model", Static).renderable),
            )

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

    async def test_initial_session_history_replays_without_private_context_or_tool_output(
        self,
    ) -> None:
        controller = SessionTuiController(current_session="target-session-123456789")
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            initial_items=restored_history(),
            provider_name="second",
            model_name="second-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()

            rendered = "\n".join(entry.text for entry in app.entries)
            self.assertIn("restored prompt", rendered)
            self.assertIn("image content preserved in session", rendered)
            self.assertIn("Restored tool request: read_file.", rendered)
            self.assertIn("Restored result for read_file.", rendered)
            self.assertIn("restored response", rendered)
            self.assertIn("Resumed session target-session-123456789", rendered)
            self.assertNotIn("never render this", rendered)
            self.assertNotIn("private assistant reasoning", rendered)
            self.assertNotIn("private tool output", rendered)
            self.assertNotIn("private-image", rendered)

    async def test_workspace_session_picker_resumes_and_replaces_transcript(self) -> None:
        controller = SessionTuiController()
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(115, 35)) as pilot:
            await pilot.press("ctrl+r")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SessionSelectionScreen):
                    break

            self.assertIsInstance(app.screen, SessionSelectionScreen)
            labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            self.assertIn("current", labels)
            self.assertIn("target-sessi", labels)
            clicked = await pilot.click("#session-choice-1")
            self.assertTrue(clicked)
            for _ in range(20):
                await pilot.pause(0.01)
                if controller.selected:
                    break

            self.assertEqual(controller.selected, ["target-session-123456789"])
            rendered = "\n".join(entry.text for entry in app.entries)
            self.assertIn("restored prompt", rendered)
            self.assertIn("Previous session current-session remains saved", rendered)
            self.assertNotIn("Ready · first/first-model", rendered)

            prompt = app.query_one("#prompt", Input)
            prompt.value = "/status"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("second/second-model", app.entries[-1].text)
            self.assertIn("target-session-123456789", app.entries[-1].text)

            await pilot.press("ctrl+r")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SessionSelectionScreen):
                    break
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, SessionSelectionScreen)

    async def test_sessions_command_searches_titles_and_content_before_opening_picker(self) -> None:
        controller = SessionTuiController()
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(120, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/sessions quoted"
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.01)
                if isinstance(app.screen, SessionSelectionScreen):
                    break

            self.assertIsInstance(app.screen, SessionSelectionScreen)
            self.assertEqual(controller.queries, ["quoted"])
            self.assertEqual(app.screen.search_query, "quoted")
            title = app.screen.query_one("#session-title", Label)
            self.assertEqual(str(title.renderable), "Session search: quoted")
            self.assertNotIn("🔎", str(title.renderable))
            labels = "\n".join(str(button.label) for button in app.screen.query(Button))
            self.assertIn("Escaped quoted session", labels)
            self.assertIn("[quoted] content", labels)
            await pilot.press("escape")

    async def test_rename_and_title_commands_update_the_current_session(self) -> None:
        controller = SessionTuiController()
        app = NeuroCodeApp(
            controller,
            session_controller=controller,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/rename   Manual session title"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(controller.renamed, ["  Manual session title"])
            self.assertIn("renamed to 'Manual session title'", app.entries[-1].text)

            prompt.value = "/title Alias title"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                controller.renamed,
                ["  Manual session title", "Alias title"],
            )
            self.assertIn("renamed to 'Alias title'", app.entries[-1].text)

    async def test_direct_session_resume_is_blocked_while_a_turn_is_running(self) -> None:
        runner = CancellableTuiConversation()
        sessions = SessionTuiController()
        app = NeuroCodeApp(
            runner,
            session_controller=sessions,
            provider_name="first",
            model_name="first-model",
            cwd=Path("/workspace"),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "long turn"
            await pilot.press("enter")
            await asyncio.wait_for(runner.started.wait(), timeout=1)

            prompt.value = "/resume target-session-123456789"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(sessions.selected, [])
            self.assertEqual(
                app.entries[-1].text,
                "Cannot resume a session while a turn is running.",
            )
            prompt.value = "/rename Blocked title"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(sessions.renamed, [])
            self.assertEqual(
                app.entries[-1].text,
                "Cannot rename a session while a turn is running.",
            )
            await pilot.press("ctrl+c")


if __name__ == "__main__":
    unittest.main()
