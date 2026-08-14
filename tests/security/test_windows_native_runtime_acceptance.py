"""Focused W3 Gate 1 and Gate 2 acceptance for the Windows native runtime.

Gate 2 intentionally uses ``cmd.exe`` rather than Python.  Every operation is
an actual ``CreateProcessAsUserW`` final child and therefore passes the same
post-create token attestation before ``SpawnReady``.  The fixture uses only
fixed non-secret sentinel content; command output is drained but never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import shutil
import socket
import subprocess
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir, mkdtemp
from typing import Any, cast

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessSecurityStrength,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    OwnedLocalProcess,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    _WindowsNativeDesktopMode,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    SANDBOX_OFFLINE_USERNAME,
    SANDBOX_ONLINE_USERNAME,
    WindowsAccountSid,
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    WRITE_ACCESS_MASK,
    WRITE_ONLY_ACCESS_MASK,
    WindowsManagedAce,
    WindowsManagedAceKind,
    _AceHeader,
    _NativeWindowsAclApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import (
    WindowsFirewallRule,
    _NativeWindowsFirewallApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _InstallationRecord,
    _NativeWindowsSetupPrivilegeApi,
)

_SENTINEL = "GATE2_NON_SECRET_SENTINEL"
_APPENDED = "GATE2_APPEND_SENTINEL"
_OVERWRITTEN = "GATE2_OVERWRITE_SENTINEL"
_NUL_CANARY_COMMAND = ("echo", "GATE2_NUL_CANARY>NUL")
_BENIGN_OUTBOUND_PROBE = ("1.1.1.1", 80)
_WINSOCK_MARKER = "W3_WINSOCK="
_STDIO_CAPTURE_STDOUT_LENGTH = 131329
_STDIO_CAPTURE_STDERR_LENGTH = 131331
_STDIO_MERGED_LENGTHS = (32771, 32773, 32779, 32783)
_STDIO_MERGED_VARIANTS = (10, 11, 12, 13)
_STDIO_PROTOCOL_LARGE_LENGTH = 96 * 1024 + 257
_STDIO_SPECIAL = bytes(
    (0x00, 0x0D, 0x0A, 0x0D, 0x0A, 0xE2, 0x82, 0xAC, 0xF0, 0x9F, 0x98, 0x80, 0xFF)
)
_STDIO_CAPTURE_STDOUT_TRAILER = b"G4_CAPTURE_STDOUT_TRAILER\x00\x0d\x0a\xff"
_STDIO_CAPTURE_STDERR_TRAILER = b"G4_CAPTURE_STDERR_TRAILER\x00\x0a\x0d\xff"
_STDIO_MERGED_TRAILERS = (
    b"G4_MERGED_A\x00\x0d\x0a",
    b"G4_MERGED_B\x00\x0a\x0d",
    b"G4_MERGED_C\xff\x0d\x0a",
    b"G4_MERGED_D\xe2\x82\xac\x0a",
)
_STDIO_PROTOCOL_DIAGNOSTIC = b"G4_PROTOCOL_DIAGNOSTIC\x00\x0d\x0a"
_STDIO_NONZERO_STDOUT = b"G4_NONZERO_STDOUT\x00\x0d\x0a\xff"
_STDIO_NONZERO_STDERR = b"G4_NONZERO_STDERR\x00\x0a\x0d\xff"
ProbeState = Callable[[], dict[str, object]]
Command = tuple[str, ...]


class _NativeProbeBuildError(RuntimeError):
    """The trusted Windows controller could not build an acceptance probe."""


def _winsock_probe_source() -> Path:
    source = Path(__file__).with_name("windows_winsock_probe.c").resolve(strict=False)
    if not source.is_file():
        raise _NativeProbeBuildError("Winsock probe source is unavailable")
    return source


def _find_vswhere() -> Path:
    candidates: list[Path] = []
    discovered = shutil.which("vswhere.exe")
    if discovered:
        candidates.append(Path(discovered))
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    if program_files_x86:
        candidates.append(
            Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise _NativeProbeBuildError("vswhere.exe is unavailable")


def _compile_msvc_probe(source: Path, output_stem: str) -> Path:  # pragma: no cover - Windows CI
    """Build one acceptance-only native probe with the runner's selected MSVC toolchain."""

    vswhere = _find_vswhere()
    try:
        discovery = subprocess.run(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationPath",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise _NativeProbeBuildError("vswhere discovery failed") from error
    if discovery.returncode != 0:
        raise _NativeProbeBuildError("vswhere did not find an MSVC installation")
    installation_path = next(
        (Path(line.strip()) for line in discovery.stdout.splitlines() if line.strip()),
        None,
    )
    if installation_path is None:
        raise _NativeProbeBuildError("vswhere returned no installation path")
    vcvars = installation_path / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise _NativeProbeBuildError("vcvars64.bat is unavailable")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", gettempdir()))
    build_directory = Path(mkdtemp(prefix=f"neuro-code-{output_stem}-", dir=runner_temp))
    output = build_directory / f"{output_stem}.exe"
    build_script = build_directory / "build_probe.cmd"
    build_script.write_text(
        "@echo off\r\n"
        f'call "{vcvars}"\r\n'
        "if errorlevel 1 exit /b 1\r\n"
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}" Ws2_32.lib\r\n',
        encoding="ascii",
    )
    try:
        build = subprocess.run(
            ["cmd.exe", "/d", "/c", build_script.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
            cwd=str(build_directory),
        )
    except (OSError, subprocess.SubprocessError) as error:
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _NativeProbeBuildError("MSVC probe build failed") from error
    if build.returncode != 0 or not output.is_file():
        diagnostic = (build.stderr or build.stdout or "").strip().replace("\x00", "")[:512]
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _NativeProbeBuildError(
            f"MSVC probe build failed (returncode={build.returncode}): {diagnostic}"
        )
    return output


def _compile_winsock_probe() -> Path:  # pragma: no cover - Windows CI
    """Build the acceptance-only Winsock probe."""

    return _compile_msvc_probe(_winsock_probe_source(), "windows_winsock_probe")


def _stdio_probe_source() -> Path:
    source = Path(__file__).with_name("windows_stdio_probe.c").resolve(strict=False)
    if not source.is_file():
        raise _NativeProbeBuildError("stdio probe source is unavailable")
    return source


def _compile_stdio_probe() -> Path:  # pragma: no cover - Windows CI
    """Build the acceptance-only raw Win32 stdio probe."""

    return _compile_msvc_probe(_stdio_probe_source(), "windows_stdio_probe")


def _stdio_payload(length: int, variant: int) -> bytes:
    if length < len(_STDIO_SPECIAL):
        raise ValueError("stdio payload length is too small")
    body = bytes(
        ((index * 37 + variant * 53 + 17) & 0xFF) for index in range(len(_STDIO_SPECIAL), length)
    )
    return _STDIO_SPECIAL + body


def _encode_stdio_frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "little") + payload


def _parse_winsock_result(value: object) -> dict[str, object]:
    preview = value if isinstance(value, str) else ""
    marker_line = next(
        (line.strip() for line in preview.splitlines() if line.strip().startswith(_WINSOCK_MARKER)),
        "",
    )
    if not marker_line:
        raise AssertionError("Winsock probe omitted W3_WINSOCK marker")
    try:
        payload = json.loads(marker_line[len(_WINSOCK_MARKER) :])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AssertionError("Winsock probe emitted invalid JSON") from error
    if not isinstance(payload, dict):
        raise AssertionError("Winsock probe JSON is not an object")
    stage = payload.get("stage")
    connected = payload.get("connected")
    wsa_error = payload.get("wsa_error")
    if stage not in {"WSA_STARTUP", "SOCKET", "CONNECT"}:
        raise AssertionError("Winsock probe emitted an invalid stage")
    if type(connected) is not bool or type(wsa_error) is not int or wsa_error < 0:
        raise AssertionError("Winsock probe emitted invalid bounded facts")
    return {"stage": stage, "connected": connected, "wsa_error": wsa_error}


