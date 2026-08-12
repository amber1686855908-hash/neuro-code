from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from neuro_code.application.ports.sandbox import (
    LocalProcessCancellationPolicy,
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
from neuro_code.application.ports.terminal import TerminalPlatformSession
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.terminal import TerminalSignal, TerminalSize
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox
from neuro_code.infrastructure.sandbox.process_tree import ProcessTree
from neuro_code.shared.errors import SandboxError


def _filesystem_policy(root: Path) -> LocalProcessFilesystemPolicy:
    return LocalProcessFilesystemPolicy(
        (LocalWorkspaceAccess(root, LocalWorkspaceAccessMode.READ_WRITE),),
    )


def _request(
    root: Path,
    *,
    stdio_mode: LocalProcessStdioMode = LocalProcessStdioMode.CAPTURE,
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        sys.executable,
        ("-c", "print('canonical local process')"),
        purpose=LocalProcessPurpose.BASH,
        cwd=root,
        sandbox_profile=SandboxProfile.OFF,
        filesystem_policy=_filesystem_policy(root),
        network_policy=LocalProcessNetworkPolicy.INHERIT,
        environment_policy=LocalProcessEnvironmentPolicy(dict(os.environ)),
        stdio_mode=stdio_mode,
        lifecycle=LocalProcessLifecycle(
            termination_grace_seconds=0.1,
            force_wait_seconds=1,
        ),
    )


class _FakeTerminalSession:
    process_id = 9001
    lifecycle_capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    def write(self, data: bytes) -> None:
        del data

    def resize(self, size: TerminalSize) -> None:
        del size

    def send_signal(self, signal: TerminalSignal) -> None:
        del signal

    def poll_exit(self) -> int | None:
        return None

    def close(self) -> None:
        return None


class _FakeTerminalPlatform:
    lifecycle_capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.session = _FakeTerminalSession()

    def spawn_exec(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        size: TerminalSize,
        on_output: object,
        on_eof: object,
        on_error: object,
    ) -> TerminalPlatformSession:
        self.calls.append(
            {
                "arguments": arguments,
                "cwd": cwd,
                "env": env,
                "executable": executable,
                "size": size,
                "on_output": on_output,
                "on_eof": on_eof,
                "on_error": on_error,
            }
        )
        return self.session


class SandboxedProcessRequestTests(unittest.TestCase):
    def test_rejects_a_cwd_outside_its_explicit_workspace_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "inside an authorized workspace root"):
                SandboxedProcessRequest.exec(
                    sys.executable,
                    ("-c", "pass"),
                    purpose=LocalProcessPurpose.BASH,
                    cwd=Path(outside).resolve(),
                    sandbox_profile=SandboxProfile.OFF,
                    filesystem_policy=_filesystem_policy(root),
                    network_policy=LocalProcessNetworkPolicy.INHERIT,
                    environment_policy=LocalProcessEnvironmentPolicy(),
                    stdio_mode=LocalProcessStdioMode.CAPTURE,
                    lifecycle=LocalProcessLifecycle(),
                )

    def test_restricted_profiles_require_network_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ValueError, "require isolated child networking"):
                SandboxedProcessRequest.exec(
                    sys.executable,
                    ("-c", "pass"),
                    purpose=LocalProcessPurpose.BASH,
                    cwd=root,
                    sandbox_profile=SandboxProfile.STRICT,
                    filesystem_policy=_filesystem_policy(root),
                    network_policy=LocalProcessNetworkPolicy.INHERIT,
                    environment_policy=LocalProcessEnvironmentPolicy(),
                    stdio_mode=LocalProcessStdioMode.CAPTURE,
                    lifecycle=LocalProcessLifecycle(),
                )

    def test_read_only_profiles_require_read_only_workspace_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ValueError, "require read-only workspace roots"):
                SandboxedProcessRequest.exec(
                    sys.executable,
                    ("-c", "pass"),
                    purpose=LocalProcessPurpose.BASH,
                    cwd=root,
                    sandbox_profile=SandboxProfile.READ_ONLY,
                    filesystem_policy=_filesystem_policy(root),
                    network_policy=LocalProcessNetworkPolicy.ISOLATED,
                    environment_policy=LocalProcessEnvironmentPolicy(),
                    stdio_mode=LocalProcessStdioMode.CAPTURE,
                    lifecycle=LocalProcessLifecycle(),
                )


