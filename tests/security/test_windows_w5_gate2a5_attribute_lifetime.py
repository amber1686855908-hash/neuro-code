"""W5 Gate 2A.5 evidence for AppContainer attribute backing lifetime.

This gate supersedes only the Gate 2A.4 harness result.  It changes no
production code and keeps the canonical A0/A1/A2 ladder behind a valid
``UpdateProcThreadAttribute`` backing-storage lifetime.
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
from tests.security.test_windows_w5_gate2a4_layered_attributes import (
    _external_token_attested,
    _marker,
    _pipe_transport_pass,
    _pty_transport_pass,
)
from tests.security.test_windows_w5_gate2a_appcontainer_feasibility import _projection

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

_OLD_HEAD = "86b5e6a82ff8cbb96da81f8170df90372543f897"
_GATE1_HEAD = "902f82e014d0728445723630bc24d70bb1b52357"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_ERROR_INVALID_HANDLE = 6


def _attribute_result(run: dict[str, object], name: str) -> str:
    return _marker(run, f"G2A_ATTRIBUTE_{name}")


def _job_backing(run: dict[str, object]) -> dict[str, object]:
    phases = ("BEFORE_ATTRIBUTES", "BEFORE_CREATE")
    facts: dict[str, object] = {}
    for phase in phases:
        facts[phase] = {
            "valid": _marker(run, f"G2A_JOB_{phase}_VALID"),
            "handle_flags": _marker(run, f"G2A_JOB_{phase}_HANDLE_FLAGS"),
            "limits": _marker(run, f"G2A_JOB_{phase}_LIMITS"),
            "limit_flags": _marker(run, f"G2A_JOB_{phase}_LIMIT_FLAGS"),
            "kill_on_close": _marker(run, f"G2A_JOB_{phase}_KILL_ON_CLOSE"),
        }
    return facts


def _job_backing_pass(run: dict[str, object]) -> bool:
    return all(
        _marker(run, f"G2A_JOB_{phase}_{field}") == expected
        for phase in ("BEFORE_ATTRIBUTES", "BEFORE_CREATE")
        for field, expected in (
            ("VALID", "PASS"),
            ("LIMITS", "PASS"),
            ("KILL_ON_CLOSE", "PASS"),
        )
    )


def _attribute_backing(run: dict[str, object]) -> dict[str, object]:
    names = (
        "SECURITY_CAPABILITIES",
        "JOB_LIST",
        "HANDLE_LIST",
        "PSEUDOCONSOLE",
    )
    return {name: _attribute_result(run, name) for name in names if _attribute_result(run, name)}


def _attribute_pass(run: dict[str, object], name: str) -> bool:
    return _attribute_result(run, name).startswith("PASS|CBSIZE=")


def _a0_pass(run: dict[str, object]) -> bool:
    return _external_token_attested(run) and _attribute_pass(run, "SECURITY_CAPABILITIES")


def _a1_pass(run: dict[str, object]) -> bool:
    return (
        _a0_pass(run)
        and _attribute_pass(run, "JOB_LIST")
        and _job_backing_pass(run)
        and run.get("job_member") == "PASS"
    )


def _cause_classification(
    a0: dict[str, object],
    a1: dict[str, object] | None,
    matrix: dict[str, dict[str, object]],
) -> str:
    if a1 is not None and _a1_pass(a1):
        return "W5_GATE2A5_ATTRIBUTE_LIFETIME_CAUSAL"
    if not matrix:
        return "W5_GATE2A5_CAUSE_STILL_UNATTRIBUTED"
    asuser_job = matrix.get("ASUSER_JOB_ONLY")
    asuser_combo = matrix.get("ASUSER_SECURITY_JOB")
    current_job = matrix.get("CURRENT_JOB_ONLY")
    current_combo = matrix.get("CURRENT_SECURITY_JOB")
    if (
        asuser_job is not None
        and current_job is not None
        and not _a1_pass(asuser_job)
        and not _a1_pass(current_job)
    ):
        return "W5_GATE2A5_JOBLIST_GENERIC_BLOCKED"
    if (
        asuser_job is not None
        and _a1_pass(asuser_job)
        and a0 is not None
        and _a0_pass(a0)
        and asuser_combo is not None
        and not _a1_pass(asuser_combo)
    ):
        return "W5_GATE2A5_APPCONTAINER_JOB_COMBINATION_BLOCKED"
    if (
        current_job is not None
        and _a1_pass(current_job)
        and current_combo is not None
        and _a1_pass(current_combo)
        and asuser_combo is not None
        and not _a1_pass(asuser_combo)
    ):
        return "W5_GATE2A5_CREATEPROCESSASUSER_INTERACTION_BLOCKED"
    return "W5_GATE2A5_CAUSE_STILL_UNATTRIBUTED"


def _runtime_classification(
    a0: dict[str, object],
    a1: dict[str, object] | None,
    pipe: dict[str, object] | None,
    pty: dict[str, object] | None,
) -> str:
    if (
        _a0_pass(a0)
        and a1 is not None
        and _a1_pass(a1)
        and pipe is not None
        and pty is not None
        and _pipe_transport_pass(pipe)
        and _pty_transport_pass(pty)
        and pipe.get("profile_delete") == "PASS"
        and pty.get("profile_delete") == "PASS"
    ):
        return "W5_GATE2A_REAL_APPCONTAINER_PRIMITIVES_FEASIBLE"
    if (
        a1 is not None
        and _job_backing_pass(a1)
        and a1.get("createprocess_error") == _ERROR_INVALID_HANDLE
    ):
        return "W5_GATE2A_APPCONTAINER_RUNTIME_COMPATIBILITY_BLOCKED"
    return "W5_GATE2A_INCONCLUSIVE"


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate2A5AttributeLifetimeTests(unittest.IsolatedAsyncioTestCase):
    """Run the corrected AppContainer attribute ladder on Windows."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 2A.5 evidence requires the enabled CI gate"
    )
    async def test_gate2a5_attribute_backing_lifetime(self) -> None:  # pragma: no cover
        self.assertEqual(
            _production_source_diff(), (), "Gate 2A.5 must not modify production source"
        )
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        probe = await asyncio.to_thread(
            _compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate2a_appcontainer_probe.c").resolve(),
            "windows_w5_gate2a5_attribute_lifetime",
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
            request = WindowsSandboxSetupRequest(
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
            snapshot = await asyncio.to_thread(authority.setup, request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            self.addAsyncCleanup(asyncio.to_thread, authority.cleanup, request)
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
                projected["attribute_backing"] = _attribute_backing(projected)
                projected["job_backing"] = _job_backing(projected)
                return projected

            runs: dict[str, dict[str, object]] = {}
            runs["A0"] = await run_mode("a0")
            a1: dict[str, object] | None = None
            pipe: dict[str, object] | None = None
            pty: dict[str, object] | None = None
            matrix: dict[str, dict[str, object]] = {}
            if _a0_pass(runs["A0"]):
                a1 = await run_mode("a1")
                runs["A1"] = a1
                if _a1_pass(a1):
                    pipe = await run_mode("a2-pipe")
                    pty = await run_mode("a2-pty")
                    runs["A2_PIPE"] = pipe
                    runs["A2_PTY"] = pty
                else:
                    for label, mode in (
                        ("ASUSER_JOB_ONLY", "a1-job-only"),
                        ("ASUSER_SECURITY_JOB", "a1"),
                        ("CURRENT_JOB_ONLY", "api-current-a1-job-only"),
                        ("CURRENT_SECURITY_JOB", "api-current-a1"),
                        ("CURRENT_SECURITY_ONLY", "api-current-a0"),
                    ):
                        matrix[label] = await run_mode(mode)

            cause = _cause_classification(runs["A0"], a1, matrix)
            runtime = _runtime_classification(runs["A0"], a1, pipe, pty)
            artifact: dict[str, object] = {
                "gate": "W5_GATE2A.5",
                "supersedes_gate": "W5_GATE2A.4",
                "supersede_reason": "UpdateProcThreadAttribute lpValue backing lifetime violated Microsoft contract",
                "old_head": _OLD_HEAD,
                "gate1_frozen_head": _GATE1_HEAD,
                "main": _MAIN,
                "status": "COMPLETED",
                "production_source_diff": _production_source_diff(),
                "controller_identity": "W2_ONLINE_WITH_PROFILE_via_CreateProcessWithLogonW",
                "lifetime_contract": {
                    "security_capabilities_old_scope": "g2a_update_common_attributes stack local",
                    "job_list_old_scope": "g2a_update_common_attributes stack local",
                    "new_scope": "g2a_create_child owning context through CreateProcess and DeleteProcThreadAttributeList",
                    "microsoft_contract_satisfied": True,
                },
                "canonical_layers": {
                    "A0": "SECURITY_CAPABILITIES",
                    "A1": "SECURITY_CAPABILITIES,JOB_LIST",
                    "A2_PIPE": "SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST",
                    "A2_PTY": "SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE",
                },
                "runs": runs,
                "a0": runs["A0"],
                "a1": a1,
                "a2_pipe": pipe,
                "a2_pty": pty,
                "job_list_attribution_matrix": matrix or "NOT_ATTEMPTED",
                "cause_classification": cause,
                "runtime_feasibility_classification": runtime,
                "ksecdd": "NOT_ATTEMPTED"
                if pipe is None or pty is None
                else {
                    "A2_PIPE": pipe.get("ntopen"),
                    "A2_PTY": pty.get("ntopen"),
                },
                "cng": "NOT_ATTEMPTED"
                if pipe is None or pty is None
                else {
                    "A2_PIPE": pipe.get("bcrypt_gen_random"),
                    "A2_PTY": pty.get("bcrypt_gen_random"),
                },
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
                    "temporary_profiles_deleted": all(
                        run.get("profile_delete") == "PASS"
                        for run in runs.values()
                        if isinstance(run, dict)
                    ),
                    "system_acl_mutation": False,
                    "ksecdd_mutation": False,
                    "registry_mutation": False,
                    "firewall_mutation": False,
                    "device_io_control": False,
                    "persistent_profile": False,
                    "post_create_job_assignment": False,
                },
                "gate2b_started": False,
            }
            destination_text = os.environ.get("NEURO_CODE_W5_GATE2A5_EVIDENCE_JSON")
            if destination_text:
                await asyncio.to_thread(
                    Path(destination_text).write_text,
                    json.dumps(artifact, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            self.assertEqual(_production_source_diff(), ())


if __name__ == "__main__":  # pragma: no cover - Windows CI entry point
    unittest.main()