class _RecordingFirewallApi:
    """Native delegating firewall adapter used only by Gate 3 evidence."""

    def __init__(self, delegate: _NativeWindowsFirewallApi) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    def ensure_outbound_block(self, rule: WindowsFirewallRule) -> None:
        self.calls.append("ENSURE")
        self.delegate.ensure_outbound_block(rule)

    def remove_rule(self, rule: WindowsFirewallRule) -> None:
        self.calls.append("REMOVE")
        self.delegate.remove_rule(rule)

    def rule_exists(self, rule: WindowsFirewallRule) -> bool:
        self.calls.append("INSPECT")
        return self.delegate.rule_exists(rule)


@dataclass(frozen=True, slots=True)
class _AclEntryProjection:
    """Test-only non-secret projection used to inspect the native DACL."""

    sid: str
    access_mask: int
    is_deny: bool
    inheritance: int


async def _drain_stream(stream: object | None) -> bytes:
    if stream is None:
        return b""
    value = await cast(Any, stream).read(65_536)
    return value if isinstance(value, bytes) else b""


async def _read_all_bounded(stream: object | None, *, maximum: int = 1 << 20) -> bytes:
    """Read one runtime stream to EOF without allowing unbounded buffering."""

    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        value = await cast(Any, stream).read(65_536)
        if not isinstance(value, bytes) or not value:
            break
        total += len(value)
        if total > maximum:
            raise AssertionError("OUTPUT_BOUND_EXCEEDED")
        chunks.append(value)
    return b"".join(chunks)


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


def _cmd_write(path: Path, text: str, *, append: bool = False) -> Command:
    operator = ">>" if append else ">"
    return ("echo", f"{text}{operator}", str(path))


def _cmd_read(path: Path) -> Command:
    return ("type", str(path))


def _cmd_move(source: Path, destination: Path) -> Command:
    return ("move", "/Y", str(source), str(destination))


def _cmd_delete(path: Path) -> Command:
    return ("del", "/F", "/Q", str(path))


def _request(
    *,
    workspace: Path,
    network: LocalProcessNetworkPolicy,
    executable: str,
    arguments: tuple[str, ...],
    purpose: LocalProcessPurpose = LocalProcessPurpose.BASH,
    stdio_mode: LocalProcessStdioMode = LocalProcessStdioMode.CAPTURE,
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        executable,
        arguments,
        purpose=purpose,
        cwd=workspace,
        sandbox_profile=SandboxProfile.WORKSPACE,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (
                LocalWorkspaceAccess(
                    workspace,
                    LocalWorkspaceAccessMode.READ_WRITE,
                ),
            )
        ),
        network_policy=network,
        environment_policy=LocalProcessEnvironmentPolicy({}),
        stdio_mode=stdio_mode,
        lifecycle=LocalProcessLifecycle(),
    )


def _state(path: Path, *, expected_content: str | None = None) -> dict[str, object]:
    exists = path.exists()
    state: dict[str, object] = {"exists": exists}
    if expected_content is not None and exists:
        state["content_unchanged"] = path.read_text(encoding="utf-8") == expected_content
    return state


def _state_probe(path: Path, *, expected_content: str | None = None) -> ProbeState:
    return lambda: _state(path, expected_content=expected_content)


def _not_exists_probe(path: Path) -> ProbeState:
    return lambda: {"exists": not path.exists()}


def _rename_probe(source: Path, destination: Path) -> ProbeState:
    return lambda: {"exists": not source.exists(), "renamed": destination.exists()}


def _deleted_probe(path: Path) -> ProbeState:
    return lambda: {"deleted": not path.exists()}


def _inspect_acl_entries(api: _NativeWindowsAclApi, path: Path) -> tuple[_AclEntryProjection, ...]:
    entries: list[_AclEntryProjection] = []
    for raw in api._raw_entries(path):
        header = _AceHeader.from_buffer_copy(raw)
        if header.AceType not in (api._ACCESS_ALLOWED_ACE_TYPE, api._ACCESS_DENIED_ACE_TYPE):
            continue
        sid_buffer = ctypes.create_string_buffer(raw[8:])
        entries.append(
            _AclEntryProjection(
                sid=api._sid_string(ctypes.addressof(sid_buffer)),
                access_mask=int.from_bytes(raw[4:8], "little", signed=False),
                is_deny=header.AceType == api._ACCESS_DENIED_ACE_TYPE,
                inheritance=int(header.AceFlags),
            )
        )
    return tuple(entries)


def _inspect_dacl_protection(api: _NativeWindowsAclApi, path: Path) -> dict[str, object]:
    """Return only the DACL protection bit for a bounded acceptance record."""

    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = cast(Any, api._get_named_security_info)(
        str(path),
        api._SE_FILE_OBJECT,
        api._DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        ctypes.byref(sacl),
        ctypes.byref(descriptor),
    )
    if result != 0 or not descriptor.value:
        return {"inspection_error": "GetNamedSecurityInfoW"}
    try:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return {"inspection_error": "Win32 ctypes unavailable"}
        advapi32 = loader("advapi32.dll", use_last_error=True)
        get_control = advapi32.GetSecurityDescriptorControl
        get_control.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_control.restype = ctypes.c_int32
        control = ctypes.c_uint16()
        revision = ctypes.c_uint32()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            return {"inspection_error": "GetSecurityDescriptorControl"}
        return {"dacl_protected": bool(control.value & 0x1000)}
    finally:
        api._local_free(descriptor)


def _acl_summary(
    api: _NativeWindowsAclApi,
    path: Path,
    *,
    online_sid: str,
    offline_sid: str,
    synthetic_sid: str,
) -> dict[str, object]:
    entries = _inspect_acl_entries(api, path)
    target_sids = {online_sid, offline_sid, synthetic_sid}

    def project(entry: _AclEntryProjection) -> dict[str, object]:
        return {
            "type": "deny" if entry.is_deny else "allow",
            "mask": entry.access_mask,
            "inherited": bool(entry.inheritance & api._INHERITED_ACE),
        }

    def for_sid(sid: str) -> list[dict[str, object]]:
        return [project(entry) for entry in entries if entry.sid == sid]

    unrelated = [project(entry) for entry in entries if entry.sid not in target_sids]
    return {
        "dacl_protected": _inspect_dacl_protection(api, path),
        "online_real_sid": for_sid(online_sid),
        "offline_real_sid": for_sid(offline_sid),
        "synthetic_sid": for_sid(synthetic_sid),
        "unrelated": unrelated[:32],
    }


def _projection_has_write_allow(entries: tuple[_AclEntryProjection, ...], sid: str) -> bool:
    return any(
        entry.sid == sid
        and not entry.is_deny
        and entry.access_mask & WRITE_ACCESS_MASK == WRITE_ACCESS_MASK
        for entry in entries
    )


def _projection_has_synthetic_write_allow(
    entries: tuple[_AclEntryProjection, ...], sid: str
) -> bool:
    return any(
        entry.sid == sid
        and not entry.is_deny
        and entry.access_mask & WRITE_ONLY_ACCESS_MASK == WRITE_ONLY_ACCESS_MASK
        for entry in entries
    )


def _projection_has_deny(entries: tuple[_AclEntryProjection, ...], sid: str) -> bool:
    return any(entry.sid == sid and entry.is_deny for entry in entries)


