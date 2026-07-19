from __future__ import annotations

import ctypes
import queue
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from neuro_code.adapters.windows_conpty import (
    WindowsPseudoConsoleSession,
    _Coord,
    _CreatedProcess,
    _environment_block,
    _NativeWindowsConPtyApi,
    _ProcessInformation,
    _StartupInfoExW,
)


class _FakeWindowsConPtyApi:
    def __init__(self) -> None:
        self.pipe_results = [(10, 11), (12, 13)]
        self.pseudo_console_handle = 20
        self.created_process = _CreatedProcess(30, 31, 40)
        self.process_done = False
        self.exit_code = 7
        self.max_write_size: int | None = None
        self.fail_operation: str | None = None
        self.close_failures: set[int] = set()
        self.calls: list[tuple[object, ...]] = []
        self.output: queue.Queue[bytes | OSError] = queue.Queue()

    def create_pipe(self) -> tuple[int, int]:
        self.calls.append(("create_pipe",))
        if self.fail_operation == f"pipe-{len(self.calls_of('create_pipe'))}":
            raise OSError("pipe failed")
        return self.pipe_results.pop(0)

    def create_pseudo_console(
        self,
        columns: int,
        rows: int,
        input_read_handle: int,
        output_write_handle: int,
    ) -> int:
        self.calls.append(
            (
                "create_pseudo_console",
                columns,
                rows,
                input_read_handle,
                output_write_handle,
            )
        )
        if self.fail_operation == "pseudo_console":
            raise OSError("pseudo console failed")
        return self.pseudo_console_handle

    def create_process(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        pseudo_console_handle: int,
        job_handle: int | None = None,
    ) -> _CreatedProcess:
        self.calls.append(
            (
                "create_process",
                arguments,
                cwd,
                dict(env),
                pseudo_console_handle,
                job_handle,
            )
        )
        if self.fail_operation == "process":
            raise OSError("process failed")
        return self.created_process

    def resize_pseudo_console(self, handle: int, columns: int, rows: int) -> None:
        self.calls.append(("resize", handle, columns, rows))
        if self.fail_operation == "resize":
            raise OSError("resize failed")

    def close_pseudo_console(self, handle: int) -> None:
        self.calls.append(("close_pseudo_console", handle))
        self.output.put(b"")
        if self.fail_operation == "close_pseudo_console":
            raise OSError("close pseudo console failed")

    def read_file(self, handle: int, byte_count: int) -> bytes:
        self.calls.append(("read", handle, byte_count))
        result = self.output.get(timeout=5)
        if isinstance(result, OSError):
            raise result
        return result

    def write_file(self, handle: int, data: bytes) -> int:
        self.calls.append(("write", handle, data))
        if self.fail_operation == "write":
            raise OSError("write failed")
        if self.max_write_size is None:
            return len(data)
        return min(self.max_write_size, len(data))

    def wait_process(self, handle: int, timeout_milliseconds: int) -> bool:
        self.calls.append(("wait", handle, timeout_milliseconds))
        if self.fail_operation == "wait":
            raise OSError("wait failed")
        return self.process_done

    def get_exit_code(self, handle: int) -> int:
        self.calls.append(("exit_code", handle))
        if self.fail_operation == "exit_code":
            raise OSError("exit code failed")
        return self.exit_code

    def terminate_process(self, handle: int, exit_code: int) -> None:
        self.calls.append(("terminate", handle, exit_code))
        if self.fail_operation == "terminate":
            raise OSError("terminate failed")
        self.process_done = True
        self.exit_code = exit_code

    def close_handle(self, handle: int) -> None:
        self.calls.append(("close", handle))
        if handle == 12:
            self.output.put(b"")
        if handle in self.close_failures:
            raise OSError(f"close {handle} failed")

    def calls_of(self, name: str) -> list[tuple[object, ...]]:
        return [call for call in self.calls if call[0] == name]


