from __future__ import annotations

import errno
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from neuro_code.adapters.windows_conpty import WindowsPseudoConsoleSession

pytestmark = pytest.mark.terminal

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SMOKE_ENVIRONMENT = "NEURO_CODE_RUN_TERMINAL_SMOKE"
_HEADLESS_DRIVER = "textual.drivers.headless_driver:HeadlessDriver"


def _write_offline_config(root: Path) -> Path:
    state = root / "state"
    state.mkdir()
    (state / "config.toml").write_text(
        """
[routing]
default = "terminal-smoke"

[providers.terminal-smoke]
protocol = "openai-chat"
model = "offline-smoke-model"
base_url = "https://provider.invalid/v1"
api_key_env = "TERMINAL_SMOKE_KEY"
""",
        encoding="utf-8",
    )
    return state


def _smoke_environment(
    root: Path,
    state: Path,
    *,
    auto_quit: bool = True,
) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in (
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "PYTHONPATH",
            "SYSTEMROOT",
            "TEMP",
            "TERMINFO",
            "TERMINFO_DIRS",
            "TMP",
            "TMPDIR",
            "VIRTUAL_ENV",
            "WINDIR",
        )
        if name in os.environ
    }
    environment.update(
        {
            "HOME": str(root),
            "USERPROFILE": str(root),
            "NEURO_CODE_HOME": str(state),
            "TERMINAL_SMOKE_KEY": "offline-fixture-key",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "COLUMNS": "100",
            "LINES": "30",
            "PYTHONUNBUFFERED": "1",
            "TEXTUAL_ANIMATIONS": "none",
        }
    )
    if auto_quit:
        environment["TEXTUAL_PRESS"] = "ctrl+q"
    return environment


