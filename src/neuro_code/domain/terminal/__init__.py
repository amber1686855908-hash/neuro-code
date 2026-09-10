"""Canonical terminal domain package.

定义规范的终端领域包."""

from neuro_code.domain.terminal.models import (
    DEFAULT_TERMINAL_OUTPUT_CAPACITY,
    MAX_TERMINAL_DIMENSION,
    MAX_TERMINAL_OUTPUT_BYTES,
    MAX_TERMINAL_READ_BYTES,
    MAX_TERMINAL_WRITE_BYTES,
    TerminalOutputChunk,
    TerminalSignal,
    TerminalSize,
)

__all__ = [
    "DEFAULT_TERMINAL_OUTPUT_CAPACITY",
    "MAX_TERMINAL_DIMENSION",
    "MAX_TERMINAL_OUTPUT_BYTES",
    "MAX_TERMINAL_READ_BYTES",
    "MAX_TERMINAL_WRITE_BYTES",
    "TerminalOutputChunk",
    "TerminalSignal",
    "TerminalSize",
]
