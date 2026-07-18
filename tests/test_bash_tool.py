from __future__ import annotations

import asyncio
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.errors import ToolError
from neuro_code.ports.sandbox import ShellLaunch
from neuro_code.ports.tools import ToolContext
from neuro_code.tools.bash import BashTool


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

    async def test_timeout_terminates_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
            with self.assertRaisesRegex(ToolError, "timed out"):
                await BashTool().execute(
                    {"command": command, "timeout_seconds": 0.05},
                    ToolContext(Path(directory), termination_grace_seconds=0.05),
                )

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
