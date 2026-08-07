"""Compatibility facade for the canonical skill tracker.

提供技能跟踪器的兼容门面,并转发到规范实现."""

from neuro_code.application.memory.skill_tracker import SkillTracker

__all__ = ["SkillTracker"]
