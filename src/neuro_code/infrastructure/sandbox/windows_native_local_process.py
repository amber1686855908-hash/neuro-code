"""Windows non-PTY native sandbox runtime adapter (W3).

The adapter owns only runtime orchestration.  W2 remains the authority for
setup/repair and persistent state.  A spawn is rejected unless W2 reports
``READY``; no runtime path performs UAC, Firewall mutation, or ACL repair.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
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
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    RunnerLaunch,
    WindowsNamedPipe,
    WindowsNamedPipeServer,
    close_runner_process,
    current_user_sid,
    launch_runner,
)
from neuro_code.infrastructure.sandbox.windows_native_runtime_protocol import (
    PROTOCOL_VERSION,
    RuntimeFrame,
    RuntimeFrameDecoder,
    RuntimeFrameType,
    decode_json,
    encode_frame,
    encode_json,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import WindowsAccountSid
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import (
    WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES,
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


@dataclass(frozen=True, slots=True)
class WindowsRuntimeIdentity:
    """The minimum W2 facts needed to launch one child; password is transient."""

    kind: WindowsSandboxIdentityKind
    username: str
    password: str
    user_sid: WindowsAccountSid
    write_sid: SyntheticWindowsSid


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

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP

    @property
    def security_capabilities(self) -> LocalProcessSecurityCapabilities:
        """Capabilities provided by the fully wired native W3 backend."""

        return WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES

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
        if os.name != "nt":
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
            snapshot = self._setup_authority.inspect(setup_request)
        except BaseException as error:
            raise SandboxError("Windows sandbox setup inspection failed closed") from error
        if snapshot.state is not WindowsSandboxSetupState.READY:
            raise SandboxError(
                "Windows sandbox setup is not READY; runtime never performs setup or UAC"
            )
        trusted_paths = (Path(__file__).resolve(), Path(sys.executable).resolve())
        if any(
            not path.is_file()
            or any(
                path == root.path or path.is_relative_to(root.path)
                for root in request.filesystem_policy.workspace_roots
            )
            for path in trusted_paths
        ):
            raise SandboxError("trusted Windows runner must remain outside model workspaces")
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
    ) -> tuple[
        WindowsNamedPipe,
        RunnerLaunch,
        RuntimeFrame,
        RuntimeFrameDecoder,
        tuple[RuntimeFrame, ...],
    ]:
        controller_sid = current_user_sid()
        server = self._pipe_server_factory(
            peer_sids=(controller_sid, identity.user_sid.value),
        )
        launch: RunnerLaunch | None = None
        pipe: WindowsNamedPipe | None = None
        try:
            launch = self._runner_launcher(
                username=identity.username,
                password=identity.password,
                pipe_name=server.name,
                environment=self._runner_environment(),
            )
            pipe = server.accept_for_runner(launch.process_handle)
            server.close()
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
            }
            if request.uses_shell:
                payload["shell_command"] = request.shell_command
            else:
                payload["executable"] = request.executable
                payload["arguments"] = list(request.arguments)
            pipe.write(encode_frame(RuntimeFrameType.SPAWN_REQUEST, encode_json(payload)))
            decoder = RuntimeFrameDecoder()
            while True:
                data = pipe.read_for_runner(launch.process_handle)
                if not data:
                    decoder.finish()
                    raise SandboxError("trusted Windows runner exited before SpawnReady")
                frames = decoder.feed(data)
                for index, frame in enumerate(frames):
                    if frame.kind is RuntimeFrameType.SPAWN_READY:
                        # A fast child can produce stdout and Exit before the
                        # controller's first pipe read completes.  Preserve
                        # every frame after SpawnReady for the owned-process
                        # reader instead of dropping coalesced data.
                        return pipe, launch, frame, decoder, frames[index + 1 :]
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
            server.close()
            if pipe is not None:
                pipe.close()
            if launch is not None:
                with contextlib.suppress(BaseException):
                    _terminate_runner_process(launch.process_handle)
                with contextlib.suppress(BaseException):
                    close_runner_process(launch.process_handle)
            raise

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess:
        setup_request = self._validate(request)
        kind = (
            WindowsSandboxIdentityKind.OFFLINE
            if request.network_policy is LocalProcessNetworkPolicy.ISOLATED
            else WindowsSandboxIdentityKind.ONLINE
        )
        identity = self._identity_provider.resolve(setup_request, kind)
        pipe, launch, ready, decoder, pending_frames = await run_blocking(
            self._start_runner, request, identity, setup_request
        )
        payload = decode_json(ready.payload)
        if not isinstance(payload, dict) or payload.get("version") != PROTOCOL_VERSION:
            pipe.close()
            raise SandboxError("trusted Windows runner returned an invalid SpawnReady frame")
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            pipe.close()
            raise SandboxError("trusted Windows runner returned an invalid child PID")
        return _WindowsNativeOwnedLocalProcess(
            pipe=pipe,
            runner=launch,
            pid=pid,
            request=request,
            decoder=decoder,
            initial_frames=pending_frames,
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


class _WindowsNativeOwnedLocalProcess(OwnedLocalProcess):
    def __init__(
        self,
        *,
        pipe: WindowsNamedPipe,
        runner: RunnerLaunch,
        pid: int,
        request: SandboxedProcessRequest,
        decoder: RuntimeFrameDecoder | None = None,
        initial_frames: tuple[RuntimeFrame, ...] = (),
    ) -> None:
        self._pipe = pipe
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
        self._decoder = decoder or RuntimeFrameDecoder()
        self._initial_frames = initial_frames
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
                data = self._pipe.read()
                if not data:
                    self._decoder.finish()
                    if self._returncode is None:
                        raise SandboxError("trusted Windows runner disconnected before Exit")
                    break
                for frame in self._decoder.feed(data):
                    self._handle_frame(frame)
                    if frame.kind is RuntimeFrameType.EXIT:
                        return
        except BaseException as error:
            self._error = error
            self._call(self._stdout.set_exception, error)
            if self._stderr is not None:
                self._call(self._stderr.set_exception, error)
        finally:
            self._closed = True
            self._call(self._done.set)
            self._pipe.close()
            with contextlib.suppress(BaseException):
                close_runner_process(self._runner.process_handle)

    def _handle_frame(self, frame: RuntimeFrame) -> None:
        if frame.kind is RuntimeFrameType.STDOUT:
            self._call(self._stdout.feed_data, frame.payload)
        elif frame.kind is RuntimeFrameType.STDERR:
            if self._stderr is not None:
                self._call(self._stderr.feed_data, frame.payload)
        elif frame.kind is RuntimeFrameType.EXIT:
            payload = decode_json(frame.payload)
            if not isinstance(payload, dict) or not isinstance(payload.get("returncode"), int):
                raise SandboxError("Windows runtime Exit frame is invalid")
            self._returncode = int(payload["returncode"])
            self._call(self._stdout.feed_eof)
            if self._stderr is not None:
                self._call(self._stderr.feed_eof)
        elif frame.kind is RuntimeFrameType.ERROR:
            raise SandboxError("trusted Windows runner reported a runtime error")

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
                self._pipe.write,
                encode_frame(RuntimeFrameType.STDIN, data),
            )

    async def close_stdin(self) -> None:
        async with self._stdin_lock:
            if self._closed:
                return
            await run_blocking(
                self._pipe.write,
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
        await run_blocking(self._pipe.write, encode_frame(RuntimeFrameType.TERMINATE))
        try:
            await asyncio.wait_for(
                self.wait(), timeout=timeout + self._request.lifecycle.force_wait_seconds
            )
        except (TimeoutError, SandboxError):
            _terminate_runner_process(self._runner.process_handle)
            await self.wait()


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
    "WindowsRuntimeIdentity",
    "WindowsRuntimeIdentityProvider",
]
