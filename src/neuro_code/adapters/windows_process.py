"""Compatibility facade for canonical Windows process primitives.

提供 Windows 进程基础能力的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.windows_process import windows_environment_block

__all__ = ["windows_environment_block"]
