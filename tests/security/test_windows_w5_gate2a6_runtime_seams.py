"""W5 Gate 2A.6 final AppContainer runtime-seam attribution.

This is the final Gate 2A evidence gate.  It deliberately stays outside the
production sandbox: the native helper is copied into a disposable authorized
workspace and all setup, profiles, jobs, pipes, and ConPTY handles are owned by
the test and cleaned up before the test returns.
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
from tests.security.test_windows_w5_gate1_7_token_ablation import _run_harness_bounded
from tests.security.test_windows_w5_gate1_runtime_root_cause import (
    _LOGON_WITH_PROFILE,
    _Gate1DirectProcess,
    _native_enabled,
)
from tests.security.test_windows_w5_gate2a4_layered_attributes import (
    _external_token_attested,
    _marker,
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

_OLD_HEAD = "b17e7c43fd89be384a6d7302b431362f68f51907"
_GATE1_HEAD = "902f82e014d0728445723630bc24d70bb1b52357"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"


def _int_marker(run: dict[str, object], key: str) -> int | None:
    value = _marker(run, key)
    try:
        return int(value, 0)
    except ValueError:
        return None


def _descendant_projection(run: dict[str, object], mode: str) -> dict[str, object]:
    projected = dict(run)
    projected.update(
        {
            "mode": mode,
            "child_exit": _int_marker(projected, "G2A_CHILD_EXIT"),
            "descendant_create_error": _int_marker(projected, "G2A_DESCENDANT_CREATE_ERROR"),
            "descendant_policy_available": _marker(projected, "G2A_DESCENDANT_POLICY_AVAILABLE"),
            "no_child_process_creation": _marker(
                projected, "G2A_DESCENDANT_NO_CHILD_PROCESS_CREATION"
            ),
            "audit_no_child_process_creation": _marker(
                projected, "G2A_DESCENDANT_AUDIT_NO_CHILD_PROCESS_CREATION"
            ),
            "self_path_available": _marker(projected, "G2A_DESCENDANT_SELF_PATH_AVAILABLE"),
            "self_exists": _marker(projected, "G2A_DESCENDANT_SELF_EXISTS"),
            "self_readable": _marker(projected, "G2A_DESCENDANT_SELF_READABLE"),
            "workspace_exists": _marker(projected, "G2A_DESCENDANT_WORKSPACE_EXISTS"),
            "environment_present": _marker(projected, "G2A_DESCENDANT_ENV_PRESENT"),
            "local_timeout": _marker(projected, "G2A_LOCAL_TIMEOUT"),
            "last_milestone": _marker(projected, "G2A_LAST_MILESTONE"),
        }
    )
    return projected


def _descendant_owned_scope_pass(run: dict[str, object]) -> bool:
    return (
        run.get("child_create") == "PASS"
        and run.get("child_exit") == 0
        and run.get("job_member") == "PASS"
        and run.get("descendant_create") == "PASS"
        and run.get("descendant_job_member") == "PASS"
        and run.get("descendant_active_before_close") == "PASS"
        and run.get("scope_complete") == "PASS"
        and run.get("job_close") == "PASS"
        and run.get("descendant_reaped") == "PASS"
        and run.get("profile_delete") == "PASS"
    )


def _descendant_classification(
    control: dict[str, object], app_runs: dict[str, dict[str, object]]
) -> str:
    if not _descendant_owned_scope_pass(control):
        return "W5_GATE2A6_DESCENDANT_CAUSE_UNATTRIBUTED"
    app_pass = {name: _descendant_owned_scope_pass(run) for name, run in app_runs.items()}
    if any(app_pass.values()):
        if app_pass.get("APP_ORIGINAL"):
            return "W5_GATE2A6_DESCENDANT_PASS"
        return "W5_GATE2A6_DESCENDANT_LAUNCH_CONTRACT_CAUSAL"
    if all(run.get("no_child_process_creation") == "PASS" for run in app_runs.values()) and all(
        run.get("descendant_policy_available") == "PASS" for run in app_runs.values()
    ):
        return "W5_GATE2A6_APPCONTAINER_CHILD_PROCESS_POLICY_BLOCKED"
    if app_runs.get("APP_ORIGINAL", {}).get("descendant_create_error") == 5 and any(
        app_runs.get(name, {}).get("descendant_create") == "PASS"
        for name in ("APP_EXPLICIT_APPLICATION", "APP_EXPLICIT_CWD", "APP_EXPLICIT_ENV")
    ):
        return "W5_GATE2A6_DESCENDANT_LAUNCH_CONTRACT_CAUSAL"
    return "W5_GATE2A6_DESCENDANT_CAUSE_UNATTRIBUTED"


def _marker_seen(run: dict[str, object], key: str) -> bool:
    markers = run.get("markers")
    return isinstance(markers, dict) and bool(markers.get(key))


def _pty_min_output_pass(run: dict[str, object]) -> bool:
    return (
        _external_token_attested(run)
        and run.get("attributes") == "SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE"
        and run.get("transport") == "PTY"
        and run.get("pty_create") == "PASS"
        and run.get("job_member") == "PASS"
        and _marker_seen(run, "G2A_CHILD_STARTED")
        and _marker_seen(run, "G2A_CHILD_FINISHED")
        and run.get("exit_code") == 0
        and run.get("scope_complete") == "PASS"
        and run.get("job_close") == "PASS"
        and run.get("profile_delete") == "PASS"
        and run.get("local_timeout") != "TRUE"
    )


def _pty_input_pass(run: dict[str, object]) -> bool:
    return (
        _pty_min_output_pass(run)
        and run.get("stdin") == "PASS"
        and _marker_seen(run, "G2A_PTY_STDIN_WRITE")
    )


def _full_transport_pass(run: dict[str, object], transport: str) -> bool:
    expected = (
        "SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE"
        if transport == "PTY"
        else "SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST"
    )
    return (
        _external_token_attested(run)
        and run.get("attributes") == expected
        and run.get("transport") == transport
        and (run.get("pty_create") if transport == "PTY" else run.get("pipe_create")) == "PASS"
        and run.get("job_member") == "PASS"
        and _marker_seen(run, "G2A_CHILD_STARTED")
        and _marker_seen(run, "G2A_CHILD_FINISHED")
        and run.get("exit_code") == 0
        and run.get("scope_complete") == "PASS"
        and run.get("job_close") == "PASS"
        and run.get("profile_delete") == "PASS"
        and run.get("local_timeout") != "TRUE"
        and _cng_pass(run)
        and _ksecdd_write_pass(run)
    )


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate2A6RuntimeSeamTests(unittest.IsolatedAsyncioTestCase):
    """Run the final bounded descendant/PTY attribution matrix."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 2A.6 evidence requires the enabled CI gate"
    )
    async def test_gate2a6_final_runtime_seams(self) -> None:  # pragma: no cover
        self.assertEqual(
            _production_source_diff(), (), "Gate 2A.6 must not modify production source"
        )
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        probe = await asyncio.to_thread(
            _compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate2a_appcontainer_probe.c").resolve(),
            "windows_w5_gate2a6_runtime_seams",
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

            async def run_mode(mode: str, harness_timeout: float = 180.0) -> dict[str, object]:
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
                    timeout=harness_timeout,
                )
                projected = _projection(raw, mode)
                projected["transport"] = _marker(projected, "G2A_TRANSPORT")
                return projected

            # Gate 2A.5 focused regression: A0/A1 must remain the known-good
            # real AppContainer + atomic Job contract before attribution starts.
            baseline_a0 = await run_mode("a0")
            baseline_a1 = await run_mode("a1")
            self.assertTrue(_external_token_attested(baseline_a0))
            self.assertTrue(_external_token_attested(baseline_a1))
            self.assertEqual(baseline_a1.get("job_member"), "PASS")

            descendant_modes = (
                "desc-job-only",
                "desc-original",
                "desc-application",
                "desc-cwd",
                "desc-env",
            )
            descendant_runs: dict[str, dict[str, object]] = {}
            for mode in descendant_modes:
                label = {
                    "desc-job-only": "CONTROL_JOB_ONLY",
                    "desc-original": "APP_ORIGINAL",
                    "desc-application": "APP_EXPLICIT_APPLICATION",
                    "desc-cwd": "APP_EXPLICIT_CWD",
                    "desc-env": "APP_EXPLICIT_ENV",
                }[mode]
                descendant_runs[label] = _descendant_projection(await run_mode(mode), mode)

            control = descendant_runs["CONTROL_JOB_ONLY"]
            app_runs = {
                name: descendant_runs[name]
                for name in (
                    "APP_ORIGINAL",
                    "APP_EXPLICIT_APPLICATION",
                    "APP_EXPLICIT_CWD",
                    "APP_EXPLICIT_ENV",
                )
            }
            descendant_classification = _descendant_classification(control, app_runs)

            pty_min = await run_mode("pty-min-output")
            pty_input: dict[str, object] | None = None
            pty_lf: dict[str, object] | None = None
            if _pty_min_output_pass(pty_min):
                pty_input = await run_mode("pty-input-cr")
                if not _pty_input_pass(pty_input):
                    pty_lf = await run_mode("pty-input-lf")

            pipe_full = await run_mode("a2-pipe-full-no-descendant")
            pty_full: dict[str, object] | None = None
            if _pty_min_output_pass(pty_min):
                pty_full = await run_mode("a2-pty-full-no-descendant")

            pty_classification = (
                "W5_GATE2A6_PTY_MINIMAL_PASS"
                if _pty_min_output_pass(pty_min)
                else "W5_GATE2A6_PTY_TRANSPORT_STILL_BLOCKED"
            )
            if (pty_input is not None and _pty_input_pass(pty_input)) or (
                pty_input is not None and pty_lf is not None and _pty_input_pass(pty_lf)
            ):
                pty_classification = "W5_GATE2A6_PTY_CR_INPUT_CAUSAL"

            pipe_full_pass = _full_transport_pass(pipe_full, "PIPE")
            pty_full_pass = pty_full is not None and _full_transport_pass(pty_full, "PTY")
            cleanup_runs = [baseline_a0, baseline_a1, *descendant_runs.values(), pty_min, pipe_full]
            if pty_input is not None:
                cleanup_runs.append(pty_input)
            if pty_lf is not None:
                cleanup_runs.append(pty_lf)
            if pty_full is not None:
                cleanup_runs.append(pty_full)

            if (
                descendant_classification
                in (
                    "W5_GATE2A6_DESCENDANT_PASS",
                    "W5_GATE2A6_DESCENDANT_LAUNCH_CONTRACT_CAUSAL",
                )
                and _pty_min_output_pass(pty_min)
                and pipe_full_pass
                and pty_full_pass
                and all(run.get("profile_delete") == "PASS" for run in cleanup_runs)
            ):
                decision = "GATE2A_DECISION_FEASIBLE"
            elif (
                descendant_classification == "W5_GATE2A6_APPCONTAINER_CHILD_PROCESS_POLICY_BLOCKED"
                or pty_classification == "W5_GATE2A6_PTY_TRANSPORT_STILL_BLOCKED"
            ):
                decision = "GATE2A_DECISION_NOT_FEASIBLE_FOR_TRANSPARENT_CLI"
            else:
                decision = "GATE2A_DECISION_INCONCLUSIVE"

            artifact: dict[str, object] = {
                "gate": "W5_GATE2A.6",
                "supersedes_gate": "W5_GATE2A.5_runtime_classification",
                "old_head": _OLD_HEAD,
                "gate1_frozen_head": _GATE1_HEAD,
                "main": _MAIN,
                "status": "COMPLETED",
                "production_source_diff": _production_source_diff(),
                "gate2b_started": False,
                "gate2a5_regression": {
                    "A0": baseline_a0,
                    "A1": baseline_a1,
                    "result": "PASS",
                },
                "descendant": {
                    "runs": descendant_runs,
                    "classification": descendant_classification,
                    "policy_fields": {
                        name: {
                            "available": run.get("descendant_policy_available"),
                            "no_child_process_creation": run.get("no_child_process_creation"),
                            "audit_no_child_process_creation": run.get(
                                "audit_no_child_process_creation"
                            ),
                        }
                        for name, run in app_runs.items()
                    },
                },
                "pty": {
                    "minimal_output": pty_min,
                    "input_cr": pty_input,
                    "input_lf_control": pty_lf,
                    "classification": pty_classification,
                    "local_deadline_seconds": 30,
                },
                "full_no_descendant": {
                    "A2_PIPE": pipe_full,
                    "A2_PTY": pty_full,
                    "pipe_pass": pipe_full_pass,
                    "pty_pass": pty_full_pass,
                    "cng": {
                        "A2_PIPE": pipe_full.get("bcrypt_gen_random"),
                        "A2_PTY": pty_full.get("bcrypt_gen_random") if pty_full else None,
                    },
                    "ksecdd": {
                        "A2_PIPE": pipe_full.get("ntopen"),
                        "A2_PTY": pty_full.get("ntopen") if pty_full else None,
                    },
                },
                "job_lifecycle": {
                    name: {
                        "job_member": run.get("job_member"),
                        "descendant_job_member": run.get("descendant_job_member"),
                        "active_before_close": run.get("descendant_active_before_close"),
                        "job_close": run.get("job_close"),
                        "reaped_after_close": run.get("descendant_reaped"),
                    }
                    for name, run in app_runs.items()
                },
                "filesystem": "FILESYSTEM_DEFERRED_PENDING_ARCHITECTURE_DECISION",
                "cleanup": {
                    "temporary_fixture_root": True,
                    "temporary_environment_block": True,
                    "temporary_profiles_deleted": all(
                        run.get("profile_delete") == "PASS" for run in cleanup_runs
                    ),
                    "system_acl_mutation": False,
                    "ksecdd_mutation": False,
                    "registry_mutation": False,
                    "firewall_mutation": False,
                    "device_io_control": False,
                    "persistent_profile": False,
                    "post_create_job_assignment": False,
                },
                "decision": decision,
            }
            destination_text = os.environ.get("NEURO_CODE_W5_GATE2A6_EVIDENCE_JSON")
            if destination_text:
                await asyncio.to_thread(
                    Path(destination_text).write_text,
                    json.dumps(artifact, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            self.assertEqual(_production_source_diff(), ())


if __name__ == "__main__":  # pragma: no cover - Windows CI entry point
    unittest.main()
