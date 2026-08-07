"""Compatibility facade for the canonical sandbox domain package.

提供沙箱领域包的兼容门面,并重新导出规范实现."""

from neuro_code.domain.sandbox.models import SandboxProfile

__all__ = ["SandboxProfile"]
