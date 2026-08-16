"""W5 Gate 1.10.5 evidence reconciliation and harness hygiene.

Gate 1.10's native token and security observations remain authoritative, but
its direct broker workload loop is not a production W3 compatibility oracle.
This gate compares a small anchor set through the real W3 adapter and through
the evidence broker, while making the broker worker/handle lifecycle explicit.
It does not change production token, ACL, runner, or workload behavior.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tests.security.test_windows_native_workload_compatibility import (
    _build_workloads,
    _compile_nul_probe,
    _completed_classification,
    _discover_base_python,
    _nul_mode_results,
    _output_matches,
    _preview,
    _provenance,
    _request,
    _tool_paths,
    _w3_run,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import _production_source_diff
from tests.security.test_windows_w5_gate1_7_token_ablation import _run_harness_bounded
from tests.security.test_windows_w5_gate1_10_wrc import (
    _MARKER_PREFIX,
    _compile_gate110_broker,
    _parse_markers,
    _token_projection,
)
from tests.security.test_windows_w5_gate1_runtime_root_cause import (
    _environment_for,
    _Gate1DirectProcess,
    _native_enabled,
)

from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import _WindowsNativeDesktopMode
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

_BASE = "25f36b492394b016cd8f5e385d4bac6f9cf3a454"
_UPSTREAM_CODEX_COMMIT = "6e5a2d6b8d148a5554fdceb6f399ca45bd1c78d9"
_ANCHOR_NAMES = (
    "CMD_BASIC",
    "NODE_VERSION",
    "NPM_VERSION",
    "GIT_VERSION",
    "POWERSHELL_BASIC",
    "PYTHON_BASE_VERSION",
    "CURL_VERSION",
    "NUL_DIRECT_WIN32",
)


async def _remove_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _clean_broker_output(data: bytes) -> bytes:
    lines = data.decode("utf-8", errors="replace").replace("\r", "").splitlines()
    prefixes = (_MARKER_PREFIX, "W5_GATE16_")
    return ("\n".join(line for line in lines if not line.startswith(prefixes)) + "\n").encode()


def _broker_classification(spec: _Workload, raw: dict[str, object]) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    data = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(data)
    output = _clean_broker_output(data)
    stderr = str(raw.get("stderr_preview", "")).encode()
    exit_code = raw.get("exit_code")
    child_timeout = markers.get(f"{_MARKER_PREFIX}CHILD_WAIT") == "TIMEOUT"
    # ``timeout`` is the direct harness' wait result.  It may mean that the
    # broker's own child exceeded its bounded native wait; that is not the
    # same as the Python worker call timing out.  Keep both facts separate so
    # cleanup assertions cannot turn an inner workload timeout into a false
    # controller-ownership failure.
    controller_timeout = bool(raw.get("timeout")) and (
        markers.get(f"{_MARKER_PREFIX}CHILD_CREATE") == "PASS"
        and f"{_MARKER_PREFIX}CHILD_WAIT_ENTER" in markers
    )
    harness_call_timeout = bool(raw.get("harness_call_timeout"))
    if child_timeout or controller_timeout or harness_call_timeout:
        classification = "TIMEOUT"
    elif isinstance(exit_code, int):
        classification = (
            "PASS"
            if exit_code == 0 and _output_matches(spec, output, stderr)
            else _completed_classification(spec, exit_code, output, stderr)
        )
    else:
        classification = "INCONCLUSIVE"
    broker = _token_projection(raw, markers)
    return {
        "execution_path": "EVIDENCE_DIRECT_BROKER",
        "evidence_scope": "BROKER_RELATIVE_ONLY",
        "production_equivalent": False,
        "resolved_executable": str(spec.executable) if spec.executable else None,
        "argv": [str(spec.executable), *spec.arguments] if spec.executable else [],
        "classification": classification,
        "exit_code": exit_code,
        "timeout": child_timeout,
        "controller_timeout": controller_timeout,
        "harness_call_timeout": harness_call_timeout,
        "stdout_preview": _preview(output),
        "stderr_preview": _preview(stderr),
        "nul_modes": _nul_mode_results(output, stderr),
        "broker": broker,
        "worker_terminal": raw.get("worker_terminal", True),
        "worker_alive": raw.get("worker_alive", False),
        "controller_tree_cleanup": raw.get("controller_tree_cleanup"),
    }


class WindowsW5Gate1105ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """Reconcile production W3 anchors with the non-production broker."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 1.10.5 evidence requires the enabled CI gate"
    )
    async def test_gate1105_reconciliation(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        self.assertEqual(_production_source_diff(), ())

        broker = await asyncio.to_thread(_compile_gate110_broker)
        nul_probe = await asyncio.to_thread(_compile_nul_probe)
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, nul_probe.parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE1_10_5_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_10_5",
            "base": _BASE,
            "production_source_diff": (),
            "status": "RUNNING",
            "gate110_core_reference": {
                "source_run": "31948176059",
                "conclusion": "WRITE_RESTRICTED_CODE_NOT_SUFFICIENT_FOR_BCRYPT",
                "native": {
                    "PROD_SYN": {
                        "PRAW": "PASS",
                        "P0": "PASS",
                        "P3": "bcrypt LoadLibraryW FAIL 1114",
                        "P4": "bcrypt LoadLibraryW FAIL 1114",
                    },
                    "PROD_SYN_WRC": {
                        "PRAW": "PASS",
                        "P0": "PASS",
                        "P3": "bcrypt LoadLibraryW FAIL 1114",
                        "P4": "bcrypt LoadLibraryW FAIL 1114",
                    },
                },
                "security": {
                    "AUTHORIZED_WORKSPACE_SYN": ["ALLOW", "ALLOW"],
                    "OUTSIDE_BROAD_NO_CAP": ["DENY", "DENY"],
                    "OUTSIDE_WRC_ONLY": ["DENY", "ALLOW"],
                    "INSTALLATION_PROTECTION": ["DENY", "DENY"],
                    "CREDENTIAL_PROTECTION": ["DENY", "DENY"],
                    "READ_ONLY_MUTATION": ["DENY", "DENY"],
                },
            },
            "broker_workload_scope": {
                "scope": "BROKER_RELATIVE_ONLY",
                "production_equivalent": False,
                "gate110_full_workload_loop": "WITHDRAWN",
            },
            "upstream_codex_snapshot": {
                "repository": "openai/codex",
                "source_commit": _UPSTREAM_CODEX_COMMIT,
                "source": "codex-rs/windows-sandbox-rs/src/token.rs",
                "restricting_set": (
                    "capability SID(s) + TokenUser via *_with_user_from + optional "
                    "additional restricting SID(s) + TokenLogonSid + Everyone"
                ),
                "default_dacl": "Logon + Everyone + capability SID(s)",
                "extra_identity_sids_in_default_dacl": False,
                "nul_mechanism": (
                    "elevated command_runner separately calls allow_null_device(capability SID)"
                ),
                "copied_into_neuro": False,
            },
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            repo = workspace / "compat-repo"
            for path in (workspace, installation, runtime_state, repo):
                path.mkdir(parents=True)
            copied_nul = workspace / "windows-nul-probe.exe"
            shutil.copy2(nul_probe, copied_nul)

            paths = _tool_paths()
            paths["python_base"] = _discover_base_python(paths["python"])
            artifact["provenance"] = _provenance(paths, workspace)
            persist()

            if paths["git"] is not None:
                await asyncio.to_thread(
                    subprocess.run,
                    [str(paths["git"]), "init", "-q", str(repo)],
                    check=False,
                    capture_output=True,
                    timeout=15,
                    shell=False,
                )

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
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            # The installation record stores a typed WindowsAccountSid.  The
            # W3 attestation contract compares its canonical SID text, so do
            # not rely on a typing-only cast here.
            expected_user_sid = online.user_sid.value
            expected_write_sid = record.write_sid.value
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                runtime_state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
                _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                _diagnostic_create_no_window=True,
            )
            broker_destination = workspace / "gate110-token-broker.exe"
            shutil.copy2(broker, broker_destination)
            harness = _Gate1DirectProcess()
            workload_specs = {
                workload.name: workload
                for workload in _build_workloads(
                    workspace=workspace,
                    repo=repo,
                    nul_probe=copied_nul,
                    cmd=paths["cmd"],
                    powershell=paths["powershell"],
                    pwsh=paths["pwsh"],
                    python=paths["python"],
                    python_base=paths["python_base"],
                    git=paths["git"],
                    node=paths["node"],
                    npm=paths["npm"],
                    curl=paths["curl"],
                )
            }

            def broker_run(spec: _Workload) -> dict[str, object]:
                self.assertIsNotNone(spec.executable)
                arguments = (
                    "PROD_SYN",
                    expected_write_sid,
                    str(spec.executable),
                    str(workspace),
                    *spec.arguments,
                )
                request = _request(spec, workspace)
                partial_stdout = bytearray()
                cleanup: dict[str, object] = {
                    "attempted": False,
                    "result": False,
                    "child_attempted": False,
                    "child_result": False,
                    "child_pid": None,
                    "controller_pid": None,
                }

                def on_spawn(process_handle: int) -> None:
                    # Borrowed handle: retain only a PID snapshot.
                    cleanup["controller_pid"] = harness.process_id(process_handle)

                def on_output(stream: str, chunk: bytes) -> None:
                    if stream == "stdout" and len(partial_stdout) < 65_536:
                        partial_stdout.extend(chunk[: 65_536 - len(partial_stdout)])

                def on_timeout() -> None:
                    markers = _parse_markers(bytes(partial_stdout))
                    child_pid = markers.get(f"{_MARKER_PREFIX}CHILD_PID")
                    if child_pid and child_pid.isdigit():
                        cleanup["child_pid"] = int(child_pid)
                        cleanup["child_attempted"] = True
                        cleanup["child_result"] = harness.terminate_process_id_tree(int(child_pid))
                    controller_pid = cleanup.get("controller_pid")
                    if isinstance(controller_pid, int) and controller_pid > 0:
                        cleanup["attempted"] = True
                        cleanup["result"] = harness.terminate_process_id_tree(controller_pid)

                raw = _run_harness_bounded(
                    harness,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=broker_destination,
                    arguments=arguments,
                    cwd=workspace,
                    environment=_environment_for(request),
                    logon_flags=0,
                    timeout=40.0,
                    on_timeout=on_timeout,
                    on_spawn=on_spawn,
                    on_output=on_output,
                )
                raw["controller_tree_cleanup"] = cleanup
                if raw.get("worker_alive") is True:
                    self.fail(f"broker worker remained alive for {spec.name}")
                result = _broker_classification(spec, raw)
                result["controller_tree_cleanup"] = cleanup
                broker = cast(dict[str, object], result["broker"])
                self.assertEqual(broker["started"], True)
                self.assertEqual(broker["token_inspection"], "PASS")
                self.assertEqual(broker["restricted_sid_count"], 1)
                if result["harness_call_timeout"]:
                    self.assertTrue(cleanup["attempted"])
                    self.assertTrue(cleanup["result"])
                return result

            production_anchor: dict[str, object] = {}
            broker_control: dict[str, object] = {}
            comparison: dict[str, object] = {}
            try:
                for name in _ANCHOR_NAMES:
                    spec = workload_specs[name]
                    production = await _w3_run(
                        spec,
                        workspace=workspace,
                        adapter=adapter,
                        expected_user_sid=expected_user_sid,
                        expected_write_sid=expected_write_sid,
                    )
                    self.assertEqual(production["spawn_result"], "PASS")
                    self.assertEqual(production["token_attestation"], "PASS")
                    production["evidence_scope"] = "PRODUCTION_W3"
                    production["production_equivalent"] = True
                    production_anchor[name] = production
                    broker = await asyncio.to_thread(broker_run, spec)
                    broker_control[name] = broker
                    production_classification = str(production.get("classification"))
                    broker_classification = str(broker.get("classification"))
                    comparison[name] = {
                        "production_classification": production_classification,
                        "broker_classification": broker_classification,
                        "same_or_different": (
                            "SAME"
                            if production_classification == broker_classification
                            else "DIFFERENT"
                        ),
                    }
                    artifact["production_anchor"] = production_anchor
                    artifact["broker_control"] = broker_control
                    artifact["comparison"] = comparison
                    persist()

                alive = [
                    thread.name
                    for thread in threading.enumerate()
                    if thread.name.startswith("W5-HarnessWorker")
                ]
                artifact["harness_hygiene"] = {
                    "worker_threads_after": alive,
                    "worker_threads_left_alive": bool(alive),
                    "ambiguous_process_handle_ownership": False,
                    "popen_unraisable_warning_policy": "FAIL_THE_FOCUSED_JOB",
                    "resource_warning_policy": "FAIL_THE_FOCUSED_JOB",
                }
                self.assertEqual(alive, [])
                artifact["broker_workload_equivalence"] = (
                    "BROKER_WORKLOAD_EQUIVALENT"
                    if all(row["same_or_different"] == "SAME" for row in comparison.values())
                    else "BROKER_WORKLOAD_DIVERGENCE_CONFIRMED"
                )
                artifact["status"] = "COMPLETED"
                artifact["production_source_diff"] = _production_source_diff()
                persist()
            finally:
                await asyncio.to_thread(authority.cleanup, setup_request)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
