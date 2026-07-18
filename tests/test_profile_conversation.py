from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import Message, Role, SessionItem
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelEvent
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_search import SessionSearchHit
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ConfigurationError
from neuro_code.ports.background_tasks import BackgroundTaskManager
from neuro_code.runtime import (
    AgentRunResult,
    ConversationBinding,
    ProfileConversationController,
    ProviderOption,
)
from neuro_code.runtime.agent import EventSink


class FixtureProvider:
    context_affinity = None

    def __init__(self, name: str, model: str) -> None:
        self.provider_name = name
        self.model_name = model

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        if False:
            yield


class FixtureConversation:
    def __init__(
        self,
        session_id: str | None = None,
        *,
        blocked: bool = False,
        items: Sequence[SessionItem] = (),
    ) -> None:
        self._session_id = session_id
        self._items = tuple(items)
        self.prompts: list[str] = []
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.reasoning_effort = ReasoningEffort.HIGH
        self.interaction_mode = InteractionMode.NORMAL
        self.auto_mode_unrestricted = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def items(self) -> tuple[SessionItem, ...]:
        return self._items

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self.reasoning_effort = effort

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        self.interaction_mode = mode

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        del sink
        self.prompts.append(prompt)
        self.started.set()
        if self.blocked:
            await self.release.wait()
        self._session_id = self._session_id or "new-session"
        return AgentRunResult(self._session_id, "ok", self._items, (), (), 1)


class FixtureTaskScope:
    def __init__(self, snapshots: Sequence[BackgroundTaskSnapshot] = ()) -> None:
        self.snapshots = tuple(snapshots)
        self.shutdown_calls = 0

    async def list(self) -> tuple[BackgroundTaskSnapshot, ...]:
        return self.snapshots

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.snapshots = ()

    def as_manager(self) -> BackgroundTaskManager:
        return cast(BackgroundTaskManager, self)


def task_snapshot(task_id: str, status: BackgroundTaskStatus) -> BackgroundTaskSnapshot:
    started_at = datetime(2026, 7, 18, 8, 30, tzinfo=UTC)
    return BackgroundTaskSnapshot(
        task_id,
        f"command for {task_id}",
        "/workspace",
        status,
        "",
        0,
        False,
        None if status is BackgroundTaskStatus.RUNNING else 0,
        started_at,
        None if status is BackgroundTaskStatus.RUNNING else started_at,
    )


def option(
    name: str,
    *,
    available: bool = True,
    credential_configured: bool = True,
) -> ProviderOption:
    return ProviderOption(
        name,
        "openai-chat",
        f"{name}-model",
        available,
        credential_configured,
        default=name == "first",
        context_window_tokens=100_000 if name == "first" else 200_000,
    )


def summary(
    session_id: str,
    provider: str,
    model: str,
    *,
    sandbox_profile: SandboxProfile | None = None,
) -> SessionSummary:
    timestamp = datetime(2026, 7, 18, 8, 30, tzinfo=UTC)
    return SessionSummary(
        session_id,
        "/workspace",
        provider,
        model,
        timestamp,
        timestamp,
        sandbox_profile=sandbox_profile,
    )


class ProfileConversationControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_interaction_mode_updates_runner_and_survives_profile_switch(self) -> None:
        first = FixtureConversation("old-session")
        second = FixtureConversation()

        async def bind(name: str) -> ConversationBinding:
            return ConversationBinding(second, FixtureProvider(name, f"{name}-model"))

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(first, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        selection = await controller.set_interaction_mode(InteractionMode.PLAN)
        await controller.select_profile("second")

        self.assertTrue(selection.changed)
        self.assertEqual(first.interaction_mode, InteractionMode.PLAN)
        self.assertEqual(second.interaction_mode, InteractionMode.PLAN)

    async def test_reasoning_effort_updates_runner_and_survives_profile_switch(self) -> None:
        first = FixtureConversation("old-session")
        second = FixtureConversation()

        async def bind(name: str) -> ConversationBinding:
            return ConversationBinding(second, FixtureProvider(name, f"{name}-model"))

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(first, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        selection = await controller.set_reasoning_effort(ReasoningEffort.ULTRACODE)
        await controller.select_profile("second")

        self.assertTrue(selection.changed)
        self.assertIs(selection.requested, ReasoningEffort.ULTRACODE)
        self.assertIs(selection.effective, ReasoningEffort.XHIGH)
        self.assertFalse(selection.workflow_orchestration_active)
        self.assertIs(first.reasoning_effort, ReasoningEffort.ULTRACODE)
        self.assertIs(second.reasoning_effort, ReasoningEffort.ULTRACODE)
        self.assertIs(controller.reasoning_effort, ReasoningEffort.ULTRACODE)
        self.assertIs(controller.effective_reasoning_effort, ReasoningEffort.XHIGH)

    async def test_reasoning_effort_change_is_rejected_during_a_turn(self) -> None:
        runner = FixtureConversation(blocked=True)

        async def bind(name: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation(),
                FixtureProvider(name, f"{name}-model"),
            )

        controller = ProfileConversationController(
            options=(option("first"),),
            selected_profile="first",
            binding=ConversationBinding(runner, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )
        turn = asyncio.create_task(controller.run("blocked"))
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        with self.assertRaisesRegex(ConfigurationError, "while a turn is running"):
            await controller.set_reasoning_effort(ReasoningEffort.LOW)

        runner.release.set()
        await turn
        self.assertIs(controller.reasoning_effort, ReasoningEffort.HIGH)

    async def test_manual_session_rename_updates_the_validated_summary_cache(self) -> None:
        renamed: list[tuple[str, str]] = []
        original = replace(
            summary("current", "first", "first-model"),
            title="Original title",
        )

        async def bind_profile(name: str) -> ConversationBinding:
            return ConversationBinding(FixtureConversation(), FixtureProvider(name, "model"))

        async def list_sessions() -> tuple[SessionSummary, ...]:
            return (original,)

        async def bind_session(profile: str, session_id: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation(session_id),
                FixtureProvider(profile, "model"),
            )

        async def rename_session(session_id: str, title: str) -> SessionSummary:
            renamed.append((session_id, title))
            return replace(original, title="Manual title")

        controller = ProfileConversationController(
            options=(option("first"),),
            selected_profile="first",
            binding=ConversationBinding(
                FixtureConversation("current"),
                FixtureProvider("first", "first-model"),
            ),
            binding_factory=bind_profile,
            session_catalog=list_sessions,
            session_binding_factory=bind_session,
            session_rename=rename_session,
        )

        result = await controller.rename_session("  Manual title  ")

        self.assertEqual(renamed, [("current", "  Manual title  ")])
        self.assertEqual(result.title, "Manual title")
        selection = await controller.select_session("current")
        self.assertFalse(selection.changed)

    async def test_session_search_projects_ranked_metadata_into_picker_options(self) -> None:
        searched: list[str] = []
        result_summary = replace(
            summary("search-result", "second", "second-model"),
            title="Escaped SQLite search",
        )

        async def bind_profile(name: str) -> ConversationBinding:
            return ConversationBinding(FixtureConversation(), FixtureProvider(name, "model"))

        async def list_sessions() -> tuple[SessionSummary, ...]:
            return ()

        async def search_sessions(query: str) -> tuple[SessionSearchHit, ...]:
            searched.append(query)
            return (
                SessionSearchHit(
                    result_summary,
                    2.5,
                    ("title", "content"),
                    "[SQLite] session content",
                ),
            )

        async def bind_session(profile: str, session_id: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation(session_id),
                FixtureProvider(profile, "model"),
            )

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(
                FixtureConversation("current"),
                FixtureProvider("first", "first-model"),
            ),
            binding_factory=bind_profile,
            session_catalog=list_sessions,
            session_search=search_sessions,
            session_binding_factory=bind_session,
        )

        options = await controller.list_sessions("  SQLite quoted  ")

        self.assertEqual(searched, ["SQLite quoted"])
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].title, "Escaped SQLite search")
        self.assertEqual(options[0].matched_fields, ("title", "content"))
        self.assertEqual(options[0].snippet, "[SQLite] session content")

        selection = await controller.select_session("search-result")
        self.assertTrue(selection.changed)
        self.assertEqual(selection.session_id, "search-result")
        self.assertEqual(controller.selected_profile, "second")

    async def test_task_visibility_tracks_binding_and_switch_closes_old_scope(self) -> None:
        old_tasks = FixtureTaskScope(
            (
                task_snapshot("task-running", BackgroundTaskStatus.RUNNING),
                task_snapshot("task-complete", BackgroundTaskStatus.COMPLETED),
            )
        )
        new_tasks = FixtureTaskScope()

        async def bind(name: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation(),
                FixtureProvider(name, f"{name}-model"),
                new_tasks.as_manager(),
            )

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(
                FixtureConversation("old-session"),
                FixtureProvider("first", "first-model"),
                old_tasks.as_manager(),
            ),
            binding_factory=bind,
        )

        self.assertEqual(
            [snapshot.task_id for snapshot in await controller.list_background_tasks()],
            ["task-running", "task-complete"],
        )
        selection = await controller.select_profile("second")

        self.assertEqual(selection.stopped_background_tasks, 1)
        self.assertEqual(old_tasks.shutdown_calls, 1)
        self.assertEqual(await controller.list_background_tasks(), ())
        self.assertEqual(new_tasks.shutdown_calls, 0)

    async def test_rejected_binding_closes_its_task_scope(self) -> None:
        rejected_tasks = FixtureTaskScope(
            (task_snapshot("task-rejected", BackgroundTaskStatus.RUNNING),)
        )

        async def bind(name: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation("unexpected-resume"),
                FixtureProvider(name, f"{name}-model"),
                rejected_tasks.as_manager(),
            )

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(
                FixtureConversation("current"),
                FixtureProvider("first", "first-model"),
            ),
            binding_factory=bind,
        )

        with self.assertRaisesRegex(ConfigurationError, "fresh conversation"):
            await controller.select_profile("second")

        self.assertEqual(rejected_tasks.shutdown_calls, 1)
        self.assertEqual(controller.selected_profile, "first")

    async def test_switch_preserves_old_session_and_uses_a_fresh_binding(self) -> None:
        first = FixtureConversation("old-session")
        second = FixtureConversation()
        requested: list[str] = []

        async def bind(name: str) -> ConversationBinding:
            requested.append(name)
            return ConversationBinding(second, FixtureProvider(name, f"{name}-model"))

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(first, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        selection = await controller.select_profile("second")
        result = await controller.run("use the second profile")

        self.assertTrue(selection.changed)
        self.assertEqual(selection.previous_session_id, "old-session")
        self.assertEqual(selection.context_window_tokens, 200_000)
        self.assertEqual(requested, ["second"])
        self.assertEqual(controller.selected_profile, "second")
        self.assertEqual([profile.selected for profile in controller.profiles], [False, True])
        self.assertEqual(first.prompts, [])
        self.assertEqual(second.prompts, ["use the second profile"])
        self.assertEqual(result.session_id, "new-session")

    async def test_reselecting_current_profile_is_a_noop(self) -> None:
        runner = FixtureConversation("existing")
        factory_calls = 0

        async def bind(_: str) -> ConversationBinding:
            nonlocal factory_calls
            factory_calls += 1
            return ConversationBinding(FixtureConversation(), FixtureProvider("first", "model"))

        controller = ProfileConversationController(
            options=(option("first"),),
            selected_profile="first",
            binding=ConversationBinding(runner, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        selection = await controller.select_profile("first")

        self.assertFalse(selection.changed)
        self.assertEqual(factory_calls, 0)
        self.assertEqual(controller.session_id, "existing")

    async def test_unready_unknown_and_nonfresh_profiles_fail_closed(self) -> None:
        runner = FixtureConversation("existing")

        async def bind(name: str) -> ConversationBinding:
            if name == "broken":
                raise ConfigurationError("fixture provider construction failed")
            return ConversationBinding(
                FixtureConversation("unexpected-resume"),
                FixtureProvider(name, f"{name}-model"),
            )

        controller = ProfileConversationController(
            options=(
                option("first"),
                option("unavailable", available=False),
                option("missing-key", credential_configured=False),
                option("resumed"),
                option("broken"),
            ),
            selected_profile="first",
            binding=ConversationBinding(runner, FixtureProvider("first", "first-model")),
            binding_factory=bind,
        )

        for name, message in (
            ("unknown", "does not exist"),
            ("unavailable", "unavailable"),
            ("missing-key", "credential is not configured"),
            ("resumed", "fresh conversation"),
            ("broken", "provider construction failed"),
        ):
            with self.assertRaisesRegex(ConfigurationError, message):
                await controller.select_profile(name)

        self.assertEqual(controller.selected_profile, "first")
        self.assertEqual(controller.session_id, "existing")

    async def test_switch_is_rejected_while_a_turn_is_running(self) -> None:
        runner = FixtureConversation(blocked=True)

        async def bind(name: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation(), FixtureProvider(name, f"{name}-model")
            )

        async def list_sessions() -> tuple[SessionSummary, ...]:
            return (summary("target", "second", "second-model"),)

        async def bind_session(profile: str, session_id: str) -> ConversationBinding:
            return ConversationBinding(
                FixtureConversation(session_id),
                FixtureProvider(profile, f"{profile}-model"),
            )

        async def rename_session(session_id: str, title: str) -> SessionSummary:
            return replace(summary(session_id, "first", "first-model"), title=title)

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(runner, FixtureProvider("first", "first-model")),
            binding_factory=bind,
            session_catalog=list_sessions,
            session_binding_factory=bind_session,
            session_rename=rename_session,
        )

        turn = asyncio.create_task(controller.run("blocked"))
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        with self.assertRaisesRegex(ConfigurationError, "while a turn is running"):
            await controller.select_profile("second")
        with self.assertRaisesRegex(ConfigurationError, "while a turn is running"):
            await controller.select_session("target")
        with self.assertRaisesRegex(ConfigurationError, "while a turn is running"):
            await controller.rename_session("Blocked rename")
        runner.release.set()
        await turn

        self.assertEqual(controller.selected_profile, "first")

    async def test_session_catalog_resumes_with_its_ready_source_profile(self) -> None:
        first = FixtureConversation("old-session")
        old_tasks = FixtureTaskScope(
            (task_snapshot("task-old-session", BackgroundTaskStatus.RUNNING),)
        )
        resumed_tasks = FixtureTaskScope()
        history = (Message(Role.USER, "restore this"), Message(Role.ASSISTANT, "restored"))
        resumed = FixtureConversation("target-session", items=history)
        requested: list[tuple[str, str]] = []

        async def bind_profile(name: str) -> ConversationBinding:
            return ConversationBinding(FixtureConversation(), FixtureProvider(name, "model"))

        async def list_sessions() -> tuple[SessionSummary, ...]:
            return (summary("target-session", "second", "second-model"),)

        async def bind_session(profile: str, session_id: str) -> ConversationBinding:
            requested.append((profile, session_id))
            return ConversationBinding(
                resumed,
                FixtureProvider(profile, "second-model"),
                resumed_tasks.as_manager(),
            )

        controller = ProfileConversationController(
            options=(option("first"), option("second")),
            selected_profile="first",
            binding=ConversationBinding(
                first,
                FixtureProvider("first", "first-model"),
                old_tasks.as_manager(),
            ),
            binding_factory=bind_profile,
            session_catalog=list_sessions,
            session_binding_factory=bind_session,
        )

        sessions = await controller.list_sessions()
        selection = await controller.select_session("target-session")

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].resume_profile, "second")
        self.assertTrue(sessions[0].source_profile_match)
        self.assertEqual(requested, [("second", "target-session")])
        self.assertEqual(selection.previous_session_id, "old-session")
        self.assertEqual(selection.context_window_tokens, 200_000)
        self.assertEqual(selection.items, history)
        self.assertEqual(selection.stopped_background_tasks, 1)
        self.assertEqual(old_tasks.shutdown_calls, 1)
        self.assertEqual(resumed_tasks.shutdown_calls, 0)
        self.assertEqual(controller.items, history)
        self.assertEqual(controller.selected_profile, "second")

    async def test_session_resume_falls_back_to_current_profile_without_native_affinity(
        self,
    ) -> None:
        history = (Message(Role.USER, "imported prompt"),)
        resumed = FixtureConversation("imported-session", items=history)
        requested: list[tuple[str, str]] = []

        async def bind_profile(name: str) -> ConversationBinding:
            return ConversationBinding(FixtureConversation(), FixtureProvider(name, "model"))

        async def list_sessions() -> tuple[SessionSummary, ...]:
            return (summary("imported-session", "upstream-rust-import", "old-model"),)

        async def bind_session(profile: str, session_id: str) -> ConversationBinding:
            requested.append((profile, session_id))
            return ConversationBinding(resumed, FixtureProvider(profile, "first-model"))

        controller = ProfileConversationController(
            options=(option("first"), option("missing", credential_configured=False)),
            selected_profile="first",
            binding=ConversationBinding(
                FixtureConversation("current"),
                FixtureProvider("first", "first-model"),
            ),
            binding_factory=bind_profile,
            session_catalog=list_sessions,
            session_binding_factory=bind_session,
        )

        sessions = await controller.list_sessions()
        selection = await controller.select_session("imported-session")

        self.assertEqual(sessions[0].resume_profile, "first")
        self.assertFalse(sessions[0].source_profile_match)
        self.assertEqual(requested, [("first", "imported-session")])
        self.assertFalse(selection.source_profile_match)
        self.assertEqual(selection.source_provider, "upstream-rust-import")

    async def test_current_unknown_and_incomplete_session_controls_fail_safely(self) -> None:
        current = FixtureConversation("current")

        async def bind_profile(name: str) -> ConversationBinding:
            return ConversationBinding(FixtureConversation(), FixtureProvider(name, "model"))

        async def list_sessions() -> tuple[SessionSummary, ...]:
            return (summary("current", "first", "first-model"),)

        async def bind_session(profile: str, session_id: str) -> ConversationBinding:
            raise AssertionError(f"unexpected binding: {profile}/{session_id}")

        controller = ProfileConversationController(
            options=(option("first"),),
            selected_profile="first",
            binding=ConversationBinding(current, FixtureProvider("first", "first-model")),
            binding_factory=bind_profile,
            session_catalog=list_sessions,
            session_binding_factory=bind_session,
        )

        selection = await controller.select_session("current")
        self.assertFalse(selection.changed)
        with self.assertRaisesRegex(ConfigurationError, "current workspace"):
            await controller.select_session("outside")

        with self.assertRaisesRegex(ValueError, "configured together"):
            ProfileConversationController(
                options=(option("first"),),
                selected_profile="first",
                binding=ConversationBinding(current, FixtureProvider("first", "first-model")),
                binding_factory=bind_profile,
                session_catalog=list_sessions,
            )

    async def test_in_process_resume_rejects_a_different_saved_sandbox(self) -> None:
        requested: list[tuple[str, str]] = []

        async def bind_profile(name: str) -> ConversationBinding:
            return ConversationBinding(FixtureConversation(), FixtureProvider(name, "model"))

        async def list_sessions() -> tuple[SessionSummary, ...]:
            return (
                summary(
                    "strict-session",
                    "first",
                    "first-model",
                    sandbox_profile=SandboxProfile.STRICT,
                ),
                summary("legacy-session", "first", "first-model"),
            )

        async def bind_session(profile: str, session_id: str) -> ConversationBinding:
            requested.append((profile, session_id))
            return ConversationBinding(
                FixtureConversation(session_id),
                FixtureProvider(profile, "first-model"),
            )

        controller = ProfileConversationController(
            options=(option("first"),),
            selected_profile="first",
            binding=ConversationBinding(
                FixtureConversation("current"),
                FixtureProvider("first", "first-model"),
            ),
            binding_factory=bind_profile,
            session_catalog=list_sessions,
            session_binding_factory=bind_session,
            sandbox_profile=SandboxProfile.WORKSPACE,
        )

        sessions = {option.session_id: option for option in await controller.list_sessions()}
        self.assertFalse(sessions["strict-session"].sandbox_profile_match)
        self.assertFalse(sessions["strict-session"].selectable)
        self.assertTrue(sessions["legacy-session"].sandbox_profile_match)
        self.assertTrue(sessions["legacy-session"].selectable)
        with self.assertRaisesRegex(ConfigurationError, "restart with --resume strict-session"):
            await controller.select_session("strict-session")
        self.assertEqual(requested, [])

        selection = await controller.select_session("legacy-session")
        self.assertTrue(selection.changed)
        self.assertEqual(requested, [("first", "legacy-session")])
