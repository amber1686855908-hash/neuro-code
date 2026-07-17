from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from neuro_code.errors import ToolError
from neuro_code.ports.tools import ToolContext
from neuro_code.tools.filesystem import GrepTool, ReadFileTool, SearchReplaceTool
from neuro_code.workspace import resolve_workspace_path, workspaces_match


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
