from __future__ import annotations

import unittest

from neuro_code.application.ports.model import (
    CapabilityStatus,
    ModelCapability,
    ModelCapabilitySet,
)
from neuro_code.application.ports.provider_services import (
    DEFAULT_PROVIDER_SERVICE_CATALOG,
    ModelCatalogStrategy,
    ProviderServiceCatalog,
    ProviderServiceDescriptor,
)
from neuro_code.configuration.app import ProviderProfile
from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
from neuro_code.infrastructure.providers.gemini_interactions import (
    GeminiInteractionsProvider,
)
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.shared.errors import ConfigurationError


class ProviderServiceCatalogTests(unittest.TestCase):
    def test_default_catalog_has_canonical_services_and_immutable_descriptors(self) -> None:
        service_ids = tuple(service.service_id for service in DEFAULT_PROVIDER_SERVICE_CATALOG)
        self.assertEqual(
            service_ids,
            (
                "openai",
                "generic-openai-compatible",
                "deepseek",
                "anthropic",
                "google-ai-studio",
                "xai",
            ),
        )
        self.assertIs(
            DEFAULT_PROVIDER_SERVICE_CATALOG.get("compatible"),
            DEFAULT_PROVIDER_SERVICE_CATALOG.get("generic-openai-compatible"),
        )
        for service in DEFAULT_PROVIDER_SERVICE_CATALOG:
            self.assertIn(service.default_protocol, service.supported_protocols)
            self.assertEqual(
                service.dialect_for(service.default_protocol),
                service.default_dialect,
            )
        with self.assertRaises(AttributeError):
            DEFAULT_PROVIDER_SERVICE_CATALOG.get("deepseek").display_name = "changed"  # type: ignore[misc]

    def test_invalid_service_metadata_fails_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            ProviderServiceDescriptor(
                service_id="bad",
                display_name="Bad",
                default_protocol="openai-chat",
                default_base_url="https://user:password@example.invalid/v1",
            )
        with self.assertRaises(ConfigurationError):
            ProviderServiceDescriptor(
                service_id="bad",
                display_name="Bad",
                default_protocol="openai-chat",
                default_base_url="https://example.invalid/v1?secret=1",
            )
        with self.assertRaises(ConfigurationError):
            ProviderServiceDescriptor(
                service_id="duplicate",
                display_name="Duplicate",
                default_protocol="openai-chat",
                default_base_url="https://example.invalid/v1",
                aliases=("legacy", "legacy"),
            )

    def test_capabilities_are_layered_service_protocol_model_then_profile(self) -> None:
        service = ProviderServiceDescriptor(
            service_id="layered",
            display_name="Layered",
            default_protocol="openai-chat",
            default_base_url="https://layered.invalid/v1",
            model_catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            capabilities=ModelCapabilitySet.from_supported(ModelCapability.FUNCTION_TOOLS),
            protocol_capabilities={
                "openai-chat": ModelCapabilitySet.from_mapping(
                    {ModelCapability.VISION: CapabilityStatus.UNSUPPORTED}
                )
            },
            model_capabilities={
                "vision-model": ModelCapabilitySet.from_supported(ModelCapability.VISION)
            },
        )
        catalog = ProviderServiceCatalog((service,))
        implementation = ModelCapabilitySet.from_supported(
            ModelCapability.FUNCTION_TOOLS,
            ModelCapability.VISION,
        )

        resolution = catalog.capability_resolution_for_profile(
            service_id="layered",
            protocol="openai-chat",
            dialect="standard",
            base_url="https://layered.invalid/v1",
            model="vision-model",
            implementation=implementation,
        )
        baseline = resolution.effective
        self.assertTrue(resolution.upstream.supports(ModelCapability.FUNCTION_TOOLS))
        self.assertEqual(
            resolution.upstream.status(ModelCapability.VISION),
            CapabilityStatus.UNSUPPORTED,
        )
        self.assertTrue(resolution.implementation.supports(ModelCapability.VISION))
        self.assertEqual(
            resolution.configuration.status(ModelCapability.VISION),
            CapabilityStatus.UNKNOWN,
        )
        self.assertTrue(baseline.supports(ModelCapability.FUNCTION_TOOLS))
        self.assertFalse(baseline.supports(ModelCapability.VISION))
        self.assertEqual(
            baseline.status(ModelCapability.VISION),
            CapabilityStatus.UNSUPPORTED,
        )
        self.assertEqual(
            baseline.status(ModelCapability.HOSTED_WEB_SEARCH),
            CapabilityStatus.UNKNOWN,
        )

        overridden = catalog.capabilities_for_profile(
            service_id="layered",
            protocol="openai-chat",
            dialect="standard",
            base_url="https://layered.invalid/v1",
            model="vision-model",
            implementation=implementation,
            configuration=ModelCapabilitySet.from_mapping(
                {ModelCapability.VISION: CapabilityStatus.UNSUPPORTED}
            ),
        )
        self.assertFalse(overridden.supports(ModelCapability.VISION))
        self.assertEqual(
            overridden.status(ModelCapability.VISION),
            CapabilityStatus.UNSUPPORTED,
        )

    def test_xai_configured_hosted_tools_are_exposed_as_canonical_capabilities(self) -> None:
        profile = ProviderProfile(
            name="xai",
            protocol="openai-responses",
            dialect="xai",
            service_id="xai",
            model="fixture-model",
            base_url="https://api.x.ai/v1",
            api_key_env="XAI_API_KEY",
            builtin_tools=("web_search", "x_search", "code_interpreter"),
        )
        capabilities = profile.effective_capabilities(
            OpenAIResponsesProvider.implementation_capabilities(
                dialect="xai",
                builtin_tools=profile.builtin_tools,
            )
        )
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH))
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_X_SEARCH))
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_CODE_INTERPRETER))
        self.assertEqual(
            capabilities.status(ModelCapability.HOSTED_WEB_FETCH),
            CapabilityStatus.UNKNOWN,
        )

    def test_anthropic_hosted_tools_require_known_model_and_explicit_builtins(self) -> None:
        supported = ProviderProfile(
            name="anthropic",
            protocol="anthropic-messages",
            service_id="anthropic",
            model="claude-sonnet-4-6",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_API_KEY",
            builtin_tools=("web_search", "web_fetch"),
        )
        capabilities = supported.effective_capabilities(
            AnthropicProvider.implementation_capabilities(
                model=supported.model,
                builtin_tools=supported.builtin_tools,
            )
        )
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH))
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_WEB_FETCH))

        unknown = ProviderProfile(
            name="anthropic-unknown",
            protocol="anthropic-messages",
            service_id="anthropic",
            model="claude-future-unknown",
            base_url="https://api.anthropic.com",
            api_key_env="ANTHROPIC_API_KEY",
            builtin_tools=("web_search", "web_fetch"),
        )
        unknown_capabilities = unknown.effective_capabilities(
            AnthropicProvider.implementation_capabilities(
                model=unknown.model,
                builtin_tools=unknown.builtin_tools,
            )
        )
        self.assertEqual(
            unknown_capabilities.status(ModelCapability.HOSTED_WEB_SEARCH),
            CapabilityStatus.UNKNOWN,
        )
        self.assertEqual(
            unknown_capabilities.status(ModelCapability.HOSTED_WEB_FETCH),
            CapabilityStatus.UNKNOWN,
        )

    def test_gemini_interactions_hosted_tools_are_protocol_and_model_specific(self) -> None:
        service = DEFAULT_PROVIDER_SERVICE_CATALOG.require("google-ai-studio")
        known_model = "gemini-3.6-flash"
        search_upstream = service.upstream_capabilities_for(
            protocol="gemini-interactions",
            model=known_model,
        )
        generate_content_upstream = service.upstream_capabilities_for(
            protocol="gemini-generate-content",
            model=known_model,
        )
        self.assertTrue(search_upstream.supports(ModelCapability.HOSTED_WEB_SEARCH))
        self.assertTrue(search_upstream.supports(ModelCapability.HOSTED_WEB_FETCH))
        self.assertTrue(search_upstream.supports(ModelCapability.MIXED_HOSTED_AND_CLIENT_TOOLS))
        self.assertEqual(
            generate_content_upstream.status(ModelCapability.HOSTED_WEB_SEARCH),
            CapabilityStatus.UNKNOWN,
        )

        profile = ProviderProfile(
            name="gemini-interactions",
            protocol="gemini-interactions",
            service_id="google-ai-studio",
            model=known_model,
            base_url="https://generativelanguage.googleapis.com/v1",
            api_key_env="GEMINI_API_KEY",
            builtin_tools=("google_search", "url_context"),
        )
        capabilities = profile.effective_capabilities(
            GeminiInteractionsProvider.implementation_capabilities(
                model=profile.model,
                builtin_tools=profile.builtin_tools,
            )
        )
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH))
        self.assertTrue(capabilities.supports(ModelCapability.HOSTED_WEB_FETCH))

        unknown = GeminiInteractionsProvider.implementation_capabilities(
            model="gemini-future-unknown",
            builtin_tools=("google_search", "url_context"),
        )
        self.assertEqual(
            unknown.status(ModelCapability.HOSTED_WEB_SEARCH),
            CapabilityStatus.UNKNOWN,
        )
        self.assertEqual(
            unknown.status(ModelCapability.HOSTED_WEB_FETCH),
            CapabilityStatus.UNKNOWN,
        )

    def test_fake_service_can_be_added_without_runtime_or_ui_changes(self) -> None:
        fake = ProviderServiceDescriptor(
            service_id="example-cn",
            display_name="Example CN",
            default_protocol="openai-chat",
            default_base_url="https://example.invalid/v1",
            model_catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
        )
        catalog = ProviderServiceCatalog((*DEFAULT_PROVIDER_SERVICE_CATALOG.services, fake))

        self.assertIs(catalog.get("example-cn"), fake)
        self.assertEqual(fake.default_protocol, "openai-chat")
        self.assertEqual(fake.default_base_url, "https://example.invalid/v1")
        self.assertEqual(
            catalog.capabilities_for_profile(
                service_id="example-cn",
                protocol="openai-chat",
                dialect="standard",
                base_url=fake.default_base_url,
                model="example-model",
            ),
            ModelCapabilitySet.all_unknown(),
        )

    def test_upstream_hosted_claim_cannot_become_runtime_support_without_adapter_evidence(
        self,
    ) -> None:
        service = ProviderServiceDescriptor(
            service_id="claims-search",
            display_name="Claims Search",
            default_protocol="openai-chat",
            default_base_url="https://claims.invalid/v1",
            capabilities=ModelCapabilitySet.from_supported(ModelCapability.HOSTED_WEB_SEARCH),
        )
        catalog = ProviderServiceCatalog((service,))

        capabilities = catalog.capabilities_for_profile(
            service_id="claims-search",
            protocol="openai-chat",
            dialect="standard",
            base_url=service.default_base_url,
            model="search-model",
            implementation=ModelCapabilitySet.all_unknown(),
            configuration=ModelCapabilitySet.from_supported(ModelCapability.HOSTED_WEB_SEARCH),
        )

        self.assertEqual(
            capabilities.status(ModelCapability.HOSTED_WEB_SEARCH),
            CapabilityStatus.UNKNOWN,
        )
        self.assertFalse(capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH))


if __name__ == "__main__":
    unittest.main()
