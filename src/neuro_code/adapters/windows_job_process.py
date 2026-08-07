"""Compatibility facade for canonical Windows process infrastructure.

提供 Windows 进程基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.windows_job_process import WindowsJobProcess

__all__ = ["WindowsJobProcess"]
