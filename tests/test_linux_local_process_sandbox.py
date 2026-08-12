from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    SandboxedProcessRequest,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.terminal import TerminalSize
from neuro_code.infrastructure.sandbox.linux_local_process import (
    LinuxBubblewrapLocalProcessSandbox,
)
from neuro_code.shared.errors import SandboxError


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux child sandbox contract")
class LinuxBubblewrapLocalProcessSandboxTests(unittest.IsolatedAsyncioTestCase):
    def _adapter(
        self,
        profile: SandboxProfile,
        workspace: Path,
        state_dir: Path,
        *,
        terminal_platform: object | None = None,
    ) -> LinuxBubblewrapLocalProcessSandbox:
        with (
            mock.patch(
                "neuro_code.infrastructure.sandbox.linux_local_process._trusted_system_executable",
                return_value=Path("/usr/bin/bwrap"),
            ),
            mock.patch.object(LinuxBubblewrapLocalProcessSandbox, "_preflight"),
        ):
            return LinuxBubblewrapLocalProcessSandbox(
                profile,
                workspace,
                state_dir,
                terminal_platform=terminal_platform,
            )

    @staticmethod
    def _request(
        workspace: Path,
        profile: SandboxProfile,
        *,
        additional_root: Path | None = None,
        purpose: LocalProcessPurpose = LocalProcessPurpose.BASH,
        stdio_mode: LocalProcessStdioMode = LocalProcessStdioMode.CAPTURE,
        command: str = "printf child",
    ) -> SandboxedProcessRequest:
        access_mode = (
            LocalWorkspaceAccessMode.READ_ONLY
            if profile is SandboxProfile.READ_ONLY
            else LocalWorkspaceAccessMode.READ_WRITE
        )
        roots = [LocalWorkspaceAccess(workspace, access_mode)]
        if additional_root is not None:
            roots.append(LocalWorkspaceAccess(additional_root, access_mode))
        return SandboxedProcessRequest.shell(
            command,
            purpose=purpose,
            cwd=workspace,
            sandbox_profile=profile,
            filesystem_policy=LocalProcessFilesystemPolicy(tuple(roots)),
            network_policy=(
                LocalProcessNetworkPolicy.ISOLATED
                if profile.restricts_child_network
                else LocalProcessNetworkPolicy.INHERIT
            ),
            environment_policy=LocalProcessEnvironmentPolicy(
                {
                    "PATH": "/custom/bin:/usr/bin",
                    "LANG": "C.UTF-8",
                    "HTTPS_PROXY": "http://controller-secret.invalid",
                    "NEURO_CODE_HOME": "/controller/state",
                }
            ),
            stdio_mode=stdio_mode,
            lifecycle=LocalProcessLifecycle(),
        )

    async def test_workspace_child_plan_has_private_root_and_minimal_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)

            self.assertIs(
                adapter.lifecycle_capability,
                LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP,
            )
            plan = adapter.build_launch_argv(self._request(workspace, SandboxProfile.WORKSPACE))

            self.assertIn("--tmpfs", plan)
            self.assertIn("--clearenv", plan)
            self.assertIn("--die-with-parent", plan)
            self.assertIn("--new-session", plan)
            self.assertNotIn("--unshare-net", plan)
            self.assertNotIn(str(state_dir), plan)
            self.assertNotIn(
                ["--ro-bind", "/", "/"], [plan[index : index + 3] for index in range(len(plan))]
            )
            workspace_bind = [
                plan[index + 1 : index + 3]
                for index, item in enumerate(plan[:-2])
                if item == "--bind"
            ]
            self.assertIn([str(workspace), str(workspace)], workspace_bind)
            self.assertIn("/home/neuro-code", plan)
            self.assertIn("/tmp", plan)
            self.assertIn("PATH", plan)
            self.assertIn("/custom/bin:/usr/bin", plan)
            self.assertNotIn("HTTPS_PROXY", plan)
            self.assertNotIn("controller-secret.invalid", plan)
            self.assertNotIn("NEURO_CODE_HOME", plan)

    def test_runtime_mounts_include_resolved_controller_interpreter_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            interpreter = Path(sys.executable).resolve()
            expected_root = interpreter.parent.parent
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)

            if interpreter.parent.name == "bin" and expected_root != Path("/"):
                self.assertIn((expected_root, expected_root), adapter._runtime_mounts)

    async def test_read_only_and_strict_children_isolate_network_and_workspace_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            for profile in (SandboxProfile.READ_ONLY, SandboxProfile.STRICT):
                with self.subTest(profile=profile.value):
                    adapter = self._adapter(profile, workspace, state_dir)
                    plan = adapter.build_launch_argv(self._request(workspace, profile))
                    self.assertIn("--unshare-net", plan)
                    self.assertIn("--remount-ro", plan)
                    if profile is SandboxProfile.READ_ONLY:
                        workspace_mounts = [
                            plan[index + 1 : index + 3]
                            for index, item in enumerate(plan[:-2])
                            if item == "--ro-bind"
                        ]
                        self.assertIn([str(workspace), str(workspace)], workspace_mounts)

    async def test_additional_workspace_root_is_explicitly_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            additional = (root / "additional").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            additional.mkdir()
            state_dir.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)

            plan = adapter.build_launch_argv(
                self._request(
                    workspace,
                    SandboxProfile.WORKSPACE,
                    additional_root=additional,
                )
            )

            self.assertIn(str(additional), plan)
            self.assertNotIn(str(state_dir), plan)

    async def test_state_overlap_and_unsupported_transports_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            workspace.mkdir()
            state_in_workspace = workspace / "controller-state"
            state_in_workspace.mkdir()
            with self.assertRaisesRegex(SandboxError, "must not overlap controller state_dir"):
                self._adapter(SandboxProfile.WORKSPACE, workspace, state_in_workspace)

            state_dir = (root / "controller-state").resolve()
            state_dir.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)
            unsupported = self._request(
                workspace,
                SandboxProfile.WORKSPACE,
                purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                stdio_mode=LocalProcessStdioMode.PROTOCOL,
            )
            with self.assertRaisesRegex(
                SandboxError,
                "interactive terminal requests require PTY stdio",
            ):
                adapter.build_launch_argv(unsupported)

    def test_controller_state_hardlink_into_workspace_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            credential = state_dir / "credentials.json"
            credential.write_text("controller-secret", encoding="utf-8")
            alias = workspace / "borrowed-credentials.json"
            alias.hardlink_to(credential)
            self.assertEqual(credential.stat().st_ino, alias.stat().st_ino)
            self.assertGreater(credential.stat().st_nlink, 1)

            with self.assertRaisesRegex(SandboxError, "hardlink outside its trusted path"):
                self._adapter(SandboxProfile.WORKSPACE, workspace, state_dir)

    async def test_real_child_cannot_read_controller_state_when_bubblewrap_is_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            (state_dir / "credentials.json").write_text("controller-secret", encoding="utf-8")
            (state_dir / "providers.json").write_text("provider-secret", encoding="utf-8")
            (state_dir / "sessions.db").write_text("session-state", encoding="utf-8")
            try:
                adapter = LinuxBubblewrapLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    state_dir,
                )
            except SandboxError as error:
                self.skipTest(str(error))
            request = SandboxedProcessRequest.shell(
                (
                    f"test ! -e {state_dir / 'credentials.json'} && "
                    f"test ! -e {state_dir / 'providers.json'} && "
                    f"test ! -e {state_dir / 'sessions.db'} && "
                    'test "$HOME" = /home/neuro-code && '
                    'test "$TMPDIR" = /tmp && '
                    "printf isolated"
                ),
                purpose=LocalProcessPurpose.BASH,
                cwd=workspace,
                sandbox_profile=SandboxProfile.WORKSPACE,
                filesystem_policy=LocalProcessFilesystemPolicy(
                    (
                        LocalWorkspaceAccess(
                            workspace,
                            LocalWorkspaceAccessMode.READ_WRITE,
                        ),
                    ),
                ),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy(),
                stdio_mode=LocalProcessStdioMode.CAPTURE,
                lifecycle=LocalProcessLifecycle(),
            )
            process = await adapter.spawn(request)
            assert process.stdout is not None
            self.assertEqual((await process.stdout.read()).decode(), "isolated")
            self.assertEqual(await process.wait(), 0)

    async def test_real_child_enforces_workspace_write_mode_when_bubblewrap_is_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            try:
                adapter = LinuxBubblewrapLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    state_dir,
                )
            except SandboxError as error:
                self.skipTest(str(error))
            marker = workspace / "workspace-write.txt"
            process = await adapter.spawn(
                self._request(
                    workspace,
                    SandboxProfile.WORKSPACE,
                    command=f"printf workspace > {marker.name}",
                )
            )
            self.assertEqual(await process.wait(), 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "workspace")

    async def test_protocol_child_uses_the_same_child_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            try:
                adapter = LinuxBubblewrapLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    state_dir,
                )
            except SandboxError as error:
                self.skipTest(str(error))
            request = SandboxedProcessRequest.exec(
                "/usr/bin/printf",
                ("protocol-child",),
                purpose=LocalProcessPurpose.MCP_STDIO,
                cwd=workspace,
                sandbox_profile=SandboxProfile.WORKSPACE,
                filesystem_policy=LocalProcessFilesystemPolicy(
                    (
                        LocalWorkspaceAccess(
                            workspace,
                            LocalWorkspaceAccessMode.READ_WRITE,
                        ),
                    ),
                ),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy(),
                stdio_mode=LocalProcessStdioMode.PROTOCOL,
                lifecycle=LocalProcessLifecycle(),
            )
            process = await adapter.spawn(request)
            assert process.stdout is not None
            self.assertEqual((await process.stdout.read()).decode(), "protocol-child")
            self.assertEqual(await process.wait(), 0)

    def test_pty_launch_uses_bubblewrap_argv_through_terminal_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            platform = mock.Mock()
            platform.lifecycle_capability = (
                LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
            )
            platform.spawn_exec.return_value = mock.Mock(
                lifecycle_capability=LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
            )
            adapter = self._adapter(
                SandboxProfile.WORKSPACE,
                workspace,
                state_dir,
                terminal_platform=platform,
            )
            request = SandboxedProcessRequest.exec(
                "/usr/bin/python3",
                ("-c", "pass"),
                purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                cwd=workspace,
                sandbox_profile=SandboxProfile.WORKSPACE,
                filesystem_policy=LocalProcessFilesystemPolicy(
                    (LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),)
                ),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy({"TERM": "xterm-256color"}),
                stdio_mode=LocalProcessStdioMode.PTY,
                lifecycle=LocalProcessLifecycle(),
            )

            result = adapter.spawn_terminal(
                request,
                size=TerminalSize(100, 30),
                on_output=lambda data: None,
                on_eof=lambda: None,
                on_error=lambda error: None,
            )

            self.assertIs(result, platform.spawn_exec.return_value)
            platform.spawn_exec.assert_called_once()
            call = platform.spawn_exec.call_args
            self.assertEqual(call.args[0], "/usr/bin/bwrap")
            self.assertIn("--die-with-parent", call.args[1])
            self.assertIn("--clearenv", call.args[1])
            self.assertEqual(call.kwargs["env"], {})
            self.assertEqual(call.kwargs["cwd"], workspace)
            self.assertEqual(call.kwargs["size"], TerminalSize(100, 30))

    def test_pty_launch_audits_all_requested_workspace_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            additional = (root / "additional").resolve()
            outside = (root / "outside").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            additional.mkdir()
            outside.mkdir()
            state_dir.mkdir()
            source = outside / "private.txt"
            source.write_text("private", encoding="utf-8")
            platform = mock.Mock()
            adapter = self._adapter(
                SandboxProfile.WORKSPACE,
                workspace,
                state_dir,
                terminal_platform=platform,
            )
            alias = workspace / "private-alias.txt"
            alias.hardlink_to(source)
            request = SandboxedProcessRequest.exec(
                "/usr/bin/python3",
                ("-c", "pass"),
                purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                cwd=workspace,
                sandbox_profile=SandboxProfile.WORKSPACE,
                filesystem_policy=LocalProcessFilesystemPolicy(
                    (
                        LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),
                        LocalWorkspaceAccess(additional, LocalWorkspaceAccessMode.READ_WRITE),
                    )
                ),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy(),
                stdio_mode=LocalProcessStdioMode.PTY,
                lifecycle=LocalProcessLifecycle(),
            )

            with self.assertRaisesRegex(SandboxError, "outside the authorized roots"):
                adapter.spawn_terminal(
                    request,
                    size=TerminalSize(100, 30),
                    on_output=lambda data: None,
                    on_eof=lambda: None,
                    on_error=lambda error: None,
                )
            platform.spawn_exec.assert_not_called()

    async def test_real_read_only_child_cannot_write_when_network_namespace_is_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            state_dir = (root / "controller-state").resolve()
            workspace.mkdir()
            state_dir.mkdir()
            try:
                adapter = LinuxBubblewrapLocalProcessSandbox(
                    SandboxProfile.READ_ONLY,
                    workspace,
                    state_dir,
                )
            except SandboxError as error:
                self.skipTest(str(error))
            denied = workspace / "read-only-write.txt"
            process = await adapter.spawn(
                self._request(
                    workspace,
                    SandboxProfile.READ_ONLY,
                    command=f"printf blocked > {denied.name}",
                )
            )
            self.assertNotEqual(await process.wait(), 0)
            self.assertFalse(denied.exists())


if __name__ == "__main__":
    unittest.main()
