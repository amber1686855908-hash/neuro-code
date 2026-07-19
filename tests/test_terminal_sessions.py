from __future__ import annotations

import asyncio
import threading
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.terminal import (
    MAX_TERMINAL_OUTPUT_BYTES,
    MAX_TERMINAL_READ_BYTES,
    MAX_TERMINAL_WRITE_BYTES,
    TerminalSignal,
    TerminalSize,
)
from neuro_code.errors import PermissionDenied, TerminalError, ToolError
from neuro_code.permissions import (
    PermissionApproval,
    PermissionManager,
    PermissionMode,
    PermissionRequest,
)
from neuro_code.ports.sandbox import ShellLaunch
from neuro_code.ports.terminal import (
    TerminalEofHandler,
    TerminalErrorHandler,
    TerminalOutputHandler,
)
from neuro_code.runtime import terminal_sessions
from neuro_code.runtime.terminal_sessions import LocalInteractiveTerminalManager


class _FakePlatformSession:
    def __init__(self) -> None:
        self.process_id = 4242
        self.writes: list[bytes] = []
        self.sizes: list[TerminalSize] = []
        self.signals: list[TerminalSignal] = []
        self.exit_code: int | None = None
        self.closed = False
        self.close_calls = 0
        self.close_error: BaseException | None = None
        self.poll_error: BaseException | None = None
        self.write_error: BaseException | None = None

    def write(self, data: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)

    def resize(self, size: TerminalSize) -> None:
        self.sizes.append(size)

    def send_signal(self, signal: TerminalSignal) -> None:
        self.signals.append(signal)

    def poll_exit(self) -> int | None:
        if self.poll_error is not None:
            raise self.poll_error
        return self.exit_code

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.exit_code is None:
            self.exit_code = 1
        if self.close_error is not None:
            raise self.close_error


