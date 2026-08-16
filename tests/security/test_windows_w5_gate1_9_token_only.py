"""W5 Gate 1.9 token-only ablation with a frozen launch contract.

This evidence gate corrects the Gate 1.8 fixture confound.  Every variant
uses the production-shaped LOGON/WORLD/SYNTHETIC_WRITE default DACL and the
same direct CreateProcessAsUserW/HANDLE_LIST path.  Only CreateRestrictedToken
flags and the singleton synthetic restricting SID vary.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import cast

from tests.security.test_windows_native_runtime_acceptance import _compile_msvc_probe, _find_vswhere
from tests.security.test_windows_native_workload_compatibility import _request, _Workload
from tests.security.test_windows_w5_gate1_6_loader_isolation import (
    _discover_vcvars,
    _inspect_imports,
    _production_source_diff,
)
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
    _run_vcvars_command,
    _source_path,
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

_BASE = "708b26235975cfeb2c62507e30b6bf8dfc5bc783"
_MAX_MARKER_VALUE = 128
_MARKER_PREFIXES = ("W5_GATE16_", "W5_GATE19_")
_PROBE_SOURCES = {
    "P0": "windows_w5_gate1_6_p0.c",
    "P3": "windows_w5_gate1_6_p3_dynamic.c",
    "P4": "windows_w5_gate1_7_p4_bcrypt_dynamic.c",
}
_VARIANTS = (
    ("DL", 0x00000005, 0, False),
    ("DLR", 0x00000005, 1, True),
    ("DLW0", 0x0000000D, 0, False),
    ("DLWR", 0x0000000D, 1, True),
)
_DACL_PRINCIPALS = ("LOGON", "WORLD", "SYNTHETIC_WRITE")
_LAUNCH_CONTRACT = {
    "api": "CreateProcessAsUserW",
    "creation_flags": (
        "CREATE_UNICODE_ENVIRONMENT",
        "CREATE_NO_WINDOW",
        "EXTENDED_STARTUPINFO_PRESENT",
    ),
    "create_suspended": False,
    "job_attachment": "NONE",
    "stdio": "HANDLE_LIST(stdin,stdout,stderr)",
    "profile": "NO_PROFILE",
}


class _Gate19BuildError(RuntimeError):
    """The trusted controller could not build a Gate 1.9 artifact."""


def _compile_broker() -> Path:  # pragma: no cover - Windows CI
    discovery = subprocess.run(
        [
            str(_find_vswhere()),
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
    if discovery.returncode != 0:
        raise _Gate19BuildError("vswhere did not find an MSVC installation")
    installation = next(
        (Path(line.strip()) for line in discovery.stdout.splitlines() if line.strip()),
        None,
    )
    if installation is None:
        raise _Gate19BuildError("vswhere returned no installation path")
    vcvars = installation / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise _Gate19BuildError("vcvars64.bat is unavailable")
    source = Path(__file__).with_name("windows_w5_gate1_7_token_broker.c").resolve()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", os.environ.get("TEMP", ".")))
    build_directory = Path(mkdtemp(prefix="neuro-code-w5-gate19-broker-", dir=runner_temp))
    output = build_directory / "windows_w5_gate1_9_token_broker.exe"
    result = _run_vcvars_command(
        vcvars,
        f'cl /nologo /W4 /WX /MT /O2 /DNEURO_GATE19 /Fe:"{output}" "{source}" Advapi32.lib',
        cwd=build_directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        diagnostic = (result.stderr or result.stdout or "").strip().replace("\x00", "")[:512]
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _Gate19BuildError(f"token broker build failed: {diagnostic}")
    return output


def _compile_praw() -> tuple[Path, dict[str, object]]:  # pragma: no cover - Windows CI
    vcvars = _discover_vcvars()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", os.environ.get("TEMP", ".")))
    build_directory = Path(mkdtemp(prefix="neuro-code-w5-gate19-praw-", dir=runner_temp))
    source = Path(__file__).with_name("windows_w5_gate1_9_praw.c").resolve()
    output = build_directory / "windows_w5_gate1_9_praw.exe"
    result = _run_vcvars_command(
        vcvars,
        (
            f'cl /nologo /W4 /WX /O2 /GS- /Fe:"{output}" "{source}" Kernel32.lib '
            f"/link /NODEFAULTLIB /ENTRY:gate19_raw_entry /SUBSYSTEM:CONSOLE"
        ),
        cwd=build_directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        diagnostic = (result.stderr or result.stdout or "").strip().replace("\x00", "")[:512]
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _Gate19BuildError(f"PRAW build failed: {diagnostic}")
    return output, _inspect_imports(output, vcvars, build_directory)


def _parse_markers(output: bytes) -> dict[str, str]:
    text = output.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith(_MARKER_PREFIXES):
            continue
        key, separator, value = line.partition("=")
        if not key.startswith(_MARKER_PREFIXES):
            continue
        markers[key] = value[:_MAX_MARKER_VALUE] if separator else "OBSERVED"
    return markers


def _marker_int(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _probe_projection(probe_name: str, markers: dict[str, str]) -> dict[str, object]:
    if probe_name == "PRAW":
        prefix = "W5_GATE19_PRAW_"
        child_markers = {key: value for key, value in markers.items() if key.startswith(prefix)}
        return {
            "started": "W5_GATE19_PRAW_ENTRY" in markers,
            "finished": "W5_GATE19_PRAW_ENTRY" in markers,
            "first_marker": "W5_GATE19_PRAW_ENTRY" if "W5_GATE19_PRAW_ENTRY" in markers else None,
            "load_library_attempted": False,
            "bcrypt_load": None,
            "bcrypt_load_error": None,
            "markers": child_markers,
        }
    prefix = f"W5_GATE16_{probe_name}_"
    prefixes: tuple[str, ...] = (prefix,)
    if probe_name == "P3":
        prefixes = (*prefixes, "W5_GATE16_BCRYPT_", "W5_GATE16_NCRYPT_", "W5_GATE16_BEFORE_")
    child_markers = {key: value for key, value in markers.items() if key.startswith(prefixes)}
    if probe_name == "P4":
        load_key = "W5_GATE16_P4_BCRYPT_LOAD"
        error_key = "W5_GATE16_P4_BCRYPT_LOAD_ERROR"
    else:
        load_key = "W5_GATE16_BCRYPT_LOAD"
        error_key = "W5_GATE16_BCRYPT_LOAD_ERROR"
    return {
        "started": f"W5_GATE16_{probe_name}_STARTED" in markers,
        "finished": f"W5_GATE16_{probe_name}_FINISHED" in markers,
        "first_marker": next((key for key in markers if key.startswith(prefix)), None),
        "load_library_attempted": (
            f"W5_GATE16_{probe_name}_BEFORE_LOAD_BCRYPT" in markers
            or "W5_GATE16_BEFORE_LOAD_BCRYPT" in markers
        ),
        "bcrypt_load": child_markers.get(load_key),
        "bcrypt_load_error": _marker_int(child_markers, error_key),
        "markers": child_markers,
    }


def _project(
    raw: dict[str, object], *, variant: str, flags: int, sid_count: int, probe: str
) -> dict[str, object]:
    captured = raw.pop("_captured_stdout", b"")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    broker = {
        "started": "W5_GATE19_BROKER_STARTED" in markers,
        "finished": "W5_GATE19_BROKER_FINISHED" in markers,
        "token_create": markers.get("W5_GATE19_TOKEN_CREATE"),
        "token_dacl": markers.get("W5_GATE19_TOKEN_DACL"),
        "dacl_principals": markers.get("W5_GATE19_DACL_PRINCIPALS"),
        "token_inspection": markers.get("W5_GATE19_TOKEN_INSPECTION"),
        "token_privileges": markers.get("W5_GATE19_TOKEN_PRIVILEGES"),
        "token_privilege_count": _marker_int(markers, "W5_GATE19_TOKEN_PRIVILEGE_COUNT"),
        "unexpected_enabled_privileges": _marker_int(
            markers, "W5_GATE19_UNEXPECTED_ENABLED_PRIVILEGES"
        ),
        "se_change_notify": markers.get("W5_GATE19_SE_CHANGE_NOTIFY"),
        "token_restricted": markers.get("W5_GATE19_TOKEN_RESTRICTED"),
        "flags_actual": _marker_int(markers, "W5_GATE19_FLAGS"),
        "restricted_sid_count_actual": _marker_int(markers, "W5_GATE19_RESTRICTED_SID_COUNT"),
        "restricted_sid_match": markers.get("W5_GATE19_RESTRICTED_SID_MATCH"),
        "child_create": markers.get("W5_GATE19_CHILD_CREATE"),
        "child_pid": _marker_int(markers, "W5_GATE19_CHILD_PID"),
        "child_initial_active": markers.get("W5_GATE19_CHILD_INITIAL_ACTIVE"),
        "child_active": markers.get("W5_GATE19_CHILD_ACTIVE"),
        "child_wait": markers.get("W5_GATE19_CHILD_WAIT"),
        "child_exit": _marker_int(markers, "W5_GATE19_CHILD_EXIT"),
        "cleanup_action": markers.get("W5_GATE19_CHILD_CLEANUP"),
        "cleanup_result": markers.get("W5_GATE19_CHILD_CLEANUP_RESULT"),
    }
    return {
        "variant": variant,
        "flags_expected": flags,
        "expected_restricted_sid_count": sid_count,
        "spawn_result": raw.get("spawn_result"),
        "classification": raw.get("classification"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "stdout_preview": raw.get("stdout_preview"),
        "stderr_preview": raw.get("stderr_preview"),
        "win32_error": raw.get("win32_error"),
        "probe": _probe_projection(probe, markers),
        "broker": broker,
    }


def _load(cell: dict[str, object]) -> str:
    probe = cast(dict[str, object], cell["probe"])
    if probe.get("started") is not True:
        return "NO_USER_CODE"
    load = probe.get("bcrypt_load")
    error = probe.get("bcrypt_load_error")
    if load == "PASS":
        return "PASS"
    if load == "FAIL":
        return f"FAIL_{error}" if isinstance(error, int) else "FAIL_UNKNOWN"
    return "NOT_APPLICABLE"


def _earliest_stage(cell: dict[str, dict[str, object]]) -> str:
    praw = cast(dict[str, object], cell["PRAW"]["probe"])
    p0 = cast(dict[str, object], cell["P0"]["probe"])
    if praw.get("started") is not True:
        return "PRE_ENTRY_OR_LOADER_STALL"
    if p0.get("started") is not True:
        return "CRT_OR_POST_ENTRY_STARTUP_STALL"
    return "AFTER_P0_ENTRY"


def _classify(results: dict[str, dict[str, dict[str, object]]]) -> str:
    def load(variant: str, probe: str) -> str:
        return _load(results[variant][probe])

    def started(variant: str, probe: str) -> bool:
        projection = cast(dict[str, object], results[variant][probe]["probe"])
        return projection.get("started") is True

    dl_base = all(started("DL", probe) for probe in ("PRAW", "P0"))
    dlw0_base = all(started("DLW0", probe) for probe in ("PRAW", "P0"))
    dlr_base = all(started("DLR", probe) for probe in ("PRAW", "P0"))
    dlwr_base = all(started("DLWR", probe) for probe in ("PRAW", "P0"))
    dlwr_fail = all(load("DLWR", probe) == "FAIL_1114" for probe in ("P3", "P4"))
    dlw0_pass = all(load("DLW0", probe) == "PASS" for probe in ("P3", "P4"))
    if dl_base and not dlr_base and not dlw0_base and dlwr_base and dlwr_fail:
        classification = "NON_MONOTONIC_TOKEN_BEHAVIOR_REPRODUCED"
    elif dlr_base or dlw0_base:
        classification = "GATE18_HARNESS_CONFOUND_CONFIRMED"
    else:
        classification = "W5_GATE19_RESULT_INCONCLUSIVE"
    if dlw0_pass and dlwr_fail:
        classification += ";SYNTHETIC_RESTRICTING_SID_REQUIRED_IN_WRITE_RESTRICTED_PATH"
    return classification


async def _cleanup_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate19TokenOnlyTests(unittest.IsolatedAsyncioTestCase):
    """Run 16 token-only observations under identical non-token conditions."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 1.9 evidence requires the enabled CI gate"
    )
    async def test_gate19_token_only_ablation(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        production_diff = _production_source_diff()
        self.assertEqual(production_diff, (), "Gate 1.9 must not modify production source")

        broker = await asyncio.to_thread(_compile_broker)
        praw, praw_imports = await asyncio.to_thread(_compile_praw)
        self.addAsyncCleanup(_cleanup_directory, broker.parent)
        self.addAsyncCleanup(_cleanup_directory, praw.parent)
        probe_paths: dict[str, Path] = {"PRAW": praw}
        for name, source_name in _PROBE_SOURCES.items():
            probe_paths[name] = await asyncio.to_thread(
                _compile_msvc_probe,
                _source_path(source_name),
                f"windows_w5_gate19_{name.casefold()}",
                libraries=("Advapi32.lib", "Userenv.lib"),
            )
            self.addAsyncCleanup(_cleanup_directory, probe_paths[name].parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE19_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_9",
            "base": _BASE,
            "production_source_diff": production_diff,
            "probe_order": tuple(probe_paths),
            "variant_order": tuple(name for name, _, _, _ in _VARIANTS),
            "authorities": ["W2_ONLINE_NO_PROFILE"],
            "token_default_dacl": {
                "principals": _DACL_PRINCIPALS,
                "identical_across_variants": True,
            },
            "launch_contract": {
                **_LAUNCH_CONTRACT,
                "identical_across_variants": True,
            },
            "praw_imports": praw_imports,
            "status": "RUNNING",
        }
        variant_results: dict[str, dict[str, dict[str, object]]] = {}
        artifact["variants"] = variant_results

        def persist_artifact() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist_artifact()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            copied: dict[str, Path] = {}
            for name, path in probe_paths.items():
                destination = workspace / f"gate19-{name.casefold()}.exe"
                shutil.copy2(path, destination)
                copied[name] = destination
            broker_destination = workspace / "gate19-token-broker.exe"
            shutil.copy2(broker, broker_destination)

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
            artifact["setup_ready"] = True
            persist_artifact()
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            password = online.password.decode("utf-8")
            harness = _Gate1DirectProcess()
            try:
                for variant, flags, sid_count, _has_sid in _VARIANTS:
                    artifact["current_variant"] = variant
                    persist_artifact()
                    per_probe: dict[str, dict[str, object]] = {}
                    for probe_name, probe_path in copied.items():
                        args = (variant, record.write_sid.value, str(probe_path), str(workspace))
                        broker_spec = _Workload(
                            "GATE19_BROKER", variant.casefold(), broker_destination, args
                        )
                        raw = await asyncio.to_thread(
                            _run_harness_bounded,
                            harness,
                            username=online.username,
                            password=password,
                            executable=broker_destination,
                            arguments=args,
                            cwd=workspace,
                            environment=_environment_for(_request(broker_spec, workspace)),
                            logon_flags=0,
                            timeout=45.0,
                        )
                        cell = _project(
                            raw,
                            variant=variant,
                            flags=flags,
                            sid_count=sid_count,
                            probe=probe_name,
                        )
                        per_probe[probe_name] = cell
                        variant_results[variant] = per_probe
                        artifact["partial_variant"] = variant
                        artifact["partial_probe"] = probe_name
                        artifact["variants"] = variant_results
                        persist_artifact()
                        broker_projection = cast(dict[str, object], cell["broker"])
                        self.assertEqual(cell.get("spawn_result"), "PASS")
                        self.assertTrue(
                            broker_projection.get("started") is True
                            and broker_projection.get("finished") is True,
                            f"Gate 1.9 broker lifecycle failed for {variant}/{probe_name}",
                        )
                        self.assertEqual(broker_projection.get("token_create"), "PASS")
                        self.assertEqual(broker_projection.get("token_dacl"), "PASS")
                        self.assertEqual(
                            broker_projection.get("dacl_principals"),
                            "LOGON,WORLD,SYNTHETIC_WRITE",
                        )
                        self.assertEqual(broker_projection.get("token_inspection"), "PASS")
                        self.assertEqual(broker_projection.get("token_privileges"), "PASS")
                        self.assertEqual(broker_projection.get("flags_actual"), flags)
                        self.assertEqual(
                            broker_projection.get("restricted_sid_count_actual"), sid_count
                        )
                        self.assertEqual(broker_projection.get("restricted_sid_match"), "PASS")
                        self.assertEqual(broker_projection.get("child_create"), "PASS")
                        if broker_projection.get("child_wait") == "TIMEOUT":
                            self.assertEqual(broker_projection.get("cleanup_action"), "TERMINATE")
                            self.assertEqual(broker_projection.get("cleanup_result"), "PASS")
                        else:
                            self.assertEqual(broker_projection.get("cleanup_action"), "NONE")
                        self.assertIsInstance(
                            broker_projection.get("unexpected_enabled_privileges"), int
                        )
                        self.assertEqual(broker_projection.get("unexpected_enabled_privileges"), 0)
                        self.assertIn(
                            broker_projection.get("se_change_notify"),
                            {"ENABLED", "DISABLED", "ABSENT"},
                        )
                    variant_results[variant] = per_probe
                    artifact["completed_variants"] = tuple(variant_results)
                    artifact["earliest_stage"] = {
                        name: _earliest_stage(probes) for name, probes in variant_results.items()
                    }
                    persist_artifact()
                artifact["copied_into_authorized_workspace"] = True
                artifact["probe_execution_count"] = sum(
                    len(probes) for probes in variant_results.values()
                )
                artifact["classification"] = _classify(variant_results)
                artifact["status"] = "COMPLETED"
                persist_artifact()
                self.assertEqual(artifact["probe_execution_count"], 16)
                self.assertEqual(
                    {
                        cast(dict[str, object], cell["broker"]).get("dacl_principals")
                        for probes in variant_results.values()
                        for cell in probes.values()
                    },
                    {"LOGON,WORLD,SYNTHETIC_WRITE"},
                )
            finally:
                await asyncio.to_thread(authority.cleanup, setup_request)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
