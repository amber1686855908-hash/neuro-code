from __future__ import annotations

import os
import tempfile
import threading
import time
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
    WindowsRuntimeControllerTransport,
    WindowsRuntimeIdentity,
    WindowsTrustedRunnerProvenance,
    _required_capabilities,
    _validated_child_token_attestation,
    _WindowsNativeOwnedLocalProcess,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    _FILE_CREATE_PIPE_INSTANCE,
    _PIPE_CONTROL_READ_ACCESS,
    _PIPE_EVENT_WRITE_ACCESS,
    RunnerLaunch,
    WindowsNamedPipeReader,
    WindowsNamedPipeWriter,
)
from neuro_code.infrastructure.sandbox.windows_native_runtime_protocol import (
    MAX_FRAME_PAYLOAD,
    RuntimeChannel,
    RuntimeFrame,
    RuntimeFrameDecoder,
    RuntimeFrameType,
    decode_json,
    encode_frame,
    encode_json,
    validate_channel_frame,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import WindowsAccountSid
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import (
    WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES,
    SyntheticWindowsSid,
)
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

    def test_directional_channels_reject_cross_direction_frames(self) -> None:
        for kind in (
            RuntimeFrameType.STDOUT,
            RuntimeFrameType.STDERR,
            RuntimeFrameType.EXIT,
            RuntimeFrameType.ERROR,
        ):
            with self.assertRaises(SandboxError):
                validate_channel_frame(RuntimeChannel.CONTROL, kind)
        for kind in (
            RuntimeFrameType.SPAWN_REQUEST,
            RuntimeFrameType.STDIN,
            RuntimeFrameType.CLOSE_STDIN,
            RuntimeFrameType.TERMINATE,
        ):
            with self.assertRaises(SandboxError):
                validate_channel_frame(RuntimeChannel.EVENT, kind)

    def test_large_stdin_frame_is_binary_safe(self) -> None:
        payload = b"\x00\xff" * 40_000
        decoder = RuntimeFrameDecoder()
        frames = decoder.feed(encode_frame(RuntimeFrameType.STDIN, payload))
        self.assertEqual(frames, (RuntimeFrame(RuntimeFrameType.STDIN, payload),))

    def test_protocol_rejects_noncanonical_types_and_invalid_frames(self) -> None:
        with self.assertRaises(TypeError):
            encode_frame(1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            encode_frame(RuntimeFrameType.STDOUT, bytearray(b"not-bytes"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            RuntimeFrameDecoder().feed(bytearray(b"frame"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            validate_channel_frame("control", RuntimeFrameType.STDIN)  # type: ignore[arg-type]

        too_large = (MAX_FRAME_PAYLOAD + 1).to_bytes(4, "little") + bytes([RuntimeFrameType.STDOUT])
        with self.assertRaises(SandboxError):
            RuntimeFrameDecoder().feed(too_large)
        invalid_kind = (0).to_bytes(4, "little") + b"\xff"
        with self.assertRaises(SandboxError):
            RuntimeFrameDecoder().feed(invalid_kind)
        decoder = RuntimeFrameDecoder()
        decoder.feed(encode_frame(RuntimeFrameType.STDOUT, b"partial")[:-1])
        with self.assertRaises(SandboxError):
            decoder.finish()

    def test_protocol_json_is_utf8_bounded_and_fail_closed(self) -> None:
        value = {"message": "中文", "count": 2}
        self.assertEqual(decode_json(encode_json(value)), value)
        with self.assertRaises(SandboxError):
            decode_json(b"{not-json")
        with self.assertRaises(SandboxError):
            decode_json(b"\xff")
        with self.assertRaises(SandboxError):
            encode_json({"not": object()})


class _FakeDirectionalPipeApi:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.read_release = threading.Event()
        self.closed: list[int] = []
        self.last_read_error = None

    def read(self, handle: int) -> bytes:
        del handle
        self.read_release.wait(timeout=2)
        return b""

    def write(self, handle: int, payload: bytes) -> None:
        del handle
        self.writes.append(payload)

    def close(self, handle: int) -> None:
        self.closed.append(handle)


class _StatePipe:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)
        self.writes: list[bytes] = []
        self.closed = False
        self.last_read_error = None

    def read(self) -> bytes:
        if not self._chunks:
            time.sleep(0.01)
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def close(self) -> None:
        self.closed = True

    def observe_process(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"state": "RUNNER_EXITED"}


class WindowsNativeDirectionalTransportTests(unittest.TestCase):
    def test_pipe_client_masks_are_specific_and_cannot_create_instances(self) -> None:
        self.assertNotEqual(_PIPE_CONTROL_READ_ACCESS, 0)
        self.assertNotEqual(_PIPE_EVENT_WRITE_ACCESS, 0)
        self.assertEqual(_PIPE_CONTROL_READ_ACCESS & _FILE_CREATE_PIPE_INSTANCE, 0)
        self.assertEqual(_PIPE_EVENT_WRITE_ACCESS & _FILE_CREATE_PIPE_INSTANCE, 0)

    def test_control_and_event_are_distinct_one_way_endpoints(self) -> None:
        api = _FakeDirectionalPipeApi()
        control = WindowsNamedPipeWriter(101, api=api)  # type: ignore[arg-type]
        events = WindowsNamedPipeReader(202, api=api)  # type: ignore[arg-type]
        self.assertNotEqual(control.handle, events.handle)
        self.assertTrue(hasattr(control, "write"))
        self.assertFalse(hasattr(control, "read"))
        self.assertTrue(hasattr(events, "read"))
        self.assertFalse(hasattr(events, "write"))

    def test_concurrent_event_writes_are_serialized_as_complete_frames(self) -> None:
        api = _FakeDirectionalPipeApi()
        events = WindowsNamedPipeWriter(202, api=api)  # type: ignore[arg-type]
        frames_to_write = [
            (
                (RuntimeFrameType.STDOUT, RuntimeFrameType.STDERR, RuntimeFrameType.EXIT)[
                    index % 3
                ],
                b"event-" + bytes([index]) * 70_000,
            )
            for index in range(8)
        ]
        threads = [
            threading.Thread(
                target=events.write,
                args=(encode_frame(kind, payload),),
            )
            for kind, payload in frames_to_write
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        decoder = RuntimeFrameDecoder()
        frames = decoder.feed(b"".join(api.writes))
        self.assertEqual(len(frames), len(frames_to_write))
        self.assertEqual(
            {(frame.kind, frame.payload) for frame in frames},
            set(frames_to_write),
        )

    def test_blocking_control_read_does_not_prevent_event_write(self) -> None:
        api = _FakeDirectionalPipeApi()
        control = WindowsNamedPipeReader(101, api=api)  # type: ignore[arg-type]
        events = WindowsNamedPipeWriter(202, api=api)  # type: ignore[arg-type]
        reader = threading.Thread(target=control.read, daemon=True)
        reader.start()
        started = time.monotonic()
        events.write(encode_frame(RuntimeFrameType.EXIT, b"done"))
        elapsed = time.monotonic() - started
        api.read_release.set()
        reader.join(timeout=2)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(len(api.writes), 1)


class WindowsNativeRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_eof_before_exit_is_a_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            process = _WindowsNativeOwnedLocalProcess(
                transport=WindowsRuntimeControllerTransport(
                    control=_StatePipe(),  # type: ignore[arg-type]
                    events=_StatePipe(),  # type: ignore[arg-type]
                ),
                runner=RunnerLaunch(process_handle=1, process_id=42),
                pid=42,
                request=_request(root),
            )
            with self.assertRaisesRegex(SandboxError, "disconnected before Exit"):
                await process.wait()
            diagnostic = process.diagnostic_snapshot()
            self.assertIsNotNone(diagnostic)
            self.assertEqual(diagnostic["pipe"], "EVENT_PIPE_BROKEN")  # type: ignore[index]

    async def test_exit_preserves_nonzero_code_and_error_diagnostic_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            payload = encode_json(
                {
                    "version": 1,
                    "returncode": 23,
                    "child": {"state": "EXITED", "exit_code": 23},
                }
            )
            process = _WindowsNativeOwnedLocalProcess(
                transport=WindowsRuntimeControllerTransport(
                    control=_StatePipe(),  # type: ignore[arg-type]
                    events=_StatePipe(),  # type: ignore[arg-type]
                ),
                runner=RunnerLaunch(process_handle=1, process_id=42),
                pid=42,
                request=_request(root),
                initial_frames=(RuntimeFrame(RuntimeFrameType.EXIT, payload),),
            )
            self.assertEqual(await process.wait(), 23)
            self.assertEqual(process.returncode, 23)
            diagnostic = process.diagnostic_snapshot()
            self.assertIsNotNone(diagnostic)
            self.assertEqual(diagnostic["child"]["exit_code"], 23)  # type: ignore[index]

            error_process = _WindowsNativeOwnedLocalProcess(
                transport=WindowsRuntimeControllerTransport(
                    control=_StatePipe(),  # type: ignore[arg-type]
                    events=_StatePipe(),  # type: ignore[arg-type]
                ),
                runner=RunnerLaunch(process_handle=1, process_id=42),
                pid=42,
                request=_request(root),
                security_attestation={"user_sid": "S-1-5-21-safe"},
                initial_frames=(
                    RuntimeFrame(
                        RuntimeFrameType.ERROR,
                        encode_json({"message": "credential=secret" * 100}),
                    ),
                ),
            )
            with self.assertRaisesRegex(SandboxError, "credential=secret"):
                await error_process.wait()
            self.assertNotIn("credential=secret", repr(error_process.diagnostic_snapshot()))

    async def test_stdin_rejects_after_exit_and_close_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            control = _StatePipe()
            process = _WindowsNativeOwnedLocalProcess(
                transport=WindowsRuntimeControllerTransport(
                    control=control,  # type: ignore[arg-type]
                    events=_StatePipe(),  # type: ignore[arg-type]
                ),
                runner=RunnerLaunch(process_handle=1, process_id=42),
                pid=42,
                request=_request(root),
                initial_frames=(
                    RuntimeFrame(RuntimeFrameType.EXIT, encode_json({"returncode": 0})),
                ),
            )
            self.assertEqual(await process.wait(), 0)
            with self.assertRaisesRegex(RuntimeError, "process is closed"):
                await process.write_stdin(b"must-not-send")
            await process.close_stdin()
            self.assertEqual(control.writes, [])

    def test_trusted_runner_provenance_accepts_external_workspace(self) -> None:
        provenance = WindowsTrustedRunnerProvenance.resolve()
        with tempfile.TemporaryDirectory() as directory:
            writable = Path(directory) / "workspace"
            writable.mkdir()
            provenance.assert_disjoint((writable,))

    def test_trusted_runner_provenance_rejects_package_and_ancestor_overlap(self) -> None:
        provenance = WindowsTrustedRunnerProvenance.resolve()
        with self.assertRaisesRegex(
            SandboxError, "trusted runner provenance overlaps model-writable authority"
        ):
            provenance.assert_disjoint((provenance.package_root,))
        with self.assertRaisesRegex(
            SandboxError, "trusted runner provenance overlaps model-writable authority"
        ):
            provenance.assert_disjoint((provenance.package_root.parent,))

    def test_editable_checkout_fails_before_runner_launch(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            installation = Path(directory) / "installation"
            installation.mkdir()
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(repository,),
                writable_roots=(repository,),
                sensitive_read_paths=(),
            )
            launcher = mock.Mock()
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                repository,
                Path(directory) / "state",
                setup_authority=_ReadySetupAuthority(WindowsSandboxSetupState.READY),
                setup_request_factory=lambda _request: setup_request,
                runner_launcher=launcher,
            )
            provenance = WindowsTrustedRunnerProvenance.resolve()
            with (
                mock.patch(
                    "neuro_code.infrastructure.sandbox.windows_native_local_process._runtime_is_windows",
                    return_value=True,
                ),
                mock.patch.object(
                    WindowsTrustedRunnerProvenance, "resolve", return_value=provenance
                ),
                self.assertRaisesRegex(
                    SandboxError,
                    "trusted runner provenance overlaps model-writable authority",
                ),
            ):
                adapter._validate(_request(repository))
            launcher.assert_not_called()

    async def test_coalesced_spawn_ready_frames_are_not_dropped(self) -> None:
        class _Pipe:
            def __init__(self, *chunks: bytes) -> None:
                self.closed = False
                self._chunks = list(chunks)

            def read(self) -> bytes:
                return self._chunks.pop(0) if self._chunks else b""

            def write(self, payload: bytes) -> None:
                del payload

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            encoded_exit = encode_frame(
                RuntimeFrameType.EXIT,
                encode_json({"version": 1, "returncode": 0}),
            )
            decoder = RuntimeFrameDecoder()
            decoder.feed(encoded_exit[:2])
            process = _WindowsNativeOwnedLocalProcess(
                transport=WindowsRuntimeControllerTransport(
                    control=_Pipe(),  # type: ignore[arg-type]
                    events=_Pipe(encoded_exit[2:]),  # type: ignore[arg-type]
                ),
                runner=RunnerLaunch(process_handle=1, process_id=42),
                pid=42,
                request=_request(root),
                decoder=decoder,
                initial_frames=(RuntimeFrame(RuntimeFrameType.STDOUT, b"fast-child\n"),),
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

    def test_spawn_ready_attestation_requires_exact_synthetic_write_sid(self) -> None:
        expected = SyntheticWindowsSid.from_components((1, 2, 3, 4))
        payload = {
            "user_sid": "S-1-5-21-10-20-30-40",
            "restricted_sids": ["S-1-1-0"],
            "is_restricted": True,
            "change_notify_privilege_enabled": True,
            "unexpected_enabled_privilege_count": 0,
        }
        with self.assertRaisesRegex(SandboxError, "unexpected restricting SID set"):
            _validated_child_token_attestation(payload, expected_write_sid=expected)

    def test_runtime_advertises_candidate_capabilities_for_native_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                root,
                Path(directory) / "state",
                setup_authority=_ReadySetupAuthority(WindowsSandboxSetupState.READY),
            )
            self.assertEqual(adapter.security_capabilities, WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES)

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
