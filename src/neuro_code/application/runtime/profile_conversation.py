from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import ContentPart, SessionItem
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_search import SessionSearchHit
from neuro_code.domain.session_tasks import SessionTask
from neuro_code.domain.sessions import SessionSummary
from neuro_code.shared.errors import ConfigurationError


class ConversationRunner(Protocol):
    @property
    def session_id(self) -> str | None: ...

    @property
    def items(self) -> tuple[SessionItem, ...]: ...

    @property
    def plan(self) -> SessionPlan | None: ...

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]: ...

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment: ...

    async def list_plan_comments(self) -> tuple[PlanComment, ...]: ...

    async def list_session_tasks(self) -> tuple[SessionTask, ...]: ...

    async def get_session_task(self, task_id: str) -> SessionTask | None: ...

    @property
    def reasoning_effort(self) -> ReasoningEffort: ...

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None: ...

    @property
    def interaction_mode(self) -> InteractionMode: ...

    @property
    def auto_mode_unrestricted(self) -> bool: ...

    def set_interaction_mode(self, mode: InteractionMode) -> None: ...

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: Sequence[ContentPart] = (),
    ) -> AgentRunResult: ...

    async def execute_plan(self, *, sink: EventSink | None = None) -> AgentRunResult: ...


@dataclass(frozen=True, slots=True)
class ProviderOption:
    name: str
    protocol: str
    model: str
    available: bool
    credential_configured: bool
    default: bool = False
    selected: bool = False
    context_window_tokens: int | None = None

    @property
    def selectable(self) -> bool:
        return self.available and self.credential_configured


@dataclass(frozen=True, slots=True)
class ConversationBinding:
    runner: ConversationRunner
    provider: ModelProvider
    background_tasks: BackgroundTaskManager | None = None


@dataclass(frozen=True, slots=True)
class ProviderSelectionResult:
    profile_name: str
    provider_name: str
    model_name: str
    previous_session_id: str | None
    changed: bool
    stopped_background_tasks: int = 0
    context_window_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ReasoningEffortSelectionResult:
    requested: ReasoningEffort
    effective: ReasoningEffort
    changed: bool
    workflow_orchestration_active: bool = False


@dataclass(frozen=True, slots=True)
class InteractionModeSelectionResult:
    requested: InteractionMode
    changed: bool
    auto_unrestricted: bool = False
    safety_classifier_active: bool = False

    @property
    def limited_auto(self) -> bool:
        return self.requested is InteractionMode.AUTO and not self.auto_unrestricted


@dataclass(frozen=True, slots=True)
class SessionOption:
    session_id: str
    source_provider: str
    source_model: str
    updated_at: datetime
    resume_profile: str
    current: bool
    source_profile_match: bool
    selectable: bool
    sandbox_profile: SandboxProfile | None = None
    sandbox_profile_match: bool = True
    title: str | None = None
    matched_fields: tuple[str, ...] = ()
    snippet: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSelectionResult:
    session_id: str
    source_provider: str
    source_model: str
    profile_name: str
    provider_name: str
    model_name: str
    previous_session_id: str | None
    changed: bool
    source_profile_match: bool
    items: tuple[SessionItem, ...]
    stopped_background_tasks: int = 0
    context_window_tokens: int | None = None


BindingFactory = Callable[[str], Awaitable[ConversationBinding]]
SessionCatalog = Callable[[], Awaitable[Sequence[SessionSummary]]]
SessionSearch = Callable[[str], Awaitable[Sequence[SessionSearchHit]]]
SessionBindingFactory = Callable[[str, str], Awaitable[ConversationBinding]]
SessionRename = Callable[[str, str], Awaitable[SessionSummary]]


