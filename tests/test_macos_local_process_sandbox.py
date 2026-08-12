from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
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
from neuro_code.bootstrap.composition import _default_local_process_sandbox_factory
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.terminal import TerminalSize
from neuro_code.infrastructure.sandbox.macos_local_process import (
    MacOSSeatbeltLocalProcessSandbox,
    _MacOSSeatbeltPolicyBuilder,
)
from neuro_code.infrastructure.sandbox.process_tree import ProcessTree
from neuro_code.shared.errors import SandboxError


class _FakePrivateDirectories:
    instances: ClassVar[list[_FakePrivateDirectories]] = []

    def __init__(self, workspace: Path, state_dir: Path) -> None:
        del state_dir
        self.root = workspace.parent / "private child"
        self.home = self.root / "home"
        self.temporary = self.root / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.temporary.mkdir(parents=True, exist_ok=True)
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 1234
    stdout = None
    stderr = None
    returncode = None


class _FakeTree:
    process = _FakeProcess()

    async def wait(self) -> int:
        self.process.returncode = 0
        return 0

    async def terminate(self, *, grace_seconds: float, force_wait_seconds: float) -> None:
        del grace_seconds, force_wait_seconds
        self.process.returncode = -15


class _FakeTerminalSession:
    process_id = 4321
    lifecycle_capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    def write(self, data: bytes) -> None:
        del data

    def resize(self, size: TerminalSize) -> None:
        del size

    def send_signal(self, signal: object) -> None:
        del signal

    def poll_exit(self) -> int | None:
        return None

    def close(self) -> None:
        return None


class _FakeTerminalPlatform:
    lifecycle_capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def spawn_exec(self, executable: str, arguments: tuple[str, ...], **kwargs: object) -> object:
        self.calls.append({"executable": executable, "arguments": arguments, **kwargs})
        return _FakeTerminalSession()


class MacOSSeatbeltLocalProcessSandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakePrivateDirectories.instances.clear()

    @staticmethod
    def _adapter(
        profile: SandboxProfile,
        workspace: Path,
        state_dir: Path,
        *,
        terminal_platform: object | None = None,
    ) -> MacOSSeatbeltLocalProcessSandbox:
        with (
            mock.patch(
                "neuro_code.infrastructure.sandbox.macos_local_process._runtime_platform",
                return_value="darwin",
            ),
            mock.patch(
                "neuro_code.infrastructure.sandbox.macos_local_process._trusted_fixed_executable",
                side_effect=lambda path: path.as_posix(),
            ),
        ):
            return MacOSSeatbeltLocalProcessSandbox(
                profile,
                workspace,
                state_dir,
                terminal_platform=terminal_platform,  # type: ignore[arg-type]
            )

    @staticmethod
    def _request(
        workspace: Path,
        profile: SandboxProfile,
        *,
        purpose: LocalProcessPurpose = LocalProcessPurpose.BASH,
        stdio: LocalProcessStdioMode = LocalProcessStdioMode.CAPTURE,
        executable: bool = False,
        extra_root: LocalWorkspaceAccess | None = None,
    ) -> SandboxedProcessRequest:
        mode = (
            LocalWorkspaceAccessMode.READ_ONLY
            if profile is SandboxProfile.READ_ONLY
            else LocalWorkspaceAccessMode.READ_WRITE
        )
        roots = [LocalWorkspaceAccess(workspace, mode)]
        if extra_root is not None:
            roots.append(extra_root)
        values = {
            "PATH": "/custom/bin:/usr/bin",
            "LANG": "C",
            "LC_MESSAGES": "C",
            "TERM": "xterm-256color",
            "CONTROLLER_SECRET": "must-not-leak",
            "MCP_TOKEN": "explicit-value",
        }
        environment = LocalProcessEnvironmentPolicy(
            values,
            explicitly_authorized_names=frozenset({"MCP_TOKEN"}),
        )
        common = {
            "purpose": purpose,
            "cwd": workspace,
            "sandbox_profile": profile,
            "filesystem_policy": LocalProcessFilesystemPolicy(tuple(roots)),
            "network_policy": (
                LocalProcessNetworkPolicy.ISOLATED
                if profile.restricts_child_network
                else LocalProcessNetworkPolicy.INHERIT
            ),
            "environment_policy": environment,
            "stdio_mode": stdio,
            "lifecycle": LocalProcessLifecycle(),
        }
        if executable:
            return SandboxedProcessRequest.exec(sys.executable, ("-c", "print('ok')"), **common)
        return SandboxedProcessRequest.shell("printf ok", **common)

    def test_constructor_and_lifecycle_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state)
            self.assertIs(
                adapter.lifecycle_capability,
                LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT,
            )
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process._runtime_platform",
                    return_value="linux",
                ),
                self.assertRaisesRegex(SandboxError, "not enforceable"),
            ):
                MacOSSeatbeltLocalProcessSandbox(SandboxProfile.WORKSPACE, workspace, state)
            with self.assertRaisesRegex(ValueError, "enabled profile"):
                self._adapter(SandboxProfile.OFF, workspace, state)
            with self.assertRaisesRegex(SandboxError, "must not overlap"):
                self._adapter(SandboxProfile.WORKSPACE, workspace, workspace)

            synthetic_home = workspace / "host-home"
            synthetic_home.mkdir()
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process.Path.home",
                    return_value=synthetic_home,
                ),
                self.assertRaisesRegex(SandboxError, "host HOME root"),
            ):
                self._adapter(SandboxProfile.WORKSPACE, workspace, state)

    def test_composition_selects_macos_and_unsupported_platforms_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            sentinel = object()
            with (
                mock.patch(
                    "neuro_code.bootstrap.composition._runtime_platform", return_value="darwin"
                ),
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process."
                    "MacOSSeatbeltLocalProcessSandbox",
                    return_value=sentinel,
                ) as create,
            ):
                result = _default_local_process_sandbox_factory(
                    SandboxProfile.WORKSPACE, workspace, state
                )
            self.assertIs(result, sentinel)
            create.assert_called_once_with(SandboxProfile.WORKSPACE, workspace, state)

            with (
                mock.patch(
                    "neuro_code.bootstrap.composition._runtime_platform", return_value="freebsd14"
                ),
                self.assertRaisesRegex(SandboxError, "not enforceable"),
            ):
                _default_local_process_sandbox_factory(SandboxProfile.STRICT, workspace, state)

    def test_policy_is_deny_default_escapes_paths_and_preserves_profile_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            private_home = root / "private home"
            private_tmp = root / "private tmp"
            for path in (workspace, state, private_home, private_tmp):
                path.mkdir()
            for profile in (
                SandboxProfile.WORKSPACE,
                SandboxProfile.READ_ONLY,
                SandboxProfile.STRICT,
            ):
                request = self._request(workspace, profile)
                adapter = self._adapter(profile, workspace, state)
                policy = adapter.build_policy(
                    request,
                    private_home=private_home,
                    private_temporary_directory=private_tmp,
                )
                self.assertIn("(deny default)", policy)
                self.assertIn(str(workspace.resolve()).replace("\\", "\\\\"), policy)
                for forbidden in (
                    '(allow file-read* (subpath "/"))',
                    '(allow file-read-metadata (subpath "/"))',
                    '(allow file-write* (subpath "/"))',
                ):
                    self.assertNotIn(forbidden, policy)
                self.assertIn('(allow file-read-metadata (literal "/"))', policy)
                self.assertIn(
                    _MacOSSeatbeltPolicyBuilder._literal_rule(
                        "file-read-metadata", workspace.parent.resolve()
                    ),
                    policy,
                )
                self.assertNotIn(
                    _MacOSSeatbeltPolicyBuilder._subpath_rule(
                        "file-read-metadata", workspace.parent.resolve()
                    ),
                    policy,
                )
                self.assertNotIn(str(state), policy)
                self.assertEqual(
                    "(allow network-outbound)" in policy, profile is SandboxProfile.WORKSPACE
                )
                workspace_write = _MacOSSeatbeltPolicyBuilder._subpath_rule(
                    "file-write*", workspace.resolve()
                )
                self.assertEqual(workspace_write in policy, profile is not SandboxProfile.READ_ONLY)
            escaped_rule = _MacOSSeatbeltPolicyBuilder._subpath_rule(
                "file-read*", Path('workspace Ω "quoted" \\ slash')
            )
            self.assertIn('workspace Ω \\"quoted\\" \\\\ slash', escaped_rule)
            escaped_literal = _MacOSSeatbeltPolicyBuilder._literal_rule(
                "file-read-metadata", Path('ancestor Ω "quoted" \\ slash')
            )
            self.assertIn('ancestor Ω \\"quoted\\" \\\\ slash', escaped_literal)

    def test_environment_is_allowlisted_and_pty_terminal_values_are_scoped(self) -> None:
        policy = self._request(Path("/tmp").resolve(), SandboxProfile.WORKSPACE).environment_policy
        home = Path("/private/tmp/private-home")
        temporary = Path("/private/tmp/private-tmp")
        pipe_environment = MacOSSeatbeltLocalProcessSandbox._child_environment(
            policy, private_home=home, private_temporary_directory=temporary, pty=False
        )
        pty_environment = MacOSSeatbeltLocalProcessSandbox._child_environment(
            policy, private_home=home, private_temporary_directory=temporary, pty=True
        )
        self.assertNotIn("CONTROLLER_SECRET", pipe_environment)
        self.assertNotIn("TERM", pipe_environment)
        self.assertEqual(pty_environment["TERM"], "xterm-256color")
        self.assertEqual(pty_environment["MCP_TOKEN"], "explicit-value")
        self.assertEqual(pipe_environment["HOME"], str(home))
        self.assertEqual(pipe_environment["TMPDIR"], str(temporary))

    def test_runtime_roots_preserve_lexical_interpreter_symlink_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            cellar = root / "homebrew" / "Cellar" / "python@3.14" / "3.14"
            opt = root / "homebrew" / "opt" / "python@3.14"
            virtual_environment = root / "venv"
            for path in (workspace, state, cellar / "bin", opt.parent, virtual_environment / "bin"):
                path.mkdir(parents=True, exist_ok=True)
            canonical_interpreter = cellar / "bin" / "python3.14"
            canonical_interpreter.touch()
            opt.symlink_to(cellar, target_is_directory=True)
            lexical_interpreter = opt / "bin" / "python3.14"
            virtual_interpreter = virtual_environment / "bin" / "python"
            virtual_interpreter.symlink_to(lexical_interpreter)

            with (
                mock.patch.object(sys, "executable", str(virtual_interpreter)),
                mock.patch.object(sys, "prefix", str(virtual_environment)),
                mock.patch.object(sys, "base_prefix", str(opt)),
            ):
                adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state)

            self.assertIn(virtual_environment, adapter._runtime_read_roots)
            self.assertIn(opt, adapter._runtime_read_roots)
            self.assertIn(cellar.resolve(), adapter._runtime_read_roots)
            private_home = root / "private-home"
            private_tmp = root / "private-tmp"
            private_home.mkdir()
            private_tmp.mkdir()
            policy = adapter.build_policy(
                self._request(workspace, SandboxProfile.WORKSPACE),
                private_home=private_home,
                private_temporary_directory=private_tmp,
            )
            self.assertIn(_MacOSSeatbeltPolicyBuilder._subpath_rule("file-read*", opt), policy)
            self.assertIn(_MacOSSeatbeltPolicyBuilder._subpath_rule("file-read*", cellar), policy)

    async def test_pipe_spawn_uses_sandbox_exec_and_cleans_private_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state)
            request = self._request(workspace, SandboxProfile.WORKSPACE)
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process._PrivateChildDirectories",
                    _FakePrivateDirectories,
                ),
                mock.patch.object(
                    ProcessTree, "spawn_exec", new_callable=mock.AsyncMock, return_value=_FakeTree()
                ) as spawn,
            ):
                process = await adapter.spawn(request)
                await process.wait()
            call = spawn.await_args
            self.assertEqual(call.args[0], "/usr/bin/sandbox-exec")
            self.assertEqual(call.args[1][0], "-p")
            self.assertEqual(call.args[1][-3:-1], ("/bin/sh", "-c"))
            self.assertNotIn("CONTROLLER_SECRET", call.kwargs["env"])
            self.assertTrue(_FakePrivateDirectories.instances[-1].closed)

    async def test_background_and_mcp_transports_share_the_outer_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state)
            requests = (
                self._request(
                    workspace,
                    SandboxProfile.WORKSPACE,
                    purpose=LocalProcessPurpose.BACKGROUND_BASH,
                    stdio=LocalProcessStdioMode.MERGED_CAPTURE,
                ),
                self._request(
                    workspace,
                    SandboxProfile.WORKSPACE,
                    purpose=LocalProcessPurpose.MCP_STDIO,
                    stdio=LocalProcessStdioMode.PROTOCOL,
                    executable=True,
                ),
            )
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process._PrivateChildDirectories",
                    _FakePrivateDirectories,
                ),
                mock.patch.object(
                    ProcessTree, "spawn_exec", new_callable=mock.AsyncMock, return_value=_FakeTree()
                ) as spawn,
            ):
                for request in requests:
                    process = await adapter.spawn(request)
                    await process.wait()
            self.assertEqual(spawn.await_count, 2)
            background, protocol = spawn.await_args_list
            self.assertEqual(background.args[0], "/usr/bin/sandbox-exec")
            self.assertTrue(background.kwargs["merge_output"])
            self.assertEqual(protocol.args[0], "/usr/bin/sandbox-exec")
            self.assertTrue(protocol.kwargs["pipe_stdin"])
            self.assertIn(sys.executable, protocol.args[1])

    async def test_spawn_failure_and_terminate_cleanup_private_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state)
            request = self._request(workspace, SandboxProfile.WORKSPACE)
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process._PrivateChildDirectories",
                    _FakePrivateDirectories,
                ),
                mock.patch.object(
                    ProcessTree,
                    "spawn_exec",
                    new_callable=mock.AsyncMock,
                    side_effect=OSError("fixture spawn failure"),
                ),
                self.assertRaisesRegex(OSError, "fixture spawn failure"),
            ):
                await adapter.spawn(request)
            self.assertTrue(_FakePrivateDirectories.instances[-1].closed)

            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process._PrivateChildDirectories",
                    _FakePrivateDirectories,
                ),
                mock.patch.object(
                    ProcessTree, "spawn_exec", new_callable=mock.AsyncMock, return_value=_FakeTree()
                ),
            ):
                process = await adapter.spawn(request)
                await process.terminate()
            self.assertTrue(_FakePrivateDirectories.instances[-1].closed)

    async def test_strong_requirement_and_inode_failure_precede_child_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            adapter = self._adapter(SandboxProfile.WORKSPACE, workspace, state)
            strong = replace(
                self._request(workspace, SandboxProfile.WORKSPACE),
                lifecycle=LocalProcessLifecycle(
                    required_capability=LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
                ),
            )
            with (
                mock.patch.object(ProcessTree, "spawn_exec", new_callable=mock.AsyncMock) as spawn,
                self.assertRaisesRegex(SandboxError, "does not satisfy required"),
            ):
                await adapter.spawn(strong)
            spawn.assert_not_awaited()

            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.macos_local_process._PrivateChildDirectories",
                    _FakePrivateDirectories,
                ),
                mock.patch.object(
                    adapter._inode_audit,
                    "ensure",
                    side_effect=SandboxError("hardlink outside workspace"),
                ) as audit,
                mock.patch.object(ProcessTree, "spawn_exec", new_callable=mock.AsyncMock) as spawn,
                self.assertRaisesRegex(SandboxError, "hardlink outside"),
            ):
                await adapter.spawn(self._request(workspace, SandboxProfile.WORKSPACE))
            audit.assert_called_once()
            spawn.assert_not_awaited()
            self.assertTrue(_FakePrivateDirectories.instances[-1].closed)

    def test_pty_launches_sandbox_exec_and_cleanup_is_session_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            state.mkdir()
            platform = _FakeTerminalPlatform()
            adapter = self._adapter(
                SandboxProfile.STRICT,
                workspace,
                state,
                terminal_platform=platform,
            )
            request = self._request(
                workspace,
                SandboxProfile.STRICT,
                purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                stdio=LocalProcessStdioMode.PTY,
                executable=True,
            )
            with mock.patch(
                "neuro_code.infrastructure.sandbox.macos_local_process._PrivateChildDirectories",
                _FakePrivateDirectories,
            ):
                session = adapter.spawn_terminal(
                    request,
                    size=TerminalSize(80, 24),
                    on_output=lambda _: None,
                    on_eof=lambda: None,
                    on_error=lambda _: None,
                )
                self.assertEqual(platform.calls[0]["executable"], "/usr/bin/sandbox-exec")
                self.assertEqual(platform.calls[0]["arguments"][0], "-p")  # type: ignore[index]
                self.assertFalse(_FakePrivateDirectories.instances[-1].closed)
                session.close()
                self.assertTrue(_FakePrivateDirectories.instances[-1].closed)


if __name__ == "__main__":
    unittest.main()
