"""Canonical UI-preferences port.

定义规范的 UI 偏好端口."""

from __future__ import annotations

from typing import Protocol

from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.shared.ui_language import UiLanguage


class UiPreferencesStore(Protocol):
    """Persist interactive user preferences outside provider and project config.

    在 Provider 配置和项目配置之外持久化交互式用户偏好."""

    async def load_language(self) -> UiLanguage: ...

    async def save_language(self, language: UiLanguage) -> None: ...

    async def load_reasoning_effort(self) -> ReasoningEffort: ...

    async def save_reasoning_effort(self, effort: ReasoningEffort) -> None: ...

    async def load_interaction_mode(self) -> InteractionMode: ...

    async def save_interaction_mode(self, mode: InteractionMode) -> None: ...


__all__ = ["UiPreferencesStore"]