def _drain_pty(master_fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(master_fd, 65_536)
        except BlockingIOError:
            break
        except OSError as error:
            if error.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def test_production_cli_composes_and_exits_with_the_headless_driver() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _write_offline_config(root)
        environment = _smoke_environment(root, state)
        environment["TEXTUAL_DRIVER"] = _HEADLESS_DRIVER

        completed = subprocess.run(
            [sys.executable, "-m", "neuro_code", "--cwd", str(root)],
            cwd=_PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )

        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, output.decode("utf-8", errors="replace")
        assert b"offline-fixture-key" not in output


@pytest.mark.skipif(os.name != "posix", reason="stdlib PTY and termios require POSIX")
@pytest.mark.skipif(
    os.environ.get(_SMOKE_ENVIRONMENT) != "1",
    reason=f"set {_SMOKE_ENVIRONMENT}=1 to run the native terminal smoke test",
)
def test_production_cli_ctrl_q_restores_the_native_terminal() -> None:
    import fcntl
    import pty
    import termios

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _write_offline_config(root)
        environment = _smoke_environment(root, state, auto_quit=False)

        master_fd, slave_fd = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        output = bytearray()
        quit_sent = False
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            terminal_before = termios.tcgetattr(slave_fd)
            os.set_blocking(master_fd, False)
            process = subprocess.Popen(
                [sys.executable, "-m", "neuro_code", "--cwd", str(root)],
                cwd=_PROJECT_ROOT,
                env=environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )

            deadline = time.monotonic() + 15.0
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait(timeout=5)
                    pytest.fail("production TUI did not exit within 15 seconds")
                readable, _, _ = select.select((master_fd,), (), (), 0.05)
                if readable:
                    output.extend(_drain_pty(master_fd))
                if not quit_sent and b"\x1b[?1049h" in output:
                    try:
                        os.write(master_fd, b"\x11")
                    except BlockingIOError:
                        pass
                    else:
                        quit_sent = True

            output.extend(_drain_pty(master_fd))
            terminal_after = termios.tcgetattr(slave_fd)
            exit_code = process.wait(timeout=1)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(slave_fd)
            os.close(master_fd)

        rendered = bytes(output)
        assert quit_sent, rendered.decode("utf-8", errors="replace")
        assert exit_code == 0, rendered.decode("utf-8", errors="replace")
        assert terminal_after == terminal_before
        for enabled, disabled in (
            (b"\x1b[?1049h", b"\x1b[?1049l"),
            (b"\x1b[?25l", b"\x1b[?25h"),
            (b"\x1b[?1004h", b"\x1b[?1004l"),
        ):
            assert enabled in rendered
            assert disabled in rendered
            assert rendered.rfind(disabled) > rendered.find(enabled)
        assert b"offline-fixture-key" not in rendered


def _wait_for_conpty_output(
    session: WindowsPseudoConsoleSession,
    marker: bytes,
    *,
    timeout_seconds: float = 15,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while marker not in session.output:
        if not session.running:
            pytest.fail(
                "ConPTY child exited before expected output:\n"
                + session.output.decode("utf-8", errors="replace")
            )
        if time.monotonic() >= deadline:
            pytest.fail(
                f"ConPTY output did not contain {marker!r} within {timeout_seconds:g} seconds:\n"
                + session.output.decode("utf-8", errors="replace")
            )
        time.sleep(0.02)


def _windows_console_modes() -> tuple[tuple[int, int], ...]:
    if os.name != "nt":
        return ()
    import ctypes

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return ()
    kernel32 = loader("kernel32.dll", use_last_error=True)
    get_std_handle = kernel32.GetStdHandle
    get_std_handle.argtypes = [ctypes.c_uint32]
    get_std_handle.restype = ctypes.c_void_p
    get_console_mode = kernel32.GetConsoleMode
    get_console_mode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    get_console_mode.restype = ctypes.c_int32

    modes: list[tuple[int, int]] = []
    invalid_handle = ctypes.c_void_p(-1).value
    for stream_id in (-10, -11, -12):
        handle = get_std_handle(stream_id & 0xFFFFFFFF)
        if handle in (None, 0, invalid_handle):
            continue
        mode = ctypes.c_uint32()
        if get_console_mode(handle, ctypes.byref(mode)):
            modes.append((stream_id, int(mode.value)))
    return tuple(modes)


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY is required")
@pytest.mark.skipif(
    os.environ.get(_SMOKE_ENVIRONMENT) != "1",
    reason=f"set {_SMOKE_ENVIRONMENT}=1 to run the native terminal smoke test",
)
def test_production_cli_ctrl_keys_resize_and_restore_through_windows_conpty() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _write_offline_config(root)
        environment = _smoke_environment(root, state, auto_quit=False)
        console_modes_before = _windows_console_modes()
        session = WindowsPseudoConsoleSession.spawn(
            (sys.executable, "-m", "neuro_code", "--cwd", str(root)),
            cwd=_PROJECT_ROOT,
            env=environment,
            columns=100,
            rows=30,
        )
        try:
            _wait_for_conpty_output(session, b"\x1b[?1049h")
            session.resize(120, 40)
            session.write(b"\x03")
            time.sleep(0.1)
            assert session.running, session.output.decode("utf-8", errors="replace")
            session.write(b"\x11")
            exit_code = session.wait(15)
            if exit_code is None:
                pytest.fail("production TUI did not exit within 15 seconds")
        finally:
            session.close()

        rendered = session.output
        assert exit_code == 0, rendered.decode("utf-8", errors="replace")
        assert not session.output_truncated
        assert _windows_console_modes() == console_modes_before
        for enabled, disabled in (
            (b"\x1b[?1049h", b"\x1b[?1049l"),
            (b"\x1b[?25l", b"\x1b[?25h"),
            (b"\x1b[?1004h", b"\x1b[?1004l"),
        ):
            assert enabled in rendered
            assert disabled in rendered
            assert rendered.rfind(disabled) > rendered.find(enabled)
        assert b"offline-fixture-key" not in rendered


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY is required")
@pytest.mark.skipif(
    os.environ.get(_SMOKE_ENVIRONMENT) != "1",
    reason=f"set {_SMOKE_ENVIRONMENT}=1 to run the native terminal smoke test",
)
def test_windows_conpty_reports_resize_and_preserves_nonzero_exit() -> None:
    child = (
        "import msvcrt,os,sys;"
        "size=os.get_terminal_size();"
        "print(f'SIZE1={size.columns}x{size.lines}',flush=True);"
        "msvcrt.getwch();"
        "size=os.get_terminal_size();"
        "print(f'SIZE2={size.columns}x{size.lines}',flush=True);"
        "sys.exit(7)"
    )
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    session = WindowsPseudoConsoleSession.spawn(
        (sys.executable, "-c", child),
        cwd=_PROJECT_ROOT,
        env=environment,
        columns=100,
        rows=30,
    )
    try:
        _wait_for_conpty_output(session, b"SIZE1=100x30")
        session.resize(120, 40)
        time.sleep(0.05)
        session.write(b"x")
        exit_code = session.wait(10)
        if exit_code is None:
            pytest.fail("ConPTY resize probe did not exit within 10 seconds")
    finally:
        session.close()

    rendered = session.output
    assert exit_code == 7, rendered.decode("utf-8", errors="replace")
    assert b"SIZE2=120x40" in rendered
    assert not session.output_truncated
