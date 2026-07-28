from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.ui_preferences import UiLanguage
from neuro_code.shared.async_utils import run_blocking

_SCHEMA_VERSION = 1


class JsonUiPreferencesStore:
    """Small atomic store for non-secret, user-scoped interface preferences."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def load_language(self) -> UiLanguage:
        language, _, _ = await run_blocking(self._load_preferences)
        return language

    async def load_reasoning_effort(self) -> ReasoningEffort:
        _, effort, _ = await run_blocking(self._load_preferences)
        return effort

    async def load_interaction_mode(self) -> InteractionMode:
        _, _, mode = await run_blocking(self._load_preferences)
        return mode

    def _load_preferences(self) -> tuple[UiLanguage, ReasoningEffort, InteractionMode]:
        try:
            payload: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return UiLanguage.ENGLISH, ReasoningEffort.HIGH, InteractionMode.NORMAL
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            return UiLanguage.ENGLISH, ReasoningEffort.HIGH, InteractionMode.NORMAL
        raw_language = payload.get("language")
        try:
            language = (
                UiLanguage(raw_language) if isinstance(raw_language, str) else UiLanguage.ENGLISH
            )
        except ValueError:
            language = UiLanguage.ENGLISH
        raw_effort = payload.get("reasoning_effort")
        try:
            effort = (
                ReasoningEffort(raw_effort) if isinstance(raw_effort, str) else ReasoningEffort.HIGH
            )
        except ValueError:
            effort = ReasoningEffort.HIGH
        raw_mode = payload.get("interaction_mode")
        try:
            mode = (
                InteractionMode(raw_mode) if isinstance(raw_mode, str) else InteractionMode.NORMAL
            )
        except ValueError:
            mode = InteractionMode.NORMAL
        return language, effort, mode

    async def save_language(self, language: UiLanguage) -> None:
        async with self._write_lock:
            await run_blocking(self._save_language, language)

    def _save_language(self, language: UiLanguage) -> None:
        _, effort, mode = self._load_preferences()
        self._save_preferences(language, effort, mode)

    async def save_reasoning_effort(self, effort: ReasoningEffort) -> None:
        async with self._write_lock:
            await run_blocking(self._save_reasoning_effort, effort)

    def _save_reasoning_effort(self, effort: ReasoningEffort) -> None:
        language, _, mode = self._load_preferences()
        self._save_preferences(language, effort, mode)

    async def save_interaction_mode(self, mode: InteractionMode) -> None:
        async with self._write_lock:
            await run_blocking(self._save_interaction_mode, mode)

    def _save_interaction_mode(self, mode: InteractionMode) -> None:
        language, effort, _ = self._load_preferences()
        self._save_preferences(language, effort, mode)

    def _save_preferences(
        self,
        language: UiLanguage,
        effort: ReasoningEffort,
        mode: InteractionMode,
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    {
                        "version": _SCHEMA_VERSION,
                        "language": language.value,
                        "reasoning_effort": effort.value,
                        "interaction_mode": mode.value,
                    },
                    temporary,
                    ensure_ascii=False,
                    indent=2,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


__all__ = ["JsonUiPreferencesStore"]
