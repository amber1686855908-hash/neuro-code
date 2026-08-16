"""W5 Gate 1.8 evidence for orthogonal restricted-token factors.

This gate is evidence-only.  It holds ``DISABLE_MAX_PRIVILEGE`` and
``LUA_TOKEN`` at their production values, then varies ``WRITE_RESTRICTED``
and the singleton synthetic restricting SID independently.  The broker and
native probes run under the same W2 Online identity and workspace model used
by Gate 1.7; no production token or runner code is changed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tests.security.test_windows_native_runtime_acceptance import (
    _compile_msvc_probe,
    _find_vswhere,
)
from tests.security.test_windows_native_workload_compatibility import (
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import (
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

_BASE = "207b362dab6a35ed2b0b3638db00a91d64268d4f"
_MAX_MARKER_VALUE = 128
_MARKER_RE = re.compile(r"^(?:W5_GATE16|W5_GATE18)_[A-Z0-9_]+(?:=.*)?$")
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


class _Gate18BuildError(RuntimeError):
    """The trusted controller could not build the Gate 1.8 broker."""


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
        raise _Gate18BuildError("vswhere did not find an MSVC installation")
    installation = next(
        (Path(line.strip()) for line in discovery.stdout.splitlines() if line.strip()),
        None,
    )
    if installation is None:
        raise _Gate18BuildError("vswhere returned no installation path")
    vcvars = installation / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise _Gate18BuildError("vcvars64.bat is unavailable")
    source = Path(__file__).with_name("windows_w5_gate1_7_token_broker.c").resolve()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", os.environ.get("TEMP", ".")))
    build_directory = Path(
        __import__("tempfile").mkdtemp(prefix="neuro-code-w5-gate18-broker-", dir=runner_temp)
    )
    output = build_directory / "windows_w5_gate1_8_token_broker.exe"
    result = _run_vcvars_command(
        vcvars,
        f'cl /nologo /W4 /WX /MT /O2 /DNEURO_GATE18 /Fe:"{output}" "{source}" Advapi32.lib',
        cwd=build_directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        diagnostic = (result.stderr or result.stdout or "").strip().replace("\x00", "")[:512]
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _Gate18BuildError(f"token broker build failed: {diagnostic}")
    return output


def _parse_markers(output: bytes) -> dict[str, str]:
    text = output.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not _MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
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
    prefix = f"W5_GATE16_{probe_name}_"
    prefixes = (prefix,)
    if probe_name == "P3":
        prefixes = (*prefixes, "W5_GATE16_BCRYPT_", "W5_GATE16_NCRYPT_", "W5_GATE16_BEFORE_")
    child_markers = {key: value for key, value in markers.items() if key.startswith(prefixes)}
    started = f"W5_GATE16_{probe_name}_STARTED" in child_markers
    finished = f"W5_GATE16_{probe_name}_FINISHED" in child_markers
    if probe_name == "P4":
        load_key = "W5_GATE16_P4_BCRYPT_LOAD"
        error_key = "W5_GATE16_P4_BCRYPT_LOAD_ERROR"
    else:
        load_key = "W5_GATE16_BCRYPT_LOAD"
        error_key = "W5_GATE16_BCRYPT_LOAD_ERROR"
    return {
        "started": started,
        "finished": finished,
        "first_marker": next((key for key in markers if key.startswith(prefix)), None),
        "load_library_attempted": (
            f"W5_GATE16_{probe_name}_BEFORE_LOAD_BCRYPT" in child_markers
            or "W5_GATE16_BEFORE_LOAD_BCRYPT" in child_markers
        ),
        "bcrypt_load": child_markers.get(load_key),
        "bcrypt_load_error": _marker_int(child_markers, error_key),
        "markers": child_markers,
    }


def _projection(
    raw: dict[str, object],
    *,
    variant: str,
    flags: int,
    expected_sid_count: int,
    expected_sid: bool,
    probe_name: str,
) -> dict[str, object]:
    captured = raw.pop("_captured_stdout", b"")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    broker = {
        "started": "W5_GATE18_BROKER_STARTED" in markers,
        "finished": "W5_GATE18_BROKER_FINISHED" in markers,
        "token_create": markers.get("W5_GATE18_TOKEN_CREATE"),
        "token_dacl": markers.get("W5_GATE18_TOKEN_DACL"),
        "token_inspection": markers.get("W5_GATE18_TOKEN_INSPECTION"),
        "token_privileges": markers.get("W5_GATE18_TOKEN_PRIVILEGES"),
        "token_privilege_count": _marker_int(markers, "W5_GATE18_TOKEN_PRIVILEGE_COUNT"),
        "se_change_notify": markers.get("W5_GATE18_SE_CHANGE_NOTIFY"),
        "token_restricted": markers.get("W5_GATE18_TOKEN_RESTRICTED"),
        "flags_actual": _marker_int(markers, "W5_GATE18_FLAGS"),
        "restricted_sid_count_actual": _marker_int(markers, "W5_GATE18_RESTRICTED_SID_COUNT"),
        "restricted_sid_expected": (
            "installation synthetic write SID (redacted)" if expected_sid else None
        ),
        "restricted_sid_match": markers.get("W5_GATE18_RESTRICTED_SID_MATCH"),
        "child_create": markers.get("W5_GATE18_CHILD_CREATE"),
        "child_create_error": _marker_int(markers, "W5_GATE18_CHILD_CREATE_ERROR"),
        "child_wait": markers.get("W5_GATE18_CHILD_WAIT"),
        "child_exit": _marker_int(markers, "W5_GATE18_CHILD_EXIT"),
    }
    return {
        "variant": variant,
        "flags_expected": flags,
        "expected_restricted_sid_count": expected_sid_count,
        "spawn_result": raw.get("spawn_result"),
        "classification": raw.get("classification"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "stdout_preview": raw.get("stdout_preview"),
        "stderr_preview": raw.get("stderr_preview"),
        "win32_error": raw.get("win32_error"),
        "profile": "NO_PROFILE",
        "broker": broker,
        "probe": _probe_projection(probe_name, markers),
    }


def _load_observation(cell: dict[str, object]) -> str:
    probe = cast(dict[str, object], cell["probe"])
    if probe.get("started") is not True:
        return "NO_USER_CODE"
    load = probe.get("bcrypt_load")
    error = probe.get("bcrypt_load_error")
    if load == "PASS":
        return "PASS"
    if load == "FAIL":
        return f"FAIL_{error}" if isinstance(error, int) else "FAIL_UNKNOWN"
    return "LOAD_NOT_OBSERVED"


def _classify(results: dict[str, dict[str, dict[str, object]]]) -> str:
    def load(variant: str, probe: str) -> str:
        return _load_observation(results[variant][probe])

    dl_pass = all(load("DL", probe) == "PASS" for probe in ("P3", "P4"))
    dlw0_pass = all(load("DLW0", probe) == "PASS" for probe in ("P3", "P4"))
    dlwr_fail = all(load("DLWR", probe) == "FAIL_1114" for probe in ("P3", "P4"))
    dlr_reaches = all(load("DLR", probe) != "NO_USER_CODE" for probe in ("P3", "P4"))
    dlr_fails = all(load("DLR", probe) == "FAIL_1114" for probe in ("P3", "P4"))
    if dlr_reaches and dlr_fails and dlw0_pass and dlwr_fail:
        return "RESTRICTING_SID_ISOLATED_AS_NECESSARY_CAUSAL_COMPONENT"
    if dl_pass and dlw0_pass and dlwr_fail:
        return "WRITE_RESTRICTED_FLAG_ALONE_NOT_SUFFICIENT;SYNTHETIC_RESTRICTING_SID_REQUIRED_IN_WR_PATH"
    if dl_pass and all(load("DLW0", probe) == "FAIL_1114" for probe in ("P3", "P4")) and dlwr_fail:
        return "WRITE_RESTRICTED_FLAG_SUFFICIENT_FOR_BCRYPT_FAILURE"
    dlr_preload_failure = all(load("DLR", probe) == "NO_USER_CODE" for probe in ("P3", "P4"))
    if dlw0_pass and dlwr_fail and dlr_preload_failure:
        return "RESTRICTING_SID_WITHOUT_WRITE_RESTRICTED_IS_TOO_STRICT_FOR_THIS_EXECUTION_PATH"
    return "W5_GATE18_RESULT_INCONCLUSIVE"


async def _cleanup_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate18TokenAblationTests(unittest.IsolatedAsyncioTestCase):
    """Run the four orthogonal token variants under the same W2 authority."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 1.8 evidence requires the enabled CI gate"
    )
    async def test_gate18_orthogonal_write_restricted_sid_ablation(
        self,
    ) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        production_diff = _production_source_diff()
        self.assertEqual(production_diff, (), "Gate 1.8 must not modify production source")

        broker = await asyncio.to_thread(_compile_broker)
        self.addAsyncCleanup(_cleanup_directory, broker.parent)
        probe_paths: dict[str, Path] = {}
        for name, source_name in _PROBE_SOURCES.items():
            probe = await asyncio.to_thread(
                _compile_msvc_probe,
                _source_path(source_name),
                f"windows_w5_gate18_{name.casefold()}",
                libraries=("Advapi32.lib", "Userenv.lib"),
            )
            probe_paths[name] = probe
            self.addAsyncCleanup(_cleanup_directory, probe.parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE18_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_8",
            "base": _BASE,
            "production_source_diff": production_diff,
            "probe_order": tuple(_PROBE_SOURCES),
            "variant_order": tuple(name for name, _, _, _ in _VARIANTS),
            "authorities": ["W2_ONLINE_NO_PROFILE"],
            "fixed_flags": {
                "DISABLE_MAX_PRIVILEGE": True,
                "LUA_TOKEN": True,
            },
            "production_token_contract": {
                "variant": "DLWR",
                "flags": 0xD,
                "restricted_sid_count": 1,
                "sid_value": "installation synthetic write SID (redacted)",
                "production_token_launch_equivalent": False,
            },
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
                destination = workspace / f"gate18-{name.casefold()}.exe"
                shutil.copy2(path, destination)
                copied[name] = destination
            broker_destination = workspace / "gate18-token-broker.exe"
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
                for variant, flags, sid_count, has_sid in _VARIANTS:
                    artifact["current_variant"] = variant
                    persist_artifact()
                    per_probe: dict[str, dict[str, object]] = {}
                    for probe_name, probe_path in copied.items():
                        args = (variant, record.write_sid.value, str(probe_path), str(workspace))
                        broker_spec = _Workload(
                            "GATE18_BROKER", variant.casefold(), broker_destination, args
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
                            timeout=90.0,
                        )
                        cell = _projection(
                            raw,
                            variant=variant,
                            flags=flags,
                            expected_sid_count=sid_count,
                            expected_sid=has_sid,
                            probe_name=probe_name,
                        )
                        broker_projection = cast(dict[str, object], cell["broker"])
                        self.assertEqual(
                            cell.get("spawn_result"),
                            "PASS",
                            f"Gate 1.8 broker launch failed for {variant}/{probe_name}",
                        )
                        self.assertTrue(
                            broker_projection.get("started") is True
                            and broker_projection.get("finished") is True,
                            f"Gate 1.8 broker lifecycle failed for {variant}/{probe_name}",
                        )
                        self.assertEqual(broker_projection.get("token_create"), "PASS")
                        self.assertEqual(broker_projection.get("token_dacl"), "PASS")
                        self.assertEqual(broker_projection.get("token_inspection"), "PASS")
                        self.assertEqual(broker_projection.get("token_privileges"), "PASS")
                        self.assertIsInstance(broker_projection.get("token_privilege_count"), int)
                        self.assertIn(
                            broker_projection.get("se_change_notify"),
                            {"ENABLED", "DISABLED", "ABSENT"},
                        )
                        self.assertEqual(broker_projection.get("flags_actual"), flags)
                        self.assertEqual(
                            broker_projection.get("restricted_sid_count_actual"), sid_count
                        )
                        self.assertEqual(broker_projection.get("restricted_sid_match"), "PASS")
                        per_probe[probe_name] = cell
                    variant_results[variant] = per_probe
                    artifact["completed_variants"] = tuple(variant_results)
                    persist_artifact()
                artifact["copied_into_authorized_workspace"] = True
                production_cell = variant_results["DLWR"]["P3"]
                production_broker = cast(dict[str, object], production_cell["broker"])
                artifact["production_token_contract"]["production_token_launch_equivalent"] = bool(
                    production_broker.get("started") is True
                    and production_broker.get("finished") is True
                    and production_broker.get("token_create") == "PASS"
                    and production_broker.get("token_inspection") == "PASS"
                    and production_broker.get("token_privileges") == "PASS"
                    and production_broker.get("flags_actual") == 0xD
                    and production_broker.get("restricted_sid_count_actual") == 1
                    and production_broker.get("restricted_sid_match") == "PASS"
                )
                artifact["probe_execution_count"] = sum(
                    len(probes) for probes in variant_results.values()
                )
                artifact["classification"] = _classify(variant_results)
                artifact["status"] = "COMPLETED"
                persist_artifact()
                self.assertEqual(artifact["probe_execution_count"], 12)
            finally:
                await asyncio.to_thread(authority.cleanup, setup_request)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
