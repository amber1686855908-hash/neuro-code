from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from neuro_code.infrastructure.sandbox.process_tree import ProcessTree
from neuro_code.infrastructure.sandbox.windows_job import WindowsJobObject


class _FastExitProcess:
    pid = 12_345

    def __init__(self) -> None:
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class _SpawnedProcessFixture(_FastExitProcess):
    def __init__(self) -> None:
        super().__init__()
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1


class _WindowsJobFixture:
    def __init__(self, active_processes: int = 0) -> None:
        self.active_process_count = active_processes
        self.closed = False
        self.terminated = False

    @property
    def process_creation_handle(self) -> int:
        return 101

    @property
    def active_processes(self) -> int:
        return self.active_process_count

    def terminate(self, exit_code: int = 1) -> None:
        del exit_code
        self.terminated = True
        self.active_process_count = 0

    def close(self) -> None:
        self.closed = True


@unittest.skipUnless(hasattr(os, "killpg"), "POSIX process-group API required")
class ProcessTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_posix_spawn_closes_inheritable_fds_unless_explicitly_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
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
                closed_tree = await ProcessTree.spawn_exec(
                    sys.executable,
                    ("-c", code, str(descriptor), str(details.st_dev), str(details.st_ino)),
                    cwd=root,
                    env=os.environ,
                )
                assert closed_tree.process.stdout is not None
                self.assertEqual((await closed_tree.process.stdout.readline()).strip(), b"closed")
                self.assertEqual(await closed_tree.wait(), 0)

                passed_tree = await ProcessTree.spawn_exec(
                    sys.executable,
                    ("-c", code, str(descriptor), str(details.st_dev), str(details.st_ino)),
                    cwd=root,
                    env=os.environ,
                    pass_fds=(descriptor,),
                )
                assert passed_tree.process.stdout is not None
                self.assertEqual(
                    (await passed_tree.process.stdout.readline()).strip(), b"inherited"
                )
                self.assertEqual(await passed_tree.wait(), 0)
            finally:
                os.close(descriptor)

    async def test_posix_termination_retries_transient_permission_error_after_reap(self) -> None:
        process = _FastExitProcess()
        tree = ProcessTree(cast(asyncio.subprocess.Process, process), process.pid)
        transient = PermissionError(1, "transient group state")
        with mock.patch(
            "neuro_code.infrastructure.sandbox.process_tree.os.killpg",
            side_effect=(transient, ProcessLookupError()),
        ) as killpg:
            await tree._terminate_posix(0.1, 0.1)

        self.assertEqual(process.returncode, 0)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGTERM),
            ],
        )

    async def test_posix_termination_keeps_persistent_permission_error_fail_closed(self) -> None:
        process = _FastExitProcess()
        tree = ProcessTree(cast(asyncio.subprocess.Process, process), process.pid)
        with (
            mock.patch(
                "neuro_code.infrastructure.sandbox.process_tree.os.killpg",
                side_effect=(
                    PermissionError(1, "transient group state"),
                    PermissionError(1, "persistent denial"),
                ),
            ),
            self.assertRaisesRegex(PermissionError, "persistent denial"),
        ):
            await tree._terminate_posix(0.1, 0.1)


class WindowsProcessTreeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_spawn_failure_closes_the_preconfigured_job(self) -> None:
        job = _WindowsJobFixture()
        with (
            mock.patch("neuro_code.infrastructure.sandbox.process_tree.os.name", "nt"),
            mock.patch(
                "neuro_code.infrastructure.sandbox.process_tree.WindowsJobObject.create",
                return_value=job,
            ),
            mock.patch(
                "neuro_code.infrastructure.sandbox.process_tree.WindowsJobProcess.spawn_exec",
                side_effect=OSError("fixture atomic creation failure"),
            ),
            self.assertRaisesRegex(OSError, "fixture atomic creation failure"),
        ):
            await ProcessTree.spawn_exec(
                "fixture",
                (),
                cwd=Path("/workspace"),
                env={},
            )

        self.assertTrue(job.closed)

    async def test_spawn_passes_job_handle_into_atomic_process_creation(self) -> None:
        process = _SpawnedProcessFixture()
        job = _WindowsJobFixture()
        cwd = Path("/workspace")
        with (
            mock.patch("neuro_code.infrastructure.sandbox.process_tree.os.name", "nt"),
            mock.patch(
                "neuro_code.infrastructure.sandbox.process_tree.WindowsJobObject.create",
                return_value=job,
            ),
            mock.patch(
                "neuro_code.infrastructure.sandbox.process_tree.WindowsJobProcess.spawn_exec",
                return_value=process,
            ) as spawn,
        ):
            tree = await ProcessTree.spawn_exec(
                "fixture",
                ("argument",),
                cwd=cwd,
                env={"NAME": "value"},
            )
            self.assertEqual(await tree.wait(), 0)

        self.assertIs(tree.process, process)
        spawn.assert_called_once_with(
            "fixture",
            ("argument",),
            cwd=cwd,
            env={"NAME": "value"},
            job_handle=101,
            merge_output=False,
            pipe_stdin=False,
        )
        self.assertTrue(job.closed)

    async def test_wait_keeps_job_until_all_descendants_exit_then_closes_it(self) -> None:
        process = _FastExitProcess()
        job = _WindowsJobFixture(active_processes=1)
        tree = ProcessTree(
            cast(asyncio.subprocess.Process, process),
            _windows_job=cast(WindowsJobObject, job),
        )

        async def release_descendant() -> None:
            await asyncio.sleep(0.03)
            job.active_process_count = 0

        release = asyncio.create_task(release_descendant())
        with mock.patch("neuro_code.infrastructure.sandbox.process_tree.os.name", "nt"):
            self.assertEqual(await tree.wait(), 0)
        await release

        self.assertTrue(job.closed)
        self.assertFalse(job.terminated)

    async def test_termination_forces_job_then_closes_and_reaps_direct_child(self) -> None:
        process = _FastExitProcess()
        job = _WindowsJobFixture(active_processes=2)
        tree = ProcessTree(
            cast(asyncio.subprocess.Process, process),
            _windows_job=cast(WindowsJobObject, job),
        )

        with mock.patch("neuro_code.infrastructure.sandbox.process_tree.os.name", "nt"):
            await tree.terminate(grace_seconds=0, force_wait_seconds=0.1)
            await tree.terminate(grace_seconds=0, force_wait_seconds=0.1)

        self.assertTrue(job.terminated)
        self.assertTrue(job.closed)
        self.assertEqual(process.returncode, 0)


@unittest.skipUnless(os.name == "nt", "native Windows Job Object required")
class WindowsProcessTreeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_observes_a_descendant_after_the_direct_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = root / "spawn-descendant"
            started = root / "descendant-started"
            completed = root / "descendant-completed"
            child_code = (
                "import pathlib,time;"
                f"pathlib.Path({str(started)!r}).write_text('started');"
                "time.sleep(0.3);"
                f"pathlib.Path({str(completed)!r}).write_text('completed')"
            )
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                f"gate=pathlib.Path({str(gate)!r});"
                "deadline=time.monotonic()+5;"
                "\nwhile not gate.exists() and time.monotonic()<deadline: time.sleep(0.01);"
                "\nif not gate.exists(): raise SystemExit(2)\n"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                f"started=pathlib.Path({str(started)!r});"
                "deadline=time.monotonic()+5;"
                "\nwhile not started.exists() and time.monotonic()<deadline: time.sleep(0.01)"
            )
            tree = await ProcessTree.spawn_exec(
                sys.executable,
                ("-c", parent_code),
                cwd=root,
                env=os.environ,
            )
            try:
                gate.write_text("go", encoding="utf-8")

                self.assertEqual(await asyncio.wait_for(tree.wait(), timeout=10), 0)
                self.assertTrue(started.exists())
                self.assertTrue(completed.exists())
            finally:
                await tree.terminate(grace_seconds=0.05)

    async def test_termination_prevents_a_descendant_from_outliving_the_tree(self) -> None:
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
                f"started=pathlib.Path({str(started)!r});"
                "deadline=time.monotonic()+5;"
                "\nwhile not started.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                "time.sleep(60)"
            )
            tree = await ProcessTree.spawn_exec(
                sys.executable,
                ("-c", parent_code),
                cwd=root,
                env=os.environ,
            )
            try:
                gate.write_text("go", encoding="utf-8")
                for _ in range(500):
                    if started.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.exists())

                await tree.terminate(grace_seconds=0.05)
                await asyncio.sleep(1.25)
                self.assertFalse(leaked.exists())
            finally:
                await tree.terminate(grace_seconds=0.05)


if __name__ == "__main__":
    unittest.main()
