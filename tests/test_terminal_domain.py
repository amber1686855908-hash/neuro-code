from __future__ import annotations

import unittest

from neuro_code.domain.terminal import (
    MAX_TERMINAL_DIMENSION,
    TerminalOutputChunk,
    TerminalSize,
)


class TerminalDomainTests(unittest.TestCase):
    def test_terminal_size_accepts_bounded_dimensions(self) -> None:
        self.assertEqual(TerminalSize(80, 24), TerminalSize(columns=80, rows=24))
        self.assertEqual(
            TerminalSize(MAX_TERMINAL_DIMENSION, 1).columns,
            MAX_TERMINAL_DIMENSION,
        )

    def test_terminal_size_rejects_boolean_zero_and_oversized_values(self) -> None:
        for columns, rows in (
            (True, 24),
            (80, False),
            (0, 24),
            (80, 0),
            (MAX_TERMINAL_DIMENSION + 1, 24),
        ):
            with self.subTest(columns=columns, rows=rows), self.assertRaises(ValueError):
                TerminalSize(columns, rows)  # type: ignore[arg-type]

    def test_output_chunk_validates_cursor_and_drop_count(self) -> None:
        self.assertEqual(
            TerminalOutputChunk(b"abc", next_offset=5, dropped_bytes=2, eof=False).data,
            b"abc",
        )
        with self.assertRaisesRegex(ValueError, "precede"):
            TerminalOutputChunk(b"abc", next_offset=2, dropped_bytes=0, eof=False)
        with self.assertRaisesRegex(ValueError, "negative"):
            TerminalOutputChunk(b"", next_offset=0, dropped_bytes=-1, eof=False)


if __name__ == "__main__":
    unittest.main()
