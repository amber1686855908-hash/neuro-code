"""Compatibility facade for the canonical OpenAI-compatible adapter.

提供 OpenAI 兼容适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.openai_compatible import (
    BACKEND_SUMMARY_FIELD_CHARS,
    CODE_SUMMARY_CHARS,
    OpenAICompatibleProvider,
    _ToolCallBuffer,
)

__all__ = [
    "BACKEND_SUMMARY_FIELD_CHARS",
    "CODE_SUMMARY_CHARS",
    "OpenAICompatibleProvider",
    "_ToolCallBuffer",
]
