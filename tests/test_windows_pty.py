from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from neuro_code.adapters.windows_pty import WindowsConPtyPlatform, WindowsConPtySession
from neuro_code.domain.terminal import TerminalSignal, TerminalSize


class _FakeNativeSession:
    def __init__(self) -> None:
        self.process_id = 42
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.terminate_calls = 0
        self.exit_code: int | None = None
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def resize(self, columns: int, rows: int) -> None:
        self.resizes.append((columns, rows))

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout_seconds: float) -> int | None:
        self.assert_timeout = timeout_seconds
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class WindowsConPtyPortAdapterTests(unittest.TestCase):
    def test_shared_session_operations_project_to_native_conpty(self) -> None:
        native = _FakeNativeSession()
        session = WindowsConPtySession(native, TerminalSize(80, 24))  # type: ignore[arg-type]

        session.write(b"input")
        session.resize(TerminalSize(100, 30))
        session.send_signal(TerminalSignal.INTERRUPT)
        session.send_signal(TerminalSignal.TERMINATE)
        session.send_signal(TerminalSignal.KILL)
        native.exit_code = 7

        self.assertEqual(session.process_id, 42)
        self.assertEqual(session.poll_exit(), 7)
        self.assertEqual(native.writes, [b"input", b"\x03"])
        self.assertEqual(native.resizes, [(100, 30)])
        self.assertEqual(native.terminate_calls, 2)
        session.close()
        self.assertTrue(native.closed)

    def test_platform_passes_callbacks_and_bounded_diagnostic_capture(self) -> None:
        native = _FakeNativeSession()

        def output(data: bytes) -> None:
            del data

        def eof() -> None:
            return

        def error(failure: BaseException) -> None:
            del failure

        with mock.patch(
            "neuro_code.adapters.windows_pty.WindowsPseudoConsoleSession.spawn",
            return_value=native,
        ) as spawn:
            platform = WindowsConPtyPlatform(diagnostic_capture_bytes=1234)
            session = platform.spawn_exec(
                "python.exe",
                ("-u", "-c", "pass"),
                cwd=Path("C:/workspace"),
                env={"PATH": "C:/bin"},
                size=TerminalSize(90, 25),
                on_output=output,
                on_eof=eof,
                on_error=error,
            )

        self.assertIsInstance(session, WindowsConPtySession)
        spawn.assert_called_once_with(
            ("python.exe", "-u", "-c", "pass"),
            cwd=Path("C:/workspace"),
            env={"PATH": "C:/bin"},
            columns=90,
            rows=25,
            max_output_bytes=1234,
            on_output=output,
            on_eof=eof,
            on_error=error,
        )


if __name__ == "__main__":
    unittest.main()
