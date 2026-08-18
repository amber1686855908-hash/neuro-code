"""W5 Gate 2A evidence for a real Windows AppContainer launch boundary.

This is an architecture spike, not a production compatibility change.  The
native helper creates one disposable AppContainer profile and launches one
final child with SECURITY_CAPABILITIES plus the existing Job and stdio/PTY
attributes.  The test records what Windows actually did and never changes
``src/neuro_code`` or any system object ACL.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.security.test_windows_native_pty_acceptance import _compile_msvc_probe
from tests.security.test_windows_w5_gate1_6_loader_isolation import (
    _production_source_diff,
)
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
)
from tests.security.test_windows_w5_gate1_runtime_root_cause import (
    _LOGON_WITH_PROFILE,
    _Gate1DirectProcess,
    _native_enabled,
)

from neuro_code.application.ports.sandbox import LocalProcessEnvironmentPolicy
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import _NativeWindowsAclApi
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import (
    _NativeWindowsFirewallApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _NativeWindowsSetupPrivilegeApi,
)

_HEAD = "902f82e014d0728445723630bc24d70bb1b52357"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_MARKER = re.compile(r"G2A_[A-Z0-9_]+=[^\r\n]*")
_STATUS_SUCCESS = 0
_ACCESS_DENIED = 0xC0000022


def _parse_markers(data: bytes) -> dict[str, list[str]]:
    """Parse bounded marker lines while retaining repeated token facts."""

    text = data.decode("utf-8", errors="replace").replace("\x1b", "")
    markers: dict[str, list[str]] = {}
    for line in text.replace("\r", "").splitlines():
        for match in _MARKER.finditer(line):
            value = match.group(0)
            key, separator, payload = value.partition("=")
            if separator:
                markers.setdefault(key, []).append(payload[:512])
            else:
                markers.setdefault(key, []).append("OBSERVED")
    return markers


def _first(markers: dict[str, list[str]], key: str, default: str = "") -> str:
    values = markers.get(key, [])
    return values[0] if values else default


def _last(markers: dict[str, list[str]], key: str, default: str = "") -> str:
    values = markers.get(key, [])
    return values[-1] if values else default


def _int_value(value: str) -> int | None:
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 16)
        except ValueError:
            return None


def _status(markers: dict[str, list[str]], key: str) -> int | None:
    value = _last(markers, key)
    if "|" in value:
        value = value.split("|", 1)[0]
    return _int_value(value)


def _operation(markers: dict[str, list[str]], key: str) -> dict[str, object]:
    value = _last(markers, key, "INCONCLUSIVE")
    status, separator, error = value.partition("|ERROR=")
    return {
        "result": status,
        "error": _int_value(error) if separator else None,
    }


def _projection(raw: dict[str, object], mode: str) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    app_sid = _first(markers, "G2A_APP_CONTAINER_SID")
    token_sid = _last(markers, "G2A_TOKEN_APPCONTAINER_SID")
    writes = {
        key: _operation(markers, f"G2A_FS_{key}_WRITE")
        for key in (
            "AUTHORIZED_WORKSPACE",
            "OUTSIDE_USER_ONLY",
            "APPCONTAINER_SID_ONLY",
            "ALL_APPLICATION_PACKAGES_ONLY",
            "ALL_RESTRICTED_APPLICATION_PACKAGES_ONLY",
            "SENSITIVE_READ",
            "READ_ONLY",
        )
    }
    reads = {key: _operation(markers, f"G2A_FS_{key}_READ") for key in writes}
    ntopen = {
        access: _status(markers, f"G2A_NTOPEN_{access}")
        for access in ("0x100000", "0x100001", "0x100002", "0x100003")
    }
    path_facts = {
        key.removeprefix("G2A_PATH_"): values[-1]
        for key, values in markers.items()
        if key.startswith("G2A_PATH_") and values
    }
    return {
        "mode": mode,
        "spawn_result": raw.get("spawn_result"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "worker_alive": raw.get("worker_alive"),
        "profile_create": _first(markers, "G2A_PROFILE_CREATE"),
        "profile_create_hresult": _first(markers, "G2A_PROFILE_CREATE_HRESULT"),
        "profile_delete": _last(markers, "G2A_PROFILE_DELETE"),
        "profile_delete_hresult": _last(markers, "G2A_PROFILE_DELETE_HRESULT"),
        "app_container_sid": app_sid,
        "environment_variant": _last(markers, "G2A_ENV_VARIANT"),
        "environment_create": _last(markers, "G2A_ENV_CREATE"),
        "environment_create_error": _int_value(_last(markers, "G2A_ENV_CREATE_ERROR")),
        "environment_destroy": _last(markers, "G2A_ENV_DESTROY"),
        "environment": {
            key: _last(markers, f"G2A_ENV_{key}")
            for key in (
                "USERNAME",
                "USERDOMAIN",
                "USERPROFILE",
                "LOCALAPPDATA",
                "APPDATA",
                "TEMP",
                "TMP",
                "PATH",
            )
        },
        "environment_paths": {
            key: _last(markers, f"G2A_ENV_{key}_EXISTS")
            for key in ("USERPROFILE", "LOCALAPPDATA", "TEMP")
        },
        "process_api": _last(markers, "G2A_PROCESS_API"),
        "lp_application_name": _last(markers, "G2A_LP_APPLICATION_NAME"),
        "command_executable": _last(markers, "G2A_COMMAND_EXECUTABLE"),
        "command_line": _last(markers, "G2A_COMMAND_LINE"),
        "lp_current_directory": _last(markers, "G2A_LP_CURRENT_DIRECTORY"),
        "current_directory_variant": _last(markers, "G2A_CURRENT_DIRECTORY_VARIANT"),
        "controller_current_directory": _last(markers, "G2A_CONTROLLER_CURRENT_DIRECTORY"),
        "path_attestation": _last(markers, "G2A_PATH_ATTESTATION"),
        "path_facts": path_facts,
        "createprocess_error": _int_value(_last(markers, "G2A_CREATEPROCESS_ERROR")),
        "child_create_error": _int_value(_last(markers, "G2A_CHILD_CREATE_ERROR")),
        "attribute_list_error": _int_value(_last(markers, "G2A_ATTRIBUTE_LIST_ERROR")),
        "createprocess_call": _last(markers, "G2A_CREATEPROCESS_CALL"),
        "child_create": _last(markers, "G2A_CHILD_CREATE"),
        "inherit_handles": _last(markers, "G2A_INHERIT_HANDLES"),
        "token_is_app_container": _last(markers, "G2A_TOKEN_IS_APP_CONTAINER"),
        "token_app_container_sid": token_sid,
        "token_user": _last(markers, "G2A_TOKEN_USER"),
        "token_capability_count": _int_value(_last(markers, "G2A_TOKEN_CAPABILITY_COUNT")),
        "token_capabilities": markers.get("G2A_TOKEN_CAPABILITY_SID", []),
        "token_restricted_count": _int_value(_last(markers, "G2A_TOKEN_RESTRICTED_COUNT")),
        "token_restricted_sids": markers.get("G2A_TOKEN_RESTRICTED_SID", []),
        "token_group_count": _int_value(_last(markers, "G2A_TOKEN_GROUP_COUNT")),
        "token_integrity_rid": _int_value(_last(markers, "G2A_TOKEN_INTEGRITY_RID")),
        "token_mandatory_policy": _last(markers, "G2A_TOKEN_MANDATORY_POLICY"),
        "token_enabled_privileges": _int_value(_last(markers, "G2A_TOKEN_ENABLED_PRIVILEGE_COUNT")),
        "unexpected_enabled_privileges": _int_value(
            _last(markers, "G2A_TOKEN_UNEXPECTED_ENABLED_PRIVILEGES")
        ),
        "attributes": _last(markers, "G2A_ATTRIBUTES"),
        "pipe_create": _first(markers, "G2A_PIPE_CREATE"),
        "pty_create": _first(markers, "G2A_PTY_CREATE"),
        "job_member": _last(markers, "G2A_JOB_MEMBER"),
        "scope_complete": _last(markers, "G2A_SCOPE_COMPLETE"),
        "job_close": _last(markers, "G2A_JOB_CLOSE"),
        "descendant_create": _last(markers, "G2A_DESCENDANT_CREATE"),
        "descendant_active_before_close": _last(markers, "G2A_DESCENDANT_ACTIVE_BEFORE_CLOSE"),
        "descendant_job_member": _last(markers, "G2A_DESCENDANT_JOB_MEMBER"),
        "descendant_reaped": _last(markers, "G2A_DESCENDANT_REAPED"),
        "stdin": _last(markers, "G2A_STDIN"),
        "ntopen": ntopen,
        "bcrypt_load": _last(markers, "G2A_BCRYPT_LOAD"),
        "bcrypt_load_error": _int_value(_last(markers, "G2A_BCRYPT_LOAD_ERROR")),
        "bcrypt_gen_random": _int_value(_last(markers, "G2A_BCRYPT_GEN_RANDOM")),
        "filesystem": {"read": reads, "write": writes},
        "markers": markers,
        "stdout_preview": raw.get("stdout_preview", ""),
        "stderr_preview": raw.get("stderr_preview", ""),
    }


def _token_attested(run: dict[str, object]) -> bool:
    return (
        run.get("spawn_result") == "PASS"
        and run.get("profile_create") == "PASS"
        and run.get("token_is_app_container") == "PASS"
        and bool(run.get("app_container_sid"))
        and run.get("token_app_container_sid") == run.get("app_container_sid")
        and run.get("token_capability_count") == 0
        and run.get("unexpected_enabled_privileges") == 0
    )


def _cng_pass(run: dict[str, object]) -> bool:
    return run.get("bcrypt_load") == "PASS" and run.get("bcrypt_gen_random") == _STATUS_SUCCESS


def _ksecdd_write_pass(run: dict[str, object]) -> bool:
    ntopen = run.get("ntopen")
    if not isinstance(ntopen, dict):
        return False
    return ntopen.get("0x100002") == _STATUS_SUCCESS and ntopen.get("0x100003") == _STATUS_SUCCESS


def _launch_pass(run: dict[str, object], mode: str) -> bool:
    expected = (
        "SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE"
        if mode == "pty"
        else ("SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST")
    )
    return (
        run.get("attributes") == expected
        and run.get("job_member") == "PASS"
        and run.get("scope_complete") == "PASS"
        and run.get("job_close") == "PASS"
        and run.get("descendant_reaped") == "PASS"
        and run.get("stdin") == "PASS"
    )


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _classify(pipe: dict[str, object], pty: dict[str, object]) -> str:
    runs = (pipe, pty)
    genuine = all(_token_attested(run) for run in runs)
    cng = all(_cng_pass(run) for run in runs)
    ksecdd = all(_ksecdd_write_pass(run) for run in runs)
    launch = _launch_pass(pipe, "pipe") and _launch_pass(pty, "pty")
    cleanup = all(run.get("profile_delete") == "PASS" for run in runs)
    if genuine and cng and ksecdd and launch and cleanup:
        return "W5_GATE2A_REAL_APPCONTAINER_PRIMITIVES_FEASIBLE"
    if genuine and cng and ksecdd and cleanup and not launch:
        return "W5_GATE2A_APPCONTAINER_CNG_FEASIBLE_LAUNCH_INTEGRATION_BLOCKED"
    if genuine and (not cng or not ksecdd):
        return "W5_GATE2A_REAL_APPCONTAINER_NOT_FEASIBLE"
    return "W5_GATE2A_INCONCLUSIVE"


class WindowsW5Gate2AAppContainerTests(unittest.IsolatedAsyncioTestCase):
    """Run the Gate 2A probe once on an elevated Windows evidence runner."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 2A evidence requires the enabled CI gate"
    )
    async def test_gate2a_real_appcontainer_feasibility(
        self,
    ) -> None:  # pragma: no cover - Windows CI
        self.assertEqual(_production_source_diff(), (), "Gate 2A must not modify production source")
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        probe = await asyncio.to_thread(
            _compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate2a_appcontainer_probe.c").resolve(),
            "windows_w5_gate2a_appcontainer_probe",
        )
        self.addAsyncCleanup(_cleanup_probe_directory, probe.parent)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            fixtures = root / "fixtures"
            workspace.mkdir()
            installation.mkdir()
            workspace_probe = workspace / "windows_w5_gate2a_appcontainer_probe.exe"
            shutil.copy2(probe, workspace_probe)
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                account_api=_NativeWindowsSandboxAccountApi(),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            self.addAsyncCleanup(asyncio.to_thread, authority.cleanup, setup_request)
            encoded = store.load()
            self.assertIsNotNone(encoded)
            from neuro_code.infrastructure.sandbox.windows_sandbox_setup import _InstallationRecord

            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            harness = _Gate1DirectProcess()
            environment = WindowsNativeLocalProcessSandbox._child_environment(
                LocalProcessEnvironmentPolicy({})
            )
            results: dict[str, dict[str, object]] = {}

            async def run_mode(mode: str) -> dict[str, object]:
                raw = await asyncio.to_thread(
                    _run_harness_bounded,
                    harness,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=workspace_probe,
                    arguments=(mode, str(workspace), str(fixtures)),
                    cwd=workspace,
                    environment=environment,
                    logon_flags=_LOGON_WITH_PROFILE,
                    timeout=120.0,
                )
                return _projection(raw, mode)

            results["pipe"] = await run_mode("pipe")
            results["pty"] = await run_mode("pty")
            classification = _classify(results["pipe"], results["pty"])
            production_diff = _production_source_diff()
            artifact: dict[str, object] = {
                "gate": "W5_GATE2A",
                "old_head": _HEAD,
                "main": _MAIN,
                "status": "COMPLETED",
                "production_source_diff": production_diff,
                "controller_identity": "W2_ONLINE_WITH_PROFILE_via_CreateProcessWithLogonW",
                "profile_scope": "disposable_per_mode",
                "pipe": results["pipe"],
                "pty": results["pty"],
                "classification": classification,
                "cleanup": {
                    "temporary_fixture_root": True,
                    "system_acl_mutation": False,
                    "ksecdd_mutation": False,
                    "registry_mutation": False,
                    "firewall_mutation": False,
                    "device_io_control": False,
                    "persistent_profile": False,
                },
                "gate2b_required": True,
            }
            destination = os.environ.get("NEURO_CODE_W5_GATE2A_EVIDENCE_JSON")
            if destination:
                await asyncio.to_thread(
                    Path(destination).write_text,
                    json.dumps(artifact, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            self.assertEqual(production_diff, ())