class _FakeWindowsJob:
    def __init__(self, api: _FakeWindowsConPtyApi) -> None:
        self.api = api
        self.process_creation_handle = 99
        self.calls: list[tuple[object, ...]] = []

    def terminate(self, exit_code: int = 1) -> None:
        self.calls.append(("terminate", exit_code))
        self.api.process_done = True
        self.api.exit_code = exit_code

    def close(self) -> None:
        self.calls.append(("close",))


class WindowsPseudoConsoleSessionTests(unittest.TestCase):
    def _spawn(
        self,
        api: _FakeWindowsConPtyApi,
        *,
        max_output_bytes: int = 1024,
    ) -> WindowsPseudoConsoleSession:
        return WindowsPseudoConsoleSession.spawn(
            ("python.exe", "-c", "pass"),
            cwd=Path("C:/workspace"),
            env={"Path": "C:/bin", "TERM": "xterm-256color"},
            columns=100,
            rows=30,
            max_output_bytes=max_output_bytes,
            api=api,
        )

    def test_non_windows_default_creation_fails_cleanly(self) -> None:
        cwd = Path(".")
        with (
            mock.patch("neuro_code.adapters.windows_conpty.os.name", "posix"),
            self.assertRaisesRegex(OSError, "only available on Windows"),
        ):
            WindowsPseudoConsoleSession.spawn(
                ("python",),
                cwd=cwd,
                env={},
            )

    def test_spawn_wires_conpty_process_and_closes_child_side_handles(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.output.put(b"frame-one")
        api.output.put(b"-frame-two")
        api.output.put(b"")

        session = self._spawn(api)
        api.process_done = True
        self.assertEqual(session.wait(1), 7)
        session.close()

        self.assertEqual(session.process_id, 40)
        self.assertEqual(session.output, b"frame-one-frame-two")
        self.assertFalse(session.output_truncated)
        self.assertIn(("create_pseudo_console", 100, 30, 10, 13), api.calls)
        self.assertIn(
            (
                "create_process",
                ("python.exe", "-c", "pass"),
                Path("C:/workspace"),
                {"Path": "C:/bin", "TERM": "xterm-256color"},
                20,
                None,
            ),
            api.calls,
        )
        for handle in (31, 10, 13, 11, 12, 30):
            self.assertEqual(api.calls.count(("close", handle)), 1)
        self.assertLess(
            api.calls.index(("close_pseudo_console", 20)), api.calls.index(("close", 12))
        )

    def test_write_handles_partial_progress_and_resize_is_explicit(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.max_write_size = 2
        session = self._spawn(api)
        try:
            session.write(b"abcde")
            session.resize(120, 40)

            self.assertEqual(
                api.calls_of("write"),
                [
                    ("write", 11, b"abcde"),
                    ("write", 11, b"cde"),
                    ("write", 11, b"e"),
                ],
            )
            self.assertIn(("resize", 20, 120, 40), api.calls)
        finally:
            session.close()

    def test_wait_timeout_then_terminate_preserves_the_exit_code(self) -> None:
        api = _FakeWindowsConPtyApi()
        session = self._spawn(api)

        self.assertIsNone(session.wait(0.025))
        self.assertTrue(session.running)
        session.terminate(exit_code=23)
        self.assertEqual(session.wait(1), 23)
        waits_before = len(api.calls_of("wait"))
        self.assertEqual(session.wait(999), 23)
        self.assertEqual(len(api.calls_of("wait")), waits_before)
        session.close()

    def test_output_capture_is_bounded_at_both_ends(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.output.put(b"abcd")
        api.output.put(b"efgh")
        api.output.put(b"")
        session = self._spawn(api, max_output_bytes=6)
        api.process_done = True
        session.close()

        self.assertEqual(session.output, b"abcfgh")
        self.assertTrue(session.output_truncated)

    def test_output_callbacks_receive_frames_and_eof(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.output.put(b"first")
        api.output.put(b"second")
        api.output.put(b"")
        output: list[bytes] = []
        errors: list[BaseException] = []
        eof = threading.Event()

        session = WindowsPseudoConsoleSession.spawn(
            ("python.exe",),
            cwd=Path("C:/workspace"),
            env={},
            api=api,
            on_output=output.append,
            on_eof=eof.set,
            on_error=errors.append,
        )
        api.process_done = True
        session.close()

        self.assertTrue(eof.is_set())
        self.assertEqual(output, [b"first", b"second"])
        self.assertEqual(errors, [])

    def test_output_callback_failure_is_reported_without_stopping_pipe_drain(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.output.put(b"first")
        api.output.put(b"second")
        api.output.put(b"")
        errors: list[BaseException] = []

        def fail_output(_: bytes) -> None:
            raise RuntimeError("observer failed")

        session = WindowsPseudoConsoleSession.spawn(
            ("python.exe",),
            cwd=Path("C:/workspace"),
            env={},
            api=api,
            on_output=fail_output,
            on_error=errors.append,
        )
        api.process_done = True
        with self.assertRaisesRegex(RuntimeError, "observer failed"):
            session.close()

        self.assertEqual(session.output, b"firstsecond")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_transferred_job_is_used_for_atomic_creation_and_tree_termination(self) -> None:
        api = _FakeWindowsConPtyApi()
        job = _FakeWindowsJob(api)
        session = WindowsPseudoConsoleSession.spawn(
            ("python.exe",),
            cwd=Path("C:/workspace"),
            env={},
            api=api,
            job=job,  # type: ignore[arg-type]
        )

        self.assertEqual(api.calls_of("create_process")[0][-1], 99)
        session.terminate(23)
        session.close()

        self.assertEqual(job.calls, [("terminate", 23), ("close",)])
        self.assertNotIn(("terminate", 30, 23), api.calls)

    def test_default_native_path_creates_and_transfers_a_kill_on_close_job(self) -> None:
        api = _FakeWindowsConPtyApi()
        job = _FakeWindowsJob(api)
        api.process_done = True
        with (
            mock.patch(
                "neuro_code.adapters.windows_conpty._NativeWindowsConPtyApi",
                return_value=api,
            ),
            mock.patch(
                "neuro_code.adapters.windows_conpty.WindowsJobObject.create",
                return_value=job,
            ) as create_job,
        ):
            session = WindowsPseudoConsoleSession.spawn(
                ("python.exe",),
                cwd=Path("C:/workspace"),
                env={},
            )
        session.close()

        create_job.assert_called_once_with()
        self.assertEqual(api.calls_of("create_process")[0][-1], 99)
        self.assertEqual(job.calls, [("close",)])

    def test_second_pipe_failure_closes_the_first_pair(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.fail_operation = "pipe-2"

        with self.assertRaisesRegex(OSError, "pipe failed"):
            self._spawn(api)

        self.assertEqual(api.calls_of("close"), [("close", 11), ("close", 10)])

    def test_pseudoconsole_failure_closes_all_four_pipe_handles(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.fail_operation = "pseudo_console"

        with self.assertRaisesRegex(OSError, "pseudo console failed"):
            self._spawn(api)

        self.assertEqual(
            {call[1] for call in api.calls_of("close")},
            {10, 11, 12, 13},
        )
        self.assertNotIn(("close_pseudo_console", 20), api.calls)

    def test_process_failure_closes_output_before_the_pseudoconsole(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.fail_operation = "process"

        with self.assertRaisesRegex(OSError, "process failed"):
            self._spawn(api)

        self.assertLess(
            api.calls.index(("close", 12)), api.calls.index(("close_pseudo_console", 20))
        )
        self.assertEqual(
            {call[1] for call in api.calls_of("close")},
            {10, 11, 12, 13},
        )

    def test_post_create_failure_terminates_process_and_releases_every_handle(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.close_failures.add(31)

        with self.assertRaisesRegex(OSError, "close 31 failed"):
            self._spawn(api)

        self.assertIn(("terminate", 30, 1), api.calls)
        self.assertIn(("close", 30), api.calls)
        self.assertIn(("close_pseudo_console", 20), api.calls)

    def test_close_failure_is_reported_after_remaining_resources_are_released(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.close_failures.add(11)
        api.process_done = True
        session = self._spawn(api)

        with self.assertRaisesRegex(OSError, "close 11 failed"):
            session.close()
        session.close()

        self.assertIn(("close_pseudo_console", 20), api.calls)
        self.assertIn(("close", 12), api.calls)
        self.assertIn(("close", 30), api.calls)
        self.assertEqual(api.calls.count(("close", 11)), 1)

    def test_reader_failure_is_reported_by_close_without_skipping_cleanup(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.output.put(OSError("read failed"))
        api.process_done = True
        session = self._spawn(api)

        with self.assertRaisesRegex(OSError, "read failed"):
            session.close()

        self.assertIn(("close_pseudo_console", 20), api.calls)
        self.assertIn(("close", 30), api.calls)

    def test_closed_session_rejects_operations(self) -> None:
        api = _FakeWindowsConPtyApi()
        api.process_done = True
        session = self._spawn(api)
        session.close()

        with self.assertRaisesRegex(RuntimeError, "input is closed"):
            session.write(b"x")
        with self.assertRaisesRegex(RuntimeError, "pseudoconsole is closed"):
            session.resize(80, 24)
        self.assertEqual(session.wait(0), 7)

    def test_arguments_dimensions_environment_and_output_limit_are_validated(self) -> None:
        api = _FakeWindowsConPtyApi()
        cases: tuple[tuple[object, dict[str, object], type[Exception], str], ...] = (
            ((), {}, ValueError, "must not be empty"),
            ("python", {}, TypeError, "sequence of strings"),
            (("python", "bad\x00arg"), {}, ValueError, "null bytes"),
            (("python",), {"columns": 0}, ValueError, "columns"),
            (("python",), {"rows": 32768}, ValueError, "rows"),
            (("python",), {"max_output_bytes": 0}, ValueError, "positive"),
        )
        for arguments, overrides, error_type, message in cases:
            options: dict[str, object] = {
                "cwd": Path("."),
                "env": {},
                "columns": 80,
                "rows": 24,
                "max_output_bytes": 100,
                "api": api,
            }
            options.update(overrides)
            with (
                self.subTest(arguments=arguments, overrides=overrides),
                self.assertRaisesRegex(error_type, message),
            ):
                WindowsPseudoConsoleSession.spawn(arguments, **options)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "invalid name or value"):
            WindowsPseudoConsoleSession.spawn(
                ("python",),
                cwd=Path("."),
                env={"BAD=NAME": "value"},
                api=api,
            )
        self.assertEqual(api.calls, [])

    def test_environment_block_is_sorted_and_preserves_drive_entries(self) -> None:
        self.assertEqual(
            _environment_block({"z": "last", "=C:": "C:/work", "A": "first"}),
            "=C:=C:/work\x00A=first\x00z=last\x00\x00",
        )

    def test_coord_uses_fixed_width_windows_fields(self) -> None:
        self.assertEqual(_Coord.X.offset, 0)
        self.assertEqual(_Coord.Y.offset, 2)


class NativeWindowsConPtyApiContractTests(unittest.TestCase):
    def test_create_process_builds_job_conpty_attributes_and_unicode_environment(self) -> None:
        api = object.__new__(_NativeWindowsConPtyApi)
        calls: list[tuple[object, ...]] = []

        def initialize(
            attribute_list: object,
            count: int,
            flags: int,
            size_pointer: object,
        ) -> int:
            size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
            calls.append(("initialize", attribute_list is None, count, flags))
            if attribute_list is None:
                size.contents.value = 128
                return 0
            return 1

        def update(
            attribute_list: object,
            flags: int,
            attribute: int,
            value: object,
            value_size: int,
            previous: object,
            return_size: object,
        ) -> int:
            rendered_value: object
            if attribute == 0x0002000D:
                rendered_value = tuple(
                    int(item)
                    for item in ctypes.cast(
                        value,
                        ctypes.POINTER(ctypes.c_void_p),
                    )[: value_size // ctypes.sizeof(ctypes.c_void_p)]
                )
            else:
                rendered_value = value
            calls.append(
                (
                    "update",
                    bool(attribute_list),
                    flags,
                    attribute,
                    rendered_value,
                    value_size,
                    previous,
                    return_size,
                )
            )
            return 1

        def create_process(
            application: str,
            command_line: object,
            process_attributes: object,
            thread_attributes: object,
            inherit_handles: bool,
            creation_flags: int,
            environment_pointer: object,
            cwd: str,
            startup_pointer: object,
            process_pointer: object,
        ) -> int:
            startup = ctypes.cast(
                startup_pointer,
                ctypes.POINTER(_StartupInfoExW),
            ).contents
            calls.append(
                (
                    "create_process",
                    application,
                    ctypes.wstring_at(command_line),
                    process_attributes,
                    thread_attributes,
                    inherit_handles,
                    creation_flags,
                    ctypes.wstring_at(environment_pointer),
                    cwd,
                    int(startup.StartupInfo.cb),
                    bool(startup.lpAttributeList),
                )
            )
            process = ctypes.cast(
                process_pointer,
                ctypes.POINTER(_ProcessInformation),
            ).contents
            process.hProcess = 30
            process.hThread = 31
            process.dwProcessId = 40
            process.dwThreadId = 41
            return 1

        api._initialize_attribute_list = initialize  # type: ignore[method-assign]
        api._update_attribute = update  # type: ignore[method-assign]
        api._delete_attribute_list = lambda attribute_list: calls.append(  # type: ignore[method-assign]
            ("delete", bool(attribute_list))
        )
        api._create_process = create_process  # type: ignore[method-assign]
        api._get_last_error = lambda: 5  # type: ignore[method-assign]

        result = api.create_process(
            ("C:/Python/python.exe", "-c", "print('hello world')"),
            cwd=Path("C:/workspace"),
            env={"z": "last", "A": "first"},
            pseudo_console_handle=20,
            job_handle=99,
        )

        self.assertEqual(result, _CreatedProcess(30, 31, 40))
        self.assertEqual(
            calls[:4],
            [
                ("initialize", True, 2, 0),
                ("initialize", False, 2, 0),
                (
                    "update",
                    True,
                    0,
                    0x00020016,
                    20,
                    ctypes.sizeof(ctypes.c_void_p),
                    None,
                    None,
                ),
                (
                    "update",
                    True,
                    0,
                    0x0002000D,
                    (99,),
                    ctypes.sizeof(ctypes.c_void_p),
                    None,
                    None,
                ),
            ],
        )
        creation = next(call for call in calls if call[0] == "create_process")
        self.assertEqual(creation[1], "C:/Python/python.exe")
        self.assertEqual(
            creation[2],
            "C:/Python/python.exe -c \"print('hello world')\"",
        )
        self.assertFalse(creation[5])
        self.assertEqual(creation[6], 0x00080400)
        self.assertEqual(creation[7], "A=first")
        self.assertEqual(creation[8], "C:/workspace")
        self.assertEqual(creation[9], ctypes.sizeof(_StartupInfoExW))
        self.assertTrue(creation[10])
        self.assertEqual(calls[-1], ("delete", True))

    def test_create_process_failure_deletes_the_initialized_attribute_list(self) -> None:
        api = object.__new__(_NativeWindowsConPtyApi)
        calls: list[str] = []

        def initialize(
            attribute_list: object,
            count: int,
            flags: int,
            size_pointer: object,
        ) -> int:
            del count, flags
            size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
            if attribute_list is None:
                size.contents.value = 64
                return 0
            return 1

        api._initialize_attribute_list = initialize  # type: ignore[method-assign]
        api._update_attribute = lambda *arguments: 1  # type: ignore[method-assign]
        api._delete_attribute_list = lambda attribute_list: calls.append("delete")  # type: ignore[method-assign]
        api._create_process = lambda *arguments: 0  # type: ignore[method-assign]
        api._get_last_error = lambda: 5  # type: ignore[method-assign]

        with self.assertRaisesRegex(OSError, "CreateProcessW.*Windows error 5"):
            api.create_process(
                ("python.exe",),
                cwd=Path("C:/workspace"),
                env={},
                pseudo_console_handle=20,
            )

        self.assertEqual(calls, ["delete"])


if __name__ == "__main__":
    unittest.main()
