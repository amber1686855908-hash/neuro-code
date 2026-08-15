"""Windows non-PTY native sandbox runtime adapter (W3).

The adapter owns only runtime orchestration.  W2 remains the authority for
setup/repair and persistent state.  A spawn is rejected unless W2 reports
``READY``; no runtime path performs UAC, Firewall mutation, or ACL repair.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessOutput,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessSecurityCapabilities,
    LocalProcessSecurityStrength,
    LocalProcessStdioMode,
    LocalWorkspaceAccessMode,
    OwnedLocalProcess,
    SandboxedProcessRequest,
    lifecycle_capability_satisfies,
    security_capability_satisfies,
)
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxIdentityKind,
    WindowsSandboxSetupAuthority,
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.terminal.models import TerminalSignal, TerminalSize
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    RunnerLaunch,
    WindowsNamedPipeReader,
    WindowsNamedPipeServer,
    WindowsNamedPipeWriter,
    _WindowsNamedPipeDirection,
    _WindowsNativeDesktopMode,
    close_runner_process,
    current_user_sid,
    launch_runner,
    observe_process_id,
)
from neuro_code.infrastructure.sandbox.windows_native_runtime_protocol import (
    PROTOCOL_VERSION,
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
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _InstallationRecord,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SandboxError

if TYPE_CHECKING:
    from neuro_code.application.ports.terminal import (
        TerminalEofHandler,
        TerminalErrorHandler,
        TerminalOutputHandler,
        TerminalPlatformSession,
    )
    from neuro_code.domain.terminal.models import TerminalSize

_SUPPORTED_PURPOSES = frozenset(
    {
        LocalProcessPurpose.BASH,
        LocalProcessPurpose.BACKGROUND_BASH,
        LocalProcessPurpose.MCP_STDIO,
    }
)
_SUPPORTED_STDIO = frozenset(
    {
        LocalProcessStdioMode.CAPTURE,
        LocalProcessStdioMode.MERGED_CAPTURE,
        LocalProcessStdioMode.PROTOCOL,
    }
)
_RUNNER_ENVIRONMENT = frozenset({"SystemRoot", "SystemDrive", "PATH", "PATHEXT"})
_WINDOWS_SID_TEXT = re.compile(r"^S-1-(?:\d+)(?:-\d+)+$")


def _runtime_is_windows() -> bool:
    return os.name == "nt"


def _validated_child_token_attestation(
    value: object, *, expected_write_sid: SyntheticWindowsSid
) -> dict[str, object]:
    """Validate SpawnReady facts, including the exact restricting SID identity."""

    if not isinstance(value, dict):
        raise SandboxError("Windows runner omitted final-child token attestation")
    user_sid = value.get("user_sid")
    restricted_sids = value.get("restricted_sids")
    is_restricted = value.get("is_restricted")
    change_notify = value.get("change_notify_privilege_enabled")
    unexpected = value.get("unexpected_enabled_privilege_count")
    if not isinstance(user_sid, str) or _WINDOWS_SID_TEXT.fullmatch(user_sid) is None:
        raise SandboxError("Windows runner returned an invalid final-child TokenUser")
    if (
        not isinstance(restricted_sids, list)
        or len(restricted_sids) > 64
        or any(
            not isinstance(sid, str) or _WINDOWS_SID_TEXT.fullmatch(sid) is None
            for sid in restricted_sids
        )
    ):
        raise SandboxError("Windows runner returned invalid final-child restricting SIDs")
    if tuple(restricted_sids) != (expected_write_sid.value,):
        raise SandboxError("Windows runner returned an unexpected restricting SID set")
    if type(is_restricted) is not bool or type(change_notify) is not bool:
        raise SandboxError("Windows runner returned invalid final-child token flags")
    if type(unexpected) is not int or unexpected < 0 or unexpected > 64:
        raise SandboxError("Windows runner returned invalid final-child privilege facts")
    return {
        "user_sid": user_sid,
        "is_restricted": is_restricted,
        "restricted_sids": tuple(restricted_sids),
        "change_notify_privilege_enabled": change_notify,
        "unexpected_enabled_privilege_count": unexpected,
    }


def _native_acceptance_stage(label: str) -> None:
    """Emit bounded setup/transport stages only in the focused native gate."""

    if (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    ):
        print(f"W3_STAGE={label}", flush=True)


@dataclass(frozen=True, slots=True)
class WindowsRuntimeIdentity:
    """The minimum W2 facts needed to launch one child; password is transient."""

    kind: WindowsSandboxIdentityKind
    username: str
    password: str
    user_sid: WindowsAccountSid
    write_sid: SyntheticWindowsSid


@dataclass(frozen=True, slots=True)
class WindowsTrustedRunnerProvenance:
    """Resolved trusted runner code facts checked before child creation.

    Python ``-I`` and the explicit runner environment remove import-path and
    user-site injection, but they do not establish where the package code was
    loaded from.  This contract keeps the interpreter, runner module, and
    Neuro Code package/dependency root outside every model-writable authority.
    """

    interpreter: Path
    package_root: Path
    runner_module: Path
    dependency_root: Path

    @staticmethod
    def _canonical(path: Path) -> Path:
        if not isinstance(path, Path) or not path.is_absolute():
            raise SandboxError("trusted runner provenance path is not absolute")
        try:
            resolved = path.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise SandboxError("trusted runner provenance path cannot be resolved") from error
        # Windows path comparisons are case-insensitive.  Normalize before
        # using Path.relative_to so C:\Repo and c:\repo cannot evade the
        # component-aware containment check.
        return Path(os.path.normcase(os.path.normpath(os.fspath(resolved))))

    @classmethod
    def resolve(cls) -> WindowsTrustedRunnerProvenance:
        runner_module = cls._canonical(Path(__file__).with_name("windows_native_runner.py"))
        package_root = cls._canonical(runner_module.parents[2])
        interpreter = cls._canonical(Path(sys.executable))
        dependency_root = package_root
        candidates = (interpreter, package_root, runner_module, dependency_root)
        if any(not candidate.exists() for candidate in candidates):
            raise SandboxError("trusted runner provenance is unavailable")
        if not runner_module.is_file() or not interpreter.is_file():
            raise SandboxError("trusted runner provenance is not backed by files")
        return cls(
            interpreter=interpreter,
            package_root=package_root,
            runner_module=runner_module,
            dependency_root=dependency_root,
        )

    @property
    def trusted_paths(self) -> tuple[Path, ...]:
        return (self.interpreter, self.package_root, self.runner_module, self.dependency_root)

    def assert_disjoint(self, writable_roots: tuple[Path, ...]) -> None:
        canonical_roots = tuple(self._canonical(root) for root in writable_roots)
        for trusted in self.trusted_paths:
            for writable in canonical_roots:
                try:
                    trusted.relative_to(writable)
                    overlaps = True
                except ValueError:
                    try:
                        writable.relative_to(trusted)
                        overlaps = True
                    except ValueError:
                        overlaps = False
                if overlaps:
                    raise SandboxError(
                        "trusted runner provenance overlaps model-writable authority"
                    )


@dataclass(frozen=True, slots=True)
class WindowsRuntimeControllerTransport:
    """One-way controller-owned endpoints for one trusted runner."""

    control: WindowsNamedPipeWriter
    events: WindowsNamedPipeReader


class WindowsRuntimeIdentityProvider(Protocol):
    def resolve(
        self,
        request: WindowsSandboxSetupRequest,
        kind: WindowsSandboxIdentityKind,
    ) -> WindowsRuntimeIdentity: ...


class DpapiWindowsRuntimeIdentityProvider:
    """Read W2's DPAPI record without exposing credentials to callers."""

    def __init__(self, *, credential_store: object | None = None) -> None:
        self._credential_store = credential_store

    def resolve(
        self,
        request: WindowsSandboxSetupRequest,
        kind: WindowsSandboxIdentityKind,
    ) -> WindowsRuntimeIdentity:
        store = self._credential_store
        if store is None:
            store = WindowsDpapiCredentialStore(request.installation_root / "credentials.dpapi")
        try:
            encoded = cast(WindowsDpapiCredentialStore, store).load()
            if encoded is None:
                raise SandboxError("Windows sandbox runtime credentials are not provisioned")
            record = _InstallationRecord.decode(encoded)
            identity = next(item for item in record.identities if item.kind is kind)
            password = identity.password.decode("utf-8")
        except (SandboxError, UnicodeError, StopIteration) as error:
            raise SandboxError("Windows sandbox runtime identity is unavailable") from error
        return WindowsRuntimeIdentity(
            kind=kind,
            username=identity.username,
            password=password,
            user_sid=identity.user_sid,
            write_sid=record.write_sid,
        )


