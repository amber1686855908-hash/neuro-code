from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from neuro_code.application.ports.configuration import ProviderProfile
from neuro_code.application.ports.model import ModelCapability
from neuro_code.application.ports.provider_services import DEFAULT_PROVIDER_SERVICE_CATALOG
from neuro_code.infrastructure.providers import create_provider
from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
from neuro_code.infrastructure.providers.openai_compatible import OpenAICompatibleProvider
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider


class PlatformProviderTests(unittest.TestCase):
    @staticmethod
    def _profile(
        service_id: str,
        protocol: str,
        model: str,
        *,
        base_url: str | None = None,
    ) -> ProviderProfile:
        service = DEFAULT_PROVIDER_SERVICE_CATALOG.require(service_id)
        return ProviderProfile(
            name=f"{service_id}-{protocol}",
            service_id=service_id,
            protocol=protocol,
            dialect=service.dialect_for(protocol),
            model=model,
            base_url=base_url or service.endpoint_for(protocol=protocol),
            api_key_env="P3B_TEST_API_KEY",
            native_context="profile",
        )

    def test_platform_profiles_reuse_existing_wire_adapters(self) -> None:
        cases = (
            ("ark", "openai-chat", "doubao-seed-2-0-lite-260215", OpenAICompatibleProvider),
            ("ark", "openai-responses", "doubao-seed-2-0-lite-260215", OpenAIResponsesProvider),
            ("qianfan", "openai-chat", "deepseek-v4-flash", OpenAICompatibleProvider),
            ("qianfan", "openai-responses", "deepseek-v4-flash", OpenAIResponsesProvider),
            ("qianfan", "anthropic-messages", "deepseek-v4-flash", AnthropicProvider),
            ("bailian", "openai-chat", "qwen3.7-plus", OpenAICompatibleProvider),
            ("bailian", "openai-responses", "deepseek-v4-pro", OpenAIResponsesProvider),
            ("bailian", "anthropic-messages", "deepseek-v4-pro", AnthropicProvider),
            ("tokenhub", "openai-chat", "glm-5.3", OpenAICompatibleProvider),
            ("tokenhub", "openai-responses", "glm-5.3", OpenAIResponsesProvider),
            ("tokenhub", "anthropic-messages", "glm-5.3", AnthropicProvider),
        )
        with patch.dict(os.environ, {"P3B_TEST_API_KEY": "fixture-secret"}, clear=True):
            for service_id, protocol, model, expected_type in cases:
                with self.subTest(service_id=service_id, protocol=protocol, model=model):
                    provider = create_provider(self._profile(service_id, protocol, model))
                    self.assertIsInstance(provider, expected_type)
                    self.assertEqual(provider.model_name, model)
                    self.assertIsNotNone(provider.context_affinity)

    def test_platform_runtime_does_not_inherit_hosted_web_from_protocol_name(self) -> None:
        cases = (
            ("ark", "openai-responses", "doubao-seed-2-0-lite-260215"),
            ("qianfan", "openai-responses", "deepseek-v4-flash"),
            ("qianfan", "anthropic-messages", "deepseek-v4-flash"),
            ("bailian", "openai-responses", "qwen3.7-plus"),
            ("tokenhub", "openai-responses", "glm-5.3"),
        )
        with patch.dict(os.environ, {"P3B_TEST_API_KEY": "fixture-secret"}, clear=True):
            for service_id, protocol, model in cases:
                with self.subTest(service_id=service_id, protocol=protocol, model=model):
                    provider = create_provider(self._profile(service_id, protocol, model))
                    self.assertFalse(
                        provider.capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH)
                    )
                    self.assertFalse(
                        provider.capabilities.supports(ModelCapability.HOSTED_WEB_FETCH)
                    )

    def test_same_publisher_across_services_has_distinct_native_affinity(self) -> None:
        services = ("deepseek", "tokenhub", "bailian", "qianfan")
        profiles = tuple(
            ProviderProfile(
                name="same-profile-name",
                service_id=service_id,
                protocol="openai-chat",
                model="deepseek-v4-pro",
                base_url="https://shared.example.invalid/v1",
                api_key_env="P3B_TEST_API_KEY",
                native_context="profile",
            )
            for service_id in services
        )
        affinities = tuple(profile.context_affinity for profile in profiles)
        self.assertEqual(len(set(affinities)), len(services))

    def test_same_bailian_model_has_distinct_region_endpoint_affinity(self) -> None:
        beijing = self._profile(
            "bailian",
            "openai-chat",
            "qwen3.7-plus",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        singapore = self._profile(
            "bailian",
            "openai-chat",
            "qwen3.7-plus",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        self.assertNotEqual(beijing.context_affinity, singapore.context_affinity)


if __name__ == "__main__":
    unittest.main()
