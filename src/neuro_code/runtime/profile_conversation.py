from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from neuro_code.errors import ConfigurationError
from neuro_code.ports.model import ModelProvider
from neuro_code.runtime.agent import AgentRunResult, EventSink


class ConversationRunner(Protocol):
    @property
    def session_id(self) -> str | None: ...

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


@dataclass(frozen=True, slots=True)
class ProviderSelectionResult:
    profile_name: str
    provider_name: str
    model_name: str
    previous_session_id: str | None
    changed: bool


BindingFactory = Callable[[str], Awaitable[ConversationBinding]]


class ProfileConversationController:
    """Serialize turns and replace the conversation at a safe profile boundary."""

    def __init__(
        self,
        *,
        options: Sequence[ProviderOption],
        selected_profile: str,
        binding: ConversationBinding,
        binding_factory: BindingFactory,
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

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
        async with self._turn_lock:
            return await self._binding.runner.run(prompt, sink=sink)

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
                raise ConfigurationError("provider profile switch must create a fresh conversation")
            self._binding = binding
            self._selected_profile = name
            return self._selection_result(
                name,
                previous_session_id=previous_session_id,
                changed=True,
            )

    def _selection_result(
        self,
        profile_name: str,
        *,
        previous_session_id: str | None,
        changed: bool,
    ) -> ProviderSelectionResult:
        return ProviderSelectionResult(
            profile_name,
            self.provider_name,
            self.model_name,
            previous_session_id,
            changed,
        )


__all__ = [
    "ConversationBinding",
    "ProfileConversationController",
    "ProviderOption",
    "ProviderSelectionResult",
]
