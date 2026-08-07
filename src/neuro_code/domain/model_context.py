"""Compatibility facade for :mod:`neuro_code.domain.conversation.context`.

提供模型上下文类型的兼容门面,并重新导出会话领域中的规范定义."""

from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext

__all__ = ["UPSTREAM_IMPORT_PROVIDER", "ModelContext"]
