"""Compatibility facade for :mod:`neuro_code.domain.conversation.reasoning`.

提供推理强度类型和指导文本的兼容门面,并重新导出会话领域中的规范定义."""

from neuro_code.domain.conversation.reasoning import ReasoningEffort, reasoning_guidance

__all__ = ["ReasoningEffort", "reasoning_guidance"]
