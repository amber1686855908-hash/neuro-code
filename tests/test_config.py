from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pygrok_build.config import load_config
from pygrok_build.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
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
                "pygrok_build.config.Path.home",
                side_effect=RuntimeError("home unavailable"),
            ):
                config = load_config(root, environ={"PYGROK_HOME": str(state_dir)})

            self.assertEqual(config.state_dir, state_dir.resolve())
            self.assertEqual(config.provider.model, "state-model")
            self.assertEqual(config.loaded_files, (state_dir / "config.toml",))

    def test_missing_home_and_state_dir_is_a_configuration_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "pygrok_build.config.Path.home",
                side_effect=RuntimeError("home unavailable"),
            ),
            self.assertRaisesRegex(ConfigurationError, "set PYGROK_HOME"),
        ):
            load_config(Path(directory), environ={})

    def test_native_project_config_overrides_legacy_user_config_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            project = root / "project"
            (home / ".grok").mkdir(parents=True)
            (project / ".pygrok-build").mkdir(parents=True)
            (home / ".grok" / "config.toml").write_text(
                """
[model.default]
model = "legacy-model"
base_url = "https://legacy.invalid/v1"
env_key = "LEGACY_KEY"
""",
                encoding="utf-8",
            )
            (project / ".pygrok-build" / "config.toml").write_text(
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
                environ={"PYGROK_MODEL": "environment-model"},
            )

            self.assertEqual(config.provider.model, "environment-model")
            self.assertEqual(config.provider.base_url, "https://project.invalid/v1")
            self.assertEqual(config.provider.api_key_env, "PROJECT_KEY")
            self.assertEqual(config.provider.timeout_seconds, 30)
            self.assertEqual(len(config.loaded_files), 2)

    def test_inspect_payload_never_contains_credential_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root, home=root, environ={"XAI_API_KEY": "top-secret"})
            serialized = repr(config.redacted_dict())
            self.assertNotIn("top-secret", serialized)
            self.assertIn("credential_configured", serialized)

    def test_native_provider_defaults_do_not_inherit_legacy_endpoint(self) -> None:
        cases = (
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
                (root / ".grok").mkdir()
                (root / ".grok" / "config.toml").write_text(
                    """
[model.default]
model = "legacy-model"
base_url = "https://legacy.invalid/v1"
env_key = "LEGACY_KEY"
""",
                    encoding="utf-8",
                )
                state = root / ".pygrok-build"
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

                config = load_config(root, home=root)

                self.assertEqual(config.provider.base_url, expected_url)
                self.assertEqual(config.provider.api_key_env, expected_env)
                self.assertEqual(config.provider.model, model)
                self.assertEqual(config.provider.max_output_tokens, 2048)

    def test_native_provider_requires_model_and_numeric_limits_are_validated(self) -> None:
        invalid_tables = (
            ('kind = "anthropic"', "requires an explicit model"),
            ('timeout_seconds = "slow"', "must be a number"),
            ("max_output_tokens = 0", "must be positive"),
            ("max_output_tokens = 1.5", "must be an integer"),
        )
        for table, expected in invalid_tables:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / ".pygrok-build"
                state.mkdir()
                (state / "config.toml").write_text(
                    f"[provider.default]\n{table}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_config(root, home=root)


if __name__ == "__main__":
    unittest.main()
