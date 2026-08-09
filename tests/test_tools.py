from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from neuro_code.application.ports.client_terminal import ClientTerminalResult
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.background_tasks import (
    BackgroundTaskKillOutcome,
    BackgroundTaskKillResult,
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.workspace.skills import SkillDiscoveryResult
from neuro_code.infrastructure.tools.client_terminal import ClientTerminalTool
from neuro_code.infrastructure.tools.filesystem import (
    GrepManyTool,
    GrepTool,
    ListTreeTool,
    ReadFilesTool,
    ReadFileTool,
    SearchReplaceTool,
)
from neuro_code.infrastructure.tools.plans import UpdatePlanTool
from neuro_code.infrastructure.tools.registry import default_tool_registry
from neuro_code.infrastructure.workspace.paths import resolve_workspace_path, workspaces_match
from neuro_code.shared.errors import ToolError


def _canonical_path(path: str | Path) -> Path:
    return Path(path).resolve()


class PathRecordingTracker:
    def __init__(self, workspace_root: Path) -> None:
        self.paths: list[Path] = []
        self._workspace_root = workspace_root

    def check_path(self, path: Path) -> None:
        self.paths.append(path)

    def check_path_for_write(self, path: Path) -> None:
        self.paths.append(path)

    def current_result(self) -> SkillDiscoveryResult:
        return SkillDiscoveryResult((), (), "0" * 64)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root


class ClientFileSystemFixture:
    def __init__(
        self,
        *,
        contents: dict[str, str] | None = None,
        supports_read: bool = True,
        supports_write: bool = True,
    ) -> None:
        self.contents = dict(contents or {})
        self.supports_read = supports_read
        self.supports_write = supports_write
        self.reads: list[tuple[Path, int | None, int | None]] = []
        self.writes: list[tuple[Path, str]] = []

    async def read_text_file(
        self,
        path: Path,
        /,
        *,
        line: int | None = None,
        limit: int | None = None,
    ) -> str:
        self.reads.append((path, line, limit))
        text = self.contents[str(path)]
        if line is None or limit is None:
            return text
        return "\n".join(text.splitlines()[line - 1 : line - 1 + limit])

    async def write_text_file(self, path: Path, content: str, /) -> None:
        self.writes.append((path, content))
        self.contents[str(path)] = content


class ClientTerminalFixture:
    def __init__(
        self,
        result: ClientTerminalResult | object | None = None,
    ) -> None:
        self.result = result or ClientTerminalResult(
            output="terminal output",
            exit_code=0,
            signal=None,
            truncated=False,
        )
        self.calls: list[tuple[str, tuple[str, ...], Path, int, float]] = []
        self.starts: list[tuple[str, tuple[str, ...], Path, int, float | None]] = []
        self.tasks: dict[str, BackgroundTaskSnapshot] = {}
        self.shutdown_calls = 0

    async def run(
        self,
        command: str,
        arguments: Sequence[str],
        /,
        *,
        cwd: Path,
        output_byte_limit: int,
        timeout_seconds: float,
    ) -> ClientTerminalResult:
        self.calls.append((command, tuple(arguments), cwd, output_byte_limit, timeout_seconds))
        return cast(ClientTerminalResult, self.result)

    async def start_exec(
        self,
        command: str,
        arguments: Sequence[str],
        /,
        *,
        cwd: Path,
        output_byte_limit: int,
        timeout_seconds: float | None = None,
    ) -> BackgroundTaskSnapshot:
        self.starts.append((command, tuple(arguments), cwd, output_byte_limit, timeout_seconds))
        task_id = f"terminal-task-{len(self.tasks) + 1}"
        snapshot = BackgroundTaskSnapshot(
            task_id=task_id,
            command=command,
            cwd=str(cwd),
            status=BackgroundTaskStatus.RUNNING,
            output="terminal output",
            total_output_bytes=15,
            truncated=False,
            exit_code=None,
            started_at=datetime.now(UTC),
        )
        self.tasks[task_id] = snapshot
        return snapshot

    async def get(
        self,
        task_id: str,
        *,
        wait_seconds: float = 0.0,
    ) -> BackgroundTaskSnapshot | None:
        del wait_seconds
        return self.tasks.get(task_id)

    async def wait(
        self,
        task_ids: tuple[str, ...],
        *,
        mode: BackgroundTaskWaitMode,
        timeout_seconds: float,
    ) -> BackgroundTaskWaitResult:
        del timeout_seconds
        snapshots = tuple(self.tasks[task_id] for task_id in task_ids if task_id in self.tasks)
        return BackgroundTaskWaitResult(
            mode=mode,
            snapshots=snapshots,
            missing_task_ids=tuple(task_id for task_id in task_ids if task_id not in self.tasks),
            timed_out=False,
        )

    async def kill(self, task_id: str) -> BackgroundTaskKillResult | None:
        current = self.tasks.get(task_id)
        if current is None:
            return None
        if current.status.terminal:
            return BackgroundTaskKillResult(BackgroundTaskKillOutcome.ALREADY_EXITED, current)
        cancelled = BackgroundTaskSnapshot(
            task_id=current.task_id,
            command=current.command,
            cwd=current.cwd,
            status=BackgroundTaskStatus.CANCELLED,
            output=current.output,
            total_output_bytes=current.total_output_bytes,
            truncated=current.truncated,
            exit_code=None,
            started_at=current.started_at,
            finished_at=datetime.now(UTC),
        )
        self.tasks[task_id] = cancelled
        return BackgroundTaskKillResult(BackgroundTaskKillOutcome.KILLED, cancelled)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class FilesystemToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_grep_and_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example.py"
            target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            context = ToolContext(root)

            read_result = await ReadFileTool().execute({"path": "example.py"}, context)
            self.assertIn("2\tbeta", read_result.content)

            grep_result = await GrepTool().execute({"query": "bet.", "path": "."}, context)
            self.assertIn("example.py:2:beta", grep_result.content)

            replace_result = await SearchReplaceTool().execute(
                {"path": "example.py", "old": "beta", "new": "delta"}, context
            )
            self.assertFalse(replace_result.is_error)
            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\ndelta\ngamma\n")

    async def test_ambiguous_replace_fails_without_mutating_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "values.txt"
            target.write_text("same\nsame\n", encoding="utf-8")
            with self.assertRaises(ToolError):
                await SearchReplaceTool().execute(
                    {"path": "values.txt", "old": "same", "new": "changed"},
                    ToolContext(root),
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "same\nsame\n")

    async def test_read_only_profile_hides_and_rejects_the_edit_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "values.txt"
            target.write_text("before\n", encoding="utf-8")

            registry = default_tool_registry(SandboxProfile.READ_ONLY)
            self.assertNotIn("search_replace", registry.names())
            with self.assertRaisesRegex(ToolError, "prohibits workspace edits"):
                await SearchReplaceTool().execute(
                    {"path": "values.txt", "old": "before", "new": "after"},
                    ToolContext(root, sandbox_profile=SandboxProfile.READ_ONLY),
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    async def test_client_file_capability_delegates_read_and_exact_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _canonical_path(directory)
            target = root / "remote.txt"
            client_file_system = ClientFileSystemFixture(
                contents={str(target): "alpha\nbeta\ngamma\n"}
            )
            context = ToolContext(root, client_file_system=client_file_system)

            read = await ReadFileTool().execute(
                {"path": "remote.txt", "start_line": 2, "max_lines": 1},
                context,
            )
            self.assertEqual(read.content, "     2\tbeta")
            self.assertEqual(
                client_file_system.reads,
                [(target, 2, 1)],
            )
            self.assertFalse(target.exists())
            self.assertTrue(read.metadata and read.metadata["client_delegated"])

            replaced = await SearchReplaceTool().execute(
                {"path": "remote.txt", "old": "beta", "new": "delta"},
                context,
            )
            self.assertFalse(replaced.is_error)
            self.assertEqual(
                client_file_system.contents[str(target)],
                "alpha\ndelta\ngamma\n",
            )
            self.assertEqual(
                client_file_system.writes,
                [(target, "alpha\ndelta\ngamma\n")],
            )
            self.assertTrue(replaced.metadata and replaced.metadata["client_delegated"])

            client_file_system.contents[str(target)] = "same\nsame\n"
            with self.assertRaisesRegex(ToolError, "ambiguous"):
                await SearchReplaceTool().execute(
                    {"path": "remote.txt", "old": "same", "new": "changed"},
                    context,
                )
            client_file_system.contents[str(target)] = "unchanged"
            with self.assertRaisesRegex(ToolError, "was not found"):
                await SearchReplaceTool().execute(
                    {"path": "remote.txt", "old": "missing", "new": "changed"},
                    context,
                )

    async def test_client_file_capability_fails_closed_without_write_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_file_system = ClientFileSystemFixture(supports_write=False)

            registry = default_tool_registry(client_file_system=client_file_system)
            self.assertNotIn("search_replace", registry.names())
            with self.assertRaisesRegex(ToolError, "does not support text-file replacement"):
                await SearchReplaceTool().execute(
                    {"path": "remote.txt", "old": "before", "new": "after"},
                    ToolContext(root, client_file_system=client_file_system),
                )

    async def test_client_file_read_requires_capability_and_honors_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _canonical_path(directory)
            target = root / "remote.txt"
            unavailable = ClientFileSystemFixture(supports_read=False)
            with self.assertRaisesRegex(ToolError, "does not support text-file reads"):
                await ReadFileTool().execute(
                    {"path": "remote.txt"},
                    ToolContext(root, client_file_system=unavailable),
                )

            bounded = ClientFileSystemFixture(contents={str(target): "alpha\nbeta"})
            result = await ReadFileTool().execute(
                {"path": "remote.txt"},
                ToolContext(root, output_byte_limit=12, client_file_system=bounded),
            )
            self.assertIn("[output truncated]", result.content)

    async def test_read_files_preserves_order_isolates_errors_and_updates_trackers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _canonical_path(directory)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("alpha\napi_key=fixture-secret\n", encoding="utf-8")
            second.write_text("beta\ngamma\n", encoding="utf-8")
            instruction_tracker = PathRecordingTracker(root)
            skill_tracker = PathRecordingTracker(root)
            result = await ReadFilesTool().execute(
                {
                    "files": [
                        {"path": "second.py", "start_line": 2, "max_lines": 1},
                        {"path": "missing.py"},
                        {"path": "first.py", "max_lines": 2},
                    ]
                },
                ToolContext(
                    root,
                    redaction_values=("fixture-secret",),
                    instruction_tracker=instruction_tracker,
                    skill_tracker=skill_tracker,
                ),
            )

            second_header = result.content.index("=== file: second.py ===")
            missing_header = result.content.index("=== file: missing.py ===")
            first_header = result.content.index("=== file: first.py ===")
            self.assertLess(second_header, missing_header)
            self.assertLess(missing_header, first_header)
            self.assertIn("     2\tgamma", result.content)
            self.assertIn("status: error", result.content)
            self.assertIn("api_key=[REDACTED]", result.content)
            self.assertNotIn("fixture-secret", result.content)
            self.assertFalse(result.is_error)
            self.assertEqual(
                result.metadata,
                {
                    "requested": 3,
                    "succeeded": 2,
                    "failed": 1,
                    "truncated": False,
                    "client_delegated": False,
                },
            )
            self.assertEqual(instruction_tracker.paths, [second, first])
            self.assertEqual(skill_tracker.paths, [second, first])

    async def test_read_files_enforces_count_output_and_workspace_bounds(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = _canonical_path(directory)
            outside = _canonical_path(outside_directory) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            target = root / "large.txt"
            target.write_text("line\n" * 100, encoding="utf-8")
            with self.assertRaisesRegex(ToolError, "between 1 and 16"):
                await ReadFilesTool().execute(
                    {"files": [{"path": "large.txt"}] * 17},
                    ToolContext(root),
                )
            invalid_lines = await ReadFilesTool().execute(
                {"files": [{"path": "large.txt", "max_lines": 5001}]},
                ToolContext(root),
            )
            self.assertTrue(invalid_lines.is_error)
            self.assertIn("max_lines must be between 1 and 5000", invalid_lines.content)

            bounded = await ReadFilesTool().execute(
                {"files": [{"path": "large.txt", "max_lines": 100}]},
                ToolContext(root, output_byte_limit=80),
            )
            self.assertLessEqual(len(bounded.content.encode("utf-8")), 80)
            self.assertTrue(bounded.metadata and bounded.metadata["truncated"])
            self.assertTrue(bounded.content.endswith("[output truncated]"))

            escaped = await ReadFilesTool().execute(
                {
                    "files": [
                        {"path": "large.txt", "max_lines": 1},
                        {"path": str(outside)},
                    ]
                },
                ToolContext(root),
            )
            self.assertFalse(escaped.is_error)
            self.assertIn("large.txt", escaped.content)
            self.assertIn("escapes the workspace", escaped.content)

    async def test_read_files_delegates_each_request_to_client_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _canonical_path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            client = ClientFileSystemFixture(
                contents={
                    str(first): "one\ntwo\nthree",
                    str(second): "four\nfive",
                }
            )
            result = await ReadFilesTool().execute(
                {
                    "files": [
                        {"path": "first.txt", "start_line": 2, "max_lines": 2},
                        {"path": "second.txt", "max_lines": 1},
                    ]
                },
                ToolContext(root, client_file_system=client),
            )

            self.assertEqual(client.reads, [(first, 2, 2), (second, 1, 1)])
            self.assertIn("     2\ttwo", result.content)
            self.assertIn("     1\tfour", result.content)
            self.assertTrue(result.metadata and result.metadata["client_delegated"])

    async def test_list_tree_is_deterministic_bounded_and_skips_unsafe_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = _canonical_path(directory)
            (root / "src" / "nested").mkdir(parents=True)
            (root / "src" / "z.py").write_text("z", encoding="utf-8")
            (root / "src" / "a.py").write_text("a", encoding="utf-8")
            (root / "src" / "nested" / "deep.py").write_text("deep", encoding="utf-8")
            for ignored in (".git", ".venv", "node_modules", "dist", "__pycache__"):
                (root / ignored).mkdir()
                (root / ignored / "hidden.txt").write_text("hidden", encoding="utf-8")
            if hasattr(os, "symlink"):
                with suppress(OSError):
                    (root / "outside-link").symlink_to(Path(outside), target_is_directory=True)

            result = await ListTreeTool().execute(
                {"path": ".", "max_depth": 2, "max_entries": 20},
                ToolContext(root),
            )

            self.assertEqual(result.content.splitlines(), ["src/", "  a.py", "  nested/", "  z.py"])
            self.assertNotIn("hidden.txt", result.content)
            self.assertNotIn("outside-link", result.content)
            self.assertEqual(result.metadata and result.metadata["ignored_directories"], 5)

            limited = await ListTreeTool().execute(
                {"path": ".", "max_depth": 3, "max_entries": 2},
                ToolContext(root),
            )
            self.assertEqual(len(limited.content.splitlines()), 2)
            self.assertTrue(limited.metadata and limited.metadata["entry_limited"])
            byte_limited = await ListTreeTool().execute(
                {"path": ".", "max_depth": 3, "max_entries": 20},
                ToolContext(root, output_byte_limit=20),
            )
            self.assertTrue(byte_limited.metadata and byte_limited.metadata["byte_limited"])
            self.assertLessEqual(len(byte_limited.content.encode("utf-8")), 20)
            with self.assertRaisesRegex(ToolError, "escapes the workspace"):
                await ListTreeTool().execute({"path": str(Path(outside))}, ToolContext(root))

    async def test_grep_many_groups_queries_filters_paths_and_isolates_invalid_regex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _canonical_path(directory)
            (root / "src").mkdir()
            (root / "src" / "b.py").write_text("alpha\nbeta\n", encoding="utf-8")
            (root / "src" / "a.py").write_text("alpha beta\n", encoding="utf-8")
            (root / "README.md").write_text("alpha", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("alpha", encoding="utf-8")

            result = await GrepManyTool().execute(
                {
                    "queries": ["alpha", "beta", "["],
                    "path": ".",
                    "include_globs": ["*.py"],
                    "exclude_globs": ["src/b.py"],
                    "max_results_per_query": 5,
                    "max_total_results": 10,
                },
                ToolContext(root),
            )

            self.assertFalse(result.is_error)
            self.assertIn("=== query 1: 'alpha' ===", result.content)
            self.assertIn("=== query 2: 'beta' ===", result.content)
            self.assertIn("=== query 3: '[' ===\nstatus: error", result.content)
            self.assertIn("src/a.py:1:alpha beta", result.content)
            self.assertNotIn("src/b.py", result.content)
            self.assertNotIn("README.md", result.content)
            self.assertNotIn(".git", result.content)
            self.assertEqual(result.metadata and result.metadata["match_count"], 2)
            self.assertEqual(result.metadata and result.metadata["valid_queries"], 2)

    async def test_grep_many_enforces_result_output_and_workspace_bounds(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = _canonical_path(directory)
            outside = _canonical_path(outside_directory)
            (root / "matches.txt").write_text("match\n" * 20, encoding="utf-8")
            result = await GrepManyTool().execute(
                {
                    "queries": ["match", "mat"],
                    "max_results_per_query": 20,
                    "max_total_results": 3,
                },
                ToolContext(root, output_byte_limit=100),
            )

            self.assertTrue(result.metadata and result.metadata["result_limited"])
            self.assertTrue(result.metadata and result.metadata["byte_limited"])
            self.assertEqual(result.metadata and result.metadata["match_count"], 3)
            self.assertLessEqual(len(result.content.encode("utf-8")), 100)
            with self.assertRaisesRegex(ToolError, "between 1 and 16"):
                await GrepManyTool().execute(
                    {"queries": ["match"] * 17},
                    ToolContext(root),
                )
            with self.assertRaisesRegex(ToolError, "escapes the workspace"):
                await GrepManyTool().execute(
                    {"queries": ["match"], "path": str(outside)},
                    ToolContext(root),
                )

    def test_default_registry_exposes_bounded_batch_read_only_tools(self) -> None:
        registry = default_tool_registry(SandboxProfile.READ_ONLY)

        self.assertTrue(
            {
                "read_file",
                "read_files",
                "list_dir",
                "list_tree",
                "grep",
                "grep_many",
            }.issubset(registry.names())
        )
        for name in ("read_files", "list_tree", "grep_many"):
            tool = registry.get(name)
            self.assertIsNotNone(tool)
            assert tool is not None
            self.assertFalse(tool.side_effecting)


class PlanToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_plan_validates_and_returns_canonical_durable_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = await UpdatePlanTool().execute(
                {
                    "explanation": "Finish the user-visible plan workflow",
                    "plan": [
                        {"step": "Inspect the current session", "status": "completed"},
                        {"step": "Implement the next slice", "status": "in_progress"},
                    ],
                },
                ToolContext(Path(directory)),
            )

            self.assertFalse(result.is_error)
            self.assertIn("Plan updated.", result.content)
            self.assertEqual(
                result.metadata,
                {
                    "plan": {
                        "explanation": "Finish the user-visible plan workflow",
                        "plan": [
                            {"step": "Inspect the current session", "status": "completed"},
                            {"step": "Implement the next slice", "status": "in_progress"},
                        ],
                    }
                },
            )
            self.assertIn("update_plan", default_tool_registry().names())

    def test_allowed_tool_names_can_construct_a_read_only_capability_set(self) -> None:
        registry = default_tool_registry(
            enable_background_tasks=True,
            allowed_tool_names=("read_file", "list_dir", "grep", "skill"),
        )

        self.assertEqual(registry.names(), ("read_file", "list_dir", "grep", "skill"))

    async def test_update_plan_without_a_purpose_omits_the_purpose_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = await UpdatePlanTool().execute(
                {
                    "explanation": None,
                    "plan": [{"step": "Inspect the stored plan", "status": "pending"}],
                },
                ToolContext(Path(directory)),
            )

        self.assertEqual(result.content, "Plan updated.\n1. [pending] Inspect the stored plan")

    async def test_update_plan_rejects_unknown_and_malformed_fields(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ToolError, "unsupported fields"),
        ):
            await UpdatePlanTool().execute(
                {
                    "explanation": None,
                    "plan": [{"step": "Inspect", "status": "pending", "extra": True}],
                },
                ToolContext(Path(directory)),
            )

    async def test_update_plan_redacts_configured_credentials_before_persisting_metadata(
        self,
    ) -> None:
        secret = "fixture-plan-secret"
        with tempfile.TemporaryDirectory() as directory:
            result = await UpdatePlanTool().execute(
                {
                    "explanation": f"Keep {secret} out of the durable plan",
                    "plan": [
                        {"step": f"Inspect {secret} safely", "status": "in_progress"},
                    ],
                },
                ToolContext(Path(directory), redaction_values=(secret,)),
            )

            self.assertNotIn(secret, result.content)
            assert result.metadata is not None
            self.assertNotIn(secret, str(result.metadata))
            self.assertIn("[REDACTED]", result.content)


class ClientTerminalToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_direct_command_and_preserves_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_terminal = ClientTerminalFixture()
            registry = default_tool_registry(client_terminal=client_terminal)
            self.assertTrue(
                {
                    "terminal_exec",
                    "terminal_start",
                    "terminal_output",
                    "terminal_wait",
                    "terminal_kill",
                }.issubset(registry.names())
            )
            tool = registry.get("terminal_exec")
            assert tool is not None

            result = await tool.execute(
                {"command": "git", "args": ["status", "--short"], "timeout_seconds": 3},
                ToolContext(root, client_terminal=client_terminal),
            )

        self.assertEqual(result.content, "terminal output")
        self.assertFalse(result.is_error)
        self.assertEqual(
            client_terminal.calls,
            [("git", ("status", "--short"), root, 200_000, 3.0)],
        )
        self.assertEqual(
            result.metadata,
            {
                "exit_code": 0,
                "signal": None,
                "truncated": False,
                "client_delegated": True,
            },
        )

    async def test_nonzero_signal_and_output_are_rendered_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_terminal = ClientTerminalFixture(
                ClientTerminalResult(
                    output="x" * 30,
                    exit_code=None,
                    signal="SIGTERM",
                    truncated=True,
                )
            )
            result = await ClientTerminalTool().execute(
                {"command": "tool"},
                ToolContext(root, output_byte_limit=12, client_terminal=client_terminal),
            )

        self.assertTrue(result.is_error)
        self.assertIn("[output truncated]", result.content)
        assert result.metadata is not None
        self.assertEqual(result.metadata["signal"], "SIGTERM")
        self.assertTrue(result.metadata["truncated"])

    async def test_manages_client_background_terminal_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_terminal = ClientTerminalFixture()
            context = ToolContext(root, client_terminal=client_terminal)
            registry = default_tool_registry(client_terminal=client_terminal)
            start = registry.get("terminal_start")
            output = registry.get("terminal_output")
            wait = registry.get("terminal_wait")
            kill = registry.get("terminal_kill")
            assert start is not None
            assert output is not None
            assert wait is not None
            assert kill is not None

            started = await start.execute(
                {"command": "git", "args": ["status", "--short"], "timeout_seconds": 5},
                context,
            )
            task_id = started.metadata["task_id"] if started.metadata is not None else None
            self.assertEqual(task_id, "terminal-task-1")
            self.assertEqual(
                client_terminal.starts,
                [("git", ("status", "--short"), root, 200_000, 5.0)],
            )

            inspected = await output.execute({"task_id": task_id}, context)
            self.assertIn("status: running", inspected.content)
            self.assertTrue(inspected.metadata and inspected.metadata["client_delegated"])

            waited = await wait.execute(
                {"task_ids": [task_id], "mode": "wait_all", "timeout_seconds": 1},
                context,
            )
            self.assertFalse(waited.is_error)
            self.assertTrue(waited.metadata and waited.metadata["client_delegated"])

            killed = await kill.execute({"task_id": task_id}, context)
            self.assertIn("outcome: killed", killed.content)
            self.assertTrue(killed.metadata and killed.metadata["client_delegated"])

            after_kill = await output.execute({"task_id": task_id}, context)
            self.assertIn("status: cancelled", after_kill.content)

    async def test_client_background_terminal_tools_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_terminal = ClientTerminalFixture()
            start = default_tool_registry(client_terminal=client_terminal).get("terminal_start")
            assert start is not None
            for arguments in (
                {},
                {"command": "git", "args": "status"},
                {"command": "git", "timeout_seconds": 0},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                    await start.execute(
                        arguments, ToolContext(root, client_terminal=client_terminal)
                    )

            with self.assertRaisesRegex(ToolError, "sandboxing is enabled"):
                await start.execute(
                    {"command": "git"},
                    ToolContext(
                        root,
                        sandbox_profile=SandboxProfile.WORKSPACE,
                        client_terminal=client_terminal,
                    ),
                )
            with self.assertRaisesRegex(ToolError, "unavailable"):
                await start.execute({"command": "git"}, ToolContext(root))

    async def test_is_hidden_for_sandbox_and_fails_closed_for_invalid_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_terminal = ClientTerminalFixture()
            sandboxed = default_tool_registry(
                SandboxProfile.WORKSPACE,
                client_terminal=client_terminal,
            )
            self.assertNotIn("terminal_exec", sandboxed.names())
            with self.assertRaisesRegex(ToolError, "sandboxing is enabled"):
                await ClientTerminalTool().execute(
                    {"command": "git"},
                    ToolContext(
                        root,
                        sandbox_profile=SandboxProfile.WORKSPACE,
                        client_terminal=client_terminal,
                    ),
                )
            with self.assertRaisesRegex(ToolError, "unavailable"):
                await ClientTerminalTool().execute({"command": "git"}, ToolContext(root))
            for arguments in (
                {},
                {"command": "bad\x00command"},
                {"command": "git", "args": "status"},
                {"command": "git", "args": [1]},
                {"command": "git", "timeout_seconds": True},
                {"command": "git", "timeout_seconds": 0},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                    await ClientTerminalTool().execute(
                        arguments,
                        ToolContext(root, client_terminal=client_terminal),
                    )

            malformed = ClientTerminalFixture(result=object())
            with self.assertRaisesRegex(ToolError, "invalid result"):
                await ClientTerminalTool().execute(
                    {"command": "git"},
                    ToolContext(root, client_terminal=malformed),
                )
            incomplete = ClientTerminalFixture(
                ClientTerminalResult(output="", exit_code=None, signal=None, truncated=False)
            )
            with self.assertRaisesRegex(ToolError, "invalid result"):
                await ClientTerminalTool().execute(
                    {"command": "git"},
                    ToolContext(root, client_terminal=incomplete),
                )

    async def test_explicit_additional_directory_is_accessible_but_not_an_escape_hatch(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as primary_directory,
            tempfile.TemporaryDirectory() as extra_directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(primary_directory)
            extra = Path(extra_directory)
            outside = Path(outside_directory)
            target = extra / "shared.txt"
            target.write_text("alpha\nbeta\n", encoding="utf-8")
            outside_target = outside / "private.txt"
            outside_target.write_text("private", encoding="utf-8")
            context = ToolContext(root, additional_workspace_roots=(extra,))

            read_result = await ReadFileTool().execute({"path": str(target)}, context)
            self.assertIn("2\tbeta", read_result.content)
            grep_result = await GrepTool().execute(
                {"query": "bet.", "path": str(extra)},
                context,
            )
            self.assertIn(f"{target.resolve()}:2:beta", grep_result.content)
            with self.assertRaisesRegex(ToolError, "escapes the workspace"):
                await ReadFileTool().execute({"path": str(outside_target)}, context)

    async def test_additional_directory_edits_follow_the_sandbox_policy(self) -> None:
        with (
            tempfile.TemporaryDirectory() as primary_directory,
            tempfile.TemporaryDirectory() as extra_directory,
        ):
            root = Path(primary_directory)
            extra = Path(extra_directory)
            target = extra / "shared.txt"
            target.write_text("before\n", encoding="utf-8")
            arguments = {"path": str(target), "old": "before", "new": "after"}

            result = await SearchReplaceTool().execute(
                arguments,
                ToolContext(root, additional_workspace_roots=(extra,)),
            )
            self.assertFalse(result.is_error)
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")

            with self.assertRaisesRegex(ToolError, "only read access"):
                await SearchReplaceTool().execute(
                    {"path": str(target), "old": "after", "new": "blocked"},
                    ToolContext(
                        root,
                        additional_workspace_roots=(extra,),
                        sandbox_profile=SandboxProfile.WORKSPACE,
                    ),
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")

    def test_workspace_path_rejects_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ToolError):
                resolve_workspace_path(root, "../outside.txt")

    def test_workspace_identity_rejects_different_directories(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.assertFalse(workspaces_match(first, second))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_workspace_identity_accepts_filesystem_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            alias = root / "workspace-alias"
            try:
                alias.symlink_to(workspace, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"cannot create symlink: {error}")
            self.assertTrue(workspaces_match(alias, workspace))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_workspace_path_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            link = root / "escape"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError as error:
                self.skipTest(f"cannot create symlink: {error}")
            with self.assertRaises(ToolError):
                resolve_workspace_path(root, "escape/secret.txt")


if __name__ == "__main__":
    unittest.main()
