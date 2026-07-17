from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neuro_code.config import ProviderProfile, load_config, override_provider
from neuro_code.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    @staticmethod
    def _profile(**overrides: object) -> ProviderProfile:
        values: dict[str, object] = {
            "name": "fixture",
            "protocol": "openai-chat",
            "model": "fixture-model",
            "base_url": "https://provider.invalid/v1",
            "api_key_env": "FIXTURE_KEY",
        }
        values.update(overrides)
        return ProviderProfile(**values)  # type: ignore[arg-type]

    def test_explicit_state_dir_works_without_a_discoverable_user_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "config.toml").write_text(
                '[provider.default]\nmodel = "state-model"\n',
                encoding="utf-8",
            )

            with patch(
                "neuro_code.config.Path.home",
                side_effect=RuntimeError("home unavailable"),
            ):
                config = load_config(root, environ={"NEURO_CODE_HOME": str(state_dir)})

            self.assertEqual(config.state_dir, state_dir.resolve())
            self.assertEqual(config.provider.model, "state-model")
            self.assertEqual(config.loaded_files, ((state_dir / "config.toml").resolve(),))

    def test_missing_home_and_state_dir_is_a_configuration_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "neuro_code.config.Path.home",
                side_effect=RuntimeError("home unavailable"),
            ),
            self.assertRaisesRegex(ConfigurationError, "set NEURO_CODE_HOME"),
        ):
            load_config(Path(directory), environ={})

    def test_native_project_config_overrides_legacy_user_config_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            project = root / "project"
            home.mkdir(parents=True)
            (project / ".neuro-code").mkdir(parents=True)
            cc_switch_config = home / "cc-switch.toml"
            cc_switch_config.write_text(
                """
[model.default]
model = "legacy-model"
base_url = "https://legacy.invalid/v1"
env_key = "LEGACY_KEY"
""",
                encoding="utf-8",
            )
            (project / ".neuro-code" / "config.toml").write_text(
                """
[provider.default]
kind = "openai-compatible"
model = "project-model"
base_url = "https://project.invalid/v1/"
api_key_env = "PROJECT_KEY"
timeout_seconds = 30
""",
                encoding="utf-8",
            )

            config = load_config(
                project,
                home=home,
                environ={
                    "NEURO_CODE_CC_SWITCH_CONFIG": str(cc_switch_config),
                    "NEURO_CODE_MODEL": "environment-model",
                },
            )

            self.assertEqual(config.provider.model, "environment-model")
            self.assertEqual(config.provider.base_url, "https://project.invalid/v1")
            self.assertEqual(config.provider.api_key_env, "PROJECT_KEY")
            self.assertEqual(config.provider.timeout_seconds, 30)
            self.assertEqual(len(config.loaded_files), 2)

    def test_split_legacy_provider_and_model_tables_still_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".neuro-code"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[provider.default]
kind = "openai-compatible"
timeout_seconds = 30

