"""Compatibility facade for the canonical Anthropic provider adapter.

提供 Anthropic Provider 适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.anthropic import AnthropicProvider

__all__ = ["AnthropicProvider"]
