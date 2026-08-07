"""Compatibility facade for the canonical shell command tool.

提供 Shell 命令工具的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.tools.bash import BashTool

__all__ = ["BashTool"]
