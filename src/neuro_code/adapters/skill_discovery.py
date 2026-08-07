"""Compatibility facade for canonical skill discovery infrastructure.

提供技能发现基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.workspace.skills import FilesystemSkillDiscovery

__all__ = ["FilesystemSkillDiscovery"]
