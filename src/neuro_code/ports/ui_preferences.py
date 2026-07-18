from __future__ import annotations

from typing import Protocol

from neuro_code.domain.ui_preferences import UiLanguage


class UiPreferencesStore(Protocol):
    """Persist presentation preferences outside provider and project config."""

    async def load_language(self) -> UiLanguage: ...

    async def save_language(self, language: UiLanguage) -> None: ...


__all__ = ["UiPreferencesStore"]