class ProcessTreeLocalProcessSandboxTests(unittest.IsolatedAsyncioTestCase):
    def test_process_tree_reports_platform_capability(self) -> None:
        expected = (
            LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
            if os.name == "nt"
            else LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT
        )
        self.assertIs(ProcessTreeLocalProcessSandbox().lifecycle_capability, expected)
        with mock.patch("neuro_code.infrastructure.sandbox.local_process.os.name", "nt"):
            self.assertIs(
                ProcessTreeLocalProcessSandbox().lifecycle_capability,
                LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP,
            )

    async def test_strong_requirement_fails_before_posix_child_creation(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX ProcessTree is the best-effort fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = SandboxedProcessRequest.exec(
                sys.executable,
                ("-c", "pass"),
                purpose=LocalProcessPurpose.BASH,
                cwd=root,
                sandbox_profile=SandboxProfile.OFF,
                filesystem_policy=_filesystem_policy(root),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy(),
                stdio_mode=LocalProcessStdioMode.CAPTURE,
                lifecycle=LocalProcessLifecycle(
                    required_capability=(
                        LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
                    )
                ),
            )
            with (
                mock.patch.object(ProcessTree, "spawn_exec", new_callable=mock.AsyncMock) as spawn,
                self.assertRaisesRegex(SandboxError, "does not satisfy required"),
            ):
                await ProcessTreeLocalProcessSandbox().spawn(request)
            spawn.assert_not_awaited()

    async def test_owns_process_output_and_lifecycle_through_the_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ProcessTreeLocalProcessSandbox()
            process = await adapter.spawn(_request(root))

            assert process.stdout is not None
            self.assertEqual(
                (await process.stdout.read()).decode().strip(), "canonical local process"
            )
            self.assertEqual(await process.wait(), 0)
            self.assertEqual(process.returncode, 0)
            self.assertGreater(process.process_id, 0)
            self.assertIs(
                process.lifecycle_capability,
                adapter.lifecycle_capability,
            )

    async def test_deprecated_cancellation_name_keeps_owned_termination_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = replace(
                _request(root),
                arguments=("-c", "import time; time.sleep(60)"),
                lifecycle=LocalProcessLifecycle(
                    cancellation_policy=LocalProcessCancellationPolicy.TERMINATE_PROCESS_TREE,
                    termination_grace_seconds=0.05,
                    force_wait_seconds=1,
                ),
            )
            process = await ProcessTreeLocalProcessSandbox().spawn(request)
            await process.terminate()
            self.assertIsNotNone(process.returncode)

    async def test_rejects_pty_until_a_platform_terminal_adapter_owns_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory), stdio_mode=LocalProcessStdioMode.PTY)
            with self.assertRaisesRegex(SandboxError, "PTY process creation requires"):
                await ProcessTreeLocalProcessSandbox().spawn(request)

    async def test_rejects_enabled_profiles_instead_of_falling_back_to_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = SandboxedProcessRequest.exec(
                sys.executable,
                ("-c", "pass"),
                purpose=LocalProcessPurpose.BASH,
                cwd=root,
                sandbox_profile=SandboxProfile.WORKSPACE,
                filesystem_policy=_filesystem_policy(root),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy(),
                stdio_mode=LocalProcessStdioMode.CAPTURE,
                lifecycle=LocalProcessLifecycle(),
            )
            with self.assertRaisesRegex(SandboxError, "requires a platform child-sandbox"):
                await ProcessTreeLocalProcessSandbox().spawn(request)

    async def test_rejects_protocol_over_a_shell_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = SandboxedProcessRequest.shell(
                "echo protocol",
                purpose=LocalProcessPurpose.MCP_STDIO,
                cwd=root,
                sandbox_profile=SandboxProfile.OFF,
                filesystem_policy=_filesystem_policy(root),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy(),
                stdio_mode=LocalProcessStdioMode.PROTOCOL,
                lifecycle=LocalProcessLifecycle(),
            )
            with self.assertRaisesRegex(SandboxError, "protocol local processes require"):
                await ProcessTreeLocalProcessSandbox().spawn(request)

    def test_routes_pty_creation_through_the_canonical_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            platform = _FakeTerminalPlatform()
            request = SandboxedProcessRequest.exec(
                sys.executable,
                ("-c", "pass"),
                purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                cwd=root,
                sandbox_profile=SandboxProfile.OFF,
                filesystem_policy=_filesystem_policy(root),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy({"TERM": "xterm"}),
                stdio_mode=LocalProcessStdioMode.PTY,
                lifecycle=LocalProcessLifecycle(),
            )

            callbacks = (lambda data: None, lambda: None, lambda error: None)
            result = ProcessTreeLocalProcessSandbox(terminal_platform=platform).spawn_terminal(
                request,
                size=TerminalSize(80, 24),
                on_output=callbacks[0],
                on_eof=callbacks[1],
                on_error=callbacks[2],
            )

            self.assertIs(result, platform.session)
            self.assertEqual(len(platform.calls), 1)
            call = platform.calls[0]
            self.assertEqual(call["executable"], sys.executable)
            self.assertEqual(call["arguments"], ("-c", "pass"))
            self.assertEqual(call["cwd"], root)
            self.assertEqual(call["env"], {"TERM": "xterm"})
            self.assertEqual(call["size"], TerminalSize(80, 24))

    def test_rejects_enabled_profile_for_terminal_until_child_adapter_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = SandboxedProcessRequest.exec(
                sys.executable,
                (),
                purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                cwd=root,
                sandbox_profile=SandboxProfile.WORKSPACE,
                filesystem_policy=_filesystem_policy(root),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy(),
                stdio_mode=LocalProcessStdioMode.PTY,
                lifecycle=LocalProcessLifecycle(),
            )
            with self.assertRaisesRegex(SandboxError, "requires a platform child-sandbox"):
                ProcessTreeLocalProcessSandbox().spawn_terminal(
                    request,
                    size=TerminalSize(80, 24),
                    on_output=lambda data: None,
                    on_eof=lambda: None,
                    on_error=lambda error: None,
                )