def _required_capabilities(profile: SandboxProfile) -> LocalProcessSecurityCapabilities:
    if profile is SandboxProfile.WORKSPACE:
        return LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
            write_isolation=LocalProcessSecurityStrength.STRONG,
        )
    if profile is SandboxProfile.READ_ONLY:
        return LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
            write_isolation=LocalProcessSecurityStrength.STRONG,
            network_isolation=LocalProcessSecurityStrength.STRONG,
        )
    if profile is SandboxProfile.STRICT:
        return LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.STRONG,
            write_isolation=LocalProcessSecurityStrength.STRONG,
            network_isolation=LocalProcessSecurityStrength.STRONG,
        )
    return LocalProcessSecurityCapabilities()


def _default_setup_request(
    *, state_dir: Path, filesystem_policy: LocalProcessFilesystemPolicy
) -> WindowsSandboxSetupRequest:
    roots = tuple(root.path for root in filesystem_policy.workspace_roots)
    writable = tuple(
        root.path for root in filesystem_policy.workspace_roots if root.mode.value == "read-write"
    )
    return WindowsSandboxSetupRequest(
        installation_root=(state_dir / "windows-sandbox").resolve(strict=False),
        read_roots=roots,
        writable_roots=writable,
        sensitive_read_paths=(),
    )


