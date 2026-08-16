"""W5 Gate 1.10 evidence for the Windows Write Restricted Code SID.

This gate compares the current singleton synthetic restricting SID with the
same token plus the documented ``WinWriteRestrictedCodeSid``.  The broker is
test-only: production token construction, ACL setup, and runtime routing are
not changed by this experiment.
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

from tests.security.test_windows_native_runtime_acceptance import (
    _compile_msvc_probe,
)
from tests.security.test_windows_native_workload_compatibility import (
    _build_workloads,
    _completed_classification,
    _discover_base_python,
    _nul_mode_results,
    _output_matches,
    _preview,
    _provenance,
    _request,
    _tool_paths,
    _Workload,
)
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
    WindowsAccountSid,
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    WRITE_ACCESS_MASK,
    WRITE_ONLY_ACCESS_MASK,
    WindowsManagedAce,
    WindowsManagedAceKind,
    _NativeWindowsAclApi,
)
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

_BASE = "3c96f9fe34a9284641927740525585f32f39efff"
_WRC_SID = "S-1-5-33"
_MARKER_PREFIX = "W5_GATE110_"
_PROBE_MARKER_PREFIXES = ("W5_GATE16_", _MARKER_PREFIX)
_VARIANTS = (
    ("PROD_SYN", 0x0000000D, 1),
    ("PROD_SYN_WRC", 0x0000000D, 2),
)
_NATIVE_PROBES = {
    "P0": "windows_w5_gate1_6_p0.c",
    "P3": "windows_w5_gate1_6_p3_dynamic.c",
    "P4": "windows_w5_gate1_7_p4_bcrypt_dynamic.c",
}
_WORKLOAD_ALIASES = {
    "CMD_BASIC": "CMD_BASIC",
    "CMD_NUL_REDIRECT": "CMD_NUL_REDIRECT",
    "NUL_DIRECT_WIN32": "NUL_DIRECT_WIN32",
    "POWERSHELL_BASIC": "POWERSHELL_BASIC",
    "PWSH_BASIC": "PWSH_BASIC",
    "PYTHON_BASE_VERSION": "PYTHON_BASE_VERSION",
    "PYTHON_BASE_MINIMAL_NO_SITE": "PYTHON_BASE_MINIMAL_NO_SITE",
    "PYTHON_VENV_VERSION": "PYTHON_VERSION",
    "PYTHON_VENV_MINIMAL_NO_SITE": "PYTHON_MINIMAL_NO_SITE",
    "GIT_VERSION": "GIT_VERSION",
    "GIT_REPO_DISCOVERY": "GIT_REPO_DISCOVERY",
    "GIT_STATUS": "GIT_STATUS",
    "NODE_VERSION": "NODE_VERSION",
    "NODE_EXEC": "NODE_EXEC",
    "NPM_VERSION": "NPM_VERSION",
    "CURL_VERSION": "CURL_VERSION",
}


class _Gate110BuildError(RuntimeError):
    """The trusted Windows controller could not build a Gate 1.10 probe."""


async def _remove_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _compile_gate110_broker() -> Path:  # pragma: no cover - Windows CI
    vcvars = _discover_vcvars()
    source = Path(__file__).with_name("windows_w5_gate1_7_token_broker.c").resolve()
    directory = Path(
        mkdtemp(prefix="neuro-code-w5-gate110-broker-", dir=os.environ.get("RUNNER_TEMP"))
    )
    output = directory / "windows_w5_gate1_10_token_broker.exe"
    result = _run_vcvars_command(
        vcvars,
        f'cl /nologo /W4 /WX /MT /O2 /DNEURO_GATE110 /Fe:"{output}" "{source}" Advapi32.lib',
        cwd=directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        detail = (result.stdout or result.stderr or "").strip()[:512]
        shutil.rmtree(directory, ignore_errors=True)
        raise _Gate110BuildError(f"Gate 1.10 broker build failed: {detail}")
    return output


def _compile_gate110_praw() -> tuple[Path, dict[str, object]]:  # pragma: no cover
    vcvars = _discover_vcvars()
    source = Path(__file__).with_name("windows_w5_gate1_10_praw.c").resolve()
    directory = Path(
        mkdtemp(prefix="neuro-code-w5-gate110-praw-", dir=os.environ.get("RUNNER_TEMP"))
    )
    output = directory / "windows_w5_gate1_10_praw.exe"
    result = _run_vcvars_command(
        vcvars,
        (
            f'cl /nologo /W4 /WX /O2 /GS- /Fe:"{output}" "{source}" Kernel32.lib '
            "/link /NODEFAULTLIB /ENTRY:gate110_raw_entry /SUBSYSTEM:CONSOLE"
        ),
        cwd=directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        detail = (result.stdout or result.stderr or "").strip()[:512]
        shutil.rmtree(directory, ignore_errors=True)
        raise _Gate110BuildError(f"Gate 1.10 PRAW build failed: {detail}")
    return output, _inspect_imports(output, vcvars, directory)


def _compile_write_probe() -> Path:  # pragma: no cover - Windows CI
    source = Path(__file__).with_name("windows_w5_gate1_10_write.c").resolve()
    return _compile_msvc_probe(
        source,
        "windows_w5_gate110_write",
        libraries=("Kernel32.lib",),
    )


def _parse_markers(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith(_PROBE_MARKER_PREFIXES):
            continue
        key, separator, value = line.partition("=")
        if key.startswith(_PROBE_MARKER_PREFIXES):
            markers[key] = value[:256] if separator else "OBSERVED"
    return markers


def _marker_int(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _probe_projection(name: str, markers: dict[str, str]) -> dict[str, object]:
    if name == "PRAW":
        started = f"{_MARKER_PREFIX}PRAW_ENTRY" in markers
        return {
            "started": started,
            "finished": started,
            "first_marker": f"{_MARKER_PREFIX}PRAW_ENTRY" if started else None,
            "bcrypt_load": None,
            "bcrypt_load_error": None,
            "markers": {
                key: value for key, value in markers.items() if key.startswith(_MARKER_PREFIX)
            },
        }
    prefix = f"W5_GATE16_{name}_"
    prefixes = (prefix, "W5_GATE16_BCRYPT_", "W5_GATE16_NCRYPT_", "W5_GATE16_BEFORE_")
    child = {key: value for key, value in markers.items() if key.startswith(prefixes)}
    load_key = f"W5_GATE16_{name}_BCRYPT_LOAD" if name == "P4" else "W5_GATE16_BCRYPT_LOAD"
    error_key = (
        f"W5_GATE16_{name}_BCRYPT_LOAD_ERROR" if name == "P4" else "W5_GATE16_BCRYPT_LOAD_ERROR"
    )
    return {
        "started": f"W5_GATE16_{name}_STARTED" in markers,
        "finished": f"W5_GATE16_{name}_FINISHED" in markers,
        "first_marker": next((key for key in markers if key.startswith(prefix)), None),
        "load_library_attempted": (
            f"W5_GATE16_{name}_BEFORE_LOAD_BCRYPT" in markers
            or "W5_GATE16_BEFORE_LOAD_BCRYPT" in markers
        ),
        "bcrypt_load": child.get(load_key),
        "bcrypt_load_error": _marker_int(child, error_key),
        "markers": child,
    }


def _token_projection(raw: dict[str, object], markers: dict[str, str]) -> dict[str, object]:
    return {
        "started": f"{_MARKER_PREFIX}BROKER_STARTED" in markers,
        "finished": f"{_MARKER_PREFIX}BROKER_FINISHED" in markers,
        "token_create": markers.get(f"{_MARKER_PREFIX}TOKEN_CREATE"),
        "token_dacl": markers.get(f"{_MARKER_PREFIX}TOKEN_DACL"),
        "dacl_principals": markers.get(f"{_MARKER_PREFIX}DACL_PRINCIPALS"),
        "flags": _marker_int(markers, f"{_MARKER_PREFIX}FLAGS"),
        "is_token_restricted": markers.get(f"{_MARKER_PREFIX}TOKEN_RESTRICTED") == "PASS",
        "restricted_sid_count": _marker_int(markers, f"{_MARKER_PREFIX}RESTRICTED_SID_COUNT"),
        "restricted_sid_match": markers.get(f"{_MARKER_PREFIX}RESTRICTED_SID_MATCH"),
        "token_inspection": markers.get(f"{_MARKER_PREFIX}TOKEN_INSPECTION"),
        "token_privilege_count": _marker_int(markers, f"{_MARKER_PREFIX}TOKEN_PRIVILEGE_COUNT"),
        "se_change_notify": markers.get(f"{_MARKER_PREFIX}SE_CHANGE_NOTIFY"),
        "unexpected_enabled_privileges": _marker_int(
            markers, f"{_MARKER_PREFIX}UNEXPECTED_ENABLED_PRIVILEGES"
        ),
        "wrc_type": markers.get(f"{_MARKER_PREFIX}WRC_TYPE"),
        "wrc_sid": markers.get(f"{_MARKER_PREFIX}WRC_SID"),
        "wrc_canonical_match": markers.get(f"{_MARKER_PREFIX}WRC_CANONICAL_MATCH"),
        "wrc_create": markers.get(f"{_MARKER_PREFIX}WRC_CREATE"),
        "child_create": markers.get(f"{_MARKER_PREFIX}CHILD_CREATE"),
        "child_wait": markers.get(f"{_MARKER_PREFIX}CHILD_WAIT"),
        "child_exit": _marker_int(markers, f"{_MARKER_PREFIX}CHILD_EXIT"),
        "child_cleanup": markers.get(f"{_MARKER_PREFIX}CHILD_CLEANUP"),
        "child_cleanup_result": markers.get(f"{_MARKER_PREFIX}CHILD_CLEANUP_RESULT"),
        "stdout_preview": raw.get("stdout_preview", ""),
        "stderr_preview": raw.get("stderr_preview", ""),
    }


def _clean_child_output(data: bytes) -> bytes:
    lines = data.decode("utf-8", errors="replace").replace("\r", "").splitlines()
    return (
        "\n".join(line for line in lines if not line.startswith(_MARKER_PREFIX)) + "\n"
    ).encode()


def _cell(
    raw: dict[str, object], *, variant: str, expected_count: int, probe: str
) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    data = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(data)
    return {
        "variant": variant,
        "expected_restricted_sid_count": expected_count,
        "spawn_result": raw.get("spawn_result"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "classification": raw.get("classification"),
        "broker": _token_projection(raw, markers),
        "probe": _probe_projection(probe, markers),
        "stdout_preview": _preview(data),
        "stderr_preview": raw.get("stderr_preview", ""),
    }


def _write_result(raw: dict[str, object]) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    data = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(data)
    marker = markers.get(f"{_MARKER_PREFIX}WRITE")
    child_exit = _marker_int(markers, f"{_MARKER_PREFIX}CHILD_EXIT")
    return {
        "spawn_result": raw.get("spawn_result"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "actual": "ALLOW" if marker == "PASS" else "DENY" if marker == "DENY" else "INCONCLUSIVE",
        "child_exit": child_exit,
        "write_error": _marker_int(markers, f"{_MARKER_PREFIX}WRITE_ERROR"),
        "broker": _token_projection(raw, markers),
        "stdout_preview": _preview(data),
        "stderr_preview": raw.get("stderr_preview", ""),
    }


def _reconcile_file(
    api: _NativeWindowsAclApi, path: Path, entries: tuple[WindowsManagedAce, ...]
) -> None:
    api.reconcile(path, desired=entries, remove=())


def _state(path: Path) -> dict[str, object]:
    return {
        "exists": path.exists(),
        "content": path.read_bytes()[:64].decode("ascii", "replace") if path.exists() else "",
    }


def _compatibility_classification(synthetic: str, candidate: str) -> str:
    if synthetic == candidate:
        return "UNCHANGED_PASS" if synthetic == "PASS" else "UNCHANGED_FAILURE"
    if synthetic != "PASS" and candidate == "PASS":
        return "RECOVERED"
    if synthetic == "PASS" and candidate != "PASS":
        return "REGRESSED"
    return "INCONCLUSIVE"


class WindowsW5Gate110WriteRestrictedCodeTests(unittest.IsolatedAsyncioTestCase):
    """Measure WRC compatibility and its exact second-pass authority."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.10 is CI-only")
    async def test_gate110_wrc_evidence(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        self.assertEqual(_production_source_diff(), ())

        broker = await asyncio.to_thread(_compile_gate110_broker)
        praw, praw_imports = await asyncio.to_thread(_compile_gate110_praw)
        write_probe = await asyncio.to_thread(_compile_write_probe)
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, praw.parent)
        self.addAsyncCleanup(_remove_directory, write_probe.parent)

        native_probes: dict[str, Path] = {"PRAW": praw}
        for name, source_name in _NATIVE_PROBES.items():
            native_probes[name] = await asyncio.to_thread(
                _compile_msvc_probe,
                _source_path(source_name),
                f"windows_w5_gate110_{name.casefold()}",
                libraries=("Advapi32.lib", "Userenv.lib"),
            )
            self.addAsyncCleanup(_remove_directory, native_probes[name].parent)

        paths = _tool_paths()
        paths["python_base"] = _discover_base_python(paths["python"])
        artifact_path = os.environ.get("NEURO_CODE_W5_GATE1_10_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_10",
            "base": _BASE,
            "production_source_diff": (),
            "wrc": {
                "creation_api": "CreateWellKnownSid",
                "well_known_type": "WinWriteRestrictedCodeSid",
                "canonical_sid_expected": _WRC_SID,
                "praw_imports": praw_imports,
            },
            "default_dacl": {
                "principals": ("LOGON", "WORLD", "SYNTHETIC_WRITE"),
                "identical": True,
            },
            "launch_contract": {
                "api": "CreateProcessAsUserW",
                "profile": "NO_PROFILE",
                "stdio": "HANDLE_LIST(stdin,stdout,stderr)",
                "job_attachment": "NONE",
                "identical": True,
            },
            "variants": {},
            "workload_aliases": _WORKLOAD_ALIASES,
            "status": "RUNNING",
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            readonly = root / "readonly"
            installation = root / "installation"
            outside = root / "outside"
            outside_wrc = root / "outside-wrc"
            repo = workspace / "compat-repo"
            for path in (workspace, readonly, installation, outside, outside_wrc, repo):
                path.mkdir(parents=True)
            persist()

            nul_probe = await asyncio.to_thread(
                __import__(
                    "tests.security.test_windows_native_workload_compatibility",
                    fromlist=["_compile_nul_probe"],
                )._compile_nul_probe
            )
            self.addAsyncCleanup(_remove_directory, nul_probe.parent)
            copied_nul = workspace / "windows-nul-probe.exe"
            shutil.copy2(nul_probe, copied_nul)
            if paths["git"] is not None:
                await asyncio.to_thread(
                    subprocess.run,
                    [str(paths["git"]), "init", "-q", str(repo)],
                    check=False,
                    capture_output=True,
                    timeout=15,
                    shell=False,
                )

            sensitive = installation / "sensitive-state.bin"
            sensitive.write_bytes(b"W5_GATE110_SENSITIVE\n")
            readonly_file = readonly / "readonly.bin"
            readonly_file.write_bytes(b"W5_GATE110_READONLY\n")
            installation_file = installation / "installation.bin"
            installation_file.write_bytes(b"W5_GATE110_INSTALLATION\n")

            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace, readonly),
                writable_roots=(workspace,),
                sensitive_read_paths=(sensitive,),
            )
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                account_api=_NativeWindowsSandboxAccountApi(),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            online_sid = online.user_sid
            write_sid = record.write_sid
            acl_api = _NativeWindowsAclApi()
            everyone = WindowsAccountSid("S-1-1-0")
            wrc = WindowsAccountSid(_WRC_SID)

            # Explicit file fixtures make each second-pass authority oracle
            # deterministic while preserving all inherited setup ACEs.
            authorized = workspace / "authorized.bin"
            authorized.write_bytes(b"W5_GATE110_AUTHORIZED\n")
            _reconcile_file(
                acl_api,
                authorized,
                (
                    WindowsManagedAce(
                        authorized, online_sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK
                    ),
                    WindowsManagedAce(
                        authorized,
                        write_sid,
                        WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                        WRITE_ONLY_ACCESS_MASK,
                    ),
                ),
            )
            broad_file = outside / "broad.bin"
            broad_file.write_bytes(b"W5_GATE110_BROAD\n")
            _reconcile_file(
                acl_api,
                broad_file,
                (
                    WindowsManagedAce(
                        broad_file, online_sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK
                    ),
                    WindowsManagedAce(
                        broad_file, everyone, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK
                    ),
                ),
            )
            wrc_file = outside_wrc / "wrc.bin"
            wrc_file.write_bytes(b"W5_GATE110_WRC\n")
            _reconcile_file(
                acl_api,
                wrc_file,
                (
                    WindowsManagedAce(
                        wrc_file, online_sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK
                    ),
                    WindowsManagedAce(
                        wrc_file, wrc, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK
                    ),
                ),
            )
            _reconcile_file(
                acl_api,
                readonly_file,
                (
                    WindowsManagedAce(
                        readonly_file,
                        online_sid,
                        WindowsManagedAceKind.READ_ONLY_WRITE_DENY,
                        WRITE_ONLY_ACCESS_MASK,
                        inheritance=0,
                    ),
                ),
            )
            _reconcile_file(
                acl_api,
                sensitive,
                (
                    WindowsManagedAce(
                        sensitive,
                        online_sid,
                        WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY,
                        WRITE_ACCESS_MASK,
                        inheritance=0,
                    ),
                ),
            )
            _reconcile_file(
                acl_api,
                installation_file,
                (
                    WindowsManagedAce(
                        installation_file,
                        online_sid,
                        WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY,
                        WRITE_ACCESS_MASK,
                        inheritance=0,
                    ),
                ),
            )
            artifact["security_fixtures"] = {
                "authorized_workspace": str(authorized),
                "outside_broad_no_cap": str(broad_file),
                "outside_wrc_only": str(wrc_file),
                "read_only": str(readonly_file),
                "installation": str(installation_file),
                "credential": str(store.path),
                "acl_contract": "synthetic-only workspace; no synthetic/WRC outside broad; WRC-only explicit ACE",
            }
            persist()

            broker_destination = workspace / "gate110-token-broker.exe"
            shutil.copy2(broker, broker_destination)
            probe_destinations: dict[str, Path] = {}
            for name, path in native_probes.items():
                target = workspace / f"gate110-{name.casefold()}.exe"
                shutil.copy2(path, target)
                probe_destinations[name] = target
            write_destination = workspace / "gate110-write.exe"
            shutil.copy2(write_probe, write_destination)

            harness = _Gate1DirectProcess()

            async def run_broker(
                variant: str,
                child: Path,
                child_args: tuple[str, ...] = (),
                limit_seconds: float = 35.0,
            ) -> dict[str, object]:
                arguments = (variant, write_sid.value, str(child), str(workspace), *child_args)
                spec = _Workload(
                    "GATE110_BROKER", variant.casefold(), broker_destination, arguments
                )
                return await asyncio.to_thread(
                    _run_harness_bounded,
                    harness,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=broker_destination,
                    arguments=arguments,
                    cwd=workspace,
                    environment=_environment_for(_request(spec, workspace)),
                    logon_flags=0,
                    timeout=limit_seconds,
                )

            variant_results: dict[str, object] = {}
            for variant, _flags, expected_count in _VARIANTS:
                native_cells: dict[str, object] = {}
                for probe_name, child in probe_destinations.items():
                    raw = await run_broker(variant, child)
                    cell = _cell(
                        raw, variant=variant, expected_count=expected_count, probe=probe_name
                    )
                    native_cells[probe_name] = cell
                    broker_projection = cast(dict[str, object], cell["broker"])
                    self.assertEqual(broker_projection["started"], True)
                    self.assertEqual(broker_projection["finished"], True)
                    self.assertEqual(broker_projection["token_create"], "PASS")
                    self.assertEqual(broker_projection["flags"], _flags)
                    self.assertEqual(broker_projection["token_dacl"], "PASS")
                    self.assertEqual(
                        broker_projection["dacl_principals"], "LOGON,WORLD,SYNTHETIC_WRITE"
                    )
                    self.assertEqual(broker_projection["restricted_sid_count"], expected_count)
                    self.assertEqual(broker_projection["restricted_sid_match"], "PASS")
                    self.assertEqual(broker_projection["token_inspection"], "PASS")
                    self.assertEqual(broker_projection["se_change_notify"], "ENABLED")
                    self.assertEqual(broker_projection["unexpected_enabled_privileges"], 0)
                    self.assertEqual(broker_projection["wrc_create"], "PASS")
                    self.assertEqual(broker_projection["wrc_canonical_match"], "PASS")
                    wrc_metadata = cast(dict[str, object], artifact["wrc"])
                    wrc_metadata["observed_type"] = broker_projection["wrc_type"]
                    wrc_metadata["observed_sid"] = broker_projection["wrc_sid"]
                    wrc_metadata["canonical_match"] = broker_projection["wrc_canonical_match"]
                variant_results[variant] = {"native": native_cells}
                artifact["variants"] = variant_results
                persist()

            provenance = _provenance(paths, workspace)
            workloads = _build_workloads(
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
            workload_by_name = {workload.name: workload for workload in workloads}
            workload_matrix: dict[str, object] = {}
            for requested_name, source_name in _WORKLOAD_ALIASES.items():
                spec = workload_by_name[source_name]
                workload_cells: dict[str, dict[str, object]] = {}
                for variant, _flags, _count in _VARIANTS:
                    if spec.executable is None:
                        workload_cells[variant] = {
                            "classification": "NOT_INSTALLED",
                            "resolved_executable": None,
                        }
                        continue
                    raw = await run_broker(
                        variant,
                        spec.executable,
                        tuple(spec.arguments),
                        limit_seconds=45.0,
                    )
                    captured = raw.get("_captured_stdout")
                    output = _clean_child_output(captured if isinstance(captured, bytes) else b"")
                    stderr = str(raw.get("stderr_preview", "")).encode()
                    exit_code = raw.get("exit_code")
                    broker_markers = _parse_markers(
                        captured if isinstance(captured, bytes) else b""
                    )
                    child_timeout = broker_markers.get(f"{_MARKER_PREFIX}CHILD_WAIT") == "TIMEOUT"
                    if child_timeout:
                        classification = "TIMEOUT"
                    elif isinstance(exit_code, int):
                        classification = (
                            "PASS"
                            if exit_code == 0 and _output_matches(spec, output, stderr)
                            else _completed_classification(spec, exit_code, output, stderr)
                        )
                    else:
                        classification = "INCONCLUSIVE"
                    workload_cells[variant] = {
                        "resolved_executable": str(spec.executable),
                        "resolved_launcher": str(spec.resolved_launcher)
                        if spec.resolved_launcher
                        else None,
                        "argv": [str(spec.executable), *spec.arguments],
                        "classification": classification,
                        "exit_code": exit_code,
                        "timeout": child_timeout,
                        "stdout_preview": _preview(output),
                        "stderr_preview": stderr.decode("utf-8", errors="replace")[:512],
                        "nul_modes": _nul_mode_results(output, stderr),
                        "broker": _token_projection(raw, broker_markers),
                    }
                    # Persist the raw fixed-marker projection before any
                    # assertion so a harness/child-startup failure remains
                    # auditable in the uploaded artifact.
                    artifact["workloads"] = workload_matrix | {requested_name: workload_cells}
                    persist()
                    broker_result = cast(dict[str, object], workload_cells[variant]["broker"])
                    self.assertEqual(broker_result["started"], True)
                    self.assertEqual(broker_result["token_inspection"], "PASS")
                    # A workload may be one of the intentionally observed
                    # startup/compatibility timeouts.  The broker reports
                    # that bounded child wait separately; that is evidence,
                    # not a harness failure.  An outer controller timeout or
                    # a missing child-wait oracle remains a hard failure.
                    if broker_result["finished"] is not True:
                        self.assertFalse(
                            bool(raw.get("timeout")),
                            f"Gate 1.10 controller timed out for {requested_name}/{variant}",
                        )
                        self.assertIn(
                            broker_result["child_wait"],
                            ("TIMEOUT", "FAIL"),
                            f"missing bounded child-wait oracle for {requested_name}/{variant}",
                        )
                        workload_cells[variant]["broker_completion"] = "CHILD_WAIT_REPORTED"
                    else:
                        workload_cells[variant]["broker_completion"] = "BROKER_FINISHED"
                    # Persist the lifecycle classification as well.
                    artifact["workloads"] = workload_matrix | {requested_name: workload_cells}
                    persist()
                workload_matrix[requested_name] = workload_cells
                synthetic_result = workload_cells["PROD_SYN"].get("classification", "INCONCLUSIVE")
                candidate_result = workload_cells["PROD_SYN_WRC"].get(
                    "classification", "INCONCLUSIVE"
                )
                workload_matrix[requested_name] = {
                    **workload_cells,
                    "impact": _compatibility_classification(
                        str(synthetic_result), str(candidate_result)
                    ),
                }
                artifact["workloads"] = workload_matrix
                persist()

            async def run_write(label: str, target: Path) -> dict[str, object]:
                before = _state(target)
                raw = await run_broker("PROD_SYN", write_destination, (str(target),))
                syn = _write_result(raw)
                after_syn = _state(target)
                await asyncio.to_thread(
                    target.write_bytes, cast(str, before.get("content", "")).encode("ascii")
                )
                before_wrc = _state(target)
                raw_wrc = await run_broker("PROD_SYN_WRC", write_destination, (str(target),))
                wrc_result = _write_result(raw_wrc)
                after_wrc = _state(target)
                # Reset the fixture so the next oracle observes the same bytes.
                await asyncio.to_thread(
                    target.write_bytes, cast(str, before.get("content", "")).encode("ascii")
                )
                return {
                    "label": label,
                    "PROD_SYN": {**syn, "before": before, "after": after_syn},
                    "PROD_SYN_WRC": {**wrc_result, "before": before_wrc, "after": after_wrc},
                }

            security = cast(
                dict[str, dict[str, dict[str, object]]],
                {
                    "AUTHORIZED_WORKSPACE_SYN": await run_write(
                        "AUTHORIZED_WORKSPACE_SYN", authorized
                    ),
                    "OUTSIDE_BROAD_NO_CAP": await run_write("OUTSIDE_BROAD_NO_CAP", broad_file),
                    "OUTSIDE_WRC_ONLY": await run_write("OUTSIDE_WRC_ONLY", wrc_file),
                    "READ_ONLY_MUTATION": await run_write("READ_ONLY_MUTATION", readonly_file),
                    "INSTALLATION_PROTECTION": await run_write(
                        "INSTALLATION_PROTECTION", installation_file
                    ),
                    "CREDENTIAL_PROTECTION": await run_write("CREDENTIAL_PROTECTION", store.path),
                },
            )
            artifact["provenance"] = provenance
            artifact["security"] = security
            artifact["status"] = "COMPLETED"
            artifact["production_source_diff"] = _production_source_diff()
            persist()

            for result in security.values():
                syn_cell = result["PROD_SYN"]
                wrc_cell = result["PROD_SYN_WRC"]
                syn_broker = cast(dict[str, object], syn_cell["broker"])
                wrc_broker = cast(dict[str, object], wrc_cell["broker"])
                self.assertEqual(syn_broker["token_inspection"], "PASS")
                self.assertEqual(wrc_broker["token_inspection"], "PASS")
            self.assertEqual(security["OUTSIDE_BROAD_NO_CAP"]["PROD_SYN"]["actual"], "DENY")
            self.assertEqual(security["OUTSIDE_BROAD_NO_CAP"]["PROD_SYN_WRC"]["actual"], "DENY")
            self.assertEqual(security["AUTHORIZED_WORKSPACE_SYN"]["PROD_SYN"]["actual"], "ALLOW")
            self.assertEqual(
                security["AUTHORIZED_WORKSPACE_SYN"]["PROD_SYN_WRC"]["actual"], "ALLOW"
            )
            self.assertEqual(security["OUTSIDE_WRC_ONLY"]["PROD_SYN"]["actual"], "DENY")
            self.assertEqual(security["OUTSIDE_WRC_ONLY"]["PROD_SYN_WRC"]["actual"], "ALLOW")
            for label in ("READ_ONLY_MUTATION", "INSTALLATION_PROTECTION", "CREDENTIAL_PROTECTION"):
                self.assertEqual(security[label]["PROD_SYN"]["actual"], "DENY")
                self.assertEqual(security[label]["PROD_SYN_WRC"]["actual"], "DENY")

            await asyncio.to_thread(authority.cleanup, setup_request)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
