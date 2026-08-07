"""Compatibility facade for the canonical instruction tracker.

提供指令跟踪器的兼容门面,并转发到规范实现."""

from neuro_code.application.memory.instruction_tracker import InstructionTracker

__all__ = ["InstructionTracker"]
