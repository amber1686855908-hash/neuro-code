"""Canonical terminal domain package.

定义规范的终端领域包."""

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
