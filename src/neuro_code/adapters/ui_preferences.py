from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from neuro_code.async_utils import run_blocking
from neuro_code.domain.ui_preferences import UiLanguage

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
        return await run_blocking(self._load_language)

    def _load_language(self) -> UiLanguage:
        try:
            payload: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return UiLanguage.ENGLISH
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            return UiLanguage.ENGLISH
        raw_language = payload.get("language")
        if not isinstance(raw_language, str):
            return UiLanguage.ENGLISH
        try:
            return UiLanguage(raw_language)
        except ValueError:
            return UiLanguage.ENGLISH

    async def save_language(self, language: UiLanguage) -> None:
        async with self._write_lock:
            await run_blocking(self._save_language, language)

    def _save_language(self, language: UiLanguage) -> None:
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
                    {"version": _SCHEMA_VERSION, "language": language.value},
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
