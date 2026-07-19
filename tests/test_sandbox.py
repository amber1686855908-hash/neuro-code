from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neuro_code.adapters.sandbox import LinuxBubblewrapSandbox
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.errors import SandboxError


class SandboxProfileTests(unittest.TestCase):
    def test_profiles_parse_canonical_names_and_safe_aliases(self) -> None:
        self.assertIs(SandboxProfile.parse("workspace"), SandboxProfile.WORKSPACE)
        self.assertIs(SandboxProfile.parse("readonly"), SandboxProfile.READ_ONLY)
        self.assertIs(SandboxProfile.parse("none"), SandboxProfile.OFF)
        with self.assertRaisesRegex(ValueError, "unsupported sandbox profile"):
            SandboxProfile.parse("permissive")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux adapter contract")
class LinuxBubblewrapSandboxTests(unittest.TestCase):
    @staticmethod
    def _adapter(
        profile: SandboxProfile,
        workspace: Path,
        state_dir: Path,
    ) -> LinuxBubblewrapSandbox:
        executable = Path("/usr/bin/true")
        with mock.patch(
            "neuro_code.adapters.sandbox._trusted_system_executable",
            return_value=executable,
        ):
            return LinuxBubblewrapSandbox(profile, workspace, state_dir)

    def test_workspace_plan_uses_read_only_root_and_writable_scoped_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            state_dir.mkdir()
            sandbox = self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)

            plan = sandbox.build_launch_argv(("python", "-m", "neuro_code"))

            self.assertIn("--ro-bind", plan)
            self.assertIn(str(workspace), plan)
            self.assertIn(str(state_dir), plan)
            self.assertNotIn("--unshare-net", plan)
            self.assertEqual(plan[-3:], ["python", "-m", "neuro_code"])

    def test_read_only_plan_does_not_add_a_writable_workspace_mount(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            state_dir.mkdir()
            sandbox = self._adapter(SandboxProfile.READ_ONLY, workspace, state_dir)

            plan = sandbox.build_launch_argv(("/bin/true",))
            writable_mounts = [
                plan[index + 1] for index, item in enumerate(plan[:-1]) if item == "--bind"
            ]

            self.assertNotIn(str(workspace), writable_mounts)
            self.assertIn(str(state_dir), writable_mounts)
            with mock.patch.object(sandbox, "verify_current_process"):
                launch = sandbox.shell_launch("echo isolated")
            self.assertEqual(launch.arguments[:4], ("--net", "--map-root-user", "--", "/bin/sh"))
            with mock.patch.object(sandbox, "verify_current_process"):
                exec_launch = sandbox.exec_launch("python", ("-c", "pass"))
            self.assertEqual(
                exec_launch.arguments,
                ("--net", "--map-root-user", "--", "python", "-c", "pass"),
            )

    def test_strict_plan_uses_an_allowlist_root_and_remounts_it_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            state_dir.mkdir()
            sandbox = self._adapter(SandboxProfile.STRICT, workspace, state_dir)

            plan = sandbox.build_launch_argv(("/bin/true",))

            self.assertIn("--tmpfs", plan)
            self.assertIn("--remount-ro", plan)
            self.assertNotEqual(plan[plan.index("--tmpfs") + 1], str(workspace))
            self.assertIn(str(workspace), plan)

    def test_active_marker_is_not_trusted_without_mount_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            state_dir.mkdir()
            sandbox = self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)

            with (
                mock.patch.dict(
                    os.environ,
                    {"NEURO_CODE_SANDBOX_ACTIVE": "workspace"},
                    clear=False,
                ),
                mock.patch.object(sandbox, "_is_read_only", return_value=False),
                self.assertRaisesRegex(SandboxError, "filesystem root is writable"),
            ):
                sandbox.verify_current_process()

    def test_profile_mismatch_and_unsafe_read_only_state_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            sandbox = self._adapter(SandboxProfile.WORKSPACE, workspace, root / "state")
            with (
                mock.patch.dict(
                    os.environ,
                    {"NEURO_CODE_SANDBOX_ACTIVE": "strict"},
                    clear=False,
                ),
                self.assertRaisesRegex(SandboxError, "is not active"),
            ):
                sandbox.verify_current_process()

            with (
                mock.patch(
                    "neuro_code.adapters.sandbox._trusted_system_executable",
                    return_value=Path("/usr/bin/true"),
                ),
                self.assertRaisesRegex(SandboxError, "state_dir cannot contain"),
            ):
                LinuxBubblewrapSandbox(SandboxProfile.READ_ONLY, workspace, root)

    def test_exec_failure_is_an_error_instead_of_an_unsandboxed_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state_dir = root / "state"
            workspace.mkdir()
            state_dir.mkdir()
            sandbox = self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(sandbox, "_preflight_bubblewrap"),
                mock.patch.object(sandbox, "_preflight_child_network"),
                mock.patch("neuro_code.adapters.sandbox.os.execv", side_effect=OSError("denied")),
                self.assertRaisesRegex(SandboxError, "could not enter"),
            ):
                sandbox.enforce_current_process((sys.executable, "-m", "neuro_code"))

    def test_real_built_in_profiles_when_kernel_support_is_available(self) -> None:
        try:
            temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        except OSError as error:
            self.skipTest(f"cannot create an integration workspace: {error}")
        with temporary as directory:
            root = Path(directory)
            for profile in (
                SandboxProfile.WORKSPACE,
                SandboxProfile.READ_ONLY,
                SandboxProfile.STRICT,
            ):
                with self.subTest(profile=profile.value):
                    workspace = root / profile.value
                    state_dir = workspace / ".state"
                    state_dir.mkdir(parents=True)
                    try:
                        sandbox = LinuxBubblewrapSandbox(profile, workspace, state_dir)
                    except SandboxError as error:
                        self.skipTest(str(error))
                    network_probe = """
import socket
try:
    stream = socket.socket()
    stream.settimeout(0.2)
    outcome = stream.connect_ex(("1.1.1.1", 53))
except OSError:
    outcome = 1
raise SystemExit(0 if outcome else 7)
"""
                    code = f"""
import os
import subprocess
import sys
from pathlib import Path

workspace = Path({str(workspace)!r})
assert os.environ["NEURO_CODE_SANDBOX_ACTIVE"] == {profile.value!r}
assert os.statvfs("/").f_flag & os.ST_RDONLY
assert bool(os.statvfs(workspace).f_flag & os.ST_RDONLY) is {not profile.workspace_writable!r}
probe = workspace / "sandbox-probe.txt"
try:
    probe.write_text("ok")
    wrote = True
except OSError:
    wrote = False
assert wrote is {profile.workspace_writable!r}
if {profile.restricts_child_network!r}:
    isolated = subprocess.run(
        ["/usr/bin/unshare", "--net", "--map-root-user", "--", sys.executable, "-c", {network_probe!r}],
        check=False,
    )
    assert isolated.returncode == 0
"""
                    plan = sandbox.build_launch_argv((sys.executable, "-c", code))
                    completed = subprocess.run(
                        plan,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=20,
                    )
                    if completed.returncode != 0 and "Operation not permitted" in completed.stderr:
                        self.skipTest(completed.stderr.strip())
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(
                        (workspace / "sandbox-probe.txt").exists(),
                        profile.workspace_writable,
                    )
                    if profile is SandboxProfile.STRICT:
                        module_launch = sandbox.build_launch_argv(
                            (sys.executable, "-m", "neuro_code", "version", "--json")
                        )
                        module_result = subprocess.run(
                            module_launch,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=20,
                        )
                        self.assertEqual(module_result.returncode, 0, module_result.stderr)
                        self.assertIn('"name": "neuro-code"', module_result.stdout)


if __name__ == "__main__":
    unittest.main()
