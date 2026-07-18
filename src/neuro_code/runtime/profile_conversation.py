from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.messages import SessionItem
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.sessions import SessionSummary
from neuro_code.errors import ConfigurationError
from neuro_code.ports.background_tasks import BackgroundTaskManager
from neuro_code.ports.model import ModelProvider
from neuro_code.runtime.agent import AgentRunResult, EventSink


class ConversationRunner(Protocol):
    @property
    def session_id(self) -> str | None: ...

    @property
    def items(self) -> tuple[SessionItem, ...]: ...

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult: ...


@dataclass(frozen=True, slots=True)
class ProviderOption:
    name: str
    protocol: str
    model: str
    available: bool
    credential_configured: bool
    default: bool = False
    selected: bool = False

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


BindingFactory = Callable[[str], Awaitable[ConversationBinding]]
SessionCatalog = Callable[[], Awaitable[Sequence[SessionSummary]]]
SessionBindingFactory = Callable[[str, str], Awaitable[ConversationBinding]]


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
        session_binding_factory: SessionBindingFactory | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
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
        self._session_binding_factory = session_binding_factory
        self._sandbox_profile = sandbox_profile
        self._turn_lock = asyncio.Lock()

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
    def session_id(self) -> str | None:
        return self._binding.runner.session_id

    @property
    def items(self) -> tuple[SessionItem, ...]:
        return self._binding.runner.items

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        async with self._turn_lock:
            return await self._binding.runner.run(prompt, sink=sink)

    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]:
        manager = self._binding.background_tasks
        if manager is None:
            raise ConfigurationError("background task visibility is unavailable")
        return await manager.list()

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
            stopped_background_tasks = await self._replace_binding(binding)
            self._selected_profile = name
            return self._selection_result(
                name,
                previous_session_id=previous_session_id,
                changed=True,
                stopped_background_tasks=stopped_background_tasks,
            )

    async def list_sessions(self) -> tuple[SessionOption, ...]:
        if self._session_catalog is None:
            raise ConfigurationError("interactive session resume is unavailable")

        summaries = await self._session_catalog()
        options: list[SessionOption] = []
        seen_ids: set[str] = set()
        for summary in summaries:
            if summary.id in seen_ids:
                continue
            seen_ids.add(summary.id)
            resume_profile, source_profile_match, selectable = self._resume_profile(summary)
            current = summary.id == self.session_id
            sandbox_profile_match = (
                summary.sandbox_profile is None or summary.sandbox_profile is self._sandbox_profile
            )
            options.append(
                SessionOption(
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
                )
            )
        return tuple(options)

    async def select_session(self, session_id: str) -> SessionSelectionResult:
        if self._turn_lock.locked():
            raise ConfigurationError("cannot resume a session while a turn is running")
        async with self._turn_lock:
            if self._session_binding_factory is None:
                raise ConfigurationError("interactive session resume is unavailable")
            options = {option.session_id: option for option in await self.list_sessions()}
            option = options.get(session_id)
            if option is None:
                raise ConfigurationError(
                    f"session does not exist in the current workspace: {session_id}"
                )
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
            stopped_background_tasks = await self._replace_binding(binding)
            self._selected_profile = option.resume_profile
            return self._session_selection_result(
                option,
                previous_session_id=previous_session_id,
                changed=True,
                items=self.items,
                stopped_background_tasks=stopped_background_tasks,
            )

    async def _replace_binding(self, binding: ConversationBinding) -> int:
        try:
            stopped = await self._shutdown_binding_tasks(self._binding)
        except BaseException:
            await self._shutdown_binding_tasks(binding)
            raise
        self._binding = binding
        return stopped

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

    def _selection_result(
        self,
        profile_name: str,
        *,
        previous_session_id: str | None,
        changed: bool,
        stopped_background_tasks: int = 0,
    ) -> ProviderSelectionResult:
        return ProviderSelectionResult(
            profile_name,
            self.provider_name,
            self.model_name,
            previous_session_id,
            changed,
            stopped_background_tasks,
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
        )


__all__ = [
    "ConversationBinding",
    "ProfileConversationController",
    "ProviderOption",
    "ProviderSelectionResult",
    "SessionOption",
    "SessionSelectionResult",
]
