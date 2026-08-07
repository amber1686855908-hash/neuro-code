"""Compatibility facade for canonical Windows Job Object infrastructure.

提供 Windows Job Object 基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.windows_job import WindowsJobObject

__all__ = ["WindowsJobObject"]
