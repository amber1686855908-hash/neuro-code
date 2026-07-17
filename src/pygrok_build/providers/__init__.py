from pygrok_build.config import ProviderConfig
from pygrok_build.errors import ConfigurationError
from pygrok_build.ports.model import ModelProvider
from pygrok_build.providers.anthropic import AnthropicProvider
from pygrok_build.providers.gemini import GeminiProvider
from pygrok_build.providers.openai_compatible import OpenAICompatibleProvider


def create_provider(config: ProviderConfig) -> ModelProvider:
    if config.kind == "openai-compatible":
        return OpenAICompatibleProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key(),
            timeout_seconds=config.timeout_seconds,
        )
    if config.kind == "anthropic":
        return AnthropicProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key(),
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
        )
    if config.kind == "gemini":
        return GeminiProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key(),
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
        )
    raise ConfigurationError(f"unsupported provider kind: {config.kind}")


__all__ = ["AnthropicProvider", "GeminiProvider", "OpenAICompatibleProvider", "create_provider"]
