from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from neuro_code.application.ports.model import CapabilityStatus, ModelCapability, ModelCapabilitySet
from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProxyPolicy,
)
from neuro_code.configuration.app import ProviderProfile
from neuro_code.configuration.managed_provider_settings import (
    load_managed_provider_settings as canonical_load_managed_provider_settings,
)
from neuro_code.domain.background_tasks import BackgroundTaskWakePolicy
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore
from neuro_code.shared.errors import ConfigurationError


class JsonProviderSettingsStoreTests(unittest.IsolatedAsyncioTestCase):
    def test_loader_is_public_only_from_the_canonical_reader(self) -> None:
        import importlib.util

        import neuro_code.infrastructure.providers.provider_settings as provider_settings

        self.assertEqual(provider_settings.__all__, ["JsonProviderSettingsStore"])
        self.assertFalse(hasattr(provider_settings, "load_managed_provider_settings"))
        self.assertIsNone(importlib.util.find_spec("neuro_code.config"))
        self.assertEqual(
            canonical_load_managed_provider_settings.__module__,
            "neuro_code.configuration.managed_provider_settings",
        )

    @staticmethod
    def _profile(
        name: str = "openai",
        *,
        api_key: str | None = "secret-value",
        model: str = "fixture-model",
        proxy_mode: str | None = None,
        proxy_url_env: str | None = None,
        background_task_wake_policy: BackgroundTaskWakePolicy | None = None,
    ) -> ManagedProviderProfile:
        return ManagedProviderProfile(
            name=name,
            protocol="openai-responses",
            dialect="standard",
            model=model,
            base_url="https://provider.invalid/v1",
            proxy_mode=proxy_mode,
            proxy_url_env=proxy_url_env,
            api_key=api_key,
            background_task_wake_policy=background_task_wake_policy,
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
            metadata_payload = json.loads(metadata)
            credentials_payload = json.loads(credentials)
            self.assertEqual(metadata_payload["version"], 2)
            self.assertEqual(metadata_payload["providers"][0]["dialect"], "standard")
            self.assertEqual(credentials_payload["version"], 2)
            self.assertNotIn("secret-value", repr(saved))
            self.assertEqual(list(state_dir.glob("*.tmp")), [])
            if os.name == "posix":
                self.assertEqual(store.metadata_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.credentials_path.stat().st_mode & 0o777, 0o600)

    async def test_persisted_supported_claim_cannot_enable_unimplemented_hosted_wire_behavior(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            await store.save_profile(
                ManagedProviderProfile(
                    name="xai-claims",
                    protocol="openai-responses",
                    dialect="xai",
                    service_id="xai",
                    model="fixture-model",
                    base_url="https://api.x.ai/v1",
                    capability_overrides=ModelCapabilitySet.from_mapping(
                        {ModelCapability.HOSTED_WEB_SEARCH: CapabilityStatus.SUPPORTED}
                    ),
                    api_key="secret-value",
                )
            )
            loaded = await store.load()
            persisted = loaded.profile("xai-claims")
            assert persisted is not None
            self.assertTrue(
                persisted.capability_overrides.supports(ModelCapability.HOSTED_WEB_SEARCH)
            )

            profile = ProviderProfile(
                name=persisted.name,
                protocol=persisted.protocol,
                dialect=persisted.dialect,
                service_id=persisted.service_id,
                model=persisted.model,
                base_url=persisted.base_url,
                api_key_env="XAI_API_KEY",
                capability_overrides=persisted.capability_overrides,
            )
            effective = profile.effective_capabilities(
                OpenAIResponsesProvider.implementation_capabilities(
                    dialect="xai",
                    builtin_tools=(),
                )
            )

        self.assertEqual(
            effective.status(ModelCapability.HOSTED_WEB_SEARCH),
            CapabilityStatus.UNKNOWN,
        )
        self.assertFalse(effective.supports(ModelCapability.HOSTED_WEB_SEARCH))

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

    async def test_proxy_policy_round_trips_without_persisting_a_proxy_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            saved = await store.save_profile(
                self._profile(
                    proxy_mode="explicit",
                    proxy_url_env="NEURO_PROVIDER_PROXY_URL",
                )
            )

            profile = saved.profiles[0]
            self.assertEqual(profile.proxy_mode, "explicit")
            self.assertEqual(profile.proxy_url_env, "NEURO_PROVIDER_PROXY_URL")
            metadata = json.loads(store.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["providers"][0]["proxy_mode"], "explicit")
            self.assertEqual(
                metadata["providers"][0]["proxy_url_env"],
                "NEURO_PROVIDER_PROXY_URL",
            )

    async def test_legacy_managed_profile_inherits_the_environment_proxy_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = JsonProviderSettingsStore(state_dir)
            store.metadata_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "default_provider": "legacy",
                        "providers": [
                            {
                                "name": "legacy",
                                "protocol": "openai-chat",
                                "dialect": "standard",
                                "model": "legacy-model",
                                "base_url": "https://provider.invalid/v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store.credentials_path.write_text(
                json.dumps({"version": 1, "api_keys": {"legacy": "secret"}}),
                encoding="utf-8",
            )

            loaded = await store.load()

            self.assertIsNone(loaded.profiles[0].proxy_mode)
            self.assertIsNone(loaded.profiles[0].proxy_url_env)
            self.assertEqual(loaded.proxy_defaults, ManagedProxyPolicy())

    async def test_legacy_official_deepseek_profile_without_dialect_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = JsonProviderSettingsStore(state_dir)
            store.metadata_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "default_provider": "deepseek",
                        "providers": [
                            {
                                "name": "deepseek",
                                "protocol": "openai-chat",
                                "model": "deepseek-v4-flash",
                                "base_url": "https://api.deepseek.com",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store.credentials_path.write_text(
                json.dumps({"version": 1, "api_keys": {"deepseek": "secret"}}),
                encoding="utf-8",
            )

            loaded = await store.load()
            profile = loaded.profile("deepseek")
            assert profile is not None
            self.assertEqual(profile.dialect, "deepseek-v4")

            await store.save_profile(profile)
            metadata = json.loads(store.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["version"], 2)
            self.assertEqual(metadata["providers"][0]["dialect"], "deepseek-v4")

    async def test_legacy_managed_profiles_without_dialect_use_conservative_defaults(self) -> None:
        cases = (
            (
                "ordinary",
                "ordinary-provider",
                "fixture-model",
                "https://provider.invalid/v1",
                "standard",
            ),
            (
                "marked-proxy",
                "deepseek-proxy",
                "v4-flash",
                "https://llm.company.com/v1",
                "deepseek-v4",
            ),
            (
                "ambiguous-proxy",
                "company-proxy",
                "v4-flash",
                "https://llm.company.com/v1",
                "standard",
            ),
        )
        for name, provider_name, model, base_url, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                store = JsonProviderSettingsStore(state_dir)
                store.metadata_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "default_provider": provider_name,
                            "providers": [
                                {
                                    "name": provider_name,
                                    "protocol": "openai-chat",
                                    "model": model,
                                    "base_url": base_url,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                store.credentials_path.write_text(
                    json.dumps({"version": 1, "api_keys": {provider_name: "secret"}}),
                    encoding="utf-8",
                )

                loaded = await store.load()
                profile = loaded.profile(provider_name)
                assert profile is not None
                self.assertEqual(profile.dialect, expected)

    async def test_explicit_managed_dialect_wins_in_new_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = JsonProviderSettingsStore(state_dir)
            store.metadata_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "default_provider": "standard-deepseek",
                        "providers": [
                            {
                                "name": "standard-deepseek",
                                "protocol": "openai-chat",
                                "dialect": "standard",
                                "model": "deepseek-v4-flash",
                                "base_url": "https://api.deepseek.com",
                            },
                            {
                                "name": "explicit-deepseek",
                                "protocol": "openai-chat",
                                "dialect": "deepseek-v4",
                                "model": "deepseek-v4-flash",
                                "base_url": "https://proxy.invalid/v1",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store.credentials_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "api_keys": {
                            "standard-deepseek": "secret",
                            "explicit-deepseek": "secret",
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = await store.load()
            profile = loaded.profile("standard-deepseek")
            assert profile is not None
            self.assertEqual(profile.dialect, "standard")
            explicit = loaded.profile("explicit-deepseek")
            assert explicit is not None
            self.assertEqual(explicit.dialect, "deepseek-v4")

    async def test_global_proxy_default_and_provider_override_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            await store.save_proxy_defaults(ManagedProxyPolicy("explicit", "NEURO_CODE_PROXY_URL"))
            settings = await store.save_profile(self._profile(proxy_mode="direct"))

            self.assertEqual(
                settings.proxy_defaults,
                ManagedProxyPolicy("explicit", "NEURO_CODE_PROXY_URL"),
            )
            self.assertEqual(settings.profiles[0].proxy_mode, "direct")
            metadata = json.loads(store.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["proxy_defaults"]["mode"], "explicit")
            self.assertEqual(
                metadata["proxy_defaults"]["proxy_url_env"],
                "NEURO_CODE_PROXY_URL",
            )

    async def test_global_background_wake_default_and_provider_override_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            await store.save_background_task_wake_policy(BackgroundTaskWakePolicy.ENABLED)
            settings = await store.save_profile(
                self._profile(
                    name="quiet",
                    background_task_wake_policy=BackgroundTaskWakePolicy.DISABLED,
                )
            )

            self.assertEqual(settings.background_task_wake_policy, BackgroundTaskWakePolicy.ENABLED)
            self.assertEqual(
                settings.effective_background_task_wake_policy("quiet"),
                BackgroundTaskWakePolicy.DISABLED,
            )
            self.assertEqual(
                settings.effective_background_task_wake_policy("inherited"),
                BackgroundTaskWakePolicy.ENABLED,
            )
            metadata = json.loads(store.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["background_task_wake_policy"], "enabled")
            self.assertEqual(
                metadata["providers"][0]["background_task_wake_policy"],
                "disabled",
            )

    async def test_legacy_background_wake_settings_default_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = JsonProviderSettingsStore(state_dir)
            store.metadata_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "default_provider": "legacy",
                        "providers": [
                            {
                                "name": "legacy",
                                "protocol": "openai-chat",
                                "dialect": "standard",
                                "model": "legacy-model",
                                "base_url": "https://provider.invalid/v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store.credentials_path.write_text(
                json.dumps({"version": 1, "api_keys": {"legacy": "secret"}}),
                encoding="utf-8",
            )

            loaded = await store.load()

            self.assertEqual(
                loaded.background_task_wake_policy,
                BackgroundTaskWakePolicy.DISABLED,
            )
            self.assertIsNone(loaded.profiles[0].background_task_wake_policy)

    async def test_context_window_capacity_round_trips_as_non_secret_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonProviderSettingsStore(Path(directory))
            saved = await store.save_profile(
                ManagedProviderProfile(
                    name="context-aware",
                    protocol="openai-chat",
                    model="fixture-model",
                    base_url="https://provider.invalid/v1",
                    context_window_tokens=128_000,
                    api_key="secret-value",
                )
            )

            self.assertEqual(saved.profiles[0].context_window_tokens, 128_000)
            metadata = json.loads(store.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["providers"][0]["context_window_tokens"], 128_000)
            self.assertNotIn("secret-value", store.metadata_path.read_text(encoding="utf-8"))

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

        with self.assertRaisesRegex(ConfigurationError, "explicit proxy"):
            self._profile(proxy_mode="explicit")
        with self.assertRaisesRegex(ConfigurationError, "requires proxy_mode"):
            self._profile(proxy_mode="direct", proxy_url_env="PROXY_URL")

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
