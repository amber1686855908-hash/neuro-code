"""Compatibility facade for the canonical Gemini provider adapter.

提供 Gemini Provider 适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.gemini import GeminiProvider

__all__ = ["GeminiProvider"]
