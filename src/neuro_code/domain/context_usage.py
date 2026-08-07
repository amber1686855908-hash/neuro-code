"""Compatibility facade for :mod:`neuro_code.domain.conversation.context`.

提供上下文用量估算函数的兼容门面,并重新导出会话领域中的规范定义."""

from neuro_code.domain.conversation.context import estimate_context_tokens, estimate_text_tokens

__all__ = ["estimate_context_tokens", "estimate_text_tokens"]
