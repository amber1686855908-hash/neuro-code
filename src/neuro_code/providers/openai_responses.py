"""Compatibility facade for the canonical OpenAI Responses adapter.

提供 OpenAI Responses 适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider

__all__ = ["OpenAIResponsesProvider"]
