"""Compatibility facade for the canonical process-tree adapter.

提供进程树适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.process_tree import ProcessTree

__all__ = ["ProcessTree"]
