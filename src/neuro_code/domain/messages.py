"""Compatibility facade for :mod:`neuro_code.domain.conversation.messages`.

提供消息和值类型的兼容门面,并重新导出会话领域中的规范定义."""

from neuro_code.domain.conversation.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPart,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    SyntheticReason,
    ToolCall,
)

__all__ = [
    "IMAGE_MODEL_PLACEHOLDER",
    "ContentPart",
    "ContentPartKind",
    "ContextItemKind",
    "Message",
    "PreservedContextItem",
    "Role",
    "SessionItem",
    "SyntheticReason",
    "ToolCall",
]
