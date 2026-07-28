from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.domain.background_tasks import (
    MAX_BACKGROUND_TASK_WAIT_IDS,
    BackgroundTaskKillOutcome,
    BackgroundTaskStatus,
    BackgroundTaskWaitMode,
)
from neuro_code.shared.errors import ToolError


class LocalBackgroundTaskManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_any_then_wait_all_uses_completion_events_and_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            try:
                quick = await manager.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(0.03);print('quick')"),
                    display_command="quick wait fixture",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                slow = await manager.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(0.35);print('slow')"),
                    display_command="slow wait fixture",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )

                any_result = await manager.wait(
                    (quick.task_id, slow.task_id),
                    mode=BackgroundTaskWaitMode.WAIT_ANY,
                    timeout_seconds=1,
                )
                self.assertFalse(any_result.timed_out)
                self.assertEqual(
                    [snapshot.task_id for snapshot in any_result.snapshots],
                    [quick.task_id, slow.task_id],
                )
                self.assertEqual(any_result.snapshots[0].status, BackgroundTaskStatus.COMPLETED)
                self.assertEqual(any_result.snapshots[1].status, BackgroundTaskStatus.RUNNING)

                all_result = await manager.wait(
                    (quick.task_id, slow.task_id),
                    mode=BackgroundTaskWaitMode.WAIT_ALL,
                    timeout_seconds=2,
                )
                self.assertFalse(all_result.timed_out)
                self.assertEqual(all_result.terminal_count, 2)
                self.assertTrue(all(snapshot.status.terminal for snapshot in all_result.snapshots))
            finally:
                await manager.shutdown()

    async def test_wait_timeout_scope_isolation_and_cancellation_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = LocalBackgroundTaskManager()
            first_scope = supervisor.open_scope()
            second_scope = supervisor.open_scope()
            try:
                first = await first_scope.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(60)"),
                    display_command="first scoped wait fixture",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                second = await second_scope.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(60)"),
                    display_command="second scoped wait fixture",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                timed = await first_scope.wait(
                    (first.task_id, second.task_id, "missing"),
                    mode=BackgroundTaskWaitMode.WAIT_ALL,
                    timeout_seconds=0.01,
                )
                self.assertTrue(timed.timed_out)
                self.assertEqual([item.task_id for item in timed.snapshots], [first.task_id])
                self.assertEqual(timed.missing_task_ids, (second.task_id, "missing"))

                waiting = asyncio.create_task(
                    first_scope.wait(
                        (first.task_id,),
                        mode=BackgroundTaskWaitMode.WAIT_ALL,
                        timeout_seconds=10,
                    )
                )
                await asyncio.sleep(0)
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting

                await first_scope.kill(first.task_id)
                self.assertEqual(
                    [item.task_id for item in await first_scope.pending_completions()],
                    [first.task_id],
                )
            finally:
                await supervisor.shutdown()

    async def test_completion_reporting_is_idempotent_and_scope_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = LocalBackgroundTaskManager()
            first_scope = supervisor.open_scope()
            second_scope = supervisor.open_scope()
            try:
                first = await first_scope.start_exec(
                    sys.executable,
                    ("-c", "print('first private output')"),
                    display_command="first private command",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                second = await second_scope.start_exec(
                    sys.executable,
                    ("-c", "print('second private output')"),
                    display_command="second private command",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                await first_scope.get(first.task_id, wait_seconds=2)
                await second_scope.get(second.task_id, wait_seconds=2)

                self.assertEqual(
                    [item.task_id for item in await first_scope.pending_completions()],
                    [first.task_id],
                )
                self.assertEqual(
                    [item.task_id for item in await second_scope.pending_completions()],
                    [second.task_id],
                )

                await first_scope.mark_completions_reported(
                    (first.task_id, second.task_id, first.task_id)
                )
                self.assertEqual(await first_scope.pending_completions(), ())
                self.assertEqual(
                    [item.task_id for item in await second_scope.pending_completions()],
                    [second.task_id],
                )
            finally:
                await supervisor.shutdown()

    async def test_scopes_isolate_task_ids_and_close_only_their_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = LocalBackgroundTaskManager()
            first_scope = supervisor.open_scope()
            second_scope = supervisor.open_scope()
            try:
                first = await first_scope.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(60)"),
                    display_command="first scoped task",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                second = await second_scope.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(60)"),
                    display_command="second scoped task",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )

                self.assertEqual(
                    [snapshot.task_id for snapshot in await first_scope.list()],
                    [first.task_id],
                )
                self.assertEqual(
                    [snapshot.task_id for snapshot in await second_scope.list()],
                    [second.task_id],
                )
                self.assertIsNone(await first_scope.get(second.task_id))
                self.assertIsNone(await first_scope.kill(second.task_id))

                await first_scope.shutdown()
                self.assertEqual(await first_scope.list(), ())
                with self.assertRaisesRegex(ToolError, "manager is closed"):
                    await first_scope.start_exec(
                        sys.executable,
                        ("-c", "print('must not spawn')"),
                        display_command="closed scope",
                        cwd=root,
                        env=os.environ,
                        output_byte_limit=2_000,
                        termination_grace_seconds=0.05,
                    )
                remaining = await second_scope.get(second.task_id)
                assert remaining is not None
                self.assertEqual(remaining.status, BackgroundTaskStatus.RUNNING)

                await supervisor.shutdown()
                stopped = await second_scope.get(second.task_id)
                assert stopped is not None
                self.assertEqual(stopped.status, BackgroundTaskStatus.CANCELLED)
                with self.assertRaisesRegex(ToolError, "manager is closed"):
                    supervisor.open_scope()
            finally:
                await supervisor.shutdown()

    async def test_shell_task_returns_immediately_then_exposes_combined_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalBackgroundTaskManager()
            code = (
                "import sys,time;"
                "print('first',flush=True);"
                "print('second',file=sys.stderr,flush=True);"
                "time.sleep(0.05)"
            )
            arguments = [sys.executable, "-u", "-c", code]
            command = (
                subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
            )
            try:
                started = await manager.start_shell(
                    command,
                    cwd=Path(directory),
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                self.assertEqual(started.status, BackgroundTaskStatus.RUNNING)

                completed = await manager.get(started.task_id, wait_seconds=2)
                assert completed is not None
                self.assertEqual(completed.status, BackgroundTaskStatus.COMPLETED)
                self.assertEqual(completed.exit_code, 0)
                self.assertIn("first", completed.output)
                self.assertIn("second", completed.output)
                self.assertLess(completed.output.index("first"), completed.output.index("second"))
            finally:
                await manager.shutdown()

    async def test_exec_failure_timeout_and_bounded_head_tail_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            try:
                failed = await manager.start_exec(
                    sys.executable,
                    ("-c", "import sys;print('bad');sys.exit(7)"),
                    display_command="fixture failure",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                failed_snapshot = await manager.get(failed.task_id, wait_seconds=2)
                assert failed_snapshot is not None
                self.assertEqual(failed_snapshot.status, BackgroundTaskStatus.FAILED)
                self.assertEqual(failed_snapshot.exit_code, 7)

                timed = await manager.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(60)"),
                    display_command="fixture timeout",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                    timeout_seconds=0.05,
                )
                timed_snapshot = await manager.get(timed.task_id, wait_seconds=2)
                assert timed_snapshot is not None
                self.assertEqual(timed_snapshot.status, BackgroundTaskStatus.TIMED_OUT)

                large = await manager.start_exec(
                    sys.executable,
                    ("-c", "print('A'*100 + 'Z'*100)"),
                    display_command="fixture large output",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=40,
                    termination_grace_seconds=0.05,
                )
                large_snapshot = await manager.get(large.task_id, wait_seconds=2)
                assert large_snapshot is not None
                self.assertTrue(large_snapshot.truncated)
                self.assertGreater(large_snapshot.total_output_bytes, 40)
                self.assertIn("A", large_snapshot.output)
                self.assertIn("Z", large_snapshot.output)
                self.assertIn("older output truncated", large_snapshot.output)
            finally:
                await manager.shutdown()

    async def test_kill_is_process_owned_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalBackgroundTaskManager()
            try:
                started = await manager.start_exec(
                    sys.executable,
                    ("-c", "import time;time.sleep(60)"),
                    display_command="fixture persistent process",
                    cwd=Path(directory),
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                killed = await manager.kill(started.task_id)
                assert killed is not None
                self.assertEqual(killed.outcome, BackgroundTaskKillOutcome.KILLED)
                self.assertEqual(killed.snapshot.status, BackgroundTaskStatus.CANCELLED)

                repeated = await manager.kill(started.task_id)
                assert repeated is not None
                self.assertEqual(
                    repeated.outcome,
                    BackgroundTaskKillOutcome.ALREADY_EXITED,
                )
                self.assertEqual(repeated.snapshot.status, BackgroundTaskStatus.CANCELLED)
            finally:
                await manager.shutdown()

    async def test_capacity_and_closed_manager_fail_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager(max_running_tasks=1, max_retained_tasks=1)
            first = await manager.start_exec(
                sys.executable,
                ("-c", "import time;time.sleep(60)"),
                display_command="first",
                cwd=root,
                env=os.environ,
                output_byte_limit=2_000,
                termination_grace_seconds=0.05,
            )
            with self.assertRaisesRegex(ToolError, "task limit"):
                await manager.start_exec(
                    sys.executable,
                    ("-c", "print('must not spawn')"),
                    display_command="second",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
            await manager.kill(first.task_id)
            await manager.shutdown()
            with self.assertRaisesRegex(ToolError, "manager is closed"):
                await manager.start_exec(
                    sys.executable,
                    ("-c", "print('must not spawn')"),
                    display_command="closed",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )

    async def test_manager_rejects_invalid_launch_and_wait_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            try:
                for command, timeout in (
                    ("", None),
                    ("valid", float("nan")),
                    ("valid", float("inf")),
                ):
                    with self.assertRaises(ToolError):
                        await manager.start_exec(
                            sys.executable,
                            ("-c", "print('must not spawn')"),
                            display_command=command,
                            cwd=root,
                            env=os.environ,
                            output_byte_limit=2_000,
                            termination_grace_seconds=0.05,
                            timeout_seconds=timeout,
                        )
                for wait_seconds in (float("nan"), float("inf"), -1.0):
                    with self.assertRaises(ToolError):
                        await manager.get("missing", wait_seconds=wait_seconds)
                for task_ids, mode, timeout_seconds in (
                    ((), BackgroundTaskWaitMode.WAIT_ALL, 1.0),
                    (
                        tuple(f"task-{index}" for index in range(MAX_BACKGROUND_TASK_WAIT_IDS + 1)),
                        BackgroundTaskWaitMode.WAIT_ALL,
                        1.0,
                    ),
                    (("duplicate", "duplicate"), BackgroundTaskWaitMode.WAIT_ALL, 1.0),
                    (("valid",), BackgroundTaskWaitMode.WAIT_ALL, float("nan")),
                    (("valid",), BackgroundTaskWaitMode.WAIT_ALL, -1.0),
                ):
                    with self.assertRaises(ToolError):
                        await manager.wait(
                            task_ids,
                            mode=mode,
                            timeout_seconds=timeout_seconds,
                        )
            finally:
                await manager.shutdown()

    @unittest.skipUnless(os.name == "posix", "POSIX process-group wait required")
    async def test_shell_background_operator_remains_owned_until_descendant_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalBackgroundTaskManager()
            child = f"{shlex.quote(sys.executable)} -c 'import time;time.sleep(0.2)'"
            command = f"{child} >/dev/null 2>&1 &"
            try:
                started = await manager.start_shell(
                    command,
                    cwd=Path(directory),
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                early = await manager.get(started.task_id, wait_seconds=0.03)
                assert early is not None
                self.assertEqual(early.status, BackgroundTaskStatus.RUNNING)
                completed = await manager.get(started.task_id, wait_seconds=2)
                assert completed is not None
                self.assertEqual(completed.status, BackgroundTaskStatus.COMPLETED)
            finally:
                await manager.shutdown()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc assertion")
    async def test_shutdown_terminates_owned_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            child_code = (
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            )
            parent_code = (
                "import pathlib,signal,subprocess,sys,time;"
                f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "pathlib.Path('background-child.pid').write_text(str(child.pid));"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(60)"
            )
            await manager.start_exec(
                sys.executable,
                ("-c", parent_code),
                display_command="fixture process tree",
                cwd=root,
                env=os.environ,
                output_byte_limit=2_000,
                termination_grace_seconds=0.05,
            )
            pid_file = root / "background-child.pid"
            for _ in range(100):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text(encoding="utf-8"))

            await manager.shutdown()
            await self._assert_process_stopped(child_pid)

    @unittest.skipUnless(os.name == "nt", "native Windows Job Object required")
    async def test_windows_shutdown_terminates_atomically_owned_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = root / "spawn-descendant"
            started = root / "descendant-started"
            leaked = root / "descendant-leaked"
            child_code = (
                "import pathlib,time;"
                f"pathlib.Path({str(started)!r}).write_text('started');"
                "time.sleep(1);"
                f"pathlib.Path({str(leaked)!r}).write_text('leaked')"
            )
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                f"gate=pathlib.Path({str(gate)!r});"
                "deadline=time.monotonic()+5;"
                "\nwhile not gate.exists() and time.monotonic()<deadline: time.sleep(0.01);"
                "\nif not gate.exists(): raise SystemExit(2)\n"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "time.sleep(60)"
            )
            manager = LocalBackgroundTaskManager()
            try:
                await manager.start_exec(
                    sys.executable,
                    ("-c", parent_code),
                    display_command="Windows Job Object descendant fixture",
                    cwd=root,
                    env=os.environ,
                    output_byte_limit=2_000,
                    termination_grace_seconds=0.05,
                )
                gate.write_text("go", encoding="utf-8")
                for _ in range(500):
                    if started.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.exists())

                await manager.shutdown()
                await asyncio.sleep(1.25)
                self.assertFalse(leaked.exists())
            finally:
                await manager.shutdown()

    async def _assert_process_stopped(self, pid: int) -> None:
        def running() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            stat = Path(f"/proc/{pid}/stat")
            if stat.is_file():
                try:
                    state = stat.read_text(encoding="utf-8").split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    return False
                if state == "Z":
                    return False
            return True

        for _ in range(200):
            if not running():
                return
            await asyncio.sleep(0.01)
        self.fail(f"process {pid} survived background-manager shutdown")


if __name__ == "__main__":
    unittest.main()
