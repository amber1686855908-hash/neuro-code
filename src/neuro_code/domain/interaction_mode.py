"""Compatibility facade for :mod:`neuro_code.domain.conversation.interaction_mode`.

提供交互模式类型和指导文本的兼容门面,并重新导出会话领域中的规范定义."""

from neuro_code.domain.conversation.interaction_mode import (
    InteractionMode,
    interaction_mode_guidance,
)

__all__ = ["InteractionMode", "interaction_mode_guidance"]
