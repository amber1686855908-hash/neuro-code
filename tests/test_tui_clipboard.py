from __future__ import annotations

import subprocess
import unittest
from collections.abc import Sequence
from unittest.mock import patch

from neuro_code.interfaces.tui.clipboard import ClipboardWriteResult, SystemClipboardWriter


class SystemClipboardWriterTests(unittest.TestCase):
    def test_result_rejects_inconsistent_native_copy_state(self) -> None:
        with self.assertRaises(ValueError):
            ClipboardWriteResult(native_copied=True)
        with self.assertRaises(ValueError):
            ClipboardWriteResult(native_copied=False, method="x11-xclip")

    def test_prefers_xclip_on_x11(self) -> None:
        calls: list[tuple[tuple[str, ...], bytes]] = []

        def run(command: Sequence[str], payload: bytes) -> bool:
            calls.append((tuple(command), payload))
            return True

        writer = SystemClipboardWriter(
            environment={"DISPLAY": ":1"},
            platform_name="linux",
            find_executable=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
            run_command=run,
        )

        result = writer.copy("hello\n世界")

        self.assertTrue(result.native_copied)
        self.assertEqual(result.method, "x11-xclip")
        self.assertEqual(
            calls,
            [
                (
                    ("/usr/bin/xclip", "-selection", "clipboard", "-in"),
                    "hello\n世界".encode(),
                )
            ],
        )

    def test_uses_xsel_after_xclip_fails(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(command: Sequence[str], payload: bytes) -> bool:
            del payload
            calls.append(tuple(command))
            return command[0].endswith("xsel")

        writer = SystemClipboardWriter(
            environment={"DISPLAY": ":1"},
            platform_name="linux",
            find_executable=lambda name: f"/usr/bin/{name}" if name in {"xclip", "xsel"} else None,
            run_command=run,
        )

        result = writer.copy("copy me")

        self.assertTrue(result.native_copied)
        self.assertEqual(result.method, "x11-xsel")
        self.assertEqual(
            calls,
            [
                ("/usr/bin/xclip", "-selection", "clipboard", "-in"),
                ("/usr/bin/xsel", "--clipboard", "--input"),
            ],
        )

    def test_prefers_wl_copy_on_wayland(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(command: Sequence[str], payload: bytes) -> bool:
            del payload
            calls.append(tuple(command))
            return True

        writer = SystemClipboardWriter(
            environment={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":1"},
            platform_name="linux",
            find_executable=lambda name: f"/usr/bin/{name}" if name == "wl-copy" else None,
            run_command=run,
        )

        result = writer.copy("wayland")

        self.assertTrue(result.native_copied)
        self.assertEqual(result.method, "wayland-wl-copy")
        self.assertEqual(
            calls,
            [("/usr/bin/wl-copy", "--type", "text/plain;charset=utf-8")],
        )

    def test_uses_pbcopy_on_macos(self) -> None:
        calls: list[tuple[tuple[str, ...], bytes]] = []

        def run(command: Sequence[str], payload: bytes) -> bool:
            calls.append((tuple(command), payload))
            return True

        writer = SystemClipboardWriter(
            environment={},
            platform_name="darwin",
            find_executable=lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None,
            run_command=run,
        )

        result = writer.copy("macOS")

        self.assertTrue(result.native_copied)
        self.assertEqual(result.method, "macos-pbcopy")
        self.assertEqual(calls, [(("/usr/bin/pbcopy",), b"macOS")])

    def test_uses_clip_exe_with_unicode_encoding_on_windows(self) -> None:
        calls: list[tuple[tuple[str, ...], bytes]] = []

        def run(command: Sequence[str], payload: bytes) -> bool:
            calls.append((tuple(command), payload))
            return True

        writer = SystemClipboardWriter(
            environment={},
            platform_name="win32",
            find_executable=lambda name: (
                r"C:\Windows\System32\clip.exe" if name == "clip.exe" else None
            ),
            run_command=run,
        )

        result = writer.copy("中文")

        self.assertTrue(result.native_copied)
        self.assertEqual(result.method, "windows-clip")
        self.assertEqual(
            calls,
            [
                (
                    (r"C:\Windows\System32\clip.exe",),
                    b"\xff\xfe" + "中文".encode("utf-16le"),
                )
            ],
        )

    def test_falls_back_cleanly_when_no_native_clipboard_is_available(self) -> None:
        writer = SystemClipboardWriter(
            environment={"DISPLAY": ":1"},
            platform_name="linux",
            find_executable=lambda name: None,
        )

        result = writer.copy("copy me")

        self.assertFalse(result.native_copied)
        self.assertIsNone(result.method)

    def test_does_not_start_x11_or_wayland_programs_without_a_desktop_session(self) -> None:
        ran = False

        def run(command: Sequence[str], payload: bytes) -> bool:
            nonlocal ran
            del command, payload
            ran = True
            return True

        writer = SystemClipboardWriter(
            environment={},
            platform_name="linux",
            find_executable=lambda name: f"/usr/bin/{name}",
            run_command=run,
        )

        result = writer.copy("headless")

        self.assertFalse(result.native_copied)
        self.assertFalse(ran)

    def test_command_runner_treats_program_failure_and_timeout_as_copy_failure(self) -> None:
        with patch(
            "neuro_code.interfaces.tui.clipboard.subprocess.run",
            return_value=subprocess.CompletedProcess(("xclip",), 0),
        ) as run:
            writer = SystemClipboardWriter(
                environment={"DISPLAY": ":1"},
                platform_name="linux",
                find_executable=lambda name: "/usr/bin/xclip" if name == "xclip" else None,
            )
            self.assertTrue(writer.copy("copied").native_copied)
            run.assert_called_once()

        with patch(
            "neuro_code.interfaces.tui.clipboard.subprocess.run",
            return_value=subprocess.CompletedProcess(("xclip",), 1),
        ):
            writer = SystemClipboardWriter(
                environment={"DISPLAY": ":1"},
                platform_name="linux",
                find_executable=lambda name: "/usr/bin/xclip" if name == "xclip" else None,
            )
            self.assertFalse(writer.copy("failed").native_copied)

        with patch(
            "neuro_code.interfaces.tui.clipboard.subprocess.run",
            side_effect=subprocess.TimeoutExpired(("xclip",), 2.0),
        ):
            writer = SystemClipboardWriter(
                environment={"DISPLAY": ":1"},
                platform_name="linux",
                find_executable=lambda name: "/usr/bin/xclip" if name == "xclip" else None,
            )
            self.assertFalse(writer.copy("timed out").native_copied)

        with patch(
            "neuro_code.interfaces.tui.clipboard.subprocess.run",
            side_effect=OSError("clipboard command missing"),
        ):
            writer = SystemClipboardWriter(
                environment={"DISPLAY": ":1"},
                platform_name="linux",
                find_executable=lambda name: "/usr/bin/xclip" if name == "xclip" else None,
            )
            self.assertFalse(writer.copy("missing").native_copied)

    def test_writer_continues_to_the_next_candidate_after_an_adapter_error(self) -> None:
        def run(command: Sequence[str], payload: bytes) -> bool:
            del payload
            if command[0].endswith("xclip"):
                raise OSError("xclip unavailable")
            return True

        writer = SystemClipboardWriter(
            environment={"DISPLAY": ":1"},
            platform_name="linux",
            find_executable=lambda name: f"/usr/bin/{name}" if name in {"xclip", "xsel"} else None,
            run_command=run,
        )

        result = writer.copy("fallback")

        self.assertTrue(result.native_copied)
        self.assertEqual(result.method, "x11-xsel")
