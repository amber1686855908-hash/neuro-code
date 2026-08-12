from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.infrastructure.sandbox.sandbox import _trusted_system_executable, _within
from neuro_code.shared.errors import SandboxError


class SandboxProfileTests(unittest.TestCase):
    def test_profiles_parse_canonical_names_and_safe_aliases(self) -> None:
        self.assertIs(SandboxProfile.parse("workspace"), SandboxProfile.WORKSPACE)
        self.assertIs(SandboxProfile.parse("readonly"), SandboxProfile.READ_ONLY)
        self.assertIs(SandboxProfile.parse("none"), SandboxProfile.OFF)
        with self.assertRaisesRegex(ValueError, "unsupported sandbox profile"):
            SandboxProfile.parse("permissive")

    def test_profile_flags_keep_child_boundary_semantics(self) -> None:
        self.assertFalse(SandboxProfile.OFF.enabled)
        self.assertTrue(SandboxProfile.WORKSPACE.workspace_writable)
        self.assertFalse(SandboxProfile.READ_ONLY.workspace_writable)
        self.assertTrue(SandboxProfile.READ_ONLY.restricts_child_network)
        self.assertTrue(SandboxProfile.STRICT.restricts_child_network)


class SandboxHelperTests(unittest.TestCase):
    def test_within_accepts_the_parent_and_descendants_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "workspace"
            child = parent / "src"
            sibling = root / "workspace-other"
            parent.mkdir()
            child.mkdir()
            sibling.mkdir()
            self.assertTrue(_within(parent, parent))
            self.assertTrue(_within(child, parent))
            self.assertFalse(_within(sibling, parent))

    def test_trusted_executable_rejects_missing_or_unusable_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.sandbox.shutil.which",
                    return_value=None,
                ),
                self.assertRaisesRegex(SandboxError, "requires the 'bwrap'"),
            ):
                _trusted_system_executable("bwrap", workspace)

            helper = workspace / "bwrap"
            helper.write_text("not trusted", encoding="utf-8")
            helper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.sandbox.shutil.which",
                    return_value=str(helper),
                ),
                self.assertRaises(SandboxError) as error,
            ):
                _trusted_system_executable("bwrap", workspace)
            self.assertRegex(str(error.exception), "workspace-controlled|caller-writable")

    @unittest.skipUnless(os.name == "posix", "POSIX ownership validation only")
    def test_trusted_executable_rejects_caller_writable_system_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            helper = root / "bwrap"
            helper.write_text("trusted only when immutable", encoding="utf-8")
            helper.chmod(stat.S_IRUSR | stat.S_IXUSR)
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.sandbox.shutil.which",
                    return_value=str(helper),
                ),
                mock.patch(
                    "neuro_code.infrastructure.sandbox.sandbox.os.geteuid",
                    return_value=os.geteuid() + 1,
                ),
                self.assertRaisesRegex(SandboxError, "caller-writable"),
            ):
                _trusted_system_executable("bwrap", workspace)


if __name__ == "__main__":
    unittest.main()