class ProfileConversationController:
    """Serialize turns and replace the conversation at a safe profile boundary."""

    def __init__(
        self,
        *,
        options: Sequence[ProviderOption],
        selected_profile: str,
        binding: ConversationBinding,
        binding_factory: BindingFactory,
        session_catalog: SessionCatalog | None = None,
        session_search: SessionSearch | None = None,
        session_binding_factory: SessionBindingFactory | None = None,
        session_rename: SessionRename | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
        reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH,
        interaction_mode: InteractionMode = InteractionMode.NORMAL,
    ) -> None:
        self._options = tuple(options)
        names = [option.name for option in self._options]
        if not names or len(names) != len(set(names)):
            raise ValueError("provider options must be non-empty and uniquely named")
        if selected_profile not in names:
            raise ValueError("selected provider profile must exist in provider options")
        self._selected_profile = selected_profile
        self._binding = binding
        self._binding_factory = binding_factory
        if (session_catalog is None) != (session_binding_factory is None):
            raise ValueError(
                "session catalog and session binding factory must be configured together"
            )
        self._session_catalog = session_catalog
        if session_search is not None and session_catalog is None:
            raise ValueError("session search requires a session catalog")
        self._session_search = session_search
        self._session_binding_factory = session_binding_factory
        self._session_rename = session_rename
        self._known_session_summaries: dict[str, SessionSummary] = {}
        self._sandbox_profile = sandbox_profile
        self._reasoning_effort = reasoning_effort
        self._interaction_mode = interaction_mode
        self._turn_lock = asyncio.Lock()
        self._apply_conversation_policies(self._binding)

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
    def provider_name(self) -> str:
        return self._binding.provider.provider_name

    @property
    def model_name(self) -> str:
        return self._binding.provider.model_name

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
        return self._binding.runner.auto_mode_unrestricted

    @property
    def session_id(self) -> str | None:
        return self._binding.runner.session_id

    @property
    def items(self) -> tuple[SessionItem, ...]:
        return self._binding.runner.items

    @property
    def plan(self) -> SessionPlan | None:
        return self._binding.runner.plan

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]:
        return self._binding.runner.plan_comments

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        async with self._turn_lock:
            return await self._binding.runner.run(prompt, sink=sink)

    async def execute_plan(self, *, sink: EventSink | None = None) -> AgentRunResult:
        async with self._turn_lock:
            return await self._binding.runner.execute_plan(sink=sink)

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment:
        if self._turn_lock.locked():
            raise ConfigurationError("cannot comment on a plan while a turn is running")
        async with self._turn_lock:
            return await self._binding.runner.add_plan_comment(step_index, content)

    async def list_plan_comments(self) -> tuple[PlanComment, ...]:
        return await self._binding.runner.list_plan_comments()

    async def list_session_tasks(self) -> tuple[SessionTask, ...]:
        return await self._binding.runner.list_session_tasks()

    async def get_session_task(self, task_id: str) -> SessionTask | None:
        return await self._binding.runner.get_session_task(task_id)

    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]:
        manager = self._binding.background_tasks
        if manager is None:
            raise ConfigurationError("background task visibility is unavailable")
        return await manager.list()

    async def set_reasoning_effort(
        self,
        effort: ReasoningEffort,
    ) -> ReasoningEffortSelectionResult:
        if not isinstance(effort, ReasoningEffort):
            raise TypeError("reasoning effort must be a ReasoningEffort")
        if self._turn_lock.locked():
            raise ConfigurationError("cannot change reasoning effort while a turn is running")
        async with self._turn_lock:
            changed = effort is not self._reasoning_effort
            if changed:
                previous = self._reasoning_effort
                self._reasoning_effort = effort
                try:
                    self._apply_reasoning_effort(self._binding)
                except BaseException:
                    self._reasoning_effort = previous
                    self._apply_reasoning_effort(self._binding)
                    raise
            return ReasoningEffortSelectionResult(
                requested=effort,
                effective=effort.effective,
                changed=changed,
                workflow_orchestration_active=False,
            )

    async def set_interaction_mode(
        self,
        mode: InteractionMode,
    ) -> InteractionModeSelectionResult:
        if not isinstance(mode, InteractionMode):
            raise TypeError("interaction mode must be an InteractionMode")
        if self._turn_lock.locked():
            raise ConfigurationError("cannot change interaction mode while a turn is running")
        async with self._turn_lock:
            changed = mode is not self._interaction_mode
            if changed:
                previous = self._interaction_mode
                self._interaction_mode = mode
                try:
                    self._binding.runner.set_interaction_mode(mode)
                except BaseException:
                    self._interaction_mode = previous
                    self._binding.runner.set_interaction_mode(previous)
                    raise
            return InteractionModeSelectionResult(
                requested=mode,
                changed=changed,
                auto_unrestricted=self._binding.runner.auto_mode_unrestricted,
                safety_classifier_active=False,
            )

    async def select_profile(self, name: str) -> ProviderSelectionResult:
        if self._turn_lock.locked():
            raise ConfigurationError("cannot switch provider profiles while a turn is running")
        async with self._turn_lock:
            options = {option.name: option for option in self._options}
            option = options.get(name)
            if option is None:
                raise ConfigurationError(f"provider profile does not exist: {name}")
            if name == self._selected_profile:
                return self._selection_result(name, previous_session_id=None, changed=False)
            if not option.available:
                raise ConfigurationError(f"provider profile is unavailable: {name}")
            if not option.credential_configured:
                raise ConfigurationError(f"provider profile credential is not configured: {name}")

            previous_session_id = self.session_id
            binding = await self._binding_factory(name)
            if binding.runner.session_id is not None:
                await self._shutdown_binding_tasks(binding)
                raise ConfigurationError("provider profile switch must create a fresh conversation")
            try:
                self._apply_conversation_policies(binding)
            except BaseException:
                await self._shutdown_binding_tasks(binding)
                raise
            stopped_background_tasks = await self._replace_binding(binding)
            self._selected_profile = name
            return self._selection_result(
                name,
                previous_session_id=previous_session_id,
                changed=True,
                stopped_background_tasks=stopped_background_tasks,
            )

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]:
        if self._session_catalog is None:
            raise ConfigurationError("interactive session resume is unavailable")

        normalized_query = query.strip() if query is not None else None
        entries: tuple[tuple[SessionSummary, SessionSearchHit | None], ...]
        if normalized_query:
            if self._session_search is None:
                raise ConfigurationError("interactive session search is unavailable")
            hits = await self._session_search(normalized_query)
            entries = tuple((hit.summary, hit) for hit in hits)
        else:
            summaries = await self._session_catalog()
            entries = tuple((summary, None) for summary in summaries)
        options: list[SessionOption] = []
        seen_ids: set[str] = set()
        for summary, hit in entries:
            if summary.id in seen_ids:
                continue
            seen_ids.add(summary.id)
            self._known_session_summaries[summary.id] = summary
            options.append(
                self._session_option(
                    summary,
                    matched_fields=hit.matched_fields if hit is not None else (),
                    snippet=hit.snippet if hit is not None else None,
                )
            )
        return tuple(options)

    async def select_session(self, session_id: str) -> SessionSelectionResult:
        if self._turn_lock.locked():
            raise ConfigurationError("cannot resume a session while a turn is running")
        async with self._turn_lock:
            if self._session_binding_factory is None:
                raise ConfigurationError("interactive session resume is unavailable")
            summary = self._known_session_summaries.get(session_id)
            if summary is None:
                await self.list_sessions()
                summary = self._known_session_summaries.get(session_id)
            if summary is None:
                raise ConfigurationError(
                    f"session does not exist in the current workspace: {session_id}"
                )
            option = self._session_option(summary)
            if option.current:
                return self._session_selection_result(
                    option,
                    previous_session_id=None,
                    changed=False,
                    items=self.items,
                )
            if not option.sandbox_profile_match:
                assert option.sandbox_profile is not None
                raise ConfigurationError(
                    f"session sandbox profile is {option.sandbox_profile.value!r}, "
                    f"but the active profile is {self._sandbox_profile.value!r}; "
                    f"restart with --resume {option.session_id}"
                )
            if not option.selectable:
                raise ConfigurationError(
                    f"no ready provider profile can resume session: {session_id}"
                )

            previous_session_id = self.session_id
            binding = await self._session_binding_factory(
                option.resume_profile,
                option.session_id,
            )
            if binding.runner.session_id != option.session_id:
                await self._shutdown_binding_tasks(binding)
                raise ConfigurationError("session resume binding returned the wrong session")
            try:
                self._apply_conversation_policies(binding)
            except BaseException:
                await self._shutdown_binding_tasks(binding)
                raise
            stopped_background_tasks = await self._replace_binding(binding)
            self._selected_profile = option.resume_profile
            return self._session_selection_result(
                option,
                previous_session_id=previous_session_id,
                changed=True,
                items=self.items,
                stopped_background_tasks=stopped_background_tasks,
            )

    async def rename_session(self, title: str) -> SessionSummary:
        if self._turn_lock.locked():
            raise ConfigurationError("cannot rename a session while a turn is running")
        async with self._turn_lock:
            if self._session_rename is None:
                raise ConfigurationError("interactive session rename is unavailable")
            session_id = self.session_id
            if session_id is None:
                raise ConfigurationError("cannot rename a session before it is created")
            summary = await self._session_rename(session_id, title)
            if summary.id != session_id:
                raise ConfigurationError("session rename returned the wrong session")
            self._known_session_summaries[session_id] = summary
            return summary

    async def _replace_binding(self, binding: ConversationBinding) -> int:
        try:
            stopped = await self._shutdown_binding_tasks(self._binding)
        except BaseException:
            await self._shutdown_binding_tasks(binding)
            raise
        self._binding = binding
        return stopped

    def _apply_reasoning_effort(self, binding: ConversationBinding) -> None:
        binding.runner.set_reasoning_effort(self._reasoning_effort)

    def _apply_conversation_policies(self, binding: ConversationBinding) -> None:
        self._apply_reasoning_effort(binding)
        binding.runner.set_interaction_mode(self._interaction_mode)

    @staticmethod
    async def _shutdown_binding_tasks(binding: ConversationBinding) -> int:
        manager = binding.background_tasks
        if manager is None:
            return 0
        snapshots = await manager.list()
        running = sum(snapshot.status is BackgroundTaskStatus.RUNNING for snapshot in snapshots)
        await manager.shutdown()
        return running

    def _resume_profile(self, summary: SessionSummary) -> tuple[str, bool, bool]:
        profiles = {option.name: option for option in self._options}
        source_profile = profiles.get(summary.provider)
        if source_profile is not None and source_profile.selectable:
            return source_profile.name, True, True
        selected = profiles[self._selected_profile]
        return selected.name, False, selected.selectable

    def _session_option(
        self,
        summary: SessionSummary,
        *,
        matched_fields: tuple[str, ...] = (),
        snippet: str | None = None,
    ) -> SessionOption:
        resume_profile, source_profile_match, selectable = self._resume_profile(summary)
        current = summary.id == self.session_id
        sandbox_profile_match = (
            summary.sandbox_profile is None or summary.sandbox_profile is self._sandbox_profile
        )
        return SessionOption(
            session_id=summary.id,
            source_provider=summary.provider,
            source_model=summary.model,
            updated_at=summary.updated_at,
            resume_profile=resume_profile,
            current=current,
            source_profile_match=source_profile_match,
            selectable=current or (selectable and sandbox_profile_match),
            sandbox_profile=summary.sandbox_profile,
            sandbox_profile_match=sandbox_profile_match,
            title=summary.title,
            matched_fields=matched_fields,
            snippet=snippet,
        )

    def _selection_result(
        self,
        profile_name: str,
        *,
        previous_session_id: str | None,
        changed: bool,
        stopped_background_tasks: int = 0,
    ) -> ProviderSelectionResult:
        option = next(option for option in self._options if option.name == profile_name)
        return ProviderSelectionResult(
            profile_name=profile_name,
            provider_name=self.provider_name,
            model_name=self.model_name,
            previous_session_id=previous_session_id,
            changed=changed,
            stopped_background_tasks=stopped_background_tasks,
            context_window_tokens=option.context_window_tokens,
        )

    def _session_selection_result(
        self,
        option: SessionOption,
        *,
        previous_session_id: str | None,
        changed: bool,
        items: tuple[SessionItem, ...],
        stopped_background_tasks: int = 0,
    ) -> SessionSelectionResult:
        return SessionSelectionResult(
            session_id=option.session_id,
            source_provider=option.source_provider,
            source_model=option.source_model,
            profile_name=self._selected_profile,
            provider_name=self.provider_name,
            model_name=self.model_name,
            previous_session_id=previous_session_id,
            changed=changed,
            source_profile_match=option.source_profile_match,
            items=items,
            stopped_background_tasks=stopped_background_tasks,
            context_window_tokens=next(
                option.context_window_tokens
                for option in self._options
                if option.name == self._selected_profile
            ),
        )


__all__ = [
    "ConversationBinding",
    "InteractionModeSelectionResult",
    "ProfileConversationController",
    "ProviderOption",
    "ProviderSelectionResult",
    "ReasoningEffortSelectionResult",
    "SessionOption",
    "SessionSelectionResult",
]
