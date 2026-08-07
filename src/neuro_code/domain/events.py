"""Compatibility facade for :mod:`neuro_code.domain.conversation.events`.

提供 Agent 事件类型的兼容门面,并重新导出会话领域中的规范定义."""

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind

__all__ = ["AgentEvent", "AgentEventKind"]
