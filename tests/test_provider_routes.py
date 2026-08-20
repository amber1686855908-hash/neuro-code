from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from neuro_code.application.ports.routing import RuntimeRole
from neuro_code.configuration.app import load_config
from neuro_code.shared.errors import ConfigurationError


class ProviderRouteTests(unittest.TestCase):
    def test_legacy_main_routing_projects_to_main_route_and_web_search_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".neuro-code"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "main"
fallbacks = ["main-fallback"]

[routing.web_search]
profile = "search"
model = "gemini-search-model"
fallbacks = ["search-fallback"]

[providers.main]
protocol = "openai-chat"
model = "main-model"
base_url = "https://main.invalid/v1"
api_key_env = "MAIN_KEY"
proxy_mode = "direct"

[providers.main-fallback]
protocol = "openai-chat"
model = "main-fallback-model"
base_url = "https://main-fallback.invalid/v1"
api_key_env = "MAIN_FALLBACK_KEY"
proxy_mode = "direct"

[providers.search]
protocol = "gemini-generate-content"
model = "gemini-default"
base_url = "https://generativelanguage.googleapis.com/v1beta"
api_key_env = "SEARCH_KEY"
proxy_mode = "direct"

[providers.search-fallback]
protocol = "gemini-generate-content"
model = "gemini-fallback"
base_url = "https://search-fallback.invalid/v1beta"
api_key_env = "SEARCH_FALLBACK_KEY"
proxy_mode = "direct"
""",
                encoding="utf-8",
            )

            config = load_config(root, home=root, environ={})

        main = config.route(RuntimeRole.MAIN)
        web_search = config.route(RuntimeRole.WEB_SEARCH)
        self.assertIsNotNone(main)
        assert main is not None
        self.assertEqual(main.provider_profile, "main")
        self.assertEqual(main.model, "main-model")
        self.assertEqual(main.fallback_profiles, ("main-fallback",))
        self.assertIsNotNone(web_search)
        assert web_search is not None
        self.assertEqual(web_search.provider_profile, "search")
        self.assertEqual(web_search.model, "gemini-search-model")
        self.assertEqual(web_search.fallback_profiles, ("search-fallback",))
        self.assertNotIn("main-fallback", web_search.fallback_profiles)

    def test_missing_or_duplicate_role_route_profiles_are_rejected(self) -> None:
        cases = (
            (
                """
[routing.web_search]
profile = "missing"
""",
                "web_search route provider profile does not exist",
            ),
            (
                """
[routing.web_search]
profile = "search"
fallbacks = ["search", "search"]
""",
                "routing.web_search fallbacks must not contain duplicates",
            ),
        )
        for route, expected in cases:
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / ".neuro-code"
                state.mkdir()
                (state / "config.toml").write_text(
                    f"""
[routing]
default = "main"
{route}

[providers.main]
protocol = "openai-chat"
model = "main-model"
base_url = "https://main.invalid/v1"
api_key_env = "MAIN_KEY"
proxy_mode = "direct"

[providers.search]
protocol = "openai-chat"
model = "search-model"
base_url = "https://search.invalid/v1"
api_key_env = "SEARCH_KEY"
proxy_mode = "direct"
""",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_config(root, home=root, environ={})

    def test_no_web_search_route_keeps_current_main_only_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".neuro-code"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "main"

[providers.main]
protocol = "openai-chat"
model = "main-model"
base_url = "https://main.invalid/v1"
api_key_env = "MAIN_KEY"
proxy_mode = "direct"
""",
                encoding="utf-8",
            )
            config = load_config(root, home=root, environ={})

        self.assertIsNone(config.web_search_route)
        self.assertEqual(config.main_route.provider_profile, "main")
        self.assertEqual(config.redacted_dict()["routing"]["routes"], {})

    def test_generic_route_rejects_unpublished_execution_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".neuro-code"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "main"

[routing.web_search]
profile = "search"
execution_path = "sidecar_hosted"

[providers.main]
protocol = "openai-chat"
model = "main-model"
base_url = "https://main.invalid/v1"
api_key_env = "MAIN_KEY"
proxy_mode = "direct"

[providers.search]
protocol = "openai-chat"
model = "search-model"
base_url = "https://search.invalid/v1"
api_key_env = "SEARCH_KEY"
proxy_mode = "direct"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "not part of the generic route contract",
            ):
                load_config(root, home=root, environ={})


if __name__ == "__main__":
    unittest.main()
