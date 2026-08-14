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
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
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
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import _NativeWindowsFirewallApi
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _NativeWindowsSetupPrivilegeApi,
)

_SENTINEL = "GATE2_NON_SECRET_SENTINEL"
_APPENDED = "GATE2_APPEND_SENTINEL"
_OVERWRITTEN = "GATE2_OVERWRITE_SENTINEL"
_NUL_CANARY_COMMAND = ("echo", "GATE2_NUL_CANARY>NUL")
ProbeState = Callable[[], dict[str, object]]
Command = tuple[str, ...]


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
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        executable,
        arguments,
        purpose=LocalProcessPurpose.BASH,
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
        stdio_mode=LocalProcessStdioMode.CAPTURE,
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
                command: str | Command,
                expected: str,
                capture_stdout: bool = False,
                expected_stdout: str | None = None,
            ) -> dict[str, object]:
                command_args = (command,) if isinstance(command, str) else command
                request = _request(
                    workspace=workspace,
                    network=network,
                    executable=executable,
                    arguments=("/d", "/s", "/c", *command_args),
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


if __name__ == "__main__":
    unittest.main()
