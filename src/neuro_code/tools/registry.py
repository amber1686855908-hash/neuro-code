"""Compatibility facade for the canonical tool registry adapter.

提供工具注册表适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry

__all__ = ["ToolRegistry", "default_tool_registry"]
