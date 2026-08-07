"""Lazy aggregate boundary for concrete provider infrastructure adapters.

提供具体 Provider 基础设施适配器的延迟聚合边界."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from neuro_code.application.ports.model import ModelProvider
from neuro_code.configuration.app import AppConfig, ProviderProfile
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
    from neuro_code.infrastructure.providers.failover import (
        FailoverModelProvider,
        ProviderCandidate,
    )
    from neuro_code.infrastructure.providers.gemini import GeminiProvider
    from neuro_code.infrastructure.providers.openai_compatible import (
        OpenAICompatibleProvider,
    )
    from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
    from neuro_code.infrastructure.providers.provider_catalog import HttpProviderCatalog
    from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore

__all__ = [
    "AnthropicProvider",
    "FailoverModelProvider",
    "GeminiProvider",
    "HttpProviderCatalog",
    "JsonProviderSettingsStore",
    "OpenAICompatibleProvider",
    "OpenAIResponsesProvider",
    "ProviderCandidate",
    "create_provider",
    "create_routed_provider",
]


def create_provider(config: ProviderProfile) -> ModelProvider:
    """Build one concrete provider from a validated profile.

    根据已验证的配置档案构建一个具体 Provider."""

    api_key = config.api_key()
    http_policy = config.http_client_policy()
    if config.protocol == "openai-chat":
        from neuro_code.infrastructure.providers.openai_compatible import (
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            context_affinity=config.context_affinity,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            http_policy=http_policy,
        )
    if config.protocol == "openai-responses":
        from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider

        return OpenAIResponsesProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            context_affinity=config.context_affinity,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            dialect=config.dialect,
            builtin_tools=config.builtin_tools,
            http_policy=http_policy,
        )
    if config.protocol == "anthropic-messages":
        from neuro_code.infrastructure.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            context_affinity=config.context_affinity,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            http_policy=http_policy,
        )
    if config.protocol == "gemini-generate-content":
        from neuro_code.infrastructure.providers.gemini import GeminiProvider

        return GeminiProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            context_affinity=config.context_affinity,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            http_policy=http_policy,
        )
    raise ConfigurationError(f"unsupported provider protocol: {config.protocol}")


def create_routed_provider(config: AppConfig, *, failover: bool = True) -> ModelProvider:
    """Build the primary provider and its optional failover chain.

    构建主 Provider 及其可选的故障转移链."""

    primary = config.provider
    if not failover or not config.fallback_providers:
        return create_provider(primary)
    from neuro_code.infrastructure.providers.failover import (
        FailoverModelProvider,
        ProviderCandidate,
    )

    profile_names = (primary.name, *config.fallback_providers)
    profiles = tuple(config.providers[name] for name in dict.fromkeys(profile_names))
    candidates = tuple(
        ProviderCandidate(
            profile.name,
            profile.model,
            profile.context_affinity,
            partial(create_provider, profile),
            context_window_tokens=profile.context_window_tokens,
        )
        for profile in profiles
    )
    if len(candidates) == 1:
        return create_provider(primary)
    return FailoverModelProvider(candidates)


def __getattr__(name: str) -> object:
    if name == "HttpProviderCatalog":
        from neuro_code.infrastructure.providers.provider_catalog import HttpProviderCatalog

        return HttpProviderCatalog
    if name == "JsonProviderSettingsStore":
        from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore

        return JsonProviderSettingsStore
    if name == "AnthropicProvider":
        from neuro_code.infrastructure.providers.anthropic import AnthropicProvider

        return AnthropicProvider
    if name == "FailoverModelProvider":
        from neuro_code.infrastructure.providers.failover import FailoverModelProvider

        return FailoverModelProvider
    if name == "GeminiProvider":
        from neuro_code.infrastructure.providers.gemini import GeminiProvider

        return GeminiProvider
    if name == "OpenAICompatibleProvider":
        from neuro_code.infrastructure.providers.openai_compatible import (
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider
    if name == "OpenAIResponsesProvider":
        from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider

        return OpenAIResponsesProvider
    if name == "ProviderCandidate":
        from neuro_code.infrastructure.providers.failover import ProviderCandidate

        return ProviderCandidate
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
