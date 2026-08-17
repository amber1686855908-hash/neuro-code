"""W5 Gate 2A.1 evidence for AppContainer profile prerequisites.

This gate compares the same native profile probe under the host, the existing
W2 Online account without a loaded profile, and that account with
``LOGON_WITH_PROFILE``.  It deliberately stops at profile creation: no
AppContainer child, filesystem authority, KsecDD, CNG, Job, or ConPTY probe is
started here.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tests.security.test_windows_native_pty_acceptance import _compile_msvc_probe
from tests.security.test_windows_w5_gate1_6_loader_isolation import (
    _production_source_diff,
)
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
)
from tests.security.test_windows_w5_gate1_runtime_root_cause import (
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
    _InstallationRecord,
    _NativeWindowsSetupPrivilegeApi,
)

_HEAD = "f030a798fe46803967b1ee9197549c542e155b23"
_GATE1_HEAD = "902f82e014d0728445723630bc24d70bb1b52357"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_LOGON_WITH_PROFILE = 0x00000001
_ERROR_FILE_NOT_FOUND_HRESULT = 0x80070002
_MAX_OUTPUT = 8192
_MARKER = re.compile(r"^W5_GATE21_([A-Z0-9_]+)=(.*)$")


def _parse_markers(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8", errors="replace").replace("\x1b", "")
    markers: dict[str, str] = {}
    for line in text.replace("\r", "").splitlines():
        if line in {"W5_GATE21_PROBE_STARTED", "W5_GATE21_PROBE_FINISHED"}:
            markers[line.removeprefix("W5_GATE21_")] = "OBSERVED"
            continue
        match = _MARKER.fullmatch(line)
        if match is not None:
            markers[match.group(1)] = match.group(2)[:512]
    return markers


def _status(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _host_run(
    executable: Path,
    *,
    arguments: tuple[str, ...],
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    """Run the identical helper as the current host identity, bounded."""

    command = [str(executable), *arguments]
    process: subprocess.Popen[bytes] | None = None
    stdout = b""
    stderr = b""
    result: dict[str, object] = {
        "execution_path": "HOST/SUBPROCESS",
        "logon_flags": "HOST_CURRENT",
        "spawn_result": "NOT_STARTED",
        "exit_code": None,
        "timeout": False,
        "classification": "INCONCLUSIVE",
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            shell=False,
        )
        result["spawn_result"] = "PASS"
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            result["timeout"] = True
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        result["exit_code"] = process.returncode
    except (OSError, subprocess.SubprocessError) as error:
        result["classification"] = type(error).__name__
        result["error"] = getattr(error, "winerror", None) or getattr(error, "errno", None)
    result["markers"] = _parse_markers(stdout[:_MAX_OUTPUT])
    result["stdout_preview"] = stdout[:512].decode("utf-8", errors="replace")
    result["stderr_preview"] = stderr[:512].decode("utf-8", errors="replace")
    return result


def _project(raw: dict[str, object], context: str) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    raw_markers = raw.get("markers")
    markers = (
        cast(dict[str, str], raw_markers)
        if isinstance(raw_markers, dict)
        else _parse_markers(output[:_MAX_OUTPUT])
    )
    result: dict[str, object] = {
        "context": context,
        "execution_path": raw.get("execution_path"),
        "logon_flags": raw.get("logon_flags"),
        "spawn_result": raw.get("spawn_result"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "worker_alive": raw.get("worker_alive"),
        "probe_started": "PROBE_STARTED" in markers,
        "probe_finished": "PROBE_FINISHED" in markers,
        "token_user_sid": markers.get("TOKEN_USER_SID"),
        "username": markers.get("USERNAME"),
        "hku_sid_status": _status(markers.get("HKU_SID_STATUS")),
        "hku_sid": markers.get("HKU_SID"),
        "profile_directory": markers.get("PROFILE_DIRECTORY"),
        "profile_directory_error": _status(markers.get("PROFILE_DIRECTORY_ERROR")),
        "profile_directory_path": markers.get("PROFILE_DIRECTORY_PATH"),
        "profile_directory_exists": markers.get("PROFILE_DIRECTORY_EXISTS"),
        "userprofile": markers.get("ENV_USERPROFILE"),
        "localappdata": markers.get("ENV_LOCALAPPDATA"),
        "localappdata_exists": markers.get("LOCALAPPDATA_EXISTS"),
        "appdata": markers.get("ENV_APPDATA"),
        "temp": markers.get("ENV_TEMP"),
        "tmp": markers.get("ENV_TMP"),
        "current_user_status": _status(markers.get("CURRENT_USER_STATUS")),
        "current_user": markers.get("CURRENT_USER"),
        "profile_arguments": markers.get("PROFILE_ARGUMENTS"),
        "profile_create": markers.get("PROFILE_CREATE"),
        "profile_create_hresult": markers.get("PROFILE_CREATE_HRESULT"),
        "profile_create_hresult_value": _status(markers.get("PROFILE_CREATE_HRESULT")),
        "profile_name": markers.get("PROFILE_NAME"),
        "profile_sid": markers.get("PROFILE_SID"),
        "profile_derive_hresult": markers.get("PROFILE_DERIVE_HRESULT"),
        "profile_derived_sid_match": markers.get("PROFILE_DERIVED_SID_MATCH"),
        "profile_existing_derived": markers.get("PROFILE_EXISTING_DERIVED"),
        "profile_delete": markers.get("PROFILE_DELETE"),
        "profile_delete_hresult": markers.get("PROFILE_DELETE_HRESULT"),
        "markers": markers,
        "stdout_preview": raw.get("stdout_preview", ""),
        "stderr_preview": raw.get("stderr_preview", ""),
    }
    return result


def _profile_outcome(row: dict[str, object]) -> tuple[str | None, int | None]:
    return (
        cast(str | None, row.get("profile_create")),
        cast(int | None, row.get("profile_create_hresult_value")),
    )


def _classify(matrix: dict[str, dict[str, object]]) -> str:
    no_profile = matrix["W2_ONLINE_NO_PROFILE"]
    with_profile = matrix["W2_ONLINE_WITH_PROFILE"]
    host = matrix["HOST_CURRENT"]
    no_outcome = _profile_outcome(no_profile)
    with_outcome = _profile_outcome(with_profile)
    host_outcome = _profile_outcome(host)
    if (
        no_outcome == ("FAIL", _ERROR_FILE_NOT_FOUND_HRESULT)
        and with_outcome[0] == "PASS"
        and with_profile.get("profile_derived_sid_match") == "PASS"
        and with_profile.get("profile_delete") == "PASS"
    ):
        return "W5_GATE2A1_PROFILE_LOAD_PRECONDITION_ESTABLISHED"
    if (
        host_outcome == ("FAIL", _ERROR_FILE_NOT_FOUND_HRESULT)
        and no_outcome == host_outcome
        and with_outcome == host_outcome
        and host.get("profile_directory_exists") == "YES"
        and host.get("hku_sid") == "LOADED"
    ):
        return "W5_GATE2A1_RUNNER_APPCONTAINER_PROFILE_ENVIRONMENT_BLOCKED"
    if no_outcome == with_outcome:
        return "W5_GATE2A1_PROFILE_LOAD_NOT_CAUSAL"
    return "W5_GATE2A1_PROFILE_CAUSE_STILL_UNATTRIBUTED"


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate2A1ProfilePrerequisiteTests(unittest.IsolatedAsyncioTestCase):
    """Compare profile contexts without launching an AppContainer child."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 2A.1 evidence requires the enabled CI gate"
    )
    async def test_gate2a1_profile_prerequisite_matrix(self) -> None:  # pragma: no cover
        production_diff = _production_source_diff()
        self.assertEqual(production_diff, (), "Gate 2A.1 must not modify production source")
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")

        probe = await asyncio.to_thread(
            _compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate2a1_profile_probe.c").resolve(),
            "windows_w5_gate2a1_profile_probe",
        )
        self.addAsyncCleanup(_cleanup_probe_directory, probe.parent)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            workspace.mkdir()
            installation.mkdir()
            workspace_probe = workspace / "windows_w5_gate2a1_profile_probe.exe"
            workspace_probe.write_bytes(probe.read_bytes())
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
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            harness = _Gate1DirectProcess()
            environment = WindowsNativeLocalProcessSandbox._child_environment(
                LocalProcessEnvironmentPolicy({})
            )

            matrix: dict[str, dict[str, object]] = {}
            profile_name = f"NeuroCodeW5A1-{uuid.uuid4().hex[:24]}"
            matrix["HOST_CURRENT"] = _project(
                await asyncio.to_thread(
                    _host_run,
                    workspace_probe,
                    arguments=(profile_name,),
                    cwd=workspace,
                    environment=environment,
                ),
                "HOST_CURRENT",
            )
            for context, flags in (
                ("W2_ONLINE_NO_PROFILE", 0),
                ("W2_ONLINE_WITH_PROFILE", _LOGON_WITH_PROFILE),
            ):
                raw = await asyncio.to_thread(
                    _run_harness_bounded,
                    harness,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=workspace_probe,
                    arguments=(profile_name,),
                    cwd=workspace,
                    environment=environment,
                    logon_flags=flags,
                    timeout=45.0,
                )
                matrix[context] = _project(raw, context)

            classification = _classify(matrix)
            cleanup = {
                "profile_delete_all_pass_or_not_created": all(
                    row.get("profile_delete") in {"PASS", "NOT_CREATED"} for row in matrix.values()
                ),
                "system_acl_mutation": False,
                "registry_mutation": False,
                "firewall_mutation": False,
                "persistent_profile": False,
            }
            same_profile_name = all(
                row.get("profile_name") == profile_name for row in matrix.values()
            )
            artifact: dict[str, object] = {
                "gate": "W5_GATE2A.1",
                "old_head": _HEAD,
                "gate1_frozen_head": _GATE1_HEAD,
                "main": _MAIN,
                "production_source_diff": production_diff,
                "status": "COMPLETED",
                "classification": classification,
                "contexts": matrix,
                "profile_arguments": {
                    "name": profile_name,
                    "display_name": "same as unique profile name",
                    "description": "Neuro Code W5 Gate 2A.1",
                    "capabilities": None,
                    "capability_count": 0,
                },
                "same_profile_name_all_contexts": same_profile_name,
                "cleanup": cleanup,
                "conditional_gate2a_continuation": classification
                == "W5_GATE2A1_PROFILE_LOAD_PRECONDITION_ESTABLISHED",
                "gate2b_started": False,
            }
            destination = os.environ.get("NEURO_CODE_W5_GATE2A1_EVIDENCE_JSON")
            if destination:
                await asyncio.to_thread(
                    Path(destination).write_text,
                    json.dumps(artifact, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            self.assertEqual(production_diff, ())
            self.assertTrue(same_profile_name, "all contexts must use the same profile API name")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
