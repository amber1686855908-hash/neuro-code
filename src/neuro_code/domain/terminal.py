from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_TERMINAL_DIMENSION = 32_767
MAX_TERMINAL_READ_BYTES = 1_048_576
MAX_TERMINAL_WRITE_BYTES = 1_048_576
MAX_TERMINAL_OUTPUT_BYTES = 16 * 1_048_576


@dataclass(frozen=True, slots=True)
class TerminalSize:
    columns: int
    rows: int

    def __post_init__(self) -> None:
        for name, value in (("columns", self.columns), ("rows", self.rows)):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= MAX_TERMINAL_DIMENSION
            ):
                raise ValueError(f"{name} must be an integer from 1 to {MAX_TERMINAL_DIMENSION}")


class TerminalSignal(StrEnum):
    INTERRUPT = "interrupt"
    TERMINATE = "terminate"
    KILL = "kill"


@dataclass(frozen=True, slots=True)
class TerminalOutputChunk:
    """A cursor-addressed slice from an owned terminal's output ring."""

    data: bytes
    next_offset: int
    dropped_bytes: int
    eof: bool

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("terminal output data must be bytes")
        if self.next_offset < len(self.data):
            raise ValueError("next_offset cannot precede the returned data")
        if self.dropped_bytes < 0:
            raise ValueError("dropped_bytes must not be negative")


__all__ = [
    "MAX_TERMINAL_DIMENSION",
    "MAX_TERMINAL_OUTPUT_BYTES",
    "MAX_TERMINAL_READ_BYTES",
    "MAX_TERMINAL_WRITE_BYTES",
    "TerminalOutputChunk",
    "TerminalSignal",
    "TerminalSize",
]
