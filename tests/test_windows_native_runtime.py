from __future__ import annotations

import os
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
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxIdentityKind,
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupSnapshot,
    WindowsSandboxSetupState,
)
from neuro_code.bootstrap.composition import _default_local_process_sandbox_factory
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
    WindowsRuntimeIdentity,
    _required_capabilities,
    _WindowsNativeOwnedLocalProcess,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import RunnerLaunch
from neuro_code.infrastructure.sandbox.windows_native_runtime_protocol import (
    MAX_FRAME_PAYLOAD,
    RuntimeFrame,
    RuntimeFrameDecoder,
    RuntimeFrameType,
    encode_frame,
    encode_json,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import WindowsAccountSid
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.shared.errors import SandboxError


class _ReadySetupAuthority:
    def __init__(self, state: WindowsSandboxSetupState) -> None:
        self.state = state
        self.requests: list[object] = []

    @property
    def privilege_boundary(self):  # type: ignore[no-untyped-def]
        return None

    def inspect(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return WindowsSandboxSetupSnapshot(self.state)

    def setup(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("runtime must never call setup")

    def repair(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("runtime must never call repair")

    def cleanup(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("runtime must never call cleanup")


class _IdentityProvider:
    def __init__(self) -> None:
        self.kinds: list[WindowsSandboxIdentityKind] = []

    def resolve(self, request, kind):  # type: ignore[no-untyped-def]
        self.kinds.append(kind)
        return WindowsRuntimeIdentity(
            kind=kind,
            username=f"NeuroSandbox{kind.value.title()}",
            password="not-a-real-password",
            user_sid=WindowsAccountSid("S-1-5-21-1-2-3-4000"),
            write_sid=SyntheticWindowsSid.from_components((1, 2, 3, 4)),
        )


def _request(
    root: Path,
    *,
    profile: SandboxProfile = SandboxProfile.WORKSPACE,
    purpose: LocalProcessPurpose = LocalProcessPurpose.BASH,
    stdio: LocalProcessStdioMode = LocalProcessStdioMode.CAPTURE,
    network: LocalProcessNetworkPolicy = LocalProcessNetworkPolicy.INHERIT,
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.shell(
        "echo ok",
        purpose=purpose,
        cwd=root,
        sandbox_profile=profile,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (LocalWorkspaceAccess(root, LocalWorkspaceAccessMode.READ_WRITE),),
        ),
        network_policy=network,
        environment_policy=LocalProcessEnvironmentPolicy({"PATH": r"C:\\Windows\\System32"}),
        stdio_mode=stdio,
        lifecycle=LocalProcessLifecycle(
            required_capability=LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT,
        ),
    )


class WindowsNativeRuntimeProtocolTests(unittest.TestCase):
    def test_frames_are_binary_safe_and_incremental(self) -> None:
        payload = bytes(range(256)) * 512
        encoded = encode_frame(RuntimeFrameType.STDOUT, payload)
        decoder = RuntimeFrameDecoder()
        frames: list[RuntimeFrame] = []
        for offset in range(0, len(encoded), 137):
            frames.extend(decoder.feed(encoded[offset : offset + 137]))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].kind, RuntimeFrameType.STDOUT)
        self.assertEqual(frames[0].payload, payload)
        decoder.finish()

    def test_payload_limit_is_explicit(self) -> None:
        with self.assertRaises(ValueError):
            encode_frame(RuntimeFrameType.STDOUT, b"x" * (MAX_FRAME_PAYLOAD + 1))


class WindowsNativeRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_spawn_ready_frames_are_not_dropped(self) -> None:
        class _Pipe:
            def __init__(self) -> None:
                self.closed = False

            def read(self) -> bytes:
                return b""

            def write(self, payload: bytes) -> None:
                del payload

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            process = _WindowsNativeOwnedLocalProcess(
                pipe=_Pipe(),  # type: ignore[arg-type]
                runner=RunnerLaunch(process_handle=1, process_id=42),
                pid=42,
                request=_request(root),
                initial_frames=(
                    RuntimeFrame(RuntimeFrameType.STDOUT, b"fast-child\n"),
                    RuntimeFrame(
                        RuntimeFrameType.EXIT,
                        encode_json({"version": 1, "returncode": 0}),
                    ),
                ),
            )
            self.assertEqual(await process.stdout.read(), b"fast-child\n")  # type: ignore[union-attr]
            self.assertEqual(await process.wait(), 0)

    def test_composition_routes_enabled_windows_profiles_to_w3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            with (
                mock.patch(
                    "neuro_code.bootstrap.composition._runtime_platform",
                    return_value="win32",
                ),
                mock.patch(
                    "neuro_code.bootstrap.composition.WindowsNativeLocalProcessSandbox"
                ) as adapter,
            ):
                result = _default_local_process_sandbox_factory(
                    SandboxProfile.WORKSPACE,
                    root,
                    Path(directory) / "state",
                )
            self.assertIs(result, adapter.return_value)
            adapter.assert_called_once_with(
                SandboxProfile.WORKSPACE,
                root,
                Path(directory) / "state",
            )

    async def test_non_windows_fails_closed_before_runner_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            authority = _ReadySetupAuthority(WindowsSandboxSetupState.READY)
            launcher = mock.Mock()
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                root,
                Path(directory) / "state",
                setup_authority=authority,
                runner_launcher=launcher,
            )
            with mock.patch.object(os, "name", "posix"), self.assertRaises(SandboxError):
                await adapter.spawn(_request(root))
            launcher.assert_not_called()
            self.assertEqual(authority.requests, [])

    def test_profile_requirements_are_orthogonal_to_lifecycle(self) -> None:
        workspace = _required_capabilities(SandboxProfile.WORKSPACE)
        read_only = _required_capabilities(SandboxProfile.READ_ONLY)
        strict = _required_capabilities(SandboxProfile.STRICT)
        self.assertEqual(workspace.read_isolation.value, "limited")
        self.assertEqual(workspace.write_isolation.value, "strong")
        self.assertEqual(workspace.network_isolation.value, "unsupported")
        self.assertEqual(read_only.network_isolation.value, "strong")
        self.assertEqual(strict.read_isolation.value, "strong")
        self.assertEqual(
            LocalProcessLifecycle().required_capability,
            LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT,
        )

    def test_enabled_runtime_rejects_pty_and_interactive_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                root,
                Path(directory) / "state",
                setup_authority=_ReadySetupAuthority(WindowsSandboxSetupState.READY),
            )
            with mock.patch.object(os, "name", "nt"):
                with self.assertRaises(SandboxError):
                    adapter._validate(_request(root, stdio=LocalProcessStdioMode.PTY))
                with self.assertRaises(SandboxError):
                    adapter._validate(
                        _request(
                            root,
                            purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                        )
                    )

    def test_setup_not_ready_fails_before_identity_or_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            provider = _IdentityProvider()
            launcher = mock.Mock()
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                root,
                Path(directory) / "state",
                setup_authority=_ReadySetupAuthority(WindowsSandboxSetupState.NEEDS_REPAIR),
                identity_provider=provider,
                runner_launcher=launcher,
            )
            with mock.patch.object(os, "name", "nt"), self.assertRaises(SandboxError):
                adapter._validate(_request(root))
            self.assertEqual(provider.kinds, [])
            launcher.assert_not_called()

    def test_strict_fails_closed_because_read_is_limited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.STRICT,
                root,
                Path(directory) / "state",
                setup_authority=_ReadySetupAuthority(WindowsSandboxSetupState.READY),
            )
            with mock.patch.object(os, "name", "nt"), self.assertRaises(SandboxError):
                adapter._validate(
                    _request(
                        root,
                        profile=SandboxProfile.STRICT,
                        network=LocalProcessNetworkPolicy.ISOLATED,
                    )
                )

    def test_runtime_scope_cannot_widen_prepared_setup_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = root / "prepared"
            requested = root / "outside"
            prepared.mkdir()
            requested.mkdir()
            setup = WindowsSandboxSetupRequest(
                installation_root=root / "installation",
                read_roots=(prepared,),
                writable_roots=(prepared,),
                sensitive_read_paths=(),
            )
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                prepared,
                root / "state",
            )
            with self.assertRaises(SandboxError):
                adapter._validate_setup_scope(
                    _request(requested),
                    setup,
                )

    def test_child_environment_removes_case_insensitive_python_injection(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SystemRoot": r"C:\\Windows", "PATH": r"C:\\Windows\\System32"},
            clear=True,
        ):
            values = WindowsNativeLocalProcessSandbox._child_environment(
                LocalProcessEnvironmentPolicy(
                    {
                        "PythonPath": r"C:\\attacker",
                        "pythonhome": r"C:\\attacker",
                        "Path": r"C:\\explicit",
                    }
                )
            )
        self.assertNotIn("PythonPath", values)
        self.assertNotIn("pythonhome", values)
        self.assertEqual(values["PATH"], r"C:\\explicit")


if __name__ == "__main__":
    unittest.main()
