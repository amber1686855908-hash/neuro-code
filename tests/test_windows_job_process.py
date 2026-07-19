from __future__ import annotations

import asyncio
import ctypes
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from neuro_code.adapters.windows_job_process import (
    WindowsJobProcess,
    _CreatedProcess,
    _NativeWindowsJobProcessApi,
    _ProcessInformation,
    _SecurityAttributes,
    _StartupInfoExW,
    _windows_shell,
)


class _FakeWindowsJobProcessApi:
    def __init__(self) -> None:
        self.pipes = [(10, 11), (12, 13)]
        self.null_handle = 14
        self.created_process = _CreatedProcess(20, 21, 1234)
        self.exit_code = 7
        self.read_chunks: dict[int, list[bytes | BaseException]] = {
            10: [b"stdout", b""],
            12: [b"stderr", b""],
        }
        self.create_error: BaseException | None = None
        self.wait_error: BaseException | None = None
        self.close_failures: set[int] = set()
        self.wait_gate: threading.Event | None = None
        self.calls: list[tuple[object, ...]] = []
        self.creation: dict[str, Any] | None = None
        self.writes: dict[int, list[bytes]] = {}

    def create_output_pipe(self) -> tuple[int, int]:
        self.calls.append(("create_pipe",))
        return self.pipes.pop(0)

    def create_input_pipe(self) -> tuple[int, int]:
        self.calls.append(("create_input_pipe",))
        return self.pipes.pop(0)

    def open_null_input(self) -> int:
        self.calls.append(("open_null",))
        return self.null_handle

    def create_process(self, **options: object) -> _CreatedProcess:
        self.calls.append(("create_process",))
        self.creation = options
        if self.create_error is not None:
            raise self.create_error
        return self.created_process

    def read_file(self, handle: int, byte_count: int) -> bytes:
        self.calls.append(("read", handle, byte_count))
        item = self.read_chunks[handle].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def write_file(self, handle: int, data: bytes) -> int:
        self.calls.append(("write", handle, data))
        self.writes.setdefault(handle, []).append(data)
        return min(len(data), 3)

    def wait_process(self, handle: int) -> None:
        self.calls.append(("wait", handle))
        if self.wait_gate is not None:
            self.wait_gate.wait(timeout=5)
        if self.wait_error is not None:
            raise self.wait_error

    def get_exit_code(self, handle: int) -> int:
        self.calls.append(("exit_code", handle))
        return self.exit_code

    def terminate_process(self, handle: int, exit_code: int) -> None:
        self.calls.append(("terminate", handle, exit_code))

    def close_handle(self, handle: int) -> None:
        self.calls.append(("close", handle))
        if handle in self.close_failures:
            raise OSError(f"close {handle} failed")


class WindowsJobProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_exec_is_atomically_job_bound_and_projects_both_streams(self) -> None:
        api = _FakeWindowsJobProcessApi()

        process = WindowsJobProcess.spawn_exec(
            "C:/Python/python.exe",
            ("-c", "print('hello world')"),
            cwd=Path("C:/workspace"),
            env={"z": "last", "A": "first"},
            job_handle=99,
            api=api,
        )

        assert process.stdout is not None
        assert process.stderr is not None
        stdout, stderr, exit_code = await asyncio.gather(
            process.stdout.read(),
            process.stderr.read(),
            process.wait(),
        )

        self.assertEqual(stdout, b"stdout")
        self.assertEqual(stderr, b"stderr")
        self.assertEqual(exit_code, 7)
        self.assertEqual(process.returncode, 7)
        self.assertEqual(process.pid, 1234)
        assert api.creation is not None
        self.assertEqual(api.creation["application_name"], "C:/Python/python.exe")
        self.assertEqual(
            api.creation["command_line"],
            "C:/Python/python.exe -c \"print('hello world')\"",
        )
        self.assertEqual(api.creation["job_handle"], 99)
        self.assertEqual(api.creation["inherited_handles"], (14, 11, 13))
        for handle in (21, 14, 11, 13, 10, 12, 20):
            self.assertIn(("close", handle), api.calls)

    async def test_merge_output_uses_one_pipe_and_one_inherited_writer(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.read_chunks = {10: [b"combined", b""]}

        process = WindowsJobProcess.spawn_exec(
            "python.exe",
            (),
            cwd=Path("C:/workspace"),
            env={},
            job_handle=99,
            merge_output=True,
            api=api,
        )

        assert process.stdout is not None
        self.assertEqual(await process.stdout.read(), b"combined")
        self.assertEqual(await process.wait(), 7)
        self.assertIsNone(process.stderr)
        assert api.creation is not None
        self.assertEqual(api.creation["stdout_handle"], 11)
        self.assertEqual(api.creation["stderr_handle"], 11)
        self.assertEqual(api.creation["inherited_handles"], (14, 11))
        self.assertEqual(api.calls.count(("create_pipe",)), 1)
        self.assertEqual(api.calls.count(("close", 11)), 1)

    async def test_piped_stdin_is_parent_owned_supports_partial_writes_and_closes(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.pipes = [(10, 11), (12, 13), (30, 31)]

        process = WindowsJobProcess.spawn_exec(
            "python.exe",
            (),
            cwd=Path("C:/workspace"),
            env={},
            job_handle=99,
            pipe_stdin=True,
            api=api,
        )
        await process.write_stdin(b"abcdef")
        await process.close_stdin()
        await process.close_stdin()
        await process.wait()

        assert api.creation is not None
        self.assertEqual(api.creation["stdin_handle"], 30)
        self.assertEqual(api.creation["inherited_handles"], (30, 11, 13))
        self.assertEqual(api.writes[31], [b"abcdef", b"def"])
        self.assertEqual(api.calls.count(("close", 30)), 1)
        self.assertEqual(api.calls.count(("close", 31)), 1)

    async def test_shell_uses_absolute_comspec_from_child_environment(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.read_chunks = {10: [b"ok", b""]}

        process = WindowsJobProcess.spawn_shell(
            "echo hello",
            cwd=Path("C:/workspace"),
            env={"ComSpec": "C:/Windows/System32/cmd.exe"},
            job_handle=99,
            merge_output=True,
            api=api,
        )
        await process.wait()

        assert api.creation is not None
        self.assertEqual(
            api.creation["application_name"],
            "C:/Windows/System32/cmd.exe",
        )
        self.assertEqual(
            api.creation["command_line"],
            'C:/Windows/System32/cmd.exe /c "echo hello"',
        )

    async def test_reader_failure_is_reported_through_the_stream(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.read_chunks[10] = [OSError("read failed")]

        process = WindowsJobProcess.spawn_exec(
            "python.exe",
            (),
            cwd=Path("C:/workspace"),
            env={},
            job_handle=99,
            api=api,
        )

        assert process.stdout is not None
        with self.assertRaisesRegex(OSError, "read failed"):
            await process.stdout.read()
        await process.wait()
        self.assertIn(("close", 10), api.calls)

    async def test_reader_handle_close_failure_is_reported_through_the_stream(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.read_chunks[10] = [b""]
        api.close_failures.add(10)

        process = WindowsJobProcess.spawn_exec(
            "python.exe",
            (),
            cwd=Path("C:/workspace"),
            env={},
            job_handle=99,
            api=api,
        )

        assert process.stdout is not None
        with self.assertRaisesRegex(OSError, "close 10 failed"):
            await process.stdout.read()
        await process.wait()

    async def test_wait_failure_closes_the_process_handle_and_is_replayed(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.wait_error = OSError("wait failed")

        process = WindowsJobProcess.spawn_exec(
            "python.exe",
            (),
            cwd=Path("C:/workspace"),
            env={},
            job_handle=99,
            api=api,
        )

        with self.assertRaisesRegex(OSError, "wait failed"):
            await process.wait()
        with self.assertRaisesRegex(OSError, "wait failed"):
            await process.wait()
        self.assertEqual(api.calls.count(("close", 20)), 1)

    async def test_direct_termination_is_idempotent_after_exit(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.wait_gate = threading.Event()
        process = WindowsJobProcess.spawn_exec(
            "python.exe",
            (),
            cwd=Path("C:/workspace"),
            env={},
            job_handle=99,
            api=api,
        )

        process.terminate()
        process.kill()
        api.wait_gate.set()
        await process.wait()
        process.terminate()

        self.assertGreaterEqual(api.calls.count(("terminate", 20, 1)), 1)
        self.assertNotEqual(api.calls[-1], ("terminate", 20, 1))

    async def test_create_failure_closes_every_pipe_without_a_process(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.create_error = OSError("creation failed")

        with self.assertRaisesRegex(OSError, "creation failed"):
            WindowsJobProcess.spawn_exec(
                "python.exe",
                (),
                cwd=Path("C:/workspace"),
                env={},
                job_handle=99,
                api=api,
            )

        for handle in (10, 11, 12, 13, 14):
            self.assertIn(("close", handle), api.calls)
        self.assertFalse(any(call[0] == "terminate" for call in api.calls))

    async def test_post_create_failure_terminates_waits_and_closes_the_process(self) -> None:
        api = _FakeWindowsJobProcessApi()
        api.close_failures.add(21)

        with self.assertRaisesRegex(OSError, "close 21 failed"):
            WindowsJobProcess.spawn_exec(
                "python.exe",
                (),
                cwd=Path("C:/workspace"),
                env={},
                job_handle=99,
                api=api,
            )

        self.assertIn(("terminate", 20, 1), api.calls)
        self.assertIn(("wait", 20), api.calls)
        self.assertIn(("close", 20), api.calls)
        for handle in (10, 11, 12, 13, 14):
            self.assertIn(("close", handle), api.calls)

    async def test_merge_output_constructor_failure_does_not_double_close_shared_writer(
        self,
    ) -> None:
        api = _FakeWindowsJobProcessApi()
        with (
            mock.patch.object(
                WindowsJobProcess,
                "__init__",
                side_effect=RuntimeError("constructor failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "constructor failed"),
        ):
            WindowsJobProcess.spawn_exec(
                "python.exe",
                (),
                cwd=Path("C:/workspace"),
                env={},
                job_handle=99,
                merge_output=True,
                api=api,
            )

        self.assertEqual(api.calls.count(("close", 11)), 1)
        self.assertIn(("terminate", 20, 1), api.calls)

    async def test_arguments_workspace_environment_and_job_are_validated_before_calls(
        self,
    ) -> None:
        cases = (
            (("", ()), ValueError),
            (("python.exe", ("\x00",)), ValueError),
            (("python.exe", "argument"), TypeError),
        )
        for (executable, arguments), error_type in cases:
            api = _FakeWindowsJobProcessApi()
            with (
                self.subTest(executable=executable, arguments=arguments),
                self.assertRaises(error_type),
            ):
                WindowsJobProcess.spawn_exec(
                    executable,
                    arguments,
                    cwd=Path("C:/workspace"),
                    env={},
                    job_handle=99,
                    api=api,
                )
            self.assertEqual(api.calls, [])

        api = _FakeWindowsJobProcessApi()
        for invalid_handle in (0, True):
            with (
                self.subTest(job_handle=invalid_handle),
                self.assertRaisesRegex(ValueError, "job_handle"),
            ):
                WindowsJobProcess.spawn_exec(
                    "python.exe",
                    (),
                    cwd=Path("C:/workspace"),
                    env={},
                    job_handle=invalid_handle,
                    api=api,
                )
        self.assertEqual(api.calls, [])

        api = _FakeWindowsJobProcessApi()
        with self.assertRaisesRegex(ValueError, "environment"):
            WindowsJobProcess.spawn_exec(
                "python.exe",
                (),
                cwd=Path("C:/workspace"),
                env={"BAD=NAME": "value"},
                job_handle=99,
                api=api,
            )
        self.assertEqual(api.calls, [])

    async def test_non_windows_default_api_fails_cleanly(self) -> None:
        with (
            mock.patch("neuro_code.adapters.windows_job_process.os.name", "posix"),
            self.assertRaisesRegex(OSError, "only available on Windows"),
        ):
            WindowsJobProcess.spawn_exec(
                "python.exe",
                (),
                cwd=Path("C:/workspace"),
                env={},
                job_handle=99,
            )


class WindowsShellTests(unittest.TestCase):
    def test_shell_falls_back_to_system_root_case_insensitively(self) -> None:
        self.assertEqual(
            _windows_shell({"SYSTEMROOT": "C:/Windows"}),
            "C:/Windows\\System32\\cmd.exe",
        )

    def test_shell_rejects_missing_or_relative_configuration(self) -> None:
        for environment in ({}, {"ComSpec": "cmd.exe"}):
            with (
                self.subTest(environment=environment),
                self.assertRaisesRegex(FileNotFoundError, "shell not found"),
            ):
                _windows_shell(environment)


class NativeWindowsJobProcessApiContractTests(unittest.TestCase):
    def test_create_process_sets_atomic_job_and_restricted_handle_lists(self) -> None:
        api = object.__new__(_NativeWindowsJobProcessApi)
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
                size.contents.value = 160
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
            values = tuple(
                int(item)
                for item in ctypes.cast(
                    value,
                    ctypes.POINTER(ctypes.c_void_p),
                )[: value_size // ctypes.sizeof(ctypes.c_void_p)]
            )
            calls.append(
                (
                    "update",
                    bool(attribute_list),
                    flags,
                    attribute,
                    values,
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
                    int(startup.StartupInfo.dwFlags),
                    int(startup.StartupInfo.hStdInput),
                    int(startup.StartupInfo.hStdOutput),
                    int(startup.StartupInfo.hStdError),
                    bool(startup.lpAttributeList),
                )
            )
            process = ctypes.cast(
                process_pointer,
                ctypes.POINTER(_ProcessInformation),
            ).contents
            process.hProcess = 20
            process.hThread = 21
            process.dwProcessId = 1234
            process.dwThreadId = 1235
            return 1

        api._initialize_attribute_list = initialize  # type: ignore[method-assign]
        api._update_attribute = update  # type: ignore[method-assign]
        api._delete_attribute_list = lambda value: calls.append(  # type: ignore[method-assign]
            ("delete", bool(value))
        )
        api._create_process = create_process  # type: ignore[method-assign]
        api._get_last_error = lambda: 5  # type: ignore[method-assign]

        result = api.create_process(
            application_name="C:/Python/python.exe",
            command_line='C:/Python/python.exe -c "print(1)"',
            cwd=Path("C:/workspace"),
            env={"z": "last", "A": "first"},
            stdin_handle=14,
            stdout_handle=11,
            stderr_handle=13,
            inherited_handles=(14, 11, 13),
            job_handle=99,
        )

        self.assertEqual(result, _CreatedProcess(20, 21, 1234))
        self.assertEqual(
            calls[:4],
            [
                ("initialize", True, 2, 0),
                ("initialize", False, 2, 0),
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
                (
                    "update",
                    True,
                    0,
                    0x00020002,
                    (14, 11, 13),
                    3 * ctypes.sizeof(ctypes.c_void_p),
                    None,
                    None,
                ),
            ],
        )
        creation = next(call for call in calls if call[0] == "create_process")
        self.assertEqual(creation[1], "C:/Python/python.exe")
        self.assertEqual(creation[2], 'C:/Python/python.exe -c "print(1)"')
        self.assertTrue(creation[5])
        self.assertEqual(creation[6], 0x08080600)
        self.assertEqual(creation[7], "A=first")
        self.assertEqual(creation[8], str(Path("C:/workspace")))
        self.assertEqual(creation[9], ctypes.sizeof(_StartupInfoExW))
        self.assertEqual(creation[10:14], (0x100, 14, 11, 13))
        self.assertTrue(creation[14])
        self.assertEqual(calls[-1], ("delete", True))

    def test_handle_list_failure_still_deletes_attribute_list(self) -> None:
        api = object.__new__(_NativeWindowsJobProcessApi)
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
                size.contents.value = 96
                return 0
            return 1

        api._initialize_attribute_list = initialize  # type: ignore[method-assign]
        api._update_attribute = mock.Mock(side_effect=(1, 0))
        api._delete_attribute_list = lambda value: calls.append("delete")  # type: ignore[method-assign]
        api._get_last_error = lambda: 87  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            OSError,
            r"UpdateProcThreadAttribute\(handle list\).*Windows error 87",
        ):
            api.create_process(
                application_name="python.exe",
                command_line="python.exe",
                cwd=Path("C:/workspace"),
                env={},
                stdin_handle=14,
                stdout_handle=11,
                stderr_handle=13,
                inherited_handles=(14, 11, 13),
                job_handle=99,
            )

        self.assertEqual(calls, ["delete"])

    def test_output_pipe_is_inheritable_only_on_the_child_writer(self) -> None:
        api = object.__new__(_NativeWindowsJobProcessApi)
        calls: list[tuple[object, ...]] = []

        def create_pipe(
            read_pointer: object,
            write_pointer: object,
            security_pointer: object,
            size: int,
        ) -> int:
            security = ctypes.cast(
                security_pointer,
                ctypes.POINTER(_SecurityAttributes),
            ).contents
            ctypes.cast(read_pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = 10
            ctypes.cast(write_pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = 11
            calls.append(
                (
                    "create_pipe",
                    int(security.nLength),
                    bool(security.bInheritHandle),
                    size,
                )
            )
            return 1

        api._create_pipe = create_pipe  # type: ignore[method-assign]
        api._set_handle_information = lambda handle, mask, flags: (
            calls.append(  # type: ignore[method-assign]
                ("set_handle", handle, mask, flags)
            )
            or 1
        )
        api._get_last_error = lambda: 5  # type: ignore[method-assign]

        self.assertEqual(api.create_output_pipe(), (10, 11))
        self.assertEqual(
            calls,
            [
                ("create_pipe", ctypes.sizeof(_SecurityAttributes), True, 0),
                ("set_handle", 10, 1, 0),
            ],
        )


if __name__ == "__main__":
    unittest.main()
