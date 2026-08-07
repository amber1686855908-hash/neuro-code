"""Compatibility facade for canonical instruction discovery infrastructure.

提供指令发现基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.workspace.instructions import FilesystemInstructionDiscovery

__all__ = ["FilesystemInstructionDiscovery"]
