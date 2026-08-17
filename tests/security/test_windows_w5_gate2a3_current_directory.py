"""W5 Gate 2A.3 evidence for AppContainer launch attribution.

This gate is intentionally evidence-only.  It first corrects the recorded
``lpCurrentDirectory`` contract, then compares fixture/workspace/NULL current
directories.  Only when those cells all fail does it compare
``CreateProcessAsUserW`` with the documented ``CreateProcessW`` construction.
No production sandbox code is changed.
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
    _projection,
    _token_attested,
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

_HEAD = "242bb243f83378c97f442f6aab03462f6cd7a5d6"
_GATE1_HEAD = "902f82e014d0728445723630bc24d70bb1b52357"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"


def _child_succeeded(run: dict[str, object]) -> bool:
    return run.get("child_create") == "PASS"


def _child_error(run: dict[str, object]) -> int | None:
    value = run.get("createprocess_error")
    if isinstance(value, int):
        return value
    value = run.get("child_create_error")
    return value if isinstance(value, int) else None


def _classify_current_directory(
    variants: dict[str, dict[str, object]],
) -> str:
    fixture = variants["CD_FIXTURE"]
    alternatives = (variants["CD_WORKSPACE"], variants["CD_NULL"])
    if _child_error(fixture) == 267 and any(_child_succeeded(run) for run in alternatives):
        return "W5_GATE2A3_CURRENT_DIRECTORY_PRECONDITION_ESTABLISHED"
    return "W5_GATE2A3_CURRENT_DIRECTORY_HYPOTHESIS_NOT_SUFFICIENT"


def _classify_process_api(
    variants: dict[str, dict[str, object]],
) -> str:
    as_user = variants["API_AS_USER"]
    current = variants["API_CURRENT_PROCESS"]
    if not _child_succeeded(as_user) and _child_error(as_user) == 267 and _child_succeeded(current):
        return "W5_GATE2A3_CREATEPROCESS_PRIMITIVE_PRECONDITION_ESTABLISHED"
    return "W5_GATE2A3_CHILD_LAUNCH_CAUSE_STILL_UNATTRIBUTED"


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate2A3CurrentDirectoryTests(unittest.IsolatedAsyncioTestCase):
    """Attribute the AppContainer launch failure without changing production."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 2A.3 evidence requires the enabled CI gate"
    )
    async def test_gate2a3_current_directory_and_process_api(self) -> None:  # pragma: no cover
        self.assertEqual(
            _production_source_diff(), (), "Gate 2A.3 must not modify production source"
        )
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        probe = await asyncio.to_thread(
            _compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate2a_appcontainer_probe.c").resolve(),
            "windows_w5_gate2a3_current_directory",
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

            current_directory_runs = {
                "CD_FIXTURE": await run_mode("cd-fixture"),
                "CD_WORKSPACE": await run_mode("cd-workspace"),
                "CD_NULL": await run_mode("cd-null"),
            }
            directory_classification = _classify_current_directory(current_directory_runs)
            process_api_runs: dict[str, dict[str, object]] = {}
            process_api_classification = "NOT_REQUIRED"
            selected_api = "CreateProcessAsUserW"
            selected_current_directory = "fixture"
            successful_directory = next(
                (
                    (name, run)
                    for name, run in current_directory_runs.items()
                    if _child_succeeded(run)
                ),
                None,
            )
            if successful_directory is not None:
                selected_current_directory = successful_directory[0].removeprefix("CD_").lower()
                if selected_current_directory == "null":
                    selected_current_directory = "null"
            else:
                process_api_runs = {
                    "API_AS_USER": await run_mode("api-workspace-as-user"),
                    "API_CURRENT_PROCESS": await run_mode("api-current-workspace"),
                }
                process_api_classification = _classify_process_api(process_api_runs)
                if _child_succeeded(process_api_runs["API_CURRENT_PROCESS"]):
                    selected_api = "CreateProcessW"
                    selected_current_directory = "workspace"
                elif _child_succeeded(process_api_runs["API_AS_USER"]):
                    selected_current_directory = "workspace"

            selected_run = successful_directory[1] if successful_directory is not None else None
            if selected_run is None:
                selected_run = next(
                    (run for run in process_api_runs.values() if _child_succeeded(run)),
                    None,
                )
            selected_process_api = (
                selected_run.get("process_api") if selected_run is not None else None
            )
            token_attestation: dict[str, object] = {
                "successful_variant": selected_run.get("mode") if selected_run else None,
                "pass": _token_attested(selected_run) if selected_run else False,
                "process_api": selected_process_api,
            }

            layered: dict[str, dict[str, object]] = {}
            layer_classification = "NOT_STARTED"
            if selected_run is not None and token_attestation["pass"]:
                layer_classification = "A0_STARTED"
                if selected_api == "CreateProcessW":
                    base = "api-current-workspace"
                elif selected_current_directory == "null":
                    base = "layer-null-as-user"
                else:
                    base = "layer-workspace-as-user"
                for stage in ("a0", "a1", "a2"):
                    pipe = await run_mode(f"{base}-{stage}")
                    pty = await run_mode(f"pty-{base}-{stage}")
                    layered[f"pipe-{stage}"] = pipe
                    layered[f"pty-{stage}"] = pty
                    if not (_token_attested(pipe) and _token_attested(pty)):
                        layer_classification = f"{stage.upper()}_TOKEN_ATTESTATION_FAILED"
                        break
                    layer_classification = f"{stage.upper()}_PASS"

            production_diff = _production_source_diff()
            artifact: dict[str, object] = {
                "gate": "W5_GATE2A.3",
                "old_head": _HEAD,
                "gate1_frozen_head": _GATE1_HEAD,
                "main": _MAIN,
                "status": "COMPLETED",
                "production_source_diff": production_diff,
                "controller_identity": "W2_ONLINE_WITH_PROFILE_via_CreateProcessWithLogonW",
                "launch_contract": {
                    "application": "copied probe in authorized workspace",
                    "command": "minimal pipe child",
                    "security_capabilities": "AppContainer SID; NULL capabilities; count 0",
                    "environment": "CreateEnvironmentBlock(current token)",
                    "creation_flags": "CREATE_UNICODE_ENVIRONMENT,CREATE_NO_WINDOW,EXTENDED_STARTUPINFO_PRESENT",
                    "inherit_handles": True,
                },
                "path_attestation": {
                    name: run.get("path_facts", {}) for name, run in current_directory_runs.items()
                },
                "current_directory_matrix": current_directory_runs,
                "current_directory_classification": directory_classification,
                "process_api_matrix": process_api_runs,
                "process_api_classification": process_api_classification,
                "appcontainer_token": token_attestation,
                "layered_attribute_matrix": layered,
                "layered_attribute_classification": layer_classification,
                "ksecdd": (
                    {name: run.get("ntopen") for name, run in layered.items()}
                    if layered
                    else "NOT_ATTEMPTED"
                ),
                "cng": (
                    {
                        name: {
                            "load": run.get("bcrypt_load"),
                            "load_error": run.get("bcrypt_load_error"),
                            "gen_random": run.get("bcrypt_gen_random"),
                        }
                        for name, run in layered.items()
                    }
                    if layered
                    else "NOT_ATTEMPTED"
                ),
                "filesystem": (
                    {name: run.get("filesystem") for name, run in layered.items()}
                    if layered
                    else "INCONCLUSIVE"
                ),
                "job_descendant_cleanup": (
                    {
                        name: {
                            "job_member": run.get("job_member"),
                            "scope_complete": run.get("scope_complete"),
                            "descendant_reaped": run.get("descendant_reaped"),
                            "job_close": run.get("job_close"),
                        }
                        for name, run in layered.items()
                    }
                    if layered
                    else "NOT_OBSERVED"
                ),
                "cleanup": {
                    "temporary_fixture_root": True,
                    "temporary_environment_block": True,
                    "system_acl_mutation": False,
                    "ksecdd_mutation": False,
                    "registry_mutation": False,
                    "firewall_mutation": False,
                    "device_io_control": False,
                    "persistent_profile": False,
                },
                "gate2b_started": False,
            }
            destination = os.environ.get("NEURO_CODE_W5_GATE2A3_EVIDENCE_JSON")
            if destination:
                await asyncio.to_thread(
                    Path(destination).write_text,
                    json.dumps(artifact, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            self.assertEqual(production_diff, ())


if __name__ == "__main__":  # pragma: no cover - Windows CI entry point
    unittest.main()
