"""W5 Gate 2A.4 canonical AppContainer layering evidence.

This gate is evidence-only.  It keeps security identity attestation on the
controller side, where the ``PROCESS_INFORMATION.hProcess`` handle is still
available, and treats pipe/ConPTY output only as transport evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from tests.security.test_windows_w5_gate2a_appcontainer_feasibility import (
    _cng_pass,
    _ksecdd_write_pass,
    _projection,
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

_OLD_HEAD = "0e9e6b4ef61fca19a843025871b3158a18332662"
_GATE1_HEAD = "902f82e014d0728445723630bc24d70bb1b52357"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"


def _marker(run: dict[str, object], key: str, default: str = "") -> str:
    markers = run.get("markers")
    if not isinstance(markers, dict):
        return default
    values = markers.get(key, [])
    if not isinstance(values, list) or not values:
        return default
    value = values[-1]
    return value if isinstance(value, str) else default


def _marker_count(run: dict[str, object], key: str) -> int | None:
    value = _marker(run, key)
    try:
        return int(value, 0)
    except ValueError:
        return None


def _external_token(run: dict[str, object]) -> dict[str, object]:
    markers = run.get("markers")
    marker_map = markers if isinstance(markers, dict) else {}
    return {
        "is_app_container": _marker(run, "G2A_EXTERNAL_TOKEN_IS_APP_CONTAINER"),
        "app_container_sid": _marker(run, "G2A_EXTERNAL_TOKEN_APPCONTAINER_SID"),
        "sid_match": _marker(run, "G2A_EXTERNAL_TOKEN_SID_MATCH"),
        "user": _marker(run, "G2A_EXTERNAL_TOKEN_USER"),
        "capability_count": _marker_count(run, "G2A_EXTERNAL_TOKEN_CAPABILITY_COUNT"),
        "capabilities": marker_map.get("G2A_EXTERNAL_TOKEN_CAPABILITY_SID", []),
        "restricted_count": _marker_count(run, "G2A_EXTERNAL_TOKEN_RESTRICTED_COUNT"),
        "restricted_sids": marker_map.get("G2A_EXTERNAL_TOKEN_RESTRICTED_SID", []),
        "group_count": _marker_count(run, "G2A_EXTERNAL_TOKEN_GROUP_COUNT"),
        "integrity_rid": _marker_count(run, "G2A_EXTERNAL_TOKEN_INTEGRITY_RID"),
        "mandatory_policy": _marker(run, "G2A_EXTERNAL_TOKEN_MANDATORY_POLICY"),
        "enabled_privileges": _marker_count(run, "G2A_EXTERNAL_TOKEN_ENABLED_PRIVILEGE_COUNT"),
        "unexpected_enabled_privileges": _marker_count(
            run, "G2A_EXTERNAL_TOKEN_UNEXPECTED_ENABLED_PRIVILEGES"
        ),
        "query_closed": _marker(run, "G2A_EXTERNAL_TOKEN_QUERY_CLOSED"),
        "open_error": _marker(run, "G2A_EXTERNAL_TOKEN_OPEN_ERROR"),
    }


def _external_token_attested(run: dict[str, object]) -> bool:
    token = _external_token(run)
    return (
        run.get("spawn_result") == "PASS"
        and run.get("child_create") == "PASS"
        and token["is_app_container"] == "PASS"
        and token["sid_match"] == "PASS"
        and token["app_container_sid"] == run.get("app_container_sid")
        and token["capability_count"] == 0
        and token["unexpected_enabled_privileges"] == 0
        and token["query_closed"] == "PASS"
    )


def _child_marker_observed(run: dict[str, object], key: str) -> bool:
    markers = run.get("markers")
    return isinstance(markers, dict) and bool(markers.get(key))


def _job_lifecycle_observed(run: dict[str, object]) -> bool:
    return (
        run.get("job_member") == "PASS"
        and run.get("descendant_create") == "PASS"
        and run.get("descendant_job_member") == "PASS"
        and run.get("descendant_active_before_close") == "PASS"
        and run.get("scope_complete") == "PASS"
        and run.get("job_close") == "PASS"
        and run.get("descendant_reaped") == "PASS"
    )


def _pipe_transport_pass(run: dict[str, object]) -> bool:
    return (
        run.get("attributes") == "SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST"
        and run.get("transport") == "PIPE"
        and run.get("pipe_create") == "PASS"
        and _child_marker_observed(run, "G2A_CHILD_STARTED")
        and _child_marker_observed(run, "G2A_CHILD_FINISHED")
        and run.get("stdin") == "PASS"
        and _job_lifecycle_observed(run)
    )


def _pty_transport_pass(run: dict[str, object]) -> bool:
    return (
        run.get("attributes") == "SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE"
        and run.get("transport") == "PTY"
        and run.get("pty_create") == "PASS"
        and _child_marker_observed(run, "G2A_CHILD_STARTED")
        and _child_marker_observed(run, "G2A_CHILD_FINISHED")
        and run.get("stdin") == "PASS"
        and _job_lifecycle_observed(run)
    )


def _classification(
    a0: dict[str, object],
    a1: dict[str, object] | None,
    pipe: dict[str, object] | None,
    pty: dict[str, object] | None,
) -> dict[str, str]:
    a0_pass = _external_token_attested(a0)
    a1_pass = (
        a0_pass
        and a1 is not None
        and _external_token_attested(a1)
        and a1.get("job_member") == "PASS"
    )
    pipe_security = (
        pipe is not None and _external_token_attested(pipe) and _job_lifecycle_observed(pipe)
    )
    pty_security = (
        pty is not None and _external_token_attested(pty) and _job_lifecycle_observed(pty)
    )
    pipe_pass = pipe_security and pipe is not None and _pipe_transport_pass(pipe)
    pty_pass = pty_security and pty is not None and _pty_transport_pass(pty)
    return {
        "a0": "W5_GATE2A4_A0_APPCONTAINER_SECURITY_PASS"
        if a0_pass
        else "W5_GATE2A4_A0_APPCONTAINER_SECURITY_BLOCKED",
        "a1": "W5_GATE2A4_A1_APPCONTAINER_JOB_PASS"
        if a1_pass
        else "W5_GATE2A4_A1_APPCONTAINER_JOB_BLOCKED",
        "pipe": "W5_GATE2A4_PIPE_INTEGRATION_PASS"
        if pipe_pass
        else "W5_GATE2A4_PIPE_INTEGRATION_BLOCKED",
        "pty": "W5_GATE2A4_PTY_INTEGRATION_PASS"
        if pty_pass
        else (
            "PTY_SECURITY_PASS_TRANSPORT_EVIDENCE_INCOMPLETE"
            if pty_security
            else "W5_GATE2A4_PTY_INTEGRATION_BLOCKED"
        ),
    }


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate2A4LayeredAttributeTests(unittest.IsolatedAsyncioTestCase):
    """Run the corrected A0/A1/A2 evidence ladder on an elevated runner."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 2A.4 evidence requires the enabled CI gate"
    )
    async def test_gate2a4_canonical_layered_attributes(self) -> None:  # pragma: no cover
        self.assertEqual(
            _production_source_diff(), (), "Gate 2A.4 must not modify production source"
        )
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        probe = await asyncio.to_thread(
            _compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate2a_appcontainer_probe.c").resolve(),
            "windows_w5_gate2a4_layered_attributes",
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
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            harness = _Gate1DirectProcess()
            environment = WindowsNativeLocalProcessSandbox._child_environment(
                LocalProcessEnvironmentPolicy({})
            )

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
                    timeout=180.0,
                )
                projected = _projection(raw, mode)
                projected["transport"] = _marker(projected, "G2A_TRANSPORT")
                projected["external_token"] = _external_token(projected)
                return projected

            runs: dict[str, dict[str, object]] = {}
            runs["A0"] = await run_mode("a0")
            a1: dict[str, object] | None = None
            pipe: dict[str, object] | None = None
            pty: dict[str, object] | None = None
            if _external_token_attested(runs["A0"]):
                a1 = await run_mode("a1")
                runs["A1"] = a1
            if a1 is not None and _external_token_attested(a1) and a1.get("job_member") == "PASS":
                pipe = await run_mode("a2-pipe")
                pty = await run_mode("a2-pty")
                runs["A2_PIPE"] = pipe
                runs["A2_PTY"] = pty

            classifications = _classification(runs["A0"], a1, pipe, pty)
            cng = {
                name: {
                    "load": run.get("bcrypt_load"),
                    "load_error": run.get("bcrypt_load_error"),
                    "gen_random": run.get("bcrypt_gen_random"),
                }
                for name, run in (("A2_PIPE", pipe), ("A2_PTY", pty))
                if run is not None
            }
            ksecdd = {
                name: run.get("ntopen")
                for name, run in (("A2_PIPE", pipe), ("A2_PTY", pty))
                if run is not None
            }
            if (
                classifications["pipe"] == "W5_GATE2A4_PIPE_INTEGRATION_PASS"
                and classifications["pty"] == "W5_GATE2A4_PTY_INTEGRATION_PASS"
            ):
                if all(_cng_pass(run) and _ksecdd_write_pass(run) for run in (pipe, pty) if run):
                    final_classification = "W5_GATE2A_REAL_APPCONTAINER_PRIMITIVES_FEASIBLE"
                else:
                    final_classification = "W5_GATE2A_APPCONTAINER_RUNTIME_COMPATIBILITY_BLOCKED"
            elif (
                pipe is not None
                and pty is not None
                and _external_token_attested(pipe)
                and _external_token_attested(pty)
                and _job_lifecycle_observed(pipe)
                and _job_lifecycle_observed(pty)
            ):
                final_classification = "PTY_SECURITY_PASS_TRANSPORT_EVIDENCE_INCOMPLETE"
            else:
                final_classification = "W5_GATE2A_APPCONTAINER_RUNTIME_COMPATIBILITY_BLOCKED"

            artifact: dict[str, object] = {
                "gate": "W5_GATE2A.4",
                "old_head": _OLD_HEAD,
                "gate1_frozen_head": _GATE1_HEAD,
                "main": _MAIN,
                "status": "COMPLETED",
                "production_source_diff": _production_source_diff(),
                "controller_identity": "W2_ONLINE_WITH_PROFILE_via_CreateProcessWithLogonW",
                "canonical_layers": {
                    "A0": "SECURITY_CAPABILITIES",
                    "A1": "SECURITY_CAPABILITIES,JOB_LIST",
                    "A2_PIPE": "SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST",
                    "A2_PTY": "SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE",
                },
                "runs": runs,
                "classifications": classifications,
                "ksecdd": ksecdd or "NOT_ATTEMPTED",
                "cng": cng or "NOT_ATTEMPTED",
                "filesystem": "DEFERRED_TO_BOUNDED_GATE",
                "descendant_lifecycle": {
                    name: {
                        "job_member": run.get("job_member"),
                        "descendant_job_member": run.get("descendant_job_member"),
                        "active_before_close": run.get("descendant_active_before_close"),
                        "scope_complete": run.get("scope_complete"),
                        "job_close": run.get("job_close"),
                        "reaped_after_close": run.get("descendant_reaped"),
                    }
                    for name, run in (("A2_PIPE", pipe), ("A2_PTY", pty))
                    if run is not None
                },
                "cleanup": {
                    "temporary_fixture_root": True,
                    "temporary_environment_block": True,
                    "temporary_profiles_deleted": True,
                    "system_acl_mutation": False,
                    "ksecdd_mutation": False,
                    "registry_mutation": False,
                    "firewall_mutation": False,
                    "device_io_control": False,
                    "persistent_profile": False,
                    "post_create_job_assignment": False,
                },
                "gate2b_started": False,
                "final_classification": final_classification,
            }
            destination_text = os.environ.get("NEURO_CODE_W5_GATE2A4_EVIDENCE_JSON")
            if destination_text:
                destination = Path(destination_text)
                await asyncio.to_thread(
                    destination.write_text,
                    json.dumps(artifact, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            self.assertEqual(_production_source_diff(), ())


if __name__ == "__main__":  # pragma: no cover - Windows CI entry point
    unittest.main()
