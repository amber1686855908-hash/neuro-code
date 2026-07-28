from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neuro_code.adapters.background_tasks import LocalBackgroundTaskManager
from neuro_code.application.ports.sandbox import ShellLaunch
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.shared.errors import ToolError
from neuro_code.tools import default_tool_registry
from neuro_code.tools.background_tasks import KillTaskTool, TaskOutputTool, WaitTasksTool
from neuro_code.tools.bash import BashTool


class BackgroundTaskToolTests(unittest.IsolatedAsyncioTestCase):
    def test_registry_only_advertises_background_contract_when_enabled(self) -> None:
        default = default_tool_registry()
        self.assertNotIn("task_output", default.names())
        self.assertNotIn("wait_tasks", default.names())
        self.assertNotIn("kill_task", default.names())
        default_bash = default.get("bash")
        assert default_bash is not None
        self.assertNotIn("is_background", default_bash.definition.input_schema["properties"])

        enabled = default_tool_registry(enable_background_tasks=True)
        self.assertIn("task_output", enabled.names())
        self.assertIn("wait_tasks", enabled.names())
        self.assertIn("kill_task", enabled.names())
        enabled_bash = enabled.get("bash")
        assert enabled_bash is not None
        self.assertIn("is_background", enabled_bash.definition.input_schema["properties"])

    async def test_bash_start_poll_wait_and_kill_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalBackgroundTaskManager()
            context = ToolContext(Path(directory), background_tasks=manager)
            bash = BashTool(background_enabled=True)
            try:
                started = await bash.execute(
                    {
                        "command": f'"{sys.executable}" -c "import time;time.sleep(60)"',
                        "is_background": True,
                    },
                    context,
                )
                assert started.metadata is not None
                task_id = started.metadata["task_id"]
                assert isinstance(task_id, str)
                self.assertIn(task_id, started.content)

                running = await TaskOutputTool().execute({"task_id": task_id}, context)
                self.assertIn("status: running", running.content)
                self.assertFalse(running.is_error)

                killed = await KillTaskTool().execute({"task_id": task_id}, context)
                self.assertIn("outcome: killed", killed.content)
                self.assertEqual(await manager.pending_completions(), ())

                cancelled = await TaskOutputTool().execute(
                    {"task_id": task_id, "wait_seconds": 1},
                    context,
                )
                self.assertIn("status: cancelled", cancelled.content)
                self.assertFalse(cancelled.is_error)
            finally:
                await manager.shutdown()

    async def test_background_command_uses_sandbox_launch_and_strips_secrets(self) -> None:
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
                    started = await BashTool(background_enabled=True).execute(
                        {"command": "original command", "is_background": True},
                        context,
                    )
                assert started.metadata is not None
                task_id = started.metadata["task_id"]
                assert isinstance(task_id, str)
                output = await TaskOutputTool().execute(
                    {"task_id": task_id, "wait_seconds": 2},
                    context,
                )
                self.assertEqual(await manager.pending_completions(), ())
                self.assertEqual(sandbox.command, "original command")
                self.assertIn("command: original command", output.content)
                self.assertIn("missing", output.content)
                self.assertNotIn("provider-secret", output.content)
            finally:
                await manager.shutdown()

    async def test_wait_tasks_waits_all_deduplicates_and_consumes_completions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            context = ToolContext(root, background_tasks=manager)
            bash = BashTool(background_enabled=True)
            try:
                first = await bash.execute(
                    {
                        "command": f'"{sys.executable}" -c "print(\'first-output\')"',
                        "is_background": True,
                    },
                    context,
                )
                second = await bash.execute(
                    {
                        "command": (
                            f'"{sys.executable}" -c "import time;time.sleep(0.03);'
                            "print('second-output')\""
                        ),
                        "is_background": True,
                    },
                    context,
                )
                assert first.metadata is not None
                assert second.metadata is not None
                first_id = first.metadata["task_id"]
                second_id = second.metadata["task_id"]
                assert isinstance(first_id, str)
                assert isinstance(second_id, str)

                waited = await WaitTasksTool().execute(
                    {
                        "task_ids": [f" {first_id} ", second_id, first_id],
                        "mode": "wait_all",
                        "timeout_seconds": 2,
                    },
                    context,
                )
                self.assertFalse(waited.is_error)
                self.assertIn("summary: 2/2 tasks reached a terminal state", waited.content)
                self.assertIn("first-output", waited.content)
                self.assertIn("second-output", waited.content)
                assert waited.metadata is not None
                self.assertEqual(waited.metadata["requested_count"], 2)
                self.assertEqual(waited.metadata["terminal_count"], 2)
                self.assertEqual(await manager.pending_completions(), ())
            finally:
                await manager.shutdown()

    async def test_wait_tasks_reports_partial_timeout_unknown_ids_and_bounds_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LocalBackgroundTaskManager()
            context = ToolContext(root, output_byte_limit=120, background_tasks=manager)
            try:
                started = await BashTool(background_enabled=True).execute(
                    {
                        "command": f'"{sys.executable}" -c "import time;time.sleep(60)"',
                        "is_background": True,
                    },
                    context,
                )
                assert started.metadata is not None
                task_id = started.metadata["task_id"]
                assert isinstance(task_id, str)

                waited = await WaitTasksTool().execute(
                    {
                        "task_ids": [task_id, "missing"],
                        "mode": "wait_any",
                        "timeout_seconds": 0.01,
                    },
                    context,
                )
                self.assertTrue(waited.is_error)
                self.assertIn("timed_out: true", waited.content)
                self.assertIn("[wait_tasks output truncated]", waited.content)
                assert waited.metadata is not None
                self.assertTrue(waited.metadata["timed_out"])
                results = waited.metadata["results"]
                assert isinstance(results, list)
                self.assertEqual(results[1], {"task_id": "missing", "status": "not_found"})
            finally:
                await manager.shutdown()

    async def test_argument_and_unavailable_manager_failures_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(Path(directory))
            with self.assertRaisesRegex(ToolError, "not enabled"):
                await BashTool().execute(
                    {"command": "echo x", "is_background": True},
                    context,
                )
            with self.assertRaisesRegex(ToolError, "unavailable"):
                await BashTool(background_enabled=True).execute(
                    {"command": "echo x", "is_background": True},
                    context,
                )
            for arguments in (
                {"task_id": ""},
                {"task_id": "unknown", "wait_seconds": True},
                {"task_id": "unknown", "wait_seconds": 31},
                {"task_id": "unknown", "wait_seconds": float("nan")},
                {"task_id": "unknown", "wait_seconds": float("inf")},
            ):
                with self.assertRaises(ToolError):
                    await TaskOutputTool().execute(arguments, context)
            for arguments in (
                {"task_ids": [], "mode": "wait_all"},
                {"task_ids": ["x"] * 21, "mode": "wait_all"},
                {"task_ids": [1], "mode": "wait_all"},
                {"task_ids": ["x"], "mode": "invalid"},
                {"task_ids": ["x"], "mode": "wait_any", "timeout_seconds": True},
                {"task_ids": ["x"], "mode": "wait_any", "timeout_seconds": 31},
                {
                    "task_ids": ["x"],
                    "mode": "wait_any",
                    "timeout_seconds": float("nan"),
                },
            ):
                with self.assertRaises(ToolError):
                    await WaitTasksTool().execute(arguments, context)
            with self.assertRaisesRegex(ToolError, "inspection is unavailable"):
                await TaskOutputTool().execute({"task_id": "unknown"}, context)
            with self.assertRaisesRegex(ToolError, "waiting is unavailable"):
                await WaitTasksTool().execute(
                    {"task_ids": ["unknown"], "mode": "wait_any"},
                    context,
                )
            with self.assertRaisesRegex(ToolError, "termination is unavailable"):
                await KillTaskTool().execute({"task_id": "unknown"}, context)

    async def test_unknown_task_reports_known_ids_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalBackgroundTaskManager()
            context = ToolContext(Path(directory), background_tasks=manager)
            try:
                started = await BashTool(background_enabled=True).execute(
                    {"command": "printf private-output", "is_background": True},
                    context,
                )
                assert started.metadata is not None
                task_id = started.metadata["task_id"]
                assert isinstance(task_id, str)
                with self.assertRaisesRegex(ToolError, task_id) as raised:
                    await TaskOutputTool().execute({"task_id": "missing"}, context)
                self.assertNotIn("private-output", str(raised.exception))
            finally:
                await manager.shutdown()


if __name__ == "__main__":
    unittest.main()
