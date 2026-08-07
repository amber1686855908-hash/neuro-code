from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neuro_code.application.ports.sandbox import ShellLaunch
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.infrastructure.background_tasks import LocalBackgroundTaskManager
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.infrastructure.tools.background_tasks import TaskOutputTool
from neuro_code.infrastructure.tools.bash import BashTool
from neuro_code.shared.errors import ToolError


def _python_shell_command(code: str) -> str:
    """Build a Python command using quoting for the host shell.

    使用适合宿主 Shell 的引号构建 Python 命令."""

    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


class BashToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_profile_requires_a_matching_shell_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(
                Path(directory),
                sandbox_profile=SandboxProfile.WORKSPACE,
            )
            with self.assertRaisesRegex(ToolError, "is not enforced"):
                await BashTool().execute({"command": "echo unsafe"}, context)

    async def test_shell_sandbox_supplies_an_argv_safe_launch(self) -> None:
        class FixtureSandbox:
            profile = SandboxProfile.WORKSPACE

            def shell_launch(self, command: str) -> ShellLaunch:
                self.command = command
                return ShellLaunch(sys.executable, ("-c", "print('sandbox launch')"))

        with tempfile.TemporaryDirectory() as directory:
            sandbox = FixtureSandbox()
            result = await BashTool().execute(
                {"command": "ignored by fixture"},
                ToolContext(
                    Path(directory),
                    sandbox_profile=SandboxProfile.WORKSPACE,
                    shell_sandbox=sandbox,
                ),
            )
            self.assertEqual(sandbox.command, "ignored by fixture")
            self.assertEqual(result.content.strip(), "sandbox launch")

    async def test_captures_stdout_stderr_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = (
                f'"{sys.executable}" -c "import sys; print(\'out\'); '
                "print('err', file=sys.stderr)\""
            )
            result = await BashTool().execute({"command": command}, ToolContext(Path(directory)))
            self.assertFalse(result.is_error)
            self.assertIn("out", result.content)
            self.assertIn("err", result.content)
            assert result.metadata is not None
            self.assertEqual(result.metadata["exit_code"], 0)

    async def test_nonzero_exit_and_output_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = f'"{sys.executable}" -c "import sys; print(\'x\'*100); sys.exit(7)"'
            result = await BashTool().execute(
                {"command": command}, ToolContext(Path(directory), output_byte_limit=20)
            )
            self.assertTrue(result.is_error)
            self.assertIn("[output truncated]", result.content)
            assert result.metadata is not None
            self.assertEqual(result.metadata["exit_code"], 7)
            self.assertTrue(result.metadata["truncated"])

    async def test_truncated_foreground_output_has_redacted_artifact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "provider-secret"
            command = _python_shell_command(
                f"print('token={secret}');print('x'*200)",
            )
            store = FileToolOutputArtifactStore(
                root / "tool-output",
                redaction_values=(secret,),
                max_bytes=64,
            )
            result = await BashTool().execute(
                {"command": command},
                ToolContext(root, output_byte_limit=20, output_artifact_store=store),
            )

            assert result.metadata is not None
            artifact_id = result.metadata["output_artifact_id"]
            artifact_path = result.metadata["output_artifact_path"]
            self.assertIsInstance(artifact_id, str)
            self.assertEqual(artifact_path, f"tool-output/{artifact_id}.log")
            content = (root / artifact_path).read_text(encoding="utf-8")
            self.assertNotIn(secret, content)
            self.assertTrue(result.metadata["output_artifact_truncated"])

    async def test_managed_background_truncation_persists_artifact_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            store = FileToolOutputArtifactStore(root / "tool-output", max_bytes=256)
            context = ToolContext(
                root,
                output_byte_limit=20,
                background_tasks=manager,
                output_artifact_store=store,
            )
            try:
                result = await BashTool(background_enabled=True).execute(
                    {"command": _python_shell_command("print('x'*200)"), "is_background": True},
                    context,
                )
                assert result.metadata is not None
                task_id = result.metadata["task_id"]
                self.assertIsInstance(task_id, str)
                snapshot = await manager.get(task_id, wait_seconds=2)
                assert snapshot is not None
                self.assertTrue(snapshot.truncated)
                self.assertIsNotNone(snapshot.output_artifact_id)
                assert snapshot.output_artifact_path is not None
                self.assertTrue((root / snapshot.output_artifact_path).is_file())
            finally:
                await manager.shutdown()

    async def test_enabled_manager_promotes_once_and_preserves_task_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "runs.txt"
            code = (
                f"import pathlib,time;pathlib.Path({str(marker)!r}).open('a').write('run\\n');"
                "print('started',flush=True);time.sleep(0.3);print('finished',flush=True)"
            )
            command = _python_shell_command(code)
            manager = LocalBackgroundTaskManager()
            context = ToolContext(
                root,
                command_timeout_seconds=0.05,
                termination_grace_seconds=0.05,
                background_tasks=manager,
            )
            try:
                result = await BashTool(background_enabled=True).execute(
                    {"command": command},
                    context,
                )
                assert result.metadata is not None
                task_id = result.metadata["task_id"]
                self.assertIsInstance(task_id, str)
                self.assertEqual(result.metadata["status"], "running")
                self.assertTrue(result.metadata["is_background"])
                self.assertTrue(result.metadata["promoted_from_foreground"])
                self.assertNotIn(command, result.content)

                output = await TaskOutputTool().execute(
                    {"task_id": task_id, "wait_seconds": 2},
                    context,
                )
                self.assertIn("finished", output.content)
                self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["run"])
                snapshot = await manager.get(task_id)
                assert snapshot is not None
                self.assertEqual(snapshot.status.value, "completed")
            finally:
                await manager.shutdown()

    async def test_enabled_manager_short_foreground_result_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = "print('short');import sys;sys.exit(7)"
            command = _python_shell_command(code)
            manager = LocalBackgroundTaskManager()
            context = ToolContext(
                root,
                command_timeout_seconds=2,
                output_byte_limit=100,
                background_tasks=manager,
            )
            try:
                result = await BashTool(background_enabled=True).execute(
                    {"command": command},
                    context,
                )
                self.assertTrue(result.is_error)
                self.assertIn("short", result.content)
                assert result.metadata is not None
                self.assertEqual(result.metadata["exit_code"], 7)
                self.assertNotIn("task_id", result.metadata)
                self.assertNotIn("promoted_from_foreground", result.metadata)
                self.assertEqual(await manager.list(), ())
                self.assertEqual(await manager.pending_completions(), ())
            finally:
                await manager.shutdown()

    async def test_full_background_registry_falls_back_to_bounded_foreground_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager(max_running_tasks=1)
            context = ToolContext(
                root,
                command_timeout_seconds=2,
                background_tasks=manager,
            )
            try:
                background = await BashTool(background_enabled=True).execute(
                    {
                        "command": _python_shell_command("import time;time.sleep(60)"),
                        "is_background": True,
                    },
                    context,
                )
                assert background.metadata is not None
                task_id = background.metadata["task_id"]
                self.assertIsInstance(task_id, str)

                result = await BashTool(background_enabled=True).execute(
                    {"command": _python_shell_command('print("foreground fallback")')},
                    context,
                )

                self.assertFalse(result.is_error)
                self.assertEqual(result.content.strip(), "foreground fallback")
                self.assertNotIn("task_id", result.metadata or {})
                await manager.kill(task_id)
            finally:
                await manager.shutdown()

    async def test_manager_capture_failure_with_zero_exit_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = "print('capture failure trigger',flush=True)"
            command = _python_shell_command(code)
            manager = LocalBackgroundTaskManager()
            context = ToolContext(root, command_timeout_seconds=2, background_tasks=manager)
            try:
                with mock.patch(
                    "neuro_code.infrastructure.background_tasks._BoundedOutput.append",
                    side_effect=RuntimeError("controlled capture failure"),
                ):
                    result = await BashTool(background_enabled=True).execute(
                        {"command": command},
                        context,
                    )
                self.assertTrue(result.is_error)
                assert result.metadata is not None
                self.assertEqual(result.metadata["exit_code"], 0)
                self.assertNotIn("task_id", result.metadata)
                self.assertEqual(await manager.list(), ())
                self.assertEqual(await manager.pending_completions(), ())
            finally:
                await manager.shutdown()

    async def test_timeout_terminates_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
            with self.assertRaisesRegex(ToolError, "timed out"):
                await BashTool().execute(
                    {"command": command, "timeout_seconds": 0.05},
                    ToolContext(Path(directory), termination_grace_seconds=0.05),
                )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group assertion")
    async def test_enabled_without_manager_keeps_foreground_timeout_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "foreground-timeout.pid"
            code = (
                f"import pathlib,os,time;pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
                "time.sleep(60)"
            )
            command = _python_shell_command(code)
            with self.assertRaisesRegex(ToolError, "timed out"):
                await BashTool(background_enabled=True).execute(
                    {"command": command, "timeout_seconds": 0.05},
                    ToolContext(root, termination_grace_seconds=0.05),
                )
            self.assertTrue(pid_file.exists())
            await self._assert_process_stopped(int(pid_file.read_text(encoding="utf-8")))

    async def test_explicit_background_timeout_remains_task_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            context = ToolContext(root, background_tasks=manager)
            command = f'"{sys.executable}" -c "import time; time.sleep(60)"'
            try:
                result = await BashTool(background_enabled=True).execute(
                    {
                        "command": command,
                        "is_background": True,
                        "timeout_seconds": 0.05,
                    },
                    context,
                )
                assert result.metadata is not None
                task_id = result.metadata["task_id"]
                self.assertNotIn("promoted_from_foreground", result.metadata)
                snapshot = await manager.get(task_id, wait_seconds=2)
                assert snapshot is not None
                self.assertEqual(snapshot.status.value, "timed_out")
            finally:
                await manager.shutdown()

    async def test_enabled_manager_short_foreground_output_truncation_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = "print('x'*100)"
            command = _python_shell_command(code)
            manager = LocalBackgroundTaskManager()
            context = ToolContext(
                root,
                command_timeout_seconds=2,
                output_byte_limit=20,
                background_tasks=manager,
            )
            try:
                result = await BashTool(background_enabled=True).execute(
                    {"command": command},
                    context,
                )
                self.assertFalse(result.is_error)
                self.assertIn("[output truncated]", result.content)
                assert result.metadata is not None
                self.assertEqual(result.metadata["exit_code"], 0)
                self.assertTrue(result.metadata["truncated"])
                self.assertNotIn("task_id", result.metadata)
                self.assertNotIn("promoted_from_foreground", result.metadata)
                self.assertEqual(await manager.list(), ())
                self.assertEqual(await manager.pending_completions(), ())
            finally:
                await manager.shutdown()

    @unittest.skipUnless(os.name == "posix", "POSIX exec syntax")
    async def test_timeout_also_covers_processes_that_close_output_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = "import os,time;os.close(1);os.close(2);time.sleep(60)"
            command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"
            with self.assertRaisesRegex(ToolError, "timed out"):
                await BashTool().execute(
                    {"command": command, "timeout_seconds": 0.05},
                    ToolContext(Path(directory), termination_grace_seconds=0.05),
                )

    async def test_headless_environment_disables_pagers_and_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = (
                "import os;"
                "print(os.environ['PAGER'],os.environ['GIT_PAGER'],"
                "os.environ['GIT_TERMINAL_PROMPT'])"
            )
            command = f'"{sys.executable}" -c "{code}"'
            with mock.patch.dict(
                os.environ,
                {"PAGER": "less", "GIT_PAGER": "less", "GIT_TERMINAL_PROMPT": "1"},
            ):
                result = await BashTool().execute(
                    {"command": command}, ToolContext(Path(directory))
                )
            self.assertEqual(result.content.strip(), "cat cat 0")

    async def test_protected_provider_and_proxy_environment_values_are_not_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = (
                "import os;"
                "print(os.environ.get('FIXTURE_API_KEY','missing'));"
                "print(os.environ.get('HTTPS_PROXY','missing'))"
            )
            command = f'"{sys.executable}" -c "{code}"'
            with mock.patch.dict(
                os.environ,
                {
                    "FIXTURE_API_KEY": "provider-secret",
                    "HTTPS_PROXY": "http://proxy-secret@127.0.0.1:8080",
                },
                clear=False,
            ):
                result = await BashTool().execute(
                    {"command": command},
                    ToolContext(
                        Path(directory),
                        protected_environment_variables=frozenset(
                            {"fixture_api_key", "https_proxy"}
                        ),
                    ),
                )
            self.assertEqual(result.content.splitlines(), ["missing", "missing"])
            self.assertNotIn("provider-secret", result.content)
            self.assertNotIn("proxy-secret", result.content)

    async def test_auto_promotion_reuses_sandbox_launch_and_protected_environment(self) -> None:
        class FixtureSandbox:
            profile = SandboxProfile.WORKSPACE

            def shell_launch(self, command: str) -> ShellLaunch:
                self.command = command
                code = "import os;print(os.environ.get('FIXTURE_API_KEY','missing'))"
                return ShellLaunch(sys.executable, ("-c", code))

        with tempfile.TemporaryDirectory() as directory:
            manager = LocalBackgroundTaskManager()
            sandbox = FixtureSandbox()
            context = ToolContext(
                Path(directory),
                command_timeout_seconds=2,
                sandbox_profile=SandboxProfile.WORKSPACE,
                shell_sandbox=sandbox,
                protected_environment_variables=frozenset({"fixture_api_key"}),
                background_tasks=manager,
            )
            try:
                with mock.patch.dict(
                    os.environ,
                    {"FIXTURE_API_KEY": "provider-secret"},
                    clear=False,
                ):
                    result = await BashTool(background_enabled=True).execute(
                        {"command": "sandboxed foreground"},
                        context,
                    )
                self.assertEqual(result.content.strip(), "missing")
                self.assertEqual(sandbox.command, "sandboxed foreground")
                self.assertEqual(await manager.list(), ())
                self.assertNotIn("provider-secret", result.content)
            finally:
                await manager.shutdown()

    async def test_argument_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(Path(directory))
            for arguments in ({}, {"command": ""}, {"command": "x\x00y"}):
                with self.assertRaises(ToolError):
                    await BashTool().execute(arguments, context)
            with self.assertRaises(ToolError):
                await BashTool().execute({"command": "echo x", "timeout_seconds": 0}, context)
            with self.assertRaises(ToolError):
                await BashTool().execute({"command": "echo x", "timeout_seconds": True}, context)
            for invalid_timeout in (float("nan"), float("inf")):
                with self.assertRaises(ToolError):
                    await BashTool().execute(
                        {"command": "echo x", "timeout_seconds": invalid_timeout},
                        context,
                    )
            with self.assertRaisesRegex(ToolError, "output_byte_limit"):
                await BashTool().execute(
                    {"command": "echo x"}, ToolContext(Path(directory), output_byte_limit=0)
                )
            with self.assertRaisesRegex(ToolError, "termination_grace_seconds"):
                await BashTool().execute(
                    {"command": "echo x"},
                    ToolContext(Path(directory), termination_grace_seconds=0),
                )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group assertion")
    async def test_timeout_terminates_grandchild_that_ignores_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_code = (
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            )
            parent_code = (
                "import pathlib,signal,subprocess,sys,time;"
                f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "pathlib.Path('grandchild.pid').write_text(str(child.pid));"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(60)"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"

            with self.assertRaisesRegex(ToolError, "timed out"):
                await BashTool().execute(
                    {"command": command, "timeout_seconds": 0.5},
                    ToolContext(root, termination_grace_seconds=0.05),
                )

            grandchild_pid = int((root / "grandchild.pid").read_text(encoding="utf-8"))
            await self._assert_process_stopped(grandchild_pid)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group assertion")
    async def test_cancellation_terminates_owned_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_code = "import time;time.sleep(60)"
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "pathlib.Path('cancel-child.pid').write_text(str(child.pid));"
                "time.sleep(60)"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"
            task = asyncio.create_task(
                BashTool().execute(
                    {"command": command},
                    ToolContext(root, termination_grace_seconds=0.05),
                )
            )
            pid_file = root / "cancel-child.pid"
            for _ in range(100):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(pid_file.exists())

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            await self._assert_process_stopped(int(pid_file.read_text(encoding="utf-8")))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group assertion")
    async def test_auto_promotion_cancellation_kills_tree_and_discards_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_code = "import time;time.sleep(60)"
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                "pathlib.Path('auto-cancel-child.pid').write_text(str(child.pid));"
                "time.sleep(60)"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"
            manager = LocalBackgroundTaskManager()
            context = ToolContext(
                root,
                command_timeout_seconds=10,
                termination_grace_seconds=0.05,
                background_tasks=manager,
            )
            task = asyncio.create_task(
                BashTool(background_enabled=True).execute({"command": command}, context)
            )
            pid_file = root / "auto-cancel-child.pid"
            try:
                for _ in range(100):
                    if pid_file.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(pid_file.exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(await manager.list(), ())
                self.assertEqual(await manager.pending_completions(), ())
                await self._assert_process_stopped(int(pid_file.read_text(encoding="utf-8")))
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
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
                    fields = stat.read_text(encoding="utf-8").split()
                except (FileNotFoundError, ProcessLookupError):
                    return False
                return len(fields) < 3 or fields[2] != "Z"
            return True

        for _ in range(200):
            if not running():
                return
            await asyncio.sleep(0.01)
        self.fail(f"process {pid} survived process-group termination")


if __name__ == "__main__":
    unittest.main()