@unittest.skipUnless(_native_enabled(), "privileged Windows W3 acceptance is CI-only")
class WindowsNativeRuntimeAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate1_and_gate2_filesystem_enforcement(
        self,
    ) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W2 setup acceptance requires elevation")
        account_api = _NativeWindowsSandboxAccountApi()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace_rw"
            readonly_root = root / "readonly_root"
            installation = root / "installation_root"
            outside = root / "outside_broad_write"
            controller_only = root / "controller_only"
            runtime_state = root / "runtime-state"
            for path in (
                workspace,
                readonly_root,
                installation,
                outside,
                controller_only,
                runtime_state,
            ):
                path.mkdir()
            readable = workspace / "readable.txt"
            sensitive = workspace / "sensitive.txt"
            readonly_file = readonly_root / "readonly.txt"
            private_file = installation / "gate2-private-canary.txt"
            credential_path = installation / "credentials.dpapi"
            readable.write_text(_SENTINEL, encoding="utf-8")
            sensitive.write_text(_SENTINEL, encoding="utf-8")
            readonly_file.write_text(_SENTINEL, encoding="utf-8")
            private_file.write_text(_SENTINEL, encoding="utf-8")

            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace, readonly_root),
                writable_roots=(workspace,),
                sensitive_read_paths=(sensitive,),
            )
            acl_api = _NativeWindowsAclApi()
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=WindowsDpapiCredentialStore(installation / "credentials.dpapi"),
                acl_api=acl_api,
                firewall_api=_NativeWindowsFirewallApi(),
                account_api=account_api,
                privilege_api=privilege_api,
            )
            executable = str(
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe"
            )
            print("W3_STAGE=setup_start", flush=True)
            snapshot = authority.setup(setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            self.assertIsNotNone(snapshot.online_user_sid)
            self.assertIsNotNone(snapshot.offline_user_sid)
            self.assertIsNotNone(snapshot.write_restricting_sid)
            print("W3_STAGE=setup_ready", flush=True)

            online_sid = cast(str, snapshot.online_user_sid)
            offline_sid = cast(str, snapshot.offline_user_sid)
            write_sid = SyntheticWindowsSid(cast(str, snapshot.write_restricting_sid))
            online_account = WindowsAccountSid(online_sid)
            offline_account = WindowsAccountSid(offline_sid)

            async def run_child(
                *,
                label: str,
                identity: str,
                network: LocalProcessNetworkPolicy,
                command: str | Command | None = None,
                direct_executable: str | None = None,
                direct_arguments: tuple[str, ...] | None = None,
                expected: str = "UNSPECIFIED",
                capture_stdout: bool = False,
                expected_stdout: str | None = None,
                record_stdout_nonempty: bool = False,
            ) -> dict[str, object]:
                if direct_arguments is not None:
                    child_executable = direct_executable or executable
                    child_arguments = direct_arguments
                else:
                    if command is None:
                        raise AssertionError("command or direct arguments are required")
                    command_args = (command,) if isinstance(command, str) else command
                    child_executable = executable
                    child_arguments = ("/d", "/s", "/c", *command_args)
                request = _request(
                    workspace=workspace,
                    network=network,
                    executable=child_executable,
                    arguments=child_arguments,
                )
                adapter = WindowsNativeLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    runtime_state,
                    setup_authority=authority,
                    setup_request_factory=lambda _request: setup_request,
                    _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                    _diagnostic_create_no_window=True,
                )
                result: dict[str, object] = {
                    "operation": label,
                    "identity": identity,
                    "expected": expected,
                    "actual": "ERROR",
                    "exit_code": None,
                    "create_process": "UNKNOWN",
                    "spawn_ready": "FAIL",
                    "token_attested": False,
                }
                process: OwnedLocalProcess | None = None
                combined: asyncio.Future[Any] | None = None
                captured_stdout = b""
                captured_stderr = b""
                try:
                    process = await adapter.spawn(request)
                    result["create_process"] = "PASS"
                    result["spawn_ready"] = "PASS"
                    combined = asyncio.gather(
                        asyncio.create_task(_drain_stream(process.stdout)),
                        asyncio.create_task(_drain_stream(process.stderr)),
                        asyncio.create_task(process.wait()),
                        return_exceptions=True,
                    )
                    values = cast(
                        object,
                        await asyncio.wait_for(asyncio.shield(combined), timeout=10),
                    )
                    if isinstance(values, list) and len(values) == 3:
                        if isinstance(values[0], bytes):
                            captured_stdout = values[0]
                        if isinstance(values[1], bytes):
                            captured_stderr = values[1]
                        wait_value = values[2]
                        if isinstance(wait_value, int):
                            result["exit_code"] = wait_value
                            result["actual"] = "ALLOW" if wait_value == 0 else "DENY"
                        elif isinstance(wait_value, BaseException):
                            result["actual"] = "ERROR"
                except TimeoutError:
                    result["actual"] = "TIMEOUT"
                except BaseException as error:
                    result["error_class"] = type(error).__name__
                finally:
                    if process is not None and process.returncode is None:
                        with contextlib.suppress(BaseException):
                            await process.terminate(grace_seconds=0.5)
                    if combined is not None and not combined.done():
                        with contextlib.suppress(BaseException):
                            values = cast(
                                object,
                                await asyncio.wait_for(combined, timeout=2),
                            )
                            if isinstance(values, list) and len(values) == 3:
                                if isinstance(values[0], bytes):
                                    captured_stdout = values[0]
                                if isinstance(values[1], bytes):
                                    captured_stderr = values[1]
                                if isinstance(values[2], int):
                                    result["exit_code"] = values[2]
                                    result["actual"] = "ALLOW" if values[2] == 0 else "DENY"
                    if process is not None and process.returncode is not None:
                        result["exit_code"] = process.returncode
                        result["actual"] = "ALLOW" if process.returncode == 0 else "DENY"
                    stdout_text = captured_stdout.decode("utf-8", errors="replace")
                    if record_stdout_nonempty:
                        result["stdout_nonempty"] = bool(captured_stdout)
                    if capture_stdout:
                        result["stdout_preview"] = stdout_text.strip()[:256]
                    if expected_stdout is not None:
                        result["stdout_matches_expected"] = (
                            stdout_text.rstrip("\r\n") == expected_stdout
                        )
                    result["stderr_preview"] = captured_stderr.decode("utf-8", errors="replace")[
                        :512
                    ]
                    diagnostic = (
                        cast(Any, process).diagnostic_snapshot() if process is not None else None
                    )
                    if isinstance(diagnostic, dict):
                        attestation = diagnostic.get("security_attestation")
                        expected_user_sid = online_sid if identity == "ONLINE" else offline_sid
                        result["token_attested"] = bool(
                            isinstance(attestation, dict)
                            and attestation.get("user_sid") == expected_user_sid
                            and attestation.get("is_restricted") is True
                            and tuple(attestation.get("restricted_sids", ())) == (write_sid.value,)
                            and attestation.get("change_notify_privilege_enabled") is True
                            and attestation.get("unexpected_enabled_privilege_count") == 0
                        )
                        runner = diagnostic.get("runner")
                        if isinstance(runner, dict):
                            result["runner_exit"] = runner.get("exit_code")
                    print(
                        "W3_FS_PROBE="
                        + json.dumps(
                            {
                                key: value
                                for key, value in result.items()
                                if key not in {"stdout_preview"}
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                return result

            def require_probe(
                result: dict[str, object],
                *,
                expected: str,
                state: dict[str, object] | Callable[[], dict[str, object]],
            ) -> None:
                self.assertEqual(result.get("create_process"), "PASS")
                self.assertEqual(result.get("spawn_ready"), "PASS")
                self.assertTrue(result.get("token_attested"))
                controller_state = state() if callable(state) else state
                result["controller_state"] = controller_state
                state_ok = all(value is not False for value in controller_state.values())
                if (
                    expected == "DENY"
                    and result.get("actual") == "ALLOW"
                    and state_ok
                    and "access is denied" in str(result.get("stderr_preview", "")).casefold()
                ):
                    # ``cmd.exe del`` can emit Access is denied while still
                    # returning zero.  The controller-side unchanged state
                    # is the decisive filesystem result for this mutation.
                    result["actual"] = "DENY"
                    result["effective_denial"] = "stderr_and_controller_state"
                self.assertEqual(result.get("actual"), expected)
                self.assertTrue(state_ok)

            try:
                # Gate 1 regression: the only stdout that is retained is the
                # bounded whoami identity line; no token JSON comes from the child.
                gate1: list[dict[str, object]] = []
                for label, identity, network, username in (
                    (
                        "GATE1_ONLINE",
                        "ONLINE",
                        LocalProcessNetworkPolicy.INHERIT,
                        SANDBOX_ONLINE_USERNAME,
                    ),
                    (
                        "GATE1_OFFLINE",
                        "OFFLINE",
                        LocalProcessNetworkPolicy.ISOLATED,
                        SANDBOX_OFFLINE_USERNAME,
                    ),
                ):
                    probe = await run_child(
                        label=label,
                        identity=identity,
                        network=network,
                        command="whoami",
                        expected="ALLOW",
                        capture_stdout=True,
                    )
                    self.assertIn(
                        username.casefold(), str(probe.get("stdout_preview", "")).casefold()
                    )
                    require_probe(probe, expected="ALLOW", state={"stdout_identity": True})
                    gate1.append(probe)
                print("W3_GATE1_REGRESSION=PASS", flush=True)

                # Gate 2A starts with a pure read.  ``type file`` deliberately
                # leaves stdout on the inherited pipe; redirecting to NUL
                # would combine READ authority with an unrelated device write.
                workspace_results: list[dict[str, object]] = []
                nul_probe: dict[str, object] | None = None
                for identity, network in (
                    ("ONLINE", LocalProcessNetworkPolicy.INHERIT),
                    ("OFFLINE", LocalProcessNetworkPolicy.ISOLATED),
                ):
                    prefix = identity.casefold()
                    source = workspace / f"{prefix}-gate2-a.txt"
                    destination = workspace / f"{prefix}-gate2-b.txt"
                    read_probe = await run_child(
                        label="WORKSPACE_READ_NO_REDIRECT",
                        identity=identity,
                        network=network,
                        command=_cmd_read(readable),
                        expected="ALLOW",
                        expected_stdout=_SENTINEL,
                    )
                    if read_probe.get("actual") != "ALLOW":
                        try:
                            require_probe(
                                read_probe,
                                expected="ALLOW",
                                state=_state_probe(readable, expected_content=_SENTINEL),
                            )
                        except AssertionError:
                            try:
                                inspection: dict[str, object] = {
                                    "workspace_root": _acl_summary(
                                        acl_api,
                                        workspace,
                                        online_sid=online_sid,
                                        offline_sid=offline_sid,
                                        synthetic_sid=write_sid.value,
                                    ),
                                    "readable_file": _acl_summary(
                                        acl_api,
                                        readable,
                                        online_sid=online_sid,
                                        offline_sid=offline_sid,
                                        synthetic_sid=write_sid.value,
                                    ),
                                }
                            except BaseException as inspection_error:
                                inspection = {"inspection_error": type(inspection_error).__name__}
                            print(
                                "W3_WORKSPACE_ACL_INSPECTION="
                                + json.dumps(inspection, sort_keys=True),
                                flush=True,
                            )
                            raise
                    require_probe(
                        read_probe,
                        expected="ALLOW",
                        state=_state_probe(readable, expected_content=_SENTINEL),
                    )
                    self.assertTrue(read_probe.get("stdout_matches_expected"))
                    workspace_results.append(read_probe)

                    if identity == "ONLINE":
                        nul_probe = await run_child(
                            label="NUL_WRITE",
                            identity=identity,
                            network=network,
                            command=_NUL_CANARY_COMMAND,
                            expected="UNSPECIFIED",
                        )
                        self.assertEqual(nul_probe.get("create_process"), "PASS")
                        self.assertEqual(nul_probe.get("spawn_ready"), "PASS")
                        self.assertTrue(nul_probe.get("token_attested"))
                        self.assertIn(nul_probe.get("actual"), {"ALLOW", "DENY"})
                        print(
                            "W3_NUL_PROBE="
                            + json.dumps(
                                {
                                    "actual": nul_probe.get("actual"),
                                    "exit_code": nul_probe.get("exit_code"),
                                    "stderr_preview": nul_probe.get("stderr_preview", ""),
                                    "classification": (
                                        "NUL_WRITE_RESTRICTED_COMPATIBILITY"
                                        if nul_probe.get("actual") == "DENY"
                                        else "ALLOW"
                                    ),
                                },
                                sort_keys=True,
                            ),
                            flush=True,
                        )

                    # Remaining operations keep stdout/stderr on the already
                    # inherited pipes and test only workspace mutation.
                    workspace_operations: tuple[tuple[str, Command, str, ProbeState], ...] = (
                        (
                            "WORKSPACE_CREATE",
                            _cmd_write(source, _SENTINEL),
                            "ALLOW",
                            _state_probe(source),
                        ),
                        (
                            "WORKSPACE_APPEND",
                            _cmd_write(source, _APPENDED, append=True),
                            "ALLOW",
                            _state_probe(
                                source,
                                expected_content=_SENTINEL + "\n" + _APPENDED + "\n",
                            ),
                        ),
                        (
                            "WORKSPACE_RENAME",
                            _cmd_move(source, destination),
                            "ALLOW",
                            _rename_probe(source, destination),
                        ),
                        (
                            "WORKSPACE_DELETE",
                            _cmd_delete(destination),
                            "ALLOW",
                            _deleted_probe(destination),
                        ),
                    )
                    for operation, command, expected, state in workspace_operations:
                        probe = await run_child(
                            label=operation,
                            identity=identity,
                            network=network,
                            command=command,
                            expected=expected,
                        )
                        require_probe(probe, expected=expected, state=state)
                        workspace_results.append(probe)

                # Gate 2B: explicitly grant ordinary real-user write access to
                # an unrelated directory, but do not grant the synthetic SID.
                outside_entries = (
                    WindowsManagedAce(
                        outside,
                        online_account,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                    WindowsManagedAce(
                        outside,
                        offline_account,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                )
                acl_api.reconcile(outside, desired=outside_entries, remove=())
                outside_projection = _inspect_acl_entries(acl_api, outside)
                online_write_ace = _projection_has_write_allow(outside_projection, online_sid)
                offline_write_ace = _projection_has_write_allow(outside_projection, offline_sid)
                synthetic_write_ace = _projection_has_synthetic_write_allow(
                    outside_projection, write_sid.value
                )
                online_write_deny = _projection_has_deny(outside_projection, online_sid)
                offline_write_deny = _projection_has_deny(outside_projection, offline_sid)
                self.assertTrue(online_write_ace)
                self.assertTrue(offline_write_ace)
                self.assertFalse(synthetic_write_ace)
                self.assertFalse(online_write_deny)
                self.assertFalse(offline_write_deny)
                for identity, network in (
                    ("ONLINE", LocalProcessNetworkPolicy.INHERIT),
                    ("OFFLINE", LocalProcessNetworkPolicy.ISOLATED),
                ):
                    blocked = outside / f"{identity.casefold()}-blocked.txt"
                    probe = await run_child(
                        label="OUTSIDE_BROAD_WRITE",
                        identity=identity,
                        network=network,
                        command=_cmd_write(blocked, _SENTINEL),
                        expected="DENY",
                    )
                    require_probe(
                        probe,
                        expected="DENY",
                        state=_not_exists_probe(blocked),
                    )
                print(
                    "W3_GATE2B_ACL="
                    + json.dumps(
                        {
                            "online_real_write_allow": online_write_ace,
                            "offline_real_write_allow": offline_write_ace,
                            "synthetic_write_allow": synthetic_write_ace,
                            "online_real_write_deny": online_write_deny,
                            "offline_real_write_deny": offline_write_deny,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

                # Gate 2C: read-only roots preserve reads but deny every
                # mutation surface covered by the managed deny mask.
                readonly_results: list[dict[str, object]] = []
                for identity, network in (
                    ("ONLINE", LocalProcessNetworkPolicy.INHERIT),
                    ("OFFLINE", LocalProcessNetworkPolicy.ISOLATED),
                ):
                    create_path = readonly_root / f"{identity.casefold()}-create.txt"
                    readonly_operations: tuple[tuple[str, Command, str, ProbeState], ...] = (
                        (
                            "READ_ONLY_READ",
                            _cmd_read(readonly_file),
                            "ALLOW",
                            _state_probe(readonly_file, expected_content=_SENTINEL),
                        ),
                        (
                            "READ_ONLY_CREATE",
                            _cmd_write(create_path, _SENTINEL),
                            "DENY",
                            _not_exists_probe(create_path),
                        ),
                        (
                            "READ_ONLY_OVERWRITE",
                            _cmd_write(readonly_file, _OVERWRITTEN),
                            "DENY",
                            _state_probe(readonly_file, expected_content=_SENTINEL),
                        ),
                        (
                            "READ_ONLY_DELETE",
                            _cmd_delete(readonly_file),
                            "DENY",
                            _state_probe(readonly_file, expected_content=_SENTINEL),
                        ),
                    )
                    for operation, command, expected, state in readonly_operations:
                        probe = await run_child(
                            label=operation,
                            identity=identity,
                            network=network,
                            command=command,
                            expected=expected,
                        )
                        require_probe(probe, expected=expected, state=state)
                        readonly_results.append(probe)

                # Gate 2D: deny only sensitive reads; the file itself remains
                # a fixed non-secret sentinel and is not printed.
                for identity, network in (
                    ("ONLINE", LocalProcessNetworkPolicy.INHERIT),
                    ("OFFLINE", LocalProcessNetworkPolicy.ISOLATED),
                ):
                    probe = await run_child(
                        label="SENSITIVE_READ",
                        identity=identity,
                        network=network,
                        command=_cmd_read(sensitive),
                        expected="DENY",
                    )
                    require_probe(
                        probe,
                        expected="DENY",
                        state=_state_probe(sensitive, expected_content=_SENTINEL),
                    )

                # Gate 2E: the private installation root is not part of the
                # runtime read authority and remains controller/setup state.
                for identity, network in (
                    ("ONLINE", LocalProcessNetworkPolicy.INHERIT),
                    ("OFFLINE", LocalProcessNetworkPolicy.ISOLATED),
                ):
                    for operation, command in (
                        ("INSTALLATION_READ", _cmd_read(private_file)),
                        ("INSTALLATION_OVERWRITE", _cmd_write(private_file, _OVERWRITTEN)),
                        ("INSTALLATION_DELETE", _cmd_delete(private_file)),
                        ("INSTALLATION_CREDENTIAL_READ", _cmd_read(credential_path)),
                    ):
                        probe = await run_child(
                            label=operation,
                            identity=identity,
                            network=network,
                            command=command,
                            expected="DENY",
                        )
                        require_probe(
                            probe,
                            expected="DENY",
                            state=(
                                _state_probe(credential_path)
                                if operation == "INSTALLATION_CREDENTIAL_READ"
                                else _state_probe(private_file, expected_content=_SENTINEL)
                            ),
                        )

                for identity, network in (
                    ("ONLINE", LocalProcessNetworkPolicy.INHERIT),
                    ("OFFLINE", LocalProcessNetworkPolicy.ISOLATED),
                ):
                    blocked = controller_only / f"{identity.casefold()}-blocked.txt"
                    probe = await run_child(
                        label="CONTROLLER_UNRELATED_WRITE",
                        identity=identity,
                        network=network,
                        command=_cmd_write(blocked, _SENTINEL),
                        expected="DENY",
                    )
                    require_probe(
                        probe,
                        expected="DENY",
                        state=_not_exists_probe(blocked),
                    )

                print(
                    "W3_GATE2_RESULTS="
                    + json.dumps(
                        {
                            "gate1": gate1,
                            "workspace": workspace_results,
                            "nul": nul_probe,
                            "readonly": readonly_results,
                            "outside_acl": {
                                "online_real_write_allow": online_write_ace,
                                "offline_real_write_allow": offline_write_ace,
                                "synthetic_write_allow": synthetic_write_ace,
                                "online_real_write_deny": online_write_deny,
                                "offline_real_write_deny": offline_write_deny,
                            },
                            "token_attestation": "active_before_every_spawn_ready",
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)

    async def test_gate3_network_isolation(self) -> None:  # pragma: no cover - Windows CI
        """Prove static Offline Firewall authority around real final children."""

        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W2 setup acceptance requires elevation")

        try:
            compiled_probe = await asyncio.to_thread(_compile_winsock_probe)
        except _NativeProbeBuildError as error:
            print(
                "W3_GATE3_BLOCKER="
                + json.dumps(
                    {
                        "classification": "NATIVE_WINSOCK_PROBE_BUILD_UNAVAILABLE",
                        "error_type": type(error).__name__,
                        "detail": str(error)[:512],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            self.fail("NATIVE_WINSOCK_PROBE_BUILD_UNAVAILABLE")
        self.addCleanup(shutil.rmtree, compiled_probe.parent, ignore_errors=True)

        def controller_python_probe() -> None:
            with socket.create_connection(_BENIGN_OUTBOUND_PROBE, timeout=5):
                return

        try:
            await asyncio.to_thread(controller_python_probe)
        except OSError as error:
            print(
                "W3_GATE3_BLOCKER="
                + json.dumps(
                    {
                        "classification": "NETWORK_PROBE_ENVIRONMENT_UNAVAILABLE",
                        "error_type": type(error).__name__,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            self.fail("NETWORK_PROBE_ENVIRONMENT_UNAVAILABLE")

        try:
            controller_winsock = await asyncio.to_thread(
                subprocess.run,
                [str(compiled_probe)],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
                shell=False,
            )
            controller_winsock_result = _parse_winsock_result(controller_winsock.stdout)
        except (OSError, subprocess.SubprocessError, AssertionError) as error:
            print(
                "W3_GATE3_BLOCKER="
                + json.dumps(
                    {
                        "classification": "NETWORK_PROBE_ENVIRONMENT_UNAVAILABLE",
                        "error_type": type(error).__name__,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            self.fail("NETWORK_PROBE_ENVIRONMENT_UNAVAILABLE")
        if controller_winsock.returncode != 0 or not controller_winsock_result["connected"]:
            print(
                "W3_GATE3_BLOCKER="
                + json.dumps(
                    {
                        "classification": "NETWORK_PROBE_ENVIRONMENT_UNAVAILABLE",
                        "controller_winsock": controller_winsock_result,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            self.fail("NETWORK_PROBE_ENVIRONMENT_UNAVAILABLE")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            probe = workspace / "w3-winsock-probe.exe"
            shutil.copy2(compiled_probe, probe)
            sensitive = workspace / "sensitive.txt"
            sensitive.write_text(_SENTINEL, encoding="utf-8")
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(sensitive,),
            )
            real_firewall = _NativeWindowsFirewallApi()
            recording_firewall = _RecordingFirewallApi(real_firewall)
            account_api = _NativeWindowsSandboxAccountApi()
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=WindowsDpapiCredentialStore(installation / "credentials.dpapi"),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=recording_firewall,
                account_api=account_api,
                privilege_api=privilege_api,
            )
            setup_snapshot = authority.setup(setup_request)
            self.assertEqual(setup_snapshot.state, WindowsSandboxSetupState.READY)
            encoded = authority._store(setup_request).load()  # type: ignore[attr-defined]
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(cast(bytes, encoded))

            firewall_observations: list[dict[str, object]] = []

            def assert_firewall_ready(label: str) -> None:
                inspected = authority.inspect(setup_request)
                native_exact = real_firewall.rule_exists(record.offline_firewall_rule)
                firewall_observations.append(
                    {
                        "label": label,
                        "authority": inspected.state.value,
                        "exact": native_exact,
                    }
                )
                self.assertEqual(inspected.state, WindowsSandboxSetupState.READY)
                self.assertTrue(native_exact)

            recording_firewall.calls.clear()
            executable = str(probe)
            winsock_arguments: tuple[str, ...] = ()

            async def run_network_child(
                *,
                label: str,
                identity: str,
                network: LocalProcessNetworkPolicy,
            ) -> dict[str, object]:
                request = _request(
                    workspace=workspace,
                    network=network,
                    executable=executable,
                    arguments=winsock_arguments,
                )
                adapter = WindowsNativeLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    runtime_state,
                    setup_authority=authority,
                    setup_request_factory=lambda _request: setup_request,
                    _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                    _diagnostic_create_no_window=True,
                )
                result: dict[str, object] = {
                    "operation": label,
                    "identity": identity,
                    "create_process": "UNKNOWN",
                    "spawn_ready": "FAIL",
                    "token_attested": False,
                    "actual": "ERROR",
                    "exit_code": None,
                    "stdout_nonempty": False,
                }
                process: OwnedLocalProcess | None = None
                combined: asyncio.Future[Any] | None = None
                stdout = b""
                stderr = b""
                try:
                    process = await adapter.spawn(request)
                    result["create_process"] = "PASS"
                    result["spawn_ready"] = "PASS"
                    combined = asyncio.gather(
                        asyncio.create_task(_drain_stream(process.stdout)),
                        asyncio.create_task(_drain_stream(process.stderr)),
                        asyncio.create_task(process.wait()),
                        return_exceptions=True,
                    )
                    values = cast(
                        object,
                        await asyncio.wait_for(asyncio.shield(combined), timeout=10),
                    )
                    if isinstance(values, list) and len(values) == 3:
                        if isinstance(values[0], bytes):
                            stdout = values[0]
                        if isinstance(values[1], bytes):
                            stderr = values[1]
                        if isinstance(values[2], int):
                            result["exit_code"] = values[2]
                            result["actual"] = "ALLOW" if values[2] == 0 else "DENY"
                except TimeoutError:
                    result["actual"] = "TIMEOUT"
                except BaseException as error:
                    result["error_class"] = type(error).__name__
                finally:
                    if process is not None and process.returncode is None:
                        with contextlib.suppress(BaseException):
                            await process.terminate(grace_seconds=0.5)
                    if combined is not None and not combined.done():
                        with contextlib.suppress(BaseException):
                            values = cast(object, await asyncio.wait_for(combined, timeout=2))
                            if isinstance(values, list) and len(values) == 3:
                                if isinstance(values[0], bytes):
                                    stdout = values[0]
                                if isinstance(values[1], bytes):
                                    stderr = values[1]
                                if isinstance(values[2], int):
                                    result["exit_code"] = values[2]
                                    result["actual"] = "ALLOW" if values[2] == 0 else "DENY"
                    if process is not None and process.returncode is not None:
                        result["exit_code"] = process.returncode
                        result["actual"] = "ALLOW" if process.returncode == 0 else "DENY"
                    result["stdout_nonempty"] = bool(stdout)
                    result["stdout_preview"] = stdout.decode("utf-8", errors="replace")[:256]
                    result["stderr_preview"] = stderr.decode("utf-8", errors="replace")[:512]
                    diagnostic = (
                        cast(Any, process).diagnostic_snapshot() if process is not None else None
                    )
                    if isinstance(diagnostic, dict):
                        attestation = diagnostic.get("security_attestation")
                        expected_sid = online_sid if identity == "ONLINE" else offline_sid
                        result["token_attested"] = bool(
                            isinstance(attestation, dict)
                            and attestation.get("user_sid") == expected_sid
                            and attestation.get("is_restricted") is True
                            and tuple(attestation.get("restricted_sids", ())) == (write_sid.value,)
                            and attestation.get("change_notify_privilege_enabled") is True
                            and attestation.get("unexpected_enabled_privilege_count") == 0
                        )
                        runner = diagnostic.get("runner")
                        if isinstance(runner, dict):
                            result["runner_exit"] = runner.get("exit_code")
                    print(
                        "W3_GATE3_NETWORK_PROBE=" + json.dumps(result, sort_keys=True),
                        flush=True,
                    )
                return result

            def winsock_result(result: dict[str, object]) -> dict[str, object]:
                parsed = _parse_winsock_result(result.get("stdout_preview"))
                result["winsock"] = parsed
                return parsed

            def online_failure_classification(parsed: dict[str, object]) -> str:
                return {
                    "WSA_STARTUP": "ONLINE_WINSOCK_STARTUP_BLOCKED",
                    "SOCKET": "ONLINE_WINSOCK_SOCKET_BLOCKED",
                    "CONNECT": "ONLINE_WINSOCK_CONNECT_BLOCKED",
                }.get(str(parsed.get("stage")), "ONLINE_WINSOCK_PROBE_PROTOCOL_FAILURE")

            online_sid = cast(str, setup_snapshot.online_user_sid)
            offline_sid = cast(str, setup_snapshot.offline_user_sid)
            write_sid = SyntheticWindowsSid(cast(str, setup_snapshot.write_restricting_sid))

            try:
                # Firewall exactness is checked before and after every child;
                # runtime setup inspection is the only permitted call path.
                assert_firewall_ready("before_online_1")
                online_1 = await run_network_child(
                    label="ONLINE_1",
                    identity="ONLINE",
                    network=LocalProcessNetworkPolicy.INHERIT,
                )
                online_1_winsock = winsock_result(online_1)
                self.assertEqual(online_1.get("create_process"), "PASS")
                self.assertEqual(online_1.get("spawn_ready"), "PASS")
                self.assertTrue(online_1.get("token_attested"))
                if (
                    online_1.get("actual") != "ALLOW"
                    or online_1.get("exit_code") != 0
                    or online_1_winsock.get("stage") != "CONNECT"
                    or online_1_winsock.get("connected") is not True
                    or online_1_winsock.get("wsa_error") != 0
                ):
                    classification = online_failure_classification(online_1_winsock)
                    print(
                        "W3_GATE3_BLOCKER="
                        + json.dumps(
                            {
                                "classification": classification,
                                "online": online_1_winsock,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    self.fail(classification)
                self.assertTrue(online_1.get("stdout_nonempty"))
                assert_firewall_ready("after_online_1")

                offline_1 = await run_network_child(
                    label="OFFLINE_1",
                    identity="OFFLINE",
                    network=LocalProcessNetworkPolicy.ISOLATED,
                )
                offline_1_winsock = winsock_result(offline_1)
                self.assertEqual(offline_1.get("create_process"), "PASS")
                self.assertEqual(offline_1.get("spawn_ready"), "PASS")
                self.assertTrue(offline_1.get("token_attested"))
                if (
                    offline_1.get("actual") != "DENY"
                    or offline_1.get("exit_code") == 0
                    or offline_1_winsock.get("stage") != "CONNECT"
                    or offline_1_winsock.get("connected") is not False
                ):
                    print(
                        "W3_GATE3_BLOCKER="
                        + json.dumps(
                            {
                                "classification": "OFFLINE_NETWORK_STACK_COMPATIBILITY",
                                "offline": offline_1_winsock,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    self.fail("OFFLINE_NETWORK_STACK_COMPATIBILITY")
                assert_firewall_ready("after_offline_1")

                online_2 = await run_network_child(
                    label="ONLINE_2",
                    identity="ONLINE",
                    network=LocalProcessNetworkPolicy.INHERIT,
                )
                online_2_winsock = winsock_result(online_2)
                self.assertEqual(online_2.get("create_process"), "PASS")
                self.assertEqual(online_2.get("spawn_ready"), "PASS")
                self.assertTrue(online_2.get("token_attested"))
                self.assertEqual(online_2.get("actual"), "ALLOW")
                self.assertEqual(online_2.get("exit_code"), 0)
                self.assertEqual(online_2_winsock.get("stage"), "CONNECT")
                self.assertTrue(online_2_winsock.get("connected"))
                self.assertEqual(online_2_winsock.get("wsa_error"), 0)
                assert_firewall_ready("after_online_2")

                offline_2 = await run_network_child(
                    label="OFFLINE_2",
                    identity="OFFLINE",
                    network=LocalProcessNetworkPolicy.ISOLATED,
                )
                offline_2_winsock = winsock_result(offline_2)
                self.assertEqual(offline_2.get("create_process"), "PASS")
                self.assertEqual(offline_2.get("spawn_ready"), "PASS")
                self.assertTrue(offline_2.get("token_attested"))
                self.assertEqual(offline_2.get("actual"), "DENY")
                self.assertNotEqual(offline_2.get("exit_code"), 0)
                self.assertEqual(offline_2_winsock.get("stage"), "CONNECT")
                self.assertFalse(offline_2_winsock.get("connected"))
                assert_firewall_ready("after_offline_2")

                # Two identities share one static setup.  Poll the real native
                # rule while both final children are alive so a hidden rule
                # toggle/removal cannot explain the observed results.
                recording_firewall.calls.clear()
                monitor_stop = asyncio.Event()
                monitor_observations: list[bool] = []

                async def monitor_firewall() -> None:
                    while not monitor_stop.is_set():
                        monitor_observations.append(
                            await asyncio.to_thread(
                                real_firewall.rule_exists,
                                record.offline_firewall_rule,
                            )
                        )
                        await asyncio.sleep(0.05)

                monitor = asyncio.create_task(monitor_firewall())
                try:
                    concurrent_online, concurrent_offline = await asyncio.gather(
                        run_network_child(
                            label="CONCURRENT_ONLINE",
                            identity="ONLINE",
                            network=LocalProcessNetworkPolicy.INHERIT,
                        ),
                        run_network_child(
                            label="CONCURRENT_OFFLINE",
                            identity="OFFLINE",
                            network=LocalProcessNetworkPolicy.ISOLATED,
                        ),
                    )
                finally:
                    monitor_stop.set()
                    with contextlib.suppress(BaseException):
                        await monitor
                self.assertTrue(monitor_observations)
                self.assertTrue(all(monitor_observations))
                self.assertTrue(concurrent_online.get("token_attested"))
                self.assertEqual(concurrent_online.get("actual"), "ALLOW")
                self.assertEqual(concurrent_online.get("exit_code"), 0)
                self.assertTrue(concurrent_online.get("stdout_nonempty"))
                concurrent_online_winsock = winsock_result(concurrent_online)
                self.assertEqual(concurrent_online_winsock.get("stage"), "CONNECT")
                self.assertTrue(concurrent_online_winsock.get("connected"))
                self.assertTrue(concurrent_offline.get("token_attested"))
                self.assertEqual(concurrent_offline.get("actual"), "DENY")
                self.assertNotEqual(concurrent_offline.get("exit_code"), 0)
                concurrent_offline_winsock = winsock_result(concurrent_offline)
                self.assertEqual(concurrent_offline_winsock.get("stage"), "CONNECT")
                self.assertFalse(concurrent_offline_winsock.get("connected"))
                mutation_calls = [
                    call for call in recording_firewall.calls if call in {"ENSURE", "REMOVE"}
                ]
                self.assertEqual(mutation_calls, [])

                try:
                    await asyncio.to_thread(controller_python_probe)
                    controller_postflight_winsock = await asyncio.to_thread(
                        subprocess.run,
                        [str(compiled_probe)],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=8,
                        shell=False,
                    )
                    controller_postflight_result = _parse_winsock_result(
                        controller_postflight_winsock.stdout
                    )
                except (OSError, subprocess.SubprocessError, AssertionError) as error:
                    self.fail(
                        f"NETWORK_PROBE_ENVIRONMENT_UNAVAILABLE_POSTFLIGHT:{type(error).__name__}"
                    )
                self.assertEqual(controller_postflight_winsock.returncode, 0)
                self.assertTrue(controller_postflight_result["connected"])
                assert_firewall_ready("controller_postflight")

                # Gate 3 evidence is only promoted after the complete network
                # sequence and static-rule invariant have passed.
                adapter = WindowsNativeLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    runtime_state,
                    setup_authority=authority,
                    setup_request_factory=lambda _request: setup_request,
                    _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                    _diagnostic_create_no_window=True,
                )
                self.assertEqual(
                    adapter.security_capabilities.read_isolation,
                    LocalProcessSecurityStrength.LIMITED,
                )
                self.assertEqual(
                    adapter.security_capabilities.write_isolation,
                    LocalProcessSecurityStrength.STRONG,
                )
                self.assertEqual(
                    adapter.security_capabilities.network_isolation,
                    LocalProcessSecurityStrength.STRONG,
                )
                print(
                    "W3_GATE3_RESULTS="
                    + json.dumps(
                        {
                            "firewall": firewall_observations,
                            "runtime_firewall_mutations": mutation_calls,
                            "controller_preflight": "PASS",
                            "controller_winsock": controller_winsock_result,
                            "controller_postflight": "PASS",
                            "controller_postflight_winsock": controller_postflight_result,
                            "online_1": online_1_winsock,
                            "offline_1": offline_1_winsock,
                            "online_2": online_2_winsock,
                            "offline_2": offline_2_winsock,
                            "concurrent_online": concurrent_online_winsock,
                            "concurrent_offline": concurrent_offline_winsock,
                            "curl_w5_classification": "CURL_RESTRICTED_RUNTIME_COMPATIBILITY",
                            "online_offline_concurrent_rule_observations": len(
                                monitor_observations
                            ),
                            "capabilities": {
                                "read": LocalProcessSecurityStrength.LIMITED.value,
                                "write": LocalProcessSecurityStrength.STRONG.value,
                                "network": LocalProcessSecurityStrength.STRONG.value,
                            },
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)

    async def test_gate4_binary_stdio_protocol(self) -> None:  # pragma: no cover - Windows CI
        """Prove bounded, binary-transparent capture and MCP-style stdio."""

        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W2 setup acceptance requires elevation")
        try:
            compiled_probe = await asyncio.to_thread(_compile_stdio_probe)
        except _NativeProbeBuildError as error:
            print(
                "W3_GATE4_BLOCKER="
                + json.dumps(
                    {
                        "classification": "NATIVE_STDIO_PROBE_BUILD_UNAVAILABLE",
                        "error_type": type(error).__name__,
                        "detail": str(error)[:512],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            self.fail("NATIVE_STDIO_PROBE_BUILD_UNAVAILABLE")
        self.addCleanup(shutil.rmtree, compiled_probe.parent, ignore_errors=True)

        stdout_expected = _stdio_payload(_STDIO_CAPTURE_STDOUT_LENGTH, 3) + (
            _STDIO_CAPTURE_STDOUT_TRAILER
        )
        stderr_expected = _stdio_payload(_STDIO_CAPTURE_STDERR_LENGTH, 7) + (
            _STDIO_CAPTURE_STDERR_TRAILER
        )
        merged_expected = b"".join(
            _stdio_payload(length, variant) + trailer
            for length, variant, trailer in zip(
                _STDIO_MERGED_LENGTHS,
                _STDIO_MERGED_VARIANTS,
                _STDIO_MERGED_TRAILERS,
                strict=True,
            )
        )
        protocol_first = _stdio_payload(_STDIO_PROTOCOL_LARGE_LENGTH, 17)
        protocol_second = bytes(
            (0x00, 0x0D, 0x0A, 0x0D, 0x0A, 0xE2, 0x82, 0xAC, 0xF0, 0x9F, 0x98, 0x80, 0xFF)
        )
        protocol_input = _encode_stdio_frame(protocol_first) + _encode_stdio_frame(protocol_second)
        self.assertGreater(len(protocol_input), 65 * 1024)
        self.assertNotIn(_STDIO_PROTOCOL_DIAGNOSTIC, protocol_input)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            probe = workspace / "w3-windows-stdio-probe.exe"
            shutil.copy2(compiled_probe, probe)
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=WindowsDpapiCredentialStore(installation / "credentials.dpapi"),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                account_api=_NativeWindowsSandboxAccountApi(),
                privilege_api=privilege_api,
            )
            setup_snapshot = authority.setup(setup_request)
            self.assertEqual(setup_snapshot.state, WindowsSandboxSetupState.READY)
            online_sid = cast(str, setup_snapshot.online_user_sid)
            write_sid = SyntheticWindowsSid(cast(str, setup_snapshot.write_restricting_sid))

            async def run_stdio_child(
                *,
                mode: LocalProcessStdioMode,
                argument: str,
                stdin_payload: bytes = b"",
                expected_exit: int = 0,
            ) -> tuple[bytes, bytes, int, bool, dict[str, object]]:
                request = _request(
                    workspace=workspace,
                    network=LocalProcessNetworkPolicy.INHERIT,
                    executable=str(probe),
                    arguments=(argument,),
                    purpose=(
                        LocalProcessPurpose.MCP_STDIO
                        if mode is LocalProcessStdioMode.PROTOCOL
                        else LocalProcessPurpose.BASH
                    ),
                    stdio_mode=mode,
                )
                adapter = WindowsNativeLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    runtime_state,
                    setup_authority=authority,
                    setup_request_factory=lambda _request: setup_request,
                    _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                    _diagnostic_create_no_window=True,
                )
                process: OwnedLocalProcess | None = None
                stdout_task: asyncio.Task[bytes] | None = None
                stderr_task: asyncio.Task[bytes] | None = None
                wait_task: asyncio.Task[int] | None = None
                try:
                    process = await adapter.spawn(request)
                    stdout_task = asyncio.create_task(_read_all_bounded(process.stdout))
                    stderr_task = asyncio.create_task(_read_all_bounded(process.stderr))
                    wait_task = asyncio.create_task(process.wait())
                    if stdin_payload:
                        await process.write_stdin(stdin_payload)
                    if mode is LocalProcessStdioMode.PROTOCOL:
                        await process.close_stdin()
                    stdout, stderr, exit_code = await asyncio.wait_for(
                        asyncio.gather(stdout_task, stderr_task, wait_task),
                        timeout=30,
                    )
                    self.assertEqual(exit_code, expected_exit)
                    diagnostic = cast(Any, process).diagnostic_snapshot()
                    if not isinstance(diagnostic, dict):
                        diagnostic = {}
                    runner = diagnostic.get("runner")
                    self.assertIsInstance(runner, dict)
                    self.assertEqual(runner.get("state"), "RUNNER_EXITED")
                    self.assertEqual(runner.get("exit_code"), 0)
                    attestation = diagnostic.get("security_attestation")
                    token_attested = bool(
                        isinstance(attestation, dict)
                        and attestation.get("user_sid") == online_sid
                        and attestation.get("is_restricted") is True
                        and tuple(attestation.get("restricted_sids", ())) == (write_sid.value,)
                        and attestation.get("change_notify_privilege_enabled") is True
                        and attestation.get("unexpected_enabled_privilege_count") == 0
                    )
                    self.assertTrue(token_attested)
                    return stdout, stderr, exit_code, token_attested, diagnostic
                finally:
                    if process is not None and process.returncode is None:
                        with contextlib.suppress(BaseException):
                            await process.terminate(grace_seconds=0.5)
                    for task in (stdout_task, stderr_task, wait_task):
                        if task is not None and not task.done():
                            task.cancel()
                            with contextlib.suppress(BaseException):
                                await task

            try:
                (
                    capture_stdout,
                    capture_stderr,
                    capture_exit,
                    capture_token,
                    capture_diag,
                ) = await run_stdio_child(mode=LocalProcessStdioMode.CAPTURE, argument="capture")
                self.assertEqual(capture_stdout, stdout_expected)
                self.assertEqual(capture_stderr, stderr_expected)
                self.assertEqual(capture_exit, 0)
                self.assertTrue(capture_token)
                self.assertTrue(capture_stdout.endswith(_STDIO_CAPTURE_STDOUT_TRAILER))
                self.assertTrue(capture_stderr.endswith(_STDIO_CAPTURE_STDERR_TRAILER))

                (
                    merged_stdout,
                    merged_stderr,
                    merged_exit,
                    merged_token,
                    merged_diag,
                ) = await run_stdio_child(
                    mode=LocalProcessStdioMode.MERGED_CAPTURE, argument="merged"
                )
                self.assertEqual(merged_stdout, merged_expected)
                self.assertEqual(merged_stderr, b"")
                self.assertEqual(merged_exit, 0)
                self.assertTrue(merged_token)

                (
                    protocol_stdout,
                    protocol_stderr,
                    protocol_exit,
                    protocol_token,
                    protocol_diag,
                ) = await run_stdio_child(
                    mode=LocalProcessStdioMode.PROTOCOL,
                    argument="protocol",
                    stdin_payload=protocol_input,
                )
                self.assertEqual(protocol_stdout, protocol_input)
                self.assertEqual(protocol_stderr, _STDIO_PROTOCOL_DIAGNOSTIC)
                self.assertEqual(protocol_exit, 0)
                self.assertTrue(protocol_token)
                self.assertNotIn(_STDIO_PROTOCOL_DIAGNOSTIC, protocol_stdout)

                (
                    nonzero_stdout,
                    nonzero_stderr,
                    nonzero_exit,
                    nonzero_token,
                    nonzero_diag,
                ) = await run_stdio_child(
                    mode=LocalProcessStdioMode.CAPTURE,
                    argument="nonzero",
                    expected_exit=7,
                )
                self.assertEqual(nonzero_stdout, _STDIO_NONZERO_STDOUT)
                self.assertEqual(nonzero_stderr, _STDIO_NONZERO_STDERR)
                self.assertEqual(nonzero_exit, 7)
                self.assertTrue(nonzero_token)

                self.assertEqual(len(capture_stdout), len(stdout_expected))
                self.assertEqual(len(capture_stderr), len(stderr_expected))
                self.assertEqual(len(merged_stdout), len(merged_expected))
                self.assertEqual(len(protocol_input), len(protocol_stdout))
                print(
                    "W3_GATE4_RESULTS="
                    + json.dumps(
                        {
                            "capture": {
                                "stdout_expected_bytes": len(stdout_expected),
                                "stdout_actual_bytes": len(capture_stdout),
                                "stdout_exact": capture_stdout == stdout_expected,
                                "stderr_expected_bytes": len(stderr_expected),
                                "stderr_actual_bytes": len(capture_stderr),
                                "stderr_exact": capture_stderr == stderr_expected,
                                "exit": capture_exit,
                                "tail_preserved": capture_stdout.endswith(
                                    _STDIO_CAPTURE_STDOUT_TRAILER
                                )
                                and capture_stderr.endswith(_STDIO_CAPTURE_STDERR_TRAILER),
                            },
                            "merged_capture": {
                                "expected_bytes": len(merged_expected),
                                "actual_bytes": len(merged_stdout),
                                "exact": merged_stdout == merged_expected,
                                "stderr_none": merged_stderr == b"",
                                "order_exact": merged_stdout == merged_expected,
                                "exit": merged_exit,
                            },
                            "protocol": {
                                "stdin_bytes": len(protocol_input),
                                "largest_write_stdin_bytes": len(protocol_input),
                                "frames": 2,
                                "stdout_exact": protocol_stdout == protocol_input,
                                "stderr_exact": protocol_stderr == _STDIO_PROTOCOL_DIAGNOSTIC,
                                "stdout_contamination": _STDIO_PROTOCOL_DIAGNOSTIC
                                in protocol_stdout,
                                "close_stdin_eof": protocol_exit == 0,
                            },
                            "nonzero": {
                                "stdout_exact": nonzero_stdout == _STDIO_NONZERO_STDOUT,
                                "stderr_exact": nonzero_stderr == _STDIO_NONZERO_STDERR,
                                "exit": nonzero_exit,
                            },
                            "output_after_exit": False,
                            "token_attestation": all(
                                (
                                    capture_token,
                                    merged_token,
                                    protocol_token,
                                    nonzero_token,
                                )
                            ),
                            "diagnostic_runner_states": [
                                diagnostic.get("runner")
                                for diagnostic in (
                                    capture_diag,
                                    merged_diag,
                                    protocol_diag,
                                    nonzero_diag,
                                )
                                if isinstance(diagnostic, dict)
                            ],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)


if __name__ == "__main__":
    unittest.main()
