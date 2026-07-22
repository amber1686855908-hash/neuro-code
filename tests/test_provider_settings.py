from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from neuro_code.adapters.provider_settings import JsonProviderSettingsStore
from neuro_code.domain.provider_settings import ManagedProviderProfile
from neuro_code.errors import ConfigurationError


class JsonProviderSettingsStoreTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _profile(
        name: str = "openai",
        *,
        api_key: str | None = "secret-value",
        model: str = "fixture-model",
    ) -> ManagedProviderProfile:
        return ManagedProviderProfile(
            name=name,
            protocol="openai-responses",
            dialect="standard",
            model=model,
            base_url="https://provider.invalid/v1",
            api_key=api_key,
        )

    async def test_profiles_and_credentials_are_separate_private_atomic_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            store = JsonProviderSettingsStore(state_dir)

            saved = await store.save_profile(self._profile())

            self.assertEqual(saved.default_provider, "openai")
            metadata = store.metadata_path.read_text(encoding="utf-8")
            credentials = store.credentials_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-value", metadata)
            self.assertIn("secret-value", credentials)
            self.assertNotIn("secret-value", repr(saved))
            self.assertEqual(list(state_dir.glob("*.tmp")), [])
            if os.name == "posix":
                self.assertEqual(store.metadata_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.credentials_path.stat().st_mode & 0o777, 0o600)

    async def test_multiple_profiles_can_be_updated_and_selected_without_retyping_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            await store.save_profile(self._profile("first"))
            await store.save_profile(self._profile("second", api_key="second-secret"))
            updated = await store.save_profile(
                self._profile("first", api_key=None, model="updated-model"),
                make_default=False,
            )
            selected = await store.set_default("first")

            self.assertEqual([profile.name for profile in updated.profiles], ["first", "second"])
            first = selected.profile("first")
            assert first is not None
            self.assertEqual(first.model, "updated-model")
            self.assertEqual(first.api_key, "secret-value")
            self.assertEqual(selected.default_provider, "first")

    async def test_new_profile_requires_key_and_invalid_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = JsonProviderSettingsStore(state_dir)
            with self.assertRaisesRegex(ConfigurationError, "requires an API key"):
                await store.save_profile(self._profile(api_key=None))

            store.metadata_path.write_text(
                json.dumps({"version": 999, "providers": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "unsupported schema"):
                await store.load()

    async def test_delete_profile_removes_its_credential_and_selects_a_safe_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            await store.save_profile(self._profile("first"))
            await store.save_profile(self._profile("second", api_key="second-secret"))

            remaining = await store.delete_profile("second")

            self.assertEqual(remaining.default_provider, "first")
            self.assertEqual([profile.name for profile in remaining.profiles], ["first"])
            credentials = json.loads(store.credentials_path.read_text(encoding="utf-8"))
            self.assertEqual(credentials["api_keys"], {"first": "secret-value"})

            empty = await store.delete_profile("first")
            self.assertEqual(empty.profiles, ())
            self.assertIsNone(empty.default_provider)
            with self.assertRaisesRegex(ConfigurationError, "does not exist"):
                await store.delete_profile("missing")


if __name__ == "__main__":
    unittest.main()
