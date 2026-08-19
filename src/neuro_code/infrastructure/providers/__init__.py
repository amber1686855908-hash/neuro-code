"""Lazy aggregate boundary for concrete provider infrastructure adapters.

提供具体 Provider 基础设施适配器的延迟聚合边界."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider
from neuro_code.configuration.app import AppConfig, ProviderProfile
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
    from neuro_code.infrastructure.providers.failover import (
        FailoverModelProvider,
        ProviderCandidate,
    )
    from neuro_code.infrastructure.providers.gemini import GeminiProvider
    from neuro_code.infrastructure.providers.gemini_interactions import (
        GeminiInteractionsProvider,
    )
    from neuro_code.infrastructure.providers.openai_compatible import (
        OpenAICompatibleProvider,
    )
    from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
    from neuro_code.infrastructure.providers.provider_catalog import HttpProviderCatalog
    from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore

__all__ = [
    "AnthropicProvider",
    "FailoverModelProvider",
    "GeminiInteractionsProvider",
    "GeminiProvider",
    "HttpProviderCatalog",
    "JsonProviderSettingsStore",
    "OpenAICompatibleProvider",
    "OpenAIResponsesProvider",
    "ProviderCandidate",
    "create_provider",
    "create_routed_provider",
]


def create_provider(
    config: ProviderProfile,
    *,
    response_observer: Callable[[Mapping[str, object]], None] | None = None,
    builtin_tool_options: Mapping[str, Mapping[str, object]] | None = None,
    tool_choice: str | Mapping[str, object] | None = None,
) -> ModelProvider:
    """Build one concrete provider from a validated profile.

    根据已验证的配置档案构建一个具体 Provider."""

    api_key = config.api_key()
    http_policy = config.http_client_policy()
    capabilities = _runtime_capabilities(config)
    if config.protocol == "openai-chat":
        from neuro_code.infrastructure.providers.openai_compatible import (
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            dialect=config.dialect,
            context_affinity=config.context_affinity,
            capabilities=capabilities,
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
            capabilities=capabilities,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            dialect=config.dialect,
            builtin_tools=config.builtin_tools,
            builtin_tool_options=builtin_tool_options,
            tool_choice=tool_choice,
            http_policy=http_policy,
            response_observer=response_observer,
        )
    if config.protocol == "anthropic-messages":
        from neuro_code.infrastructure.providers.anthropic import AnthropicProvider

        if isinstance(tool_choice, str):
            raise ConfigurationError("Anthropic tool_choice must be a mapping")
        return AnthropicProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            context_affinity=config.context_affinity,
            capabilities=capabilities,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            builtin_tools=config.builtin_tools,
            builtin_tool_options=builtin_tool_options,
            tool_choice=tool_choice,
            http_policy=http_policy,
            response_observer=response_observer,
        )
    if config.protocol == "gemini-generate-content":
        from neuro_code.infrastructure.providers.gemini import GeminiProvider

        return GeminiProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            context_affinity=config.context_affinity,
            capabilities=capabilities,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            http_policy=http_policy,
        )
    if config.protocol == "gemini-interactions":
        from neuro_code.infrastructure.providers.gemini_interactions import (
            GeminiInteractionsProvider,
        )

        return GeminiInteractionsProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=api_key,
            provider_name=config.name,
            service_id=config.service_id,
            context_affinity=config.context_affinity,
            capabilities=capabilities,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            builtin_tools=config.builtin_tools,
            builtin_tool_options=builtin_tool_options,
            tool_choice=tool_choice,
            http_policy=http_policy,
            response_observer=response_observer,
        )
    raise ConfigurationError(f"unsupported provider protocol: {config.protocol}")


def create_routed_provider(config: AppConfig, *, failover: bool = True) -> ModelProvider:
    """Build the primary provider and its optional failover chain.

    构建主 Provider 及其可选的故障转移链."""

    route = config.main_route
    primary = _route_profile(config, route.provider_profile, route.model)
    fallback_names = route.fallback_profiles
    if not failover or not fallback_names:
        return create_provider(primary)
    from neuro_code.infrastructure.providers.failover import (
        FailoverModelProvider,
        ProviderCandidate,
    )

    profile_names = (primary.name, *fallback_names)
    profiles = tuple(
        primary if name == primary.name else config.providers[name]
        for name in dict.fromkeys(profile_names)
    )
    candidates = tuple(
        ProviderCandidate(
            profile.name,
            profile.model,
            profile.context_affinity,
            partial(create_provider, profile),
            context_window_tokens=profile.context_window_tokens,
            capabilities=_runtime_capabilities(profile),
        )
        for profile in profiles
    )
    if len(candidates) == 1:
        return create_provider(primary)
    return FailoverModelProvider(candidates)


def _route_profile(config: AppConfig, profile_name: str, model: str) -> ProviderProfile:
    try:
        profile = config.providers[profile_name]
    except KeyError as error:
        raise ConfigurationError(
            f"route provider profile does not exist: {profile_name}"
        ) from error
    if profile.model == model:
        return profile
    return replace(profile, model=model)


def _runtime_capabilities(config: ProviderProfile) -> ModelCapabilitySet:
    """Resolve capabilities through the concrete adapter implementation seam."""

    if config.protocol == "openai-chat":
        from neuro_code.infrastructure.providers.openai_compatible import (
            OpenAICompatibleProvider,
        )

        implementation = OpenAICompatibleProvider.implementation_capabilities(
            dialect=config.dialect
        )
    elif config.protocol == "openai-responses":
        from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider

        implementation = OpenAIResponsesProvider.implementation_capabilities(
            dialect=config.dialect,
            builtin_tools=config.builtin_tools,
        )
    elif config.protocol == "anthropic-messages":
        from neuro_code.infrastructure.providers.anthropic import AnthropicProvider

        implementation = AnthropicProvider.implementation_capabilities(
            model=config.model,
            builtin_tools=config.builtin_tools,
        )
    elif config.protocol == "gemini-generate-content":
        from neuro_code.infrastructure.providers.gemini import GeminiProvider

        implementation = GeminiProvider.implementation_capabilities()
    elif config.protocol == "gemini-interactions":
        from neuro_code.infrastructure.providers.gemini_interactions import (
            GeminiInteractionsProvider,
        )

        implementation = GeminiInteractionsProvider.implementation_capabilities(
            model=config.model,
            builtin_tools=config.builtin_tools,
        )
    else:
        return ModelCapabilitySet.all_unknown()
    return config.effective_capabilities(implementation)


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
    if name == "GeminiInteractionsProvider":
        from neuro_code.infrastructure.providers.gemini_interactions import (
            GeminiInteractionsProvider,
        )

        return GeminiInteractionsProvider
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
