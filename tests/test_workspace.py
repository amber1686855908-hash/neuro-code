from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.ports.workspace import WorkspaceIdentity, WorkspacePathResolver
from neuro_code.shared.errors import ToolError
from neuro_code.workspace import (
    FilesystemWorkspaceIdentity,
    FilesystemWorkspacePathResolver,
    workspaces_match,
)


def _as_workspace_identity(identity: WorkspaceIdentity) -> WorkspaceIdentity:
    return identity


def _as_workspace_path_resolver(
    resolver: WorkspacePathResolver,
) -> WorkspacePathResolver:
    return resolver


class FilesystemWorkspaceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = _as_workspace_identity(FilesystemWorkspaceIdentity())

    def test_matches_the_same_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self.assertTrue(self.identity.matches(workspace, workspace))
            self.assertEqual(
                self.identity.matches(workspace, workspace),
                workspaces_match(workspace, workspace),
            )

    def test_matches_relative_and_absolute_aliases(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            workspace = Path(directory)
            relative = workspace.relative_to(Path.cwd())
            self.assertTrue(self.identity.matches(relative, workspace))
            self.assertEqual(
                self.identity.matches(relative, workspace),
                workspaces_match(relative, workspace),
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_matches_symbolic_link_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            alias = root / "workspace-alias"
            try:
                alias.symlink_to(workspace, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"cannot create symlink: {error}")
            self.assertTrue(self.identity.matches(alias, workspace))
            self.assertEqual(
                self.identity.matches(alias, workspace),
                workspaces_match(alias, workspace),
            )

    def test_matches_identical_nonexistent_paths_and_rejects_different_ones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            other = root / "other"
            self.assertTrue(self.identity.matches(missing, missing))
            self.assertFalse(self.identity.matches(missing, other))
            self.assertEqual(
                self.identity.matches(missing, other),
                workspaces_match(missing, other),
            )

    def test_samefile_failure_uses_the_existing_normalized_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with patch.object(Path, "samefile", side_effect=OSError("unavailable")):
                self.assertTrue(self.identity.matches(workspace, workspace))

    def test_resolve_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with (
                patch.object(Path, "samefile", side_effect=OSError("unavailable")),
                patch.object(Path, "resolve", side_effect=OSError("unavailable")),
            ):
                self.assertFalse(self.identity.matches(workspace, workspace))

    def test_uses_platform_normcase_for_normalized_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "Mixed-Case"
            requested = root / "mixed-case"
            with (
                patch.object(Path, "samefile", side_effect=OSError("unavailable")),
                patch(
                    "neuro_code.workspace.os.path.normcase",
                    side_effect=lambda value: value.casefold(),
                ) as normcase,
            ):
                self.assertTrue(self.identity.matches(recorded, requested))
            self.assertEqual(normcase.call_count, 2)


class FilesystemWorkspacePathResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = _as_workspace_path_resolver(FilesystemWorkspacePathResolver())

    def test_resolves_existing_relative_and_absolute_workspace_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child"
            child.mkdir()
            self.assertEqual(self.resolver.resolve_existing(root, "."), root.resolve())
            self.assertEqual(self.resolver.resolve_existing(root, "child"), child.resolve())
            self.assertEqual(
                self.resolver.resolve_existing(root, str(child)),
                child.resolve(),
            )

    def test_preserves_empty_null_and_none_runtime_input_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for requested in ("", "bad\x00path", None):
                with (
                    self.subTest(requested=requested),
                    self.assertRaisesRegex(
                        ToolError,
                        "path must be a non-empty filesystem path",
                    ),
                ):
                    self.resolver.resolve_existing(root, requested)

    def test_rejects_relative_and_absolute_workspace_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / f"{root.name}-outside"
            outside.mkdir()
            try:
                with self.assertRaisesRegex(ToolError, "path escapes the workspace: '\\.\\.'"):
                    self.resolver.resolve_existing(root, "..")
                with self.assertRaisesRegex(ToolError, "path escapes the workspace"):
                    self.resolver.resolve_existing(root, str(outside))
            finally:
                outside.rmdir()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_rejects_symbolic_link_escapes_and_uses_workspace_link_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            outside = root / "outside"
            outside.mkdir()
            workspace_link = root / "workspace-link"
            escape_link = target / "escape"
            try:
                workspace_link.symlink_to(target, target_is_directory=True)
                escape_link.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"cannot create symlink: {error}")
            self.assertEqual(
                self.resolver.resolve_existing(workspace_link, "."),
                target.resolve(),
            )
            with self.assertRaisesRegex(ToolError, "path escapes the workspace"):
                self.resolver.resolve_existing(workspace_link, "../outside")
            with self.assertRaisesRegex(ToolError, "path escapes the workspace"):
                self.resolver.resolve_existing(target, "escape")

    def test_rejects_nonexistent_paths_and_keeps_runtime_errors_unwrapped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ToolError, "cannot resolve path 'missing'"):
                self.resolver.resolve_existing(root, "missing")
            with (
                patch.object(Path, "resolve", side_effect=RuntimeError("resolver failure")),
                self.assertRaisesRegex(RuntimeError, "resolver failure"),
            ):
                self.resolver.resolve_existing(root, ".")

    def test_returns_existing_files_for_the_terminal_to_validate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "file.txt"
            file_path.write_text("fixture", encoding="utf-8")
            self.assertEqual(
                self.resolver.resolve_existing(root, "file.txt"),
                file_path.resolve(),
            )
