from functools import partial

from neuro_code.config import AppConfig, ProviderProfile
from neuro_code.errors import ConfigurationError
from neuro_code.ports.model import ModelProvider
from neuro_code.providers.anthropic import AnthropicProvider
from neuro_code.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.providers.gemini import GeminiProvider
from neuro_code.providers.openai_compatible import OpenAICompatibleProvider
from neuro_code.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.providers.xai_responses import XAIResponsesProvider


def create_provider(config: ProviderProfile) -> ModelProvider:
    api_key = config.api_key()
    http_policy = config.http_client_policy()
    if config.protocol == "openai-chat":
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
    primary = config.provider
    if not failover or not config.fallback_providers:
        return create_provider(primary)
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


__all__ = [
    "AnthropicProvider",
    "FailoverModelProvider",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "OpenAIResponsesProvider",
    "XAIResponsesProvider",
    "create_provider",
    "create_routed_provider",
]
