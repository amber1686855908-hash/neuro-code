from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from neuro_code.adapters.ui_preferences import JsonUiPreferencesStore
from neuro_code.domain.ui_preferences import UiLanguage


class JsonUiPreferencesStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_invalid_preferences_fall_back_to_english(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-preferences.json"
            store = JsonUiPreferencesStore(path)

            self.assertEqual(await store.load_language(), UiLanguage.ENGLISH)
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(await store.load_language(), UiLanguage.ENGLISH)
            path.write_text(
                json.dumps({"version": 1, "language": "unsupported"}),
                encoding="utf-8",
            )
            self.assertEqual(await store.load_language(), UiLanguage.ENGLISH)

    async def test_language_is_saved_atomically_with_private_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "ui-preferences.json"
            store = JsonUiPreferencesStore(path)

            await store.save_language(UiLanguage.SIMPLIFIED_CHINESE)

            self.assertEqual(
                await JsonUiPreferencesStore(path).load_language(),
                UiLanguage.SIMPLIFIED_CHINESE,
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"version": 1, "language": "zh-CN"},
            )
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