[model.default]
model = "legacy-model"
base_url = "https://legacy.invalid/v1"
env_key = "LEGACY_KEY"
""",
                encoding="utf-8",
            )

            config = load_config(root, home=root)
            self.assertEqual(config.provider.model, "legacy-model")
            self.assertEqual(config.provider.base_url, "https://legacy.invalid/v1")
            self.assertEqual(config.provider.api_key_env, "LEGACY_KEY")

    def test_inspect_payload_never_contains_credential_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".neuro-code"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
""",
                encoding="utf-8",
            )
            config = load_config(root, home=root, environ={"FIXTURE_KEY": "top-secret"})
            serialized = repr(config.redacted_dict())
            self.assertNotIn("top-secret", serialized)
            self.assertIn("credential_configured", serialized)

    def test_proxy_modes_are_strict_resolved_and_secret_redacted(self) -> None:
        invalid_profiles = (
            ({"proxy_mode": "automatic"}, "proxy_mode"),
            ({"proxy_mode": "explicit"}, "requires proxy_url_env"),
            ({"proxy_mode": "direct", "proxy_url_env": "PROXY_URL"}, "requires proxy_mode"),
        )
        for overrides, expected in invalid_profiles:
            with (
                self.subTest(overrides=overrides),
                self.assertRaisesRegex(ConfigurationError, expected),
            ):
                self._profile(**overrides)

        direct = self._profile(proxy_mode="direct").http_client_policy(
            {"ALL_PROXY": "socks://127.0.0.1:7890"}
        )
        self.assertFalse(direct.trust_env)
        self.assertIsNone(direct.proxy_url)
        self.assertEqual(direct.client_options(timeout=5), {"timeout": 5, "trust_env": False})

        secret_proxy = "http://proxy-user:proxy-secret@127.0.0.1:8080"
        explicit_profile = self._profile(
            proxy_mode="explicit",
            proxy_url_env="FIXTURE_PROXY_URL",
        )
        with self.assertRaisesRegex(ConfigurationError, "FIXTURE_PROXY_URL"):
            explicit_profile.http_client_policy({})
        explicit = explicit_profile.http_client_policy({"FIXTURE_PROXY_URL": secret_proxy})
        self.assertFalse(explicit.trust_env)
        self.assertEqual(explicit.proxy_url, secret_proxy)
        self.assertNotIn("proxy-secret", repr(explicit))
        self.assertNotIn(
            "proxy-secret",
            explicit.redact(f"failed through {secret_proxy}: proxy-secret"),
        )
        inspection = explicit_profile.redacted_dict({"FIXTURE_PROXY_URL": secret_proxy})
        self.assertEqual(inspection["proxy_mode"], "explicit")
        self.assertEqual(inspection["proxy_url_env"], "FIXTURE_PROXY_URL")
        self.assertTrue(inspection["proxy_url_configured"])
        self.assertNotIn("proxy-secret", repr(inspection))

    def test_environment_proxy_errors_are_actionable_without_leaking_urls(self) -> None:
        profile = self._profile()
        secret_proxy = "socks://proxy-user:proxy-secret@127.0.0.1:7890/"
        with self.assertRaisesRegex(ConfigurationError, "ALL_PROXY.*socks") as raised:
            profile.http_client_policy({"ALL_PROXY": secret_proxy})
        self.assertNotIn("proxy-secret", str(raised.exception))
        self.assertNotIn("127.0.0.1", str(raised.exception))

        invalid_values = (
            "ftp://127.0.0.1:21",
            "http://127.0.0.1:8080/proxy-path",
            "http://127.0.0.1:not-a-port",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                profile.http_client_policy({"HTTPS_PROXY": value})

        with (
            patch("neuro_code.config.find_spec", return_value=None),
            self.assertRaisesRegex(ConfigurationError, "optional SOCKS support"),
        ):
            profile.http_client_policy({"ALL_PROXY": "socks5://127.0.0.1:7890"})

        inherited = profile.http_client_policy({"HTTPS_PROXY": "http://127.0.0.1:8080"})
        self.assertTrue(inherited.trust_env)
        self.assertIsNone(inherited.proxy_url)

    def test_no_configuration_has_no_implicit_xai_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root, home=root, environ={})

            self.assertIsNone(config.selected_provider)
            self.assertEqual(config.providers, {})
            with self.assertRaisesRegex(ConfigurationError, "no model provider is configured"):
                _ = config.provider

    def test_native_provider_defaults_do_not_inherit_legacy_endpoint(self) -> None:
        cases = (
            (
                "xai-responses",
                "fixture-model",
                "https://api.x.ai/v1",
                "XAI_API_KEY",
            ),
            (
                "anthropic",
                "claude-fixture",
                "https://api.anthropic.com",
                "ANTHROPIC_API_KEY",
            ),
            (
                "gemini",
                "gemini-fixture",
                "https://generativelanguage.googleapis.com/v1beta",
                "GEMINI_API_KEY",
            ),
        )
        for kind, model, expected_url, expected_env in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cc_switch_config = root / "cc-switch.toml"
                cc_switch_config.write_text(
                    """
[model.default]
model = "legacy-model"
base_url = "https://legacy.invalid/v1"
env_key = "LEGACY_KEY"
""",
                    encoding="utf-8",
                )
                state = root / ".neuro-code"
                state.mkdir()
                (state / "config.toml").write_text(
                    f"""
[provider.default]
kind = "{kind}"
model = "{model}"
max_output_tokens = 2048
""",
                    encoding="utf-8",
                )

                config = load_config(
                    root,
                    home=root,
                    environ={"NEURO_CODE_CC_SWITCH_CONFIG": str(cc_switch_config)},
                )

                self.assertEqual(config.provider.base_url, expected_url)
                self.assertEqual(config.provider.api_key_env, expected_env)
                self.assertEqual(config.provider.model, model)
                self.assertEqual(config.provider.max_output_tokens, 2048)

    def test_native_provider_requires_model_and_numeric_limits_are_validated(self) -> None:
        invalid_tables = (
            ('kind = "anthropic"', "requires an explicit model"),
            ('kind = "xai-responses"', "requires an explicit model"),
            ('model = "fixture-model"\ntimeout_seconds = "slow"', "must be a number"),
            ('model = "fixture-model"\nmax_output_tokens = 0', "must be positive"),
            ('model = "fixture-model"\nmax_output_tokens = 1.5', "must be an integer"),
        )
        for table, expected in invalid_tables:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / ".neuro-code"
                state.mkdir()
                (state / "config.toml").write_text(
                    f"[provider.default]\n{table}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_config(root, home=root)

    def test_xai_builtin_tools_are_loaded_and_redacted_for_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".neuro-code"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[provider.default]
kind = "xai-responses"
model = "fixture-model"
builtin_tools = ["web_search", "x_search", "code_interpreter"]
""",
                encoding="utf-8",
            )

            config = load_config(root, home=root)

            self.assertEqual(
                config.provider.builtin_tools,
                ("web_search", "x_search", "code_interpreter"),
            )
            self.assertEqual(
                config.redacted_dict()["provider"]["builtin_tools"],
                ["web_search", "x_search", "code_interpreter"],
            )

    def test_xai_builtin_tools_are_strictly_validated(self) -> None:
        invalid_tables = (
            ('kind = "xai-responses"\nbuiltin_tools = "web_search"', "TOML array"),
            (
                'kind = "xai-responses"\nbuiltin_tools = ["web_search", 1]',
                "non-empty strings",
            ),
            (
                'kind = "xai-responses"\nbuiltin_tools = ["web_search", "web_search"]',
                "duplicates",
            ),
            (
                'kind = "xai-responses"\nbuiltin_tools = ["file_search"]',
                "unsupported xAI builtin_tools",
            ),
            (
                'kind = "openai-compatible"\nbuiltin_tools = ["web_search"]',
                "require dialect 'xai'",
            ),
        )
        for table, expected in invalid_tables:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / ".neuro-code"
                state.mkdir()
                (state / "config.toml").write_text(
                    f'[provider.default]\nmodel = "fixture-model"\n{table}\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_config(root, home=root)

    def test_named_profiles_and_cli_style_selection_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".neuro-code"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "deepseek"
fallbacks = ["xai"]

[providers.deepseek]
protocol = "openai-chat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
proxy_mode = "explicit"
proxy_url_env = "DEEPSEEK_PROXY_URL"

[providers.xai]
protocol = "openai-responses"
dialect = "xai"
model = "fixture-model"
base_url = "https://api.x.ai/v1"
api_key_env = "XAI_API_KEY"
builtin_tools = ["web_search"]
""",
                encoding="utf-8",
            )

            config = load_config(root, home=root)
            self.assertEqual(config.selected_provider, "deepseek")
            self.assertEqual(config.fallback_providers, ("xai",))
            self.assertEqual(config.provider.protocol, "openai-chat")
            self.assertEqual(config.provider.proxy_mode, "explicit")
            self.assertEqual(config.provider.proxy_url_env, "DEEPSEEK_PROXY_URL")
            self.assertEqual(config.redacted_dict()["routing"]["fallbacks"], ["xai"])
            selected = override_provider(config, provider="xai", model="override-model")
            self.assertEqual(selected.provider.name, "xai")
            self.assertEqual(selected.provider.model, "override-model")
            self.assertIsNotNone(selected.provider.context_affinity)

    def test_routing_fallbacks_are_strictly_validated(self) -> None:
        cases = (
            ('fallbacks = "second"', "TOML array"),
            ('fallbacks = ["   "]', "non-empty strings"),
            ('fallbacks = ["second", "second"]', "duplicates"),
            ('fallbacks = ["missing"]', "do not exist"),
            ('fallbacks = ["first"]', "must not include the default"),
        )
        for routing, expected in cases:
            with self.subTest(routing=routing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / ".neuro-code"
                state.mkdir()
                (state / "config.toml").write_text(
                    f"""
[routing]
default = "first"
{routing}

[providers.first]
protocol = "openai-chat"
model = "first-model"
base_url = "https://first.invalid/v1"
api_key_env = "FIRST_KEY"

[providers.second]
protocol = "openai-chat"
model = "second-model"
base_url = "https://second.invalid/v1"
api_key_env = "SECOND_KEY"
""",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_config(root, home=root)

    def test_cc_switch_active_profile_is_read_only_low_priority_configuration(self) -> None:
        cases = (
            ("responses", "openai-responses"),
            ("chat_completions", "openai-chat"),
            ("messages", "anthropic-messages"),
        )
        for backend, protocol in cases:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cc_switch_config = root / "cc-switch.toml"
                cc_switch_config.write_text(
                    f"""
[models]
default = "relay"

[model.relay]
model = "upstream-model"
base_url = "http://127.0.0.1:15721/provider/v1"
api_key = "PROXY_MANAGED"
api_backend = "{backend}"
""",
                    encoding="utf-8",
                )

                config = load_config(
                    root,
                    home=root,
                    environ={"NEURO_CODE_CC_SWITCH_CONFIG": str(cc_switch_config)},
                )
                profile = config.provider
                self.assertEqual(profile.name, "cc-switch:relay")
                self.assertEqual(profile.protocol, protocol)
                self.assertEqual(profile.auth, "proxy-managed")
                self.assertEqual(profile.api_key(), "PROXY_MANAGED")
                self.assertEqual(profile.native_context, "disabled")

    def test_cc_switch_inline_key_is_never_exposed_or_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cc_switch_config = root / "cc-switch.toml"
            cc_switch_config.write_text(
                """
[models]
default = "inline"

[model.inline]
model = "upstream-model"
base_url = "https://relay.invalid/v1"
api_key = "plain-secret"
api_backend = "responses"
""",
                encoding="utf-8",
            )

            config = load_config(
                root,
                home=root,
                environ={"NEURO_CODE_CC_SWITCH_CONFIG": str(cc_switch_config)},
            )
            profile = config.provider
            self.assertFalse(profile.available)
            self.assertNotIn("plain-secret", repr(config.redacted_dict()))
            with self.assertRaisesRegex(ConfigurationError, "inline API key"):
                profile.api_key()

    def test_native_project_routing_overrides_cc_switch_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cc_switch_config = root / "cc-switch.toml"
            cc_switch_config.write_text(
                """
[models]
default = "relay"
[model.relay]
model = "relay-model"
base_url = "http://127.0.0.1:15721/provider/v1"
api_key = "PROXY_MANAGED"
""",
                encoding="utf-8",
            )
            project = root / ".neuro-code"
            project.mkdir()
            (project / "config.toml").write_text(
                """
[routing]
default = "direct"
[providers.direct]
protocol = "openai-chat"
model = "direct-model"
base_url = "https://direct.invalid/v1"
api_key_env = "DIRECT_KEY"
""",
                encoding="utf-8",
            )

            config = load_config(
                root,
                home=root,
                environ={"NEURO_CODE_CC_SWITCH_CONFIG": str(cc_switch_config)},
            )
            self.assertEqual(config.selected_provider, "direct")
            self.assertIn("cc-switch:relay", config.providers)


if __name__ == "__main__":
    unittest.main()
