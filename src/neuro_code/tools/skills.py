"""Compatibility facade for the canonical skill tool adapter.

提供技能工具适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.tools.skills import SkillTool

__all__ = ["SkillTool"]
