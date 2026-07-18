from __future__ import annotations

from typing import Protocol

from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.ui_preferences import UiLanguage


class UiPreferencesStore(Protocol):
    """Persist interactive user preferences outside provider and project config."""

    async def load_language(self) -> UiLanguage: ...

    async def save_language(self, language: UiLanguage) -> None: ...

    async def load_reasoning_effort(self) -> ReasoningEffort: ...

    async def save_reasoning_effort(self, effort: ReasoningEffort) -> None: ...

    async def load_interaction_mode(self) -> InteractionMode: ...

    async def save_interaction_mode(self, mode: InteractionMode) -> None: ...


__all__ = ["UiPreferencesStore"]