class _FakeTerminalPlatform:
    def __init__(self) -> None:
        self.session = _FakePlatformSession()
        self.spawn_calls: list[dict[str, object]] = []
        self.on_output: TerminalOutputHandler | None = None
        self.on_eof: TerminalEofHandler | None = None
        self.on_error: TerminalErrorHandler | None = None
        self.spawn_started = threading.Event()
        self.spawn_release: threading.Event | None = None
        self.spawn_error: BaseException | None = None

    def spawn_exec(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> _FakePlatformSession:
        self.spawn_calls.append(
            {
                "arguments": tuple(arguments),
                "cwd": cwd,
                "env": dict(env),
                "executable": executable,
                "size": size,
            }
        )
        self.on_output = on_output
        self.on_eof = on_eof
        self.on_error = on_error
        self.spawn_started.set()
        if self.spawn_release is not None:
            self.spawn_release.wait(timeout=5)
        if self.spawn_error is not None:
            raise self.spawn_error
        return self.session

    def emit(self, data: bytes) -> None:
        assert self.on_output is not None
        self.on_output(data)

    def finish(self) -> None:
        assert self.on_eof is not None
        self.on_eof()

    def fail(self, error: BaseException) -> None:
        assert self.on_error is not None
        self.on_error(error)


class _FakeApprover:
    def __init__(self, approval: PermissionApproval) -> None:
        self.approval = approval
        self.requests: list[PermissionRequest] = []

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        self.requests.append(request)
        return self.approval


class _FakeSandbox:
    def __init__(self, profile: SandboxProfile) -> None:
        self.profile = profile
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def shell_launch(self, command: str) -> ShellLaunch:
        raise AssertionError(f"terminal manager must not use shell_launch: {command}")

    def exec_launch(self, executable: str, arguments: tuple[str, ...]) -> ShellLaunch:
        self.calls.append((executable, arguments))
        return ShellLaunch("/sandbox/exec", ("--", executable, *arguments))


class LocalInteractiveTerminalManagerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _manager(
        root: Path,
        platform: _FakeTerminalPlatform,
        *,
        permissions: PermissionManager | None = None,
        approver: _FakeApprover | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
        shell_sandbox: _FakeSandbox | None = None,
        max_sessions: int = 8,
    ) -> LocalInteractiveTerminalManager:
        return LocalInteractiveTerminalManager(
            workspace=root,
            permissions=permissions or PermissionManager(mode=PermissionMode.BYPASS),
            approver=approver,
            sandbox_profile=sandbox_profile,
            shell_sandbox=shell_sandbox,
            protected_environment_variables=frozenset({"fixture_api_key"}),
            platform=platform,
            max_sessions=max_sessions,
        )

    async def test_create_filters_environment_and_forwards_terminal_operations(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child"
            child.mkdir()
            platform = _FakeTerminalPlatform()
            manager = self._manager(root, platform)
            try:
                session = await manager.create_exec(
                    "call-1",
                    "/usr/bin/python",
                    ("-u", "-c", "print('ok')"),
                    cwd="child",
                    env={
                        "FIXTURE_API_KEY": "must-not-spawn",
                        "PATH": "/usr/bin",
                        "TERM": "caller-value",
                    },
                    size=TerminalSize(90, 25),
                    output_capacity=5,
                )

                self.assertEqual(session.process_id, 4242)
                self.assertEqual(session.size, TerminalSize(90, 25))
                spawn = platform.spawn_calls[0]
                spawn_cwd = spawn["cwd"]
                assert isinstance(spawn_cwd, Path)
                self.assertEqual(spawn_cwd.resolve(), child.resolve())
                self.assertEqual(spawn["executable"], "/usr/bin/python")
                self.assertEqual(spawn["arguments"], ("-u", "-c", "print('ok')"))
                environment = spawn["env"]
                assert isinstance(environment, dict)
                self.assertNotIn("FIXTURE_API_KEY", environment)
                self.assertEqual(environment["TERM"], "xterm-256color")
                self.assertEqual(environment["COLORTERM"], "truecolor")
                self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

                platform.emit(b"abcdefgh")
                first = await session.read(after_offset=0)
                self.assertEqual(first.data, b"defgh")
                self.assertEqual(first.dropped_bytes, 3)
                self.assertEqual(first.next_offset, 8)
                self.assertFalse(first.eof)
                platform.finish()
                finished = await session.read(after_offset=8)
                self.assertEqual(finished.data, b"")
                self.assertTrue(finished.eof)

                await session.write(b"input")
                await session.resize(TerminalSize(120, 40))
                await session.send_signal(TerminalSignal.INTERRUPT)
                self.assertEqual(platform.session.writes, [b"input"])
                self.assertEqual(platform.session.sizes, [TerminalSize(120, 40)])
                self.assertEqual(platform.session.signals, [TerminalSignal.INTERRUPT])
                self.assertEqual(session.size, TerminalSize(120, 40))
                self.assertIsNone(await session.wait(timeout_seconds=0))
                platform.session.exit_code = 7
                self.assertEqual(await session.wait(), 7)
            finally:
                await manager.shutdown()
            self.assertTrue(platform.session.closed)

    async def test_output_wait_error_and_input_limits_are_bounded(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            platform = _FakeTerminalPlatform()
            manager = self._manager(root, platform)
            session = await manager.create_exec(
                "call-1",
                "python",
                (),
                cwd=".",
                env={},
                size=TerminalSize(80, 24),
                output_capacity=100,
            )
            try:
                waiting = asyncio.create_task(session.read(wait_seconds=1))
                await asyncio.sleep(0)
                platform.emit(b"ready")
                self.assertEqual((await waiting).data, b"ready")
                platform.fail(OSError("fixture read failure"))
                with self.assertRaisesRegex(TerminalError, "output stream failed"):
                    await session.read(after_offset=5)
                with self.assertRaisesRegex(TerminalError, "cannot exceed"):
                    await session.write(b"x" * (MAX_TERMINAL_WRITE_BYTES + 1))
                with self.assertRaisesRegex(TerminalError, "max_bytes"):
                    await session.read(max_bytes=MAX_TERMINAL_READ_BYTES + 1)
                with self.assertRaisesRegex(TerminalError, "exceeds current"):
                    await session.read(after_offset=6)
                with self.assertRaisesRegex(TerminalError, "after_offset"):
                    await session.read(after_offset=-1)
                with self.assertRaisesRegex(TerminalError, "wait_seconds"):
                    await session.read(wait_seconds=float("nan"))
                with self.assertRaisesRegex(TerminalError, "must be bytes"):
                    await session.write("invalid")  # type: ignore[arg-type]
                await session.write(b"")
                with self.assertRaisesRegex(TerminalError, "TerminalSize"):
                    await session.resize((80, 24))  # type: ignore[arg-type]
                with self.assertRaisesRegex(TerminalError, "TerminalSignal"):
                    await session.send_signal("interrupt")  # type: ignore[arg-type]
                with self.assertRaisesRegex(TerminalError, "timeout_seconds"):
                    await session.wait(timeout_seconds=-1)
            finally:
                await manager.shutdown()

    async def test_permission_denial_and_interactive_approval_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            denied_platform = _FakeTerminalPlatform()
            denied = self._manager(
                root,
                denied_platform,
                permissions=PermissionManager(),
            )
            with self.assertRaisesRegex(PermissionDenied, "headless"):
                await denied.create_exec(
                    "call-denied",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            self.assertEqual(denied_platform.spawn_calls, [])
            await denied.shutdown()

            approved_platform = _FakeTerminalPlatform()
            approver = _FakeApprover(PermissionApproval.allow_once())
            approved = self._manager(
                root,
                approved_platform,
                permissions=PermissionManager(interactive=True),
                approver=approver,
            )
            session = await approved.create_exec(
                "call-approved",
                "python",
                ("-V",),
                cwd=".",
                env={"TOKEN": "not-rendered"},
                size=TerminalSize(80, 24),
                output_capacity=100,
            )
            self.assertEqual(len(approver.requests), 1)
            request = approver.requests[0]
            self.assertEqual(request.tool_name, "create_terminal")
            self.assertIn("python -V", request.summary)
            self.assertNotIn("not-rendered", request.summary)
            await session.close()
            await approved.shutdown()

            unavailable_platform = _FakeTerminalPlatform()
            unavailable = self._manager(
                root,
                unavailable_platform,
                permissions=PermissionManager(interactive=True),
            )
            with self.assertRaisesRegex(PermissionDenied, "approval UI"):
                await unavailable.create_exec(
                    "call-unavailable",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            await unavailable.shutdown()

            rejected_platform = _FakeTerminalPlatform()
            rejector = _FakeApprover(PermissionApproval.deny("fixture rejection"))
            rejected = self._manager(
                root,
                rejected_platform,
                permissions=PermissionManager(interactive=True),
                approver=rejector,
            )
            with self.assertRaisesRegex(PermissionDenied, "fixture rejection"):
                await rejected.create_exec(
                    "call-rejected",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            self.assertEqual(rejected_platform.spawn_calls, [])
            await rejected.shutdown()

    async def test_workspace_and_sandbox_boundaries_precede_spawn(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / f"{root.name}-outside"
            outside.mkdir()
            platform = _FakeTerminalPlatform()
            sandbox = _FakeSandbox(SandboxProfile.READ_ONLY)
            manager = self._manager(
                root,
                platform,
                sandbox_profile=SandboxProfile.READ_ONLY,
                shell_sandbox=sandbox,
            )
            try:
                with self.assertRaisesRegex(ToolError, "escapes"):
                    await manager.create_exec(
                        "call-escape",
                        "python",
                        (),
                        cwd=str(outside),
                        env={},
                        size=TerminalSize(80, 24),
                        output_capacity=100,
                    )
                self.assertEqual(platform.spawn_calls, [])

                session = await manager.create_exec(
                    "call-sandboxed",
                    "python",
                    ("-V",),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
                self.assertEqual(sandbox.calls, [("python", ("-V",))])
                self.assertEqual(platform.spawn_calls[0]["executable"], "/sandbox/exec")
                self.assertEqual(platform.spawn_calls[0]["arguments"], ("--", "python", "-V"))
                await session.close()
                await manager.shutdown()

                mismatch_platform = _FakeTerminalPlatform()
                mismatch = self._manager(
                    root,
                    mismatch_platform,
                    sandbox_profile=SandboxProfile.STRICT,
                    shell_sandbox=sandbox,
                )
                with self.assertRaisesRegex(TerminalError, "does not match"):
                    await mismatch.create_exec(
                        "call-mismatch",
                        "python",
                        (),
                        cwd=".",
                        env={},
                        size=TerminalSize(80, 24),
                        output_capacity=100,
                    )
                self.assertEqual(mismatch_platform.spawn_calls, [])
                await mismatch.shutdown()

                missing_platform = _FakeTerminalPlatform()
                missing = self._manager(
                    root,
                    missing_platform,
                    sandbox_profile=SandboxProfile.WORKSPACE,
                )
                with self.assertRaisesRegex(TerminalError, "is not enforced"):
                    await missing.create_exec(
                        "call-missing",
                        "python",
                        (),
                        cwd=".",
                        env={},
                        size=TerminalSize(80, 24),
                        output_capacity=100,
                    )
                await missing.shutdown()
            finally:
                outside.rmdir()

    async def test_limit_close_and_shutdown_pending_creation_do_not_leak_sessions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            platform = _FakeTerminalPlatform()
            manager = self._manager(root, platform, max_sessions=1)
            first = await manager.create_exec(
                "call-1",
                "python",
                (),
                cwd=".",
                env={},
                size=TerminalSize(80, 24),
                output_capacity=100,
            )
            with self.assertRaisesRegex(TerminalError, "limit reached"):
                await manager.create_exec(
                    "call-2",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            await first.close()
            second = await manager.create_exec(
                "call-3",
                "python",
                (),
                cwd=".",
                env={},
                size=TerminalSize(80, 24),
                output_capacity=100,
            )
            await second.close()
            await manager.shutdown()

            pending_platform = _FakeTerminalPlatform()
            pending_platform.spawn_release = threading.Event()
            pending_manager = self._manager(root, pending_platform)
            creation = asyncio.create_task(
                pending_manager.create_exec(
                    "call-pending",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            )
            await asyncio.sleep(0.05)
            self.assertTrue(pending_platform.spawn_started.is_set())
            shutdown = asyncio.create_task(pending_manager.shutdown())
            await asyncio.sleep(0)
            pending_platform.spawn_release.set()
            with self.assertRaisesRegex(TerminalError, "closed during creation"):
                await creation
            await shutdown
            self.assertTrue(pending_platform.session.closed)

            cancelled_platform = _FakeTerminalPlatform()
            cancelled_platform.spawn_release = threading.Event()
            cancelled_manager = self._manager(root, cancelled_platform)
            cancelled_creation = asyncio.create_task(
                cancelled_manager.create_exec(
                    "call-cancelled",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            )
            await asyncio.sleep(0.05)
            self.assertTrue(cancelled_platform.spawn_started.is_set())
            cancelled_creation.cancel()
            cancelled_platform.spawn_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_creation
            self.assertEqual(cancelled_platform.session.close_calls, 1)
            await cancelled_manager.shutdown()

    async def test_capacity_and_argument_validation_happens_before_spawn(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            platform = _FakeTerminalPlatform()
            manager = self._manager(root, platform)
            for capacity in (0, MAX_TERMINAL_OUTPUT_BYTES + 1):
                with (
                    self.subTest(capacity=capacity),
                    self.assertRaisesRegex(ValueError, "output_capacity"),
                ):
                    await manager.create_exec(
                        "call-invalid",
                        "python",
                        (),
                        cwd=".",
                        env={},
                        size=TerminalSize(80, 24),
                        output_capacity=capacity,
                    )
            with self.assertRaisesRegex(TerminalError, "null bytes"):
                await manager.create_exec(
                    "call-invalid",
                    "python",
                    ("bad\x00argument",),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            self.assertEqual(platform.spawn_calls, [])
            await manager.shutdown()

    async def test_platform_failures_closed_operations_and_shutdown_errors_are_visible(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            spawn_platform = _FakeTerminalPlatform()
            spawn_platform.spawn_error = OSError("fixture spawn failure")
            spawn_manager = self._manager(root, spawn_platform)
            with self.assertRaisesRegex(TerminalError, "could not create"):
                await spawn_manager.create_exec(
                    "call-spawn-error",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            await spawn_manager.shutdown()

            operation_platform = _FakeTerminalPlatform()
            manager = self._manager(root, operation_platform)
            session = await manager.create_exec(
                "call-operation-error",
                "python",
                (),
                cwd=".",
                env={},
                size=TerminalSize(80, 24),
                output_capacity=100,
            )
            operation_platform.session.write_error = OSError("fixture write error")
            with self.assertRaisesRegex(TerminalError, "platform operation"):
                await session.write(b"x")
            operation_platform.session.poll_error = OSError("fixture poll error")
            with self.assertRaisesRegex(TerminalError, "inspect terminal"):
                await session.wait(timeout_seconds=0)
            operation_platform.session.poll_error = None
            await session.close()
            await session.close()
            with self.assertRaisesRegex(TerminalError, "session is closed"):
                await session.write(b"x")
            await manager.shutdown()

            close_platform = _FakeTerminalPlatform()
            close_manager = self._manager(root, close_platform)
            await close_manager.create_exec(
                "call-close-error",
                "python",
                (),
                cwd=".",
                env={},
                size=TerminalSize(80, 24),
                output_capacity=100,
            )
            close_platform.session.close_error = OSError("fixture close error")
            with self.assertRaisesRegex(TerminalError, "failed to close"):
                await close_manager.shutdown()

    async def test_manager_and_creation_metadata_validation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            platform = _FakeTerminalPlatform()
            with self.assertRaises(TypeError):
                LocalInteractiveTerminalManager(
                    workspace="not-a-path",  # type: ignore[arg-type]
                    permissions=PermissionManager(),
                    platform=platform,
                )
            with self.assertRaisesRegex(ValueError, "max_sessions"):
                self._manager(root, platform, max_sessions=0)

            manager = self._manager(root, platform)
            invalid_calls: tuple[tuple[object, object, object, object, str], ...] = (
                (None, "python", (), TerminalSize(80, 24), "call_id"),
                ("call", 7, (), TerminalSize(80, 24), "executable"),
                ("call", "python", "arg", TerminalSize(80, 24), "sequence"),
                ("call", "", (), TerminalSize(80, 24), "non-empty"),
                ("call", "python", (), (80, 24), "TerminalSize"),
            )
            for call_id, executable, arguments, size, message in invalid_calls:
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(TerminalError, message),
                ):
                    await manager.create_exec(
                        call_id,  # type: ignore[arg-type]
                        executable,  # type: ignore[arg-type]
                        arguments,  # type: ignore[arg-type]
                        cwd=".",
                        env={},
                        size=size,  # type: ignore[arg-type]
                        output_capacity=100,
                    )

            file_path = root / "file.txt"
            file_path.write_text("fixture", encoding="utf-8")
            with self.assertRaisesRegex(TerminalError, "not a directory"):
                await manager.create_exec(
                    "call-file",
                    "python",
                    (),
                    cwd="file.txt",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            with self.assertRaisesRegex(TerminalError, "environment"):
                await manager.create_exec(
                    "call-env",
                    "python",
                    (),
                    cwd=".",
                    env={"BAD=NAME": "value"},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )
            await manager.shutdown()
            await manager.shutdown()
            with self.assertRaisesRegex(TerminalError, "manager is closed"):
                await manager.create_exec(
                    "call-closed",
                    "python",
                    (),
                    cwd=".",
                    env={},
                    size=TerminalSize(80, 24),
                    output_capacity=100,
                )

            with (
                mock.patch.object(terminal_sessions.os, "name", "unsupported"),
                self.assertRaisesRegex(TerminalError, "unsupported"),
            ):
                terminal_sessions._default_platform()


if __name__ == "__main__":
    unittest.main()