class WindowsNativeLocalProcessSandbox(LocalProcessSandbox):
    """Windows enabled-profile adapter for non-PTY canonical workloads."""

    def __init__(
        self,
        profile: SandboxProfile,
        workspace: Path,
        state_dir: Path,
        *,
        setup_authority: WindowsSandboxSetupAuthority | None = None,
        identity_provider: WindowsRuntimeIdentityProvider | None = None,
        setup_request_factory: Callable[[SandboxedProcessRequest], WindowsSandboxSetupRequest]
        | None = None,
        runner_launcher: Callable[..., RunnerLaunch] = launch_runner,
        pipe_server_factory: Callable[..., WindowsNamedPipeServer] = WindowsNamedPipeServer,
        _diagnostic_desktop_mode: _WindowsNativeDesktopMode = _WindowsNativeDesktopMode.PRIVATE_DESKTOP,
        _diagnostic_create_no_window: bool = True,
    ) -> None:
        if not isinstance(profile, SandboxProfile) or not profile.enabled:
            raise ValueError("Windows native adapter requires an enabled sandbox profile")
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            raise ValueError("Windows native adapter workspace must be absolute")
        if not isinstance(state_dir, Path) or not state_dir.is_absolute():
            raise ValueError("Windows native adapter state_dir must be absolute")
        self._profile = profile
        self._workspace = workspace.expanduser().resolve(strict=False)
        self._state_dir = state_dir.expanduser().resolve(strict=False)
        self._setup_authority = setup_authority or WindowsNativeSandboxSetupAuthority()
        self._identity_provider = identity_provider or DpapiWindowsRuntimeIdentityProvider()
        self._setup_request_factory = setup_request_factory
        self._runner_launcher = runner_launcher
        self._pipe_server_factory = pipe_server_factory
        if not isinstance(_diagnostic_desktop_mode, _WindowsNativeDesktopMode):
            raise ValueError("Windows native diagnostic desktop mode is invalid")
        if not isinstance(_diagnostic_create_no_window, bool):
            raise ValueError("Windows native diagnostic console mode is invalid")
        self._diagnostic_desktop_mode = _diagnostic_desktop_mode
        self._diagnostic_create_no_window = _diagnostic_create_no_window

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP

    @property
    def security_capabilities(self) -> LocalProcessSecurityCapabilities:
        """Capabilities provided by the concrete W3 runtime provider.

        Native acceptance and the required PR gate certify this provider
        contract.  W1/W2's foundation actual declaration remains fail-closed;
        the architecture target is not used to admit runtime requests.
        """

        return WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES

    def _setup_request(self, request: SandboxedProcessRequest) -> WindowsSandboxSetupRequest:
        if self._setup_request_factory is not None:
            return self._setup_request_factory(request)
        return _default_setup_request(
            state_dir=self._state_dir,
            filesystem_policy=request.filesystem_policy,
        )

    @staticmethod
    def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path == root or path.is_relative_to(root) for root in roots)

    @classmethod
    def _validate_setup_scope(
        cls,
        request: SandboxedProcessRequest,
        setup_request: WindowsSandboxSetupRequest,
    ) -> None:
        """Ensure runtime roots are covered by the prepared W2 authority.

        A caller-supplied setup factory is useful for installation selection,
        but it must not let a runtime request silently widen the ACL scope
        beyond the roots that W2 inspected.  Requested roots may be nested
        inside an authorized root (inheritance is the intended model); an
        authorized root may not merely be nested inside a broader request.
        """

        if not isinstance(setup_request, WindowsSandboxSetupRequest):
            raise SandboxError("Windows sandbox setup request is not canonical")
        requested_roots = tuple(root.path for root in request.filesystem_policy.workspace_roots)
        if not all(cls._is_within(root, setup_request.read_roots) for root in requested_roots):
            raise SandboxError("Windows runtime root is outside the prepared read authority")
        writable_roots = tuple(
            root.path
            for root in request.filesystem_policy.workspace_roots
            if root.mode is LocalWorkspaceAccessMode.READ_WRITE
        )
        if not all(cls._is_within(root, setup_request.writable_roots) for root in writable_roots):
            raise SandboxError(
                "Windows runtime writable root is outside the prepared write authority"
            )
        if not cls._is_within(request.cwd, setup_request.read_roots):
            raise SandboxError("Windows runtime cwd is outside the prepared read authority")

    def _validate(self, request: SandboxedProcessRequest) -> WindowsSandboxSetupRequest:
        if not _runtime_is_windows():
            raise SandboxError("Windows native sandbox is only available on Windows")
        if request.sandbox_profile is not self._profile:
            raise SandboxError("Windows native sandbox profile does not match the session")
        if request.purpose not in _SUPPORTED_PURPOSES:
            raise SandboxError("Windows W3 supports only BASH, background Bash, and MCP stdio")
        if request.stdio_mode not in _SUPPORTED_STDIO:
            raise SandboxError("Windows W3 does not provide an interactive PTY sandbox")
        if request.stdio_mode is LocalProcessStdioMode.PROTOCOL and request.uses_shell:
            raise SandboxError("protocol local processes require an argv-safe executable request")
        if not lifecycle_capability_satisfies(
            self.lifecycle_capability, request.lifecycle.required_capability
        ):
            raise SandboxError("Windows native runtime cannot satisfy the requested lifecycle")
        if not security_capability_satisfies(
            self.security_capabilities, _required_capabilities(self._profile)
        ):
            raise SandboxError("Windows native runtime security capabilities are insufficient")
        if (
            not request.filesystem_policy.private_home
            or not request.filesystem_policy.private_temporary_directory
        ):
            raise SandboxError(
                "Windows enabled profiles require private HOME and temporary storage"
            )
        setup_request = self._setup_request(request)
        if (
            setup_request.installation_root == self._workspace
            or setup_request.installation_root.is_relative_to(self._workspace)
        ):
            raise SandboxError("Windows setup state must remain outside the sandbox workspace")
        self._validate_setup_scope(request, setup_request)
        try:
            WindowsTrustedRunnerProvenance.resolve().assert_disjoint(setup_request.writable_roots)
        except SandboxError:
            raise
        except BaseException as error:
            raise SandboxError("trusted runner provenance is unavailable") from error
        try:
            snapshot = self._setup_authority.inspect(setup_request)
        except BaseException as error:
            raise SandboxError("Windows sandbox setup inspection failed closed") from error
        if snapshot.state is not WindowsSandboxSetupState.READY:
            raise SandboxError(
                "Windows sandbox setup is not READY; runtime never performs setup or UAC"
            )
        return setup_request

    @staticmethod
    def _runner_environment() -> dict[str, str]:
        values: dict[str, str] = {}
        for canonical in _RUNNER_ENVIRONMENT:
            for name, value in os.environ.items():
                if name.casefold() == canonical.casefold():
                    values[canonical] = value
                    break
        if "SystemRoot" not in values:
            raise SandboxError("trusted Windows runner requires SystemRoot")
        values["PYTHONNOUSERSITE"] = "1"
        values["PYTHONUTF8"] = "1"
        values.pop("PYTHONPATH", None)
        return values

    @staticmethod
    def _child_environment(policy: LocalProcessEnvironmentPolicy) -> dict[str, str]:
        """Build the explicit final-child environment allowlist."""

        values = dict(policy.variables)
        forbidden = {"pythonpath", "pythonhome", "pythonuserbase"}
        values = {name: value for name, value in values.items() if name.casefold() not in forbidden}
        for canonical in ("SystemRoot", "SystemDrive", "PATH", "PATHEXT"):
            matching = next(
                (
                    (name, value)
                    for name, value in values.items()
                    if name.casefold() == canonical.casefold()
                ),
                None,
            )
            if matching is not None:
                if matching[0] != canonical:
                    values.pop(matching[0])
                values[canonical] = matching[1]
                continue
            for name, value in os.environ.items():
                if name.casefold() == canonical.casefold():
                    values[canonical] = value
                    break
        if "SystemRoot" not in values:
            raise SandboxError("Windows native child requires SystemRoot")
        values["PYTHONNOUSERSITE"] = "1"
        return values

    def _start_runner(
        self,
        request: SandboxedProcessRequest,
        identity: WindowsRuntimeIdentity,
        setup_request: WindowsSandboxSetupRequest,
        *,
        pty_size: TerminalSize | None = None,
    ) -> tuple[
        WindowsRuntimeControllerTransport,
        RunnerLaunch,
        RuntimeFrame,
        RuntimeFrameDecoder,
        tuple[RuntimeFrame, ...],
    ]:
        _native_acceptance_stage("identity_resolve_start")
        controller_sid = current_user_sid()
        control_server = self._pipe_server_factory(
            peer_sids=(controller_sid, identity.user_sid.value),
            direction=_WindowsNamedPipeDirection.OUTBOUND,
        )
        event_server = self._pipe_server_factory(
            peer_sids=(controller_sid, identity.user_sid.value),
            direction=_WindowsNamedPipeDirection.INBOUND,
        )
        _native_acceptance_stage("pipe_servers_ready")
        launch: RunnerLaunch | None = None
        control: WindowsNamedPipeWriter | None = None
        events: WindowsNamedPipeReader | None = None
        try:
            launch = self._runner_launcher(
                username=identity.username,
                password=identity.password,
                control_pipe_name=control_server.name,
                event_pipe_name=event_server.name,
                environment=self._runner_environment(),
            )
            _native_acceptance_stage("runner_launched")
            control_endpoint, event_endpoint = self._accept_runner_pipes(
                control_server,
                event_server,
                launch.process_handle,
            )
            _native_acceptance_stage("control_connected")
            _native_acceptance_stage("event_connected")
            if not isinstance(control_endpoint, WindowsNamedPipeWriter):
                raise SandboxError("Windows control pipe did not provide a writer endpoint")
            if not isinstance(event_endpoint, WindowsNamedPipeReader):
                raise SandboxError("Windows event pipe did not provide a reader endpoint")
            control, events = control_endpoint, event_endpoint
            control_server.close()
            event_server.close()
            payload: dict[str, object] = {
                "version": PROTOCOL_VERSION,
                "write_sid": identity.write_sid.value,
                # The managed local accounts may not have a loaded Windows
                # profile yet.  The runner uses this fixed W2 identity name
                # only as a fallback for the standard per-user profile root;
                # it is never derived from model-controlled request data.
                "profile_username": identity.username,
                "cwd": str(request.cwd),
                "environment": self._child_environment(request.environment_policy),
                "merge_output": request.stdio_mode is LocalProcessStdioMode.MERGED_CAPTURE,
                "pipe_stdin": request.stdio_mode is LocalProcessStdioMode.PROTOCOL,
                "stdio_mode": request.stdio_mode.value,
                # These fields are trusted adapter composition knobs for the
                # native acceptance probes only.  They are not part of the
                # model-facing request or any public sandbox configuration.
                "desktop_mode": self._diagnostic_desktop_mode.value,
                "create_no_window": self._diagnostic_create_no_window,
            }
            if pty_size is not None:
                if request.stdio_mode is not LocalProcessStdioMode.PTY:
                    raise SandboxError("Windows PTY candidate requires PTY stdio")
                payload.update(
                    {
                        "terminal_mode": "pty",
                        "columns": pty_size.columns,
                        "rows": pty_size.rows,
                        "create_no_window": False,
                    }
                )
            if request.uses_shell:
                payload["shell_command"] = request.shell_command
            else:
                payload["executable"] = request.executable
                payload["arguments"] = list(request.arguments)
            control.write(encode_frame(RuntimeFrameType.SPAWN_REQUEST, encode_json(payload)))
            _native_acceptance_stage("spawn_request_sent")
            decoder = RuntimeFrameDecoder()
            while True:
                data = events.read_for_runner(launch.process_handle)
                _native_acceptance_stage("event_read")
                if not data:
                    decoder.finish()
                    raise SandboxError("trusted Windows runner exited before SpawnReady")
                frames = decoder.feed(data)
                for index, frame in enumerate(frames):
                    validate_channel_frame(RuntimeChannel.EVENT, frame.kind)
                    if frame.kind is RuntimeFrameType.SPAWN_READY:
                        # A fast child can produce stdout and Exit before the
                        # controller's first pipe read completes.  Preserve
                        # every frame after SpawnReady for the owned-process
                        # reader instead of dropping coalesced data.
                        return (
                            WindowsRuntimeControllerTransport(control=control, events=events),
                            launch,
                            frame,
                            decoder,
                            frames[index + 1 :],
                        )
                    if frame.kind is RuntimeFrameType.ERROR:
                        # The runner deliberately sends only a bounded, safe
                        # operation/error diagnostic.  Preserve it here so a
                        # native acceptance failure identifies the rejected
                        # Win32 boundary instead of collapsing into a generic
                        # protocol error.  Credentials, command arguments,
                        # token contents, and paths are never included by the
                        # runner's error payload.
                        detail = ""
                        with contextlib.suppress(BaseException):
                            error_payload = decode_json(frame.payload)
                            if isinstance(error_payload, dict):
                                message = error_payload.get("message")
                                if isinstance(message, str) and message:
                                    detail = f": {message[:512]}"
                        raise SandboxError(
                            f"trusted Windows runner rejected the child request{detail}"
                        )
        except BaseException:
            control_server.close()
            event_server.close()
            if control is not None:
                control.close()
            if events is not None:
                events.close()
            if launch is not None:
                with contextlib.suppress(BaseException):
                    _terminate_runner_process(launch.process_handle)
                with contextlib.suppress(BaseException):
                    close_runner_process(launch.process_handle)
            raise

    @staticmethod
    def _accept_runner_pipes(
        control_server: WindowsNamedPipeServer,
        event_server: WindowsNamedPipeServer,
        runner_handle: int,
    ) -> tuple[
        WindowsNamedPipeReader | WindowsNamedPipeWriter,
        WindowsNamedPipeReader | WindowsNamedPipeWriter,
    ]:
        """Accept both synchronous pipe clients without handshake ordering."""

        results: dict[str, WindowsNamedPipeReader | WindowsNamedPipeWriter] = {}
        failures: list[BaseException] = []
        completed = threading.Event()

        def accept(label: str, server: WindowsNamedPipeServer) -> None:
            try:
                results[label] = server.accept_for_runner(runner_handle)
                _native_acceptance_stage(f"{label}_accept_returned")
            except BaseException as error:
                _native_acceptance_stage(
                    f"{label}_accept_error:{type(error).__name__}:{str(error)[:160]}"
                )
                failures.append(error)
            finally:
                completed.set()

        threads = [
            threading.Thread(
                target=accept,
                args=("control", control_server),
                name="neuro-code-windows-control-accept",
                daemon=True,
            ),
            threading.Thread(
                target=accept,
                args=("event", event_server),
                name="neuro-code-windows-event-accept",
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 35.0
        while len(results) < 2 and not failures:
            if time.monotonic() >= deadline:
                control_server.close()
                event_server.close()
                raise SandboxError("trusted Windows runner did not connect to both named pipes")
            completed.wait(0.05)
        if failures:
            control_server.close()
            event_server.close()
            raise failures[0]
        control_endpoint = results.get("control")
        event_endpoint = results.get("event")
        if control_endpoint is None or event_endpoint is None:
            raise SandboxError("trusted Windows named-pipe accept returned incomplete endpoints")
        return control_endpoint, event_endpoint

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess:
        setup_request = self._validate(request)
        kind = (
            WindowsSandboxIdentityKind.OFFLINE
            if request.network_policy is LocalProcessNetworkPolicy.ISOLATED
            else WindowsSandboxIdentityKind.ONLINE
        )
        identity = self._identity_provider.resolve(setup_request, kind)
        _native_acceptance_stage("identity_resolved")
        transport, launch, ready, decoder, pending_frames = await run_blocking(
            self._start_runner, request, identity, setup_request
        )
        _native_acceptance_stage("start_runner_returned")
        payload = decode_json(ready.payload)
        if not isinstance(payload, dict) or payload.get("version") != PROTOCOL_VERSION:
            transport.control.close()
            transport.events.close()
            raise SandboxError("trusted Windows runner returned an invalid SpawnReady frame")
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            transport.control.close()
            transport.events.close()
            raise SandboxError("trusted Windows runner returned an invalid child PID")
        security_attestation = _validated_child_token_attestation(
            payload.get("security"), expected_write_sid=identity.write_sid
        )
        return _WindowsNativeOwnedLocalProcess(
            transport=transport,
            runner=launch,
            pid=pid,
            request=request,
            decoder=decoder,
            initial_frames=pending_frames,
            security_attestation=security_attestation,
        )

    def spawn_terminal(
        self,
        request: SandboxedProcessRequest,
        *,
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession:
        del request, size, on_output, on_eof, on_error
        raise SandboxError("Windows W3 does not provide a sandboxed interactive terminal; use W4")

    def _validate_terminal_candidate(
        self, request: SandboxedProcessRequest
    ) -> WindowsSandboxSetupRequest:
        """Validate the private Gate 1 W4 candidate without changing public routing."""

        if not _runtime_is_windows():
            raise SandboxError("Windows native sandbox is only available on Windows")
        if request.sandbox_profile is not self._profile:
            raise SandboxError("Windows native sandbox profile does not match the session")
        if request.purpose is not LocalProcessPurpose.INTERACTIVE_TERMINAL:
            raise SandboxError("Windows W4 requires interactive-terminal purpose")
        if request.stdio_mode is not LocalProcessStdioMode.PTY:
            raise SandboxError("Windows W4 requires PTY stdio")
        if request.uses_shell:
            raise SandboxError("Windows W4 requires an argv-safe executable")
        if not lifecycle_capability_satisfies(
            self.lifecycle_capability, request.lifecycle.required_capability
        ):
            raise SandboxError("Windows W4 cannot satisfy the requested lifecycle")
        if not security_capability_satisfies(
            self.security_capabilities, _required_capabilities(self._profile)
        ):
            raise SandboxError("Windows W4 security capabilities are insufficient")
        if (
            not request.filesystem_policy.private_home
            or not request.filesystem_policy.private_temporary_directory
        ):
            raise SandboxError(
                "Windows enabled profiles require private HOME and temporary storage"
            )
        setup_request = self._setup_request(request)
        if (
            setup_request.installation_root == self._workspace
            or setup_request.installation_root.is_relative_to(self._workspace)
        ):
            raise SandboxError("Windows setup state must remain outside the sandbox workspace")
        self._validate_setup_scope(request, setup_request)
        try:
            WindowsTrustedRunnerProvenance.resolve().assert_disjoint(setup_request.writable_roots)
        except SandboxError:
            raise
        except BaseException as error:
            raise SandboxError("trusted runner provenance is unavailable") from error
        try:
            snapshot = self._setup_authority.inspect(setup_request)
        except BaseException as error:
            raise SandboxError("Windows sandbox setup inspection failed closed") from error
        if snapshot.state is not WindowsSandboxSetupState.READY:
            raise SandboxError("Windows sandbox setup is not READY; W4 never performs setup")
        return setup_request

    def _spawn_terminal_candidate(
        self,
        request: SandboxedProcessRequest,
        *,
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession:
        """Start the focused W4 Gate 1 candidate; public PTY remains fail closed."""

        if not isinstance(size, TerminalSize):
            raise TypeError("size must be a TerminalSize")
        setup_request = self._validate_terminal_candidate(request)
        kind = (
            WindowsSandboxIdentityKind.OFFLINE
            if request.network_policy is LocalProcessNetworkPolicy.ISOLATED
            else WindowsSandboxIdentityKind.ONLINE
        )
        identity = self._identity_provider.resolve(setup_request, kind)
        transport, launch, ready, decoder, pending_frames = self._start_runner(
            request,
            identity,
            setup_request,
            pty_size=size,
        )
        payload = decode_json(ready.payload)
        if not isinstance(payload, dict) or payload.get("version") != PROTOCOL_VERSION:
            transport.control.close()
            transport.events.close()
            close_runner_process(launch.process_handle)
            raise SandboxError("trusted Windows runner returned an invalid SpawnReady frame")
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            transport.control.close()
            transport.events.close()
            close_runner_process(launch.process_handle)
            raise SandboxError("trusted Windows runner returned an invalid child PID")
        security_attestation = _validated_child_token_attestation(
            payload.get("security"), expected_write_sid=identity.write_sid
        )
        return _WindowsNativePtySession(
            transport=transport,
            runner=launch,
            pid=pid,
            size=size,
            on_output=on_output,
            on_eof=on_eof,
            on_error=on_error,
            decoder=decoder,
            initial_frames=pending_frames,
            security_attestation=security_attestation,
        )


class _WindowsNativePtySession:
    """Controller-side projection of the private W4 PTY candidate."""

    def __init__(
        self,
        *,
        transport: WindowsRuntimeControllerTransport,
        runner: RunnerLaunch,
        pid: int,
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
        decoder: RuntimeFrameDecoder,
        initial_frames: tuple[RuntimeFrame, ...],
        security_attestation: dict[str, object],
    ) -> None:
        self._control = transport.control
        self._events = transport.events
        self._runner = runner
        self._pid = pid
        self._size = size
        self._on_output = on_output
        self._on_eof = on_eof
        self._on_error = on_error
        self._decoder = decoder
        self._initial_frames = initial_frames
        self._security_attestation = dict(security_attestation)
        self._returncode: int | None = None
        self._error: BaseException | None = None
        self._done = threading.Event()
        self._closed = False
        self._eof_sent = False
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._read_frames,
            name=f"neuro-code-windows-pty-{pid}",
            daemon=True,
        )
        self._reader.start()

    @property
    def process_id(self) -> int:
        return self._pid

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP

    @property
    def size(self) -> TerminalSize:
        return self._size

    def _read_frames(self) -> None:
        try:
            for frame in self._initial_frames:
                self._handle_frame(frame)
                if frame.kind is RuntimeFrameType.EXIT:
                    return
            self._initial_frames = ()
            while True:
                data = self._events.read()
                if not data:
                    self._decoder.finish()
                    if self._returncode is None:
                        raise SandboxError("trusted Windows PTY runner disconnected before Exit")
                    return
                for frame in self._decoder.feed(data):
                    validate_channel_frame(RuntimeChannel.EVENT, frame.kind)
                    self._handle_frame(frame)
                    if frame.kind is RuntimeFrameType.EXIT:
                        return
        except BaseException as error:
            self._error = error
            if not self._closed:
                with contextlib.suppress(BaseException):
                    self._on_error(error)
        finally:
            self._closed = True
            self._control.close()
            wait_runner = getattr(self._events, "wait_process", None)
            if callable(wait_runner) and os.name == "nt":
                with contextlib.suppress(BaseException):
                    result = wait_runner(
                        self._runner.process_handle,
                        timeout_seconds=2.0,
                        active_state="RUNNER_STILL_ACTIVE",
                        exited_state="RUNNER_EXITED",
                    )
                    if result.get("state") == "RUNNER_STILL_ACTIVE":
                        _terminate_runner_process(self._runner.process_handle)
            self._events.close()
            with contextlib.suppress(BaseException):
                close_runner_process(self._runner.process_handle)
            self._done.set()

    def _handle_frame(self, frame: RuntimeFrame) -> None:
        if frame.kind is RuntimeFrameType.PTY_OUTPUT:
            self._on_output(frame.payload)
            return
        if frame.kind is RuntimeFrameType.EXIT:
            payload = decode_json(frame.payload)
            if not isinstance(payload, dict) or not isinstance(payload.get("returncode"), int):
                raise SandboxError("Windows PTY Exit frame is invalid")
            self._returncode = int(payload["returncode"])
            if not self._eof_sent:
                self._eof_sent = True
                self._on_eof()
            return
        if frame.kind is RuntimeFrameType.ERROR:
            payload = decode_json(frame.payload)
            if not isinstance(payload, dict):
                raise SandboxError("trusted Windows PTY runner reported an invalid error")
            message = payload.get("message")
            detail = message[:512] if isinstance(message, str) and message else "runtime error"
            raise SandboxError(f"trusted Windows PTY runner reported a runtime error: {detail}")
        raise SandboxError("Windows PTY event frame is invalid")

    def write(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("terminal input data must be bytes")
        if not data:
            return
        with self._write_lock:
            if self._closed:
                raise RuntimeError("Windows PTY session is closed")
            self._control.write(encode_frame(RuntimeFrameType.STDIN, data))

    def resize(self, size: TerminalSize) -> None:
        if not isinstance(size, TerminalSize):
            raise TypeError("size must be a TerminalSize")
        with self._write_lock:
            if self._closed:
                raise RuntimeError("Windows PTY session is closed")
            self._control.write(
                encode_frame(
                    RuntimeFrameType.RESIZE,
                    encode_json(
                        {
                            "version": PROTOCOL_VERSION,
                            "columns": size.columns,
                            "rows": size.rows,
                        }
                    ),
                )
            )
            self._size = size

    def send_signal(self, signal: TerminalSignal) -> None:
        if not isinstance(signal, TerminalSignal):
            raise TypeError("signal must be a TerminalSignal")
        if signal is TerminalSignal.INTERRUPT:
            self.write(b"\x03")
            return
        with self._write_lock:
            if self._closed:
                return
            self._control.write(encode_frame(RuntimeFrameType.TERMINATE))

    def poll_exit(self) -> int | None:
        return self._returncode

    def close(self) -> None:
        if not self._closed:
            with contextlib.suppress(BaseException):
                self.send_signal(TerminalSignal.TERMINATE)
            if not self._done.wait(5.0):
                _terminate_runner_process(self._runner.process_handle)
                self._control.close()
                self._events.close()
                self._done.wait(2.0)
        self._reader.join(timeout=1.0)


class _WindowsNativeOwnedLocalProcess(OwnedLocalProcess):
    def __init__(
        self,
        *,
        transport: WindowsRuntimeControllerTransport,
        runner: RunnerLaunch,
        pid: int,
        request: SandboxedProcessRequest,
        decoder: RuntimeFrameDecoder | None = None,
        initial_frames: tuple[RuntimeFrame, ...] = (),
        security_attestation: dict[str, object] | None = None,
    ) -> None:
        self._control = transport.control
        self._events = transport.events
        self._runner = runner
        self._request = request
        self._loop = asyncio.get_running_loop()
        self._stdout = asyncio.StreamReader()
        self._stderr = (
            None
            if request.stdio_mode is LocalProcessStdioMode.MERGED_CAPTURE
            else asyncio.StreamReader()
        )
        self._returncode: int | None = None
        self._error: BaseException | None = None
        self._done = asyncio.Event()
        self._stdin_lock = asyncio.Lock()
        self._closed = False
        self._force_closed = False
        self._diagnostic: dict[str, object] | None = None
        self._cleanup_error: str | None = None
        self._decoder = decoder or RuntimeFrameDecoder()
        self._initial_frames = initial_frames
        self._security_attestation = dict(security_attestation or {})
        self._reader = threading.Thread(
            target=self._read_frames,
            name=f"neuro-code-windows-runtime-{pid}",
            daemon=True,
        )
        self._reader.start()
        self._process_id = pid

    @property
    def process_id(self) -> int:
        return self._process_id

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP

    @property
    def stdout(self) -> LocalProcessOutput | None:
        return self._stdout

    @property
    def stderr(self) -> LocalProcessOutput | None:
        return self._stderr

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def _read_frames(self) -> None:
        try:
            for frame in self._initial_frames:
                self._handle_frame(frame)
                if frame.kind is RuntimeFrameType.EXIT:
                    return
            self._initial_frames = ()
            while True:
                data = self._events.read()
                if not data:
                    if self._returncode is None:
                        self._diagnostic = self._pipe_eof_diagnostic()
                        with contextlib.suppress(BaseException):
                            self._decoder.finish()
                        raise SandboxError(
                            "trusted Windows runner disconnected before Exit "
                            + json.dumps(self._diagnostic, sort_keys=True)
                        )
                    self._decoder.finish()
                    break
                for frame in self._decoder.feed(data):
                    self._handle_frame(frame)
                    if frame.kind is RuntimeFrameType.EXIT:
                        return
        except BaseException as error:
            if not self._force_closed:
                self._error = error
                self._call(self._stdout.set_exception, error)
                if self._stderr is not None:
                    self._call(self._stderr.set_exception, error)
        finally:
            self._closed = True
            # Exit is an event from runner to controller.  Closing control
            # first is what releases the runner's blocking control ReadFile;
            # only after its bounded final wait do we close the event reader.
            self._control.close()
            runner_final: dict[str, object] | None = None
            if os.name == "nt":
                wait_runner = getattr(self._events, "wait_process", None)
                if callable(wait_runner):
                    with contextlib.suppress(BaseException):
                        runner_final = wait_runner(
                            self._runner.process_handle,
                            timeout_seconds=2.0,
                            active_state="RUNNER_STILL_ACTIVE",
                            exited_state="RUNNER_EXITED",
                        )
                        if runner_final.get("state") == "RUNNER_STILL_ACTIVE":
                            _terminate_runner_process(self._runner.process_handle)
                            runner_final = wait_runner(
                                self._runner.process_handle,
                                timeout_seconds=1.0,
                                active_state="RUNNER_STILL_ACTIVE",
                                exited_state="RUNNER_EXITED",
                            )
            if runner_final is not None:
                diagnostic = dict(self._diagnostic or {})
                diagnostic["runner"] = runner_final
                diagnostic["control_pipe"] = "CLOSED_AFTER_EXIT"
                diagnostic["event_pipe"] = "CLOSED_AFTER_RUNNER_EXIT"
                self._diagnostic = diagnostic
            self._events.close()
            with contextlib.suppress(BaseException):
                close_runner_process(self._runner.process_handle)
            self._call(self._done.set)

    def _pipe_eof_diagnostic(self) -> dict[str, object]:
        return {
            "pipe": "EVENT_PIPE_BROKEN",
            "pipe_error": self._events.last_read_error,
            "runner": self._events.observe_process(
                self._runner.process_handle,
                active_state="RUNNER_STILL_ACTIVE",
                exited_state="RUNNER_EXITED",
            ),
            "child": observe_process_id(self._process_id),
            "security_attestation": dict(self._security_attestation),
        }

    def diagnostic_snapshot(self) -> dict[str, object] | None:
        """Return the last safe native lifecycle diagnostic, if available."""

        if self._diagnostic is not None:
            return dict(self._diagnostic)
        runner: dict[str, object] | None = None
        with contextlib.suppress(BaseException):
            runner = self._events.observe_process(
                self._runner.process_handle,
                active_state="RUNNER_STILL_ACTIVE",
                exited_state="RUNNER_EXITED",
            )
        if runner is None:
            runner = {"state": "WAIT_FAILED", "wait_error": "HANDLE_CLOSED"}
        return {
            "pipe": "EVENT_PIPE_BROKEN" if self._closed and self._returncode is None else None,
            "runner": runner,
            "child": observe_process_id(self._process_id),
            "security_attestation": dict(self._security_attestation),
            "cleanup_error": self._cleanup_error,
        }

    def _handle_frame(self, frame: RuntimeFrame) -> None:
        if frame.kind is RuntimeFrameType.STDOUT:
            self._call(self._stdout.feed_data, frame.payload)
        elif frame.kind is RuntimeFrameType.STDERR:
            if self._stderr is not None:
                self._call(self._stderr.feed_data, frame.payload)
        elif frame.kind is RuntimeFrameType.PTY_OUTPUT:
            raise SandboxError("Windows W3 process received a PTY output frame")
        elif frame.kind is RuntimeFrameType.EXIT:
            payload = decode_json(frame.payload)
            if not isinstance(payload, dict) or not isinstance(payload.get("returncode"), int):
                raise SandboxError("Windows runtime Exit frame is invalid")
            self._returncode = int(payload["returncode"])
            child_diagnostic = payload.get("child")
            termination_observation = payload.get("termination_observation")
            if isinstance(child_diagnostic, dict) or isinstance(termination_observation, dict):
                self._diagnostic = {
                    "pipe": None,
                    "child": child_diagnostic,
                    "termination_observation": termination_observation,
                    "security_attestation": dict(self._security_attestation),
                }
            self._call(self._stdout.feed_eof)
            if self._stderr is not None:
                self._call(self._stderr.feed_eof)
        elif frame.kind is RuntimeFrameType.ERROR:
            payload = decode_json(frame.payload)
            if not isinstance(payload, dict):
                raise SandboxError("trusted Windows runner reported a runtime error")
            self._diagnostic = {
                "pipe": "EVENT_PIPE_BROKEN",
                "runner": {"state": "RUNNER_EXITED"},
                "child": payload.get("child"),
                "security_attestation": dict(self._security_attestation),
            }
            message = payload.get("message")
            detail = message[:512] if isinstance(message, str) and message else "runtime error"
            raise SandboxError(f"trusted Windows runner reported a runtime error: {detail}")

    def _call(self, callback: Callable[..., object], *args: object) -> None:
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(callback, *args)

    async def write_stdin(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("process stdin data must be bytes")
        if not data:
            return
        async with self._stdin_lock:
            if self._closed:
                raise RuntimeError("Windows runtime process is closed")
            await run_blocking(
                self._control.write,
                encode_frame(RuntimeFrameType.STDIN, data),
            )

    async def close_stdin(self) -> None:
        async with self._stdin_lock:
            if self._closed:
                return
            await run_blocking(
                self._control.write,
                encode_frame(RuntimeFrameType.CLOSE_STDIN),
            )

    async def wait(self) -> int:
        await self._done.wait()
        if self._error is not None:
            raise self._error
        if self._returncode is None:
            raise SandboxError("Windows runtime ended without an exit code")
        return self._returncode

    async def terminate(self, *, grace_seconds: float | None = None) -> None:
        if self._returncode is not None:
            return
        timeout = (
            self._request.lifecycle.termination_grace_seconds
            if grace_seconds is None
            else grace_seconds
        )
        # A synchronous named-pipe write can remain in a native ReadFile/WriteFile
        # call after the remote runner has stopped servicing the pipe.  Shield
        # the executor task so its cancellation cannot hold the event loop, then
        # use the runner's Job Object as the bounded force boundary below.
        send_task = asyncio.create_task(
            run_blocking(self._control.write, encode_frame(RuntimeFrameType.TERMINATE))
        )
        try:
            await asyncio.wait_for(asyncio.shield(send_task), timeout=max(0.1, timeout))
        except BaseException:
            pass
        finally:
            self._consume_send_task(send_task)
        try:
            await asyncio.wait_for(
                self.wait(), timeout=timeout + self._request.lifecycle.force_wait_seconds
            )
        except (TimeoutError, SandboxError):
            self._diagnostic = self.diagnostic_snapshot()
            _terminate_runner_process(self._runner.process_handle)
            self._events.close()
            self._control.close()
            try:
                await asyncio.wait_for(self.wait(), timeout=2.0)
            except (TimeoutError, SandboxError) as error:
                self._force_close(error)

    def _consume_send_task(self, task: asyncio.Task[object]) -> None:
        if not task.done():
            task.add_done_callback(self._consume_send_task)
            return
        try:
            task.result()
        except BaseException as error:
            self._cleanup_error = str(error)[:256]
            if self._diagnostic is not None:
                self._diagnostic["cleanup_error"] = self._cleanup_error
        else:
            self._cleanup_error = None

    def _force_close(self, error: BaseException | None = None) -> None:
        """Bound controller cancellation if a native pipe reader is stuck."""

        if self._done.is_set():
            return
        self._force_closed = True
        self._returncode = 1
        self._error = error
        self._closed = True
        self._call(self._stdout.feed_eof)
        if self._stderr is not None:
            self._call(self._stderr.feed_eof)
        self._events.close()
        self._control.close()
        self._call(self._done.set)


def _terminate_runner_process(handle: int) -> None:
    if os.name != "nt":
        return
    loader = getattr(__import__("ctypes"), "WinDLL", None)
    if loader is None:
        return
    kernel32 = loader("kernel32.dll", use_last_error=True)
    terminate = kernel32.TerminateProcess
    terminate.argtypes = [__import__("ctypes").c_void_p, __import__("ctypes").c_uint32]
    terminate.restype = __import__("ctypes").c_int32
    terminate(handle, 1)


__all__ = [
    "DpapiWindowsRuntimeIdentityProvider",
    "WindowsNativeLocalProcessSandbox",
    "WindowsRuntimeControllerTransport",
    "WindowsRuntimeIdentity",
    "WindowsRuntimeIdentityProvider",
]
