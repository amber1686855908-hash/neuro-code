"""Compatibility facade for the canonical terminal package.

提供终端领域包的兼容门面,并重新导出规范实现."""

from neuro_code.domain.terminal.models import (
    MAX_TERMINAL_DIMENSION,
    MAX_TERMINAL_OUTPUT_BYTES,
    MAX_TERMINAL_READ_BYTES,
    MAX_TERMINAL_WRITE_BYTES,
    TerminalOutputChunk,
    TerminalSignal,
    TerminalSize,
)

__all__ = [
    "MAX_TERMINAL_DIMENSION",
    "MAX_TERMINAL_OUTPUT_BYTES",
    "MAX_TERMINAL_READ_BYTES",
    "MAX_TERMINAL_WRITE_BYTES",
    "TerminalOutputChunk",
    "TerminalSignal",
    "TerminalSize",
]
