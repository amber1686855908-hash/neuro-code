from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from neuro_code.application.ports.sandbox import LocalProcessLifecycleCapability
from neuro_code.domain.terminal import TerminalSignal, TerminalSize
from neuro_code.infrastructure.sandbox.posix_pty import PosixPtySession


class _FakeProcess:
    pid = 12_345

    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _StoppedThread:
    def __init__(self) -> None:
        self.join_calls: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)

    def is_alive(self) -> bool:
        return False


@unittest.skipUnless(os.name == "posix", "POSIX PTY integration contract")
class PosixPtySessionTests(unittest.TestCase):
    @staticmethod
    def _wait_for_output(
        output: bytearray,
        lock: threading.Lock,
        expected: bytes,
    ) -> bytes:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with lock:
                captured = bytes(output)
            if expected in captured:
                return captured
            time.sleep(0.01)
        raise AssertionError(f"PTY output did not contain {expected!r}: {captured!r}")

    @staticmethod
    def _wait_for_exit(session: PosixPtySession) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            exit_code = session.poll_exit()
            if exit_code is not None:
                return exit_code
            time.sleep(0.01)
        raise AssertionError("PTY process did not exit")

    @staticmethod
    def _session_for_close(process: _FakeProcess) -> PosixPtySession:
        session = object.__new__(PosixPtySession)
        session._master_fd = os.open(os.devnull, os.O_RDONLY)
        session._process = process  # type: ignore[assignment]
        session._boundary_pidfd = None
        session._pidfd_ops = None
        session._lifecycle_capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT
        session._size = TerminalSize(80, 24)
        session._on_output = lambda _: None
        session._on_eof = lambda: None
        session._on_error = lambda _: None
        session._closed = False
        session._closing = threading.Event()
        session._lock = threading.RLock()
        session._reader = _StoppedThread()  # type: ignore[assignment]
        session._waiter = _StoppedThread()  # type: ignore[assignment]
        return session

    def test_spawn_resize_input_output_and_nonzero_exit_share_one_pty(self) -> None:
        output = bytearray()
        lock = threading.Lock()
        eof = threading.Event()
        errors: list[BaseException] = []

        def capture(data: bytes) -> None:
            with lock:
                output.extend(data)

        code = """
import os
import sys

initial = os.get_terminal_size()
print(f"initial:{initial.columns}x{initial.lines}", flush=True)
line = input()
resized = os.get_terminal_size()
print(f"resized:{resized.columns}x{resized.lines}:{line}", flush=True)
raise SystemExit(7)
"""
        with TemporaryDirectory() as directory:
            session = PosixPtySession.spawn(
                sys.executable,
                ("-u", "-c", code),
                cwd=Path(directory),
                env=os.environ,
                size=TerminalSize(90, 25),
                on_output=capture,
                on_eof=eof.set,
                on_error=errors.append,
            )
            try:
                self._wait_for_output(output, lock, b"initial:90x25")
                session.resize(TerminalSize(120, 40))
                session.write(b"fixture-input\n")
                captured = self._wait_for_output(
                    output,
                    lock,
                    b"resized:120x40:fixture-input",
                )
                self.assertIn(b"fixture-input", captured)
                self.assertEqual(self._wait_for_exit(session), 7)
                self.assertTrue(eof.wait(timeout=5))
                self.assertEqual(errors, [])
            finally:
                session.close()

    def test_spawn_closes_inheritable_controller_file_descriptors(self) -> None:
        output = bytearray()
        lock = threading.Lock()

        def capture(data: bytes) -> None:
            with lock:
                output.extend(data)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            sensitive = root / "sensitive"
            sensitive.write_text("controller-secret", encoding="utf-8")
            descriptor = os.open(sensitive, os.O_RDONLY)
            try:
                os.set_inheritable(descriptor, True)
                details = os.fstat(descriptor)
                code = (
                    "import os,sys; fd=int(sys.argv[1]); expected=(int(sys.argv[2]),int(sys.argv[3]));"
                    "\ntry: actual=(os.fstat(fd).st_dev,os.fstat(fd).st_ino)"
                    "\nexcept OSError: actual=None"
                    "\nprint('inherited' if actual == expected else 'closed', flush=True)"
                )
                session = PosixPtySession.spawn(
                    sys.executable,
                    ("-c", code, str(descriptor), str(details.st_dev), str(details.st_ino)),
                    cwd=root,
                    env=os.environ,
                    size=TerminalSize(80, 24),
                    on_output=capture,
                    on_eof=lambda: None,
                    on_error=lambda _: None,
                )
                try:
                    captured = self._wait_for_output(output, lock, b"closed")
                    self.assertNotIn(b"inherited", captured)
                finally:
                    session.close()
            finally:
                os.close(descriptor)

    def test_close_retries_transient_group_permission_error_after_direct_reap(self) -> None:
        process = _FakeProcess()
        session = self._session_for_close(process)
        transient = PermissionError(1, "transient group state")
        with mock.patch(
            "neuro_code.infrastructure.sandbox.posix_pty.os.killpg",
            side_effect=(transient, ProcessLookupError()),
        ) as killpg:
            session.close()

        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGTERM),
            ],
        )
        self.assertEqual(process.wait_calls, [0.1, 5.0])

    def test_close_keeps_persistent_group_permission_error_fail_closed(self) -> None:
        process = _FakeProcess()
        session = self._session_for_close(process)
        persistent = PermissionError(1, "persistent group denial")
        with (
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.os.killpg",
                side_effect=(PermissionError(1, "transient group state"), persistent),
            ) as killpg,
            self.assertRaisesRegex(PermissionError, "persistent group denial"),
        ):
            session.close()

        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGTERM),
            ],
        )
        self.assertEqual(process.wait_calls, [0.1, 5.0])

    def test_close_skips_force_signal_when_group_is_already_gone(self) -> None:
        process = _FakeProcess()
        session = self._session_for_close(process)
        with mock.patch(
            "neuro_code.infrastructure.sandbox.posix_pty.os.killpg",
            side_effect=ProcessLookupError(),
        ) as killpg:
            session.close()

        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(process.wait_calls, [5.0])
        self.assertEqual(session._reader.join_calls, [5.0])
        self.assertEqual(session._waiter.join_calls, [5.0])

    def test_close_kills_group_that_survives_term_grace(self) -> None:
        process = _FakeProcess()
        session = self._session_for_close(process)
        with (
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.os.killpg",
                return_value=None,
            ) as killpg,
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.time.monotonic",
                side_effect=(0.0, 1.0),
            ),
            mock.patch("neuro_code.infrastructure.sandbox.posix_pty.time.sleep") as sleep,
        ):
            session.close()

        self.assertEqual(
            [call.args[1] for call in killpg.call_args_list],
            [signal.SIGTERM, 0, 0, signal.SIGKILL],
        )
        sleep.assert_not_called()

    def test_close_does_not_kill_group_that_exits_during_term_grace(self) -> None:
        process = _FakeProcess()
        session = self._session_for_close(process)
        with (
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.os.killpg",
                side_effect=(None, None, ProcessLookupError()),
            ) as killpg,
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.time.monotonic",
                side_effect=(0.0, 0.5),
            ),
            mock.patch("neuro_code.infrastructure.sandbox.posix_pty.time.sleep") as sleep,
        ):
            session.close()

        self.assertEqual(
            [call.args[1] for call in killpg.call_args_list],
            [signal.SIGTERM, 0, 0],
        )
        sleep.assert_called_once_with(0.01)

    def test_close_keeps_group_cleanup_when_leader_already_exited(self) -> None:
        process = _FakeProcess(returncode=0)
        session = self._session_for_close(process)
        with (
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.os.killpg",
                return_value=None,
            ) as killpg,
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.time.monotonic",
                side_effect=(0.0, 1.0),
            ),
        ):
            session.close()

        self.assertEqual(
            [call.args[1] for call in killpg.call_args_list],
            [signal.SIGTERM, 0, 0, signal.SIGKILL],
        )

    def test_close_is_idempotent_after_success(self) -> None:
        process = _FakeProcess()
        session = self._session_for_close(process)
        with mock.patch(
            "neuro_code.infrastructure.sandbox.posix_pty.os.killpg",
            side_effect=ProcessLookupError(),
        ) as killpg:
            session.close()
            session.close()

        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(process.wait_calls, [5.0])
        self.assertEqual(session._reader.join_calls, [5.0])
        self.assertEqual(session._waiter.join_calls, [5.0])

    def test_pidfd_close_does_not_fallback_to_process_group(self) -> None:
        class RecordingPidfdOps:
            def __init__(self) -> None:
                self.signals: list[int] = []

            def send_signal(self, pidfd: int, native_signal: int) -> None:
                del pidfd
                self.signals.append(native_signal)

        process = _FakeProcess(returncode=0)
        session = self._session_for_close(process)
        operations = RecordingPidfdOps()
        session._boundary_pidfd = os.open(os.devnull, os.O_RDONLY)
        session._pidfd_ops = operations  # type: ignore[assignment]
        with (
            mock.patch("neuro_code.infrastructure.sandbox.posix_pty.os.killpg") as killpg,
            mock.patch(
                "neuro_code.infrastructure.sandbox.posix_pty.time.monotonic",
                return_value=0.0,
            ),
        ):
            session.close()

        self.assertEqual(operations.signals, [signal.SIGTERM])
        killpg.assert_not_called()

    def test_interrupt_targets_the_owned_process_group(self) -> None:
        output = bytearray()
        lock = threading.Lock()
        errors: list[BaseException] = []

        def capture(data: bytes) -> None:
            with lock:
                output.extend(data)

        code = """
import signal
import time

signal.signal(signal.SIGINT, lambda *_: raise_exit())
def raise_exit():
    raise SystemExit(23)
print("ready", flush=True)
while True:
    time.sleep(1)
"""
        with TemporaryDirectory() as directory:
            session = PosixPtySession.spawn(
                sys.executable,
                ("-u", "-c", code),
                cwd=Path(directory),
                env=os.environ,
                size=TerminalSize(80, 24),
                on_output=capture,
                on_eof=lambda: None,
                on_error=errors.append,
            )
            try:
                self._wait_for_output(output, lock, b"ready")
                session.send_signal(TerminalSignal.INTERRUPT)
                self.assertEqual(self._wait_for_exit(session), 23)
                self.assertEqual(errors, [])
            finally:
                session.close()

    def test_non_posix_default_fails_before_opening_a_pty(self) -> None:
        with (
            mock.patch("neuro_code.infrastructure.sandbox.posix_pty.os.name", "nt"),
            self.assertRaisesRegex(OSError, "only available on POSIX"),
        ):
            PosixPtySession.spawn(
                "python",
                (),
                cwd=Path("."),
                env={},
                size=TerminalSize(80, 24),
                on_output=lambda _: None,
                on_eof=lambda: None,
                on_error=lambda _: None,
            )

    def test_secure_boundary_close_uses_pidfd_operations(self) -> None:
        class RecordingPidfdOps:
            def __init__(self) -> None:
                self.pid: int | None = None
                self.signals: list[int] = []

            def open(self, pid: int) -> int:
                self.pid = pid
                return os.open(os.devnull, os.O_RDONLY)

            def send_signal(self, pidfd: int, native_signal: int) -> None:
                self.signals.append(native_signal)
                if self.pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(self.pid, native_signal)

            def probe(self) -> None:
                return

        operations = RecordingPidfdOps()
        with TemporaryDirectory() as directory:
            session = PosixPtySession.spawn(
                sys.executable,
                ("-c", "import time; time.sleep(30)"),
                cwd=Path(directory),
                env=os.environ,
                size=TerminalSize(80, 24),
                on_output=lambda _: None,
                on_eof=lambda: None,
                on_error=lambda _: None,
                pidfd_ops=operations,
            )
            session.close()

        self.assertIn(signal.SIGTERM, operations.signals)
        self.assertIsNotNone(operations.pid)


if __name__ == "__main__":
    unittest.main()
