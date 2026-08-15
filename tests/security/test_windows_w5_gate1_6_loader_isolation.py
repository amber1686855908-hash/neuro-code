"""W5 Gate 1.6 evidence for PE imports and restricted-child DLL startup.

This gate deliberately keeps the four crypto experiments in separate native
executables.  The first marker is emitted from ``main`` before the relevant
API is called, so a missing marker is evidence about process creation or the
PE loader path rather than about a later API result.  The test is evidence
only: it does not change the production Windows sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir, mkdtemp
from typing import cast

from tests.security.test_windows_native_runtime_acceptance import _find_vswhere
from tests.security.test_windows_native_workload_compatibility import (
    _host_run,
    _request,
    _w3_run,
    _Workload,
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
from neuro_code.infrastructure.sandbox import windows_native_runner
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    _WindowsNativeDesktopMode,
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

_BASE = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_LOGON_WITH_PROFILE = 0x00000001
_MAX_MARKER_VALUE = 128
_DLL_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+\.dll)\s*$", re.IGNORECASE | re.MULTILINE)
_MARKER_RE = re.compile(r"^W5_GATE16_[A-Z0-9_]+(?:=.*)?$")
_AUTHORITIES = (
    "HOST",
    "W2_UNRESTRICTED_NO_PROFILE",
    "W2_UNRESTRICTED_WITH_PROFILE",
    "W2_RESTRICTED_NO_PROFILE",
    "W2_RESTRICTED_WITH_PROFILE",
)


class _Gate16ProbeBuildError(RuntimeError):
    """The trusted Windows controller could not build a Gate 1.6 probe."""


@dataclass(frozen=True, slots=True)
class _ProbeDefinition:
    name: str
    source_name: str
    libraries: tuple[str, ...]
    start_marker: str
    finish_marker: str


@dataclass(frozen=True, slots=True)
class _ProbeBinary:
    definition: _ProbeDefinition
    executable: Path
    imports: dict[str, object]


_PROBES = (
    _ProbeDefinition(
        "P0",
        "windows_w5_gate1_6_p0.c",
        ("userenv.lib", "Advapi32.lib"),
        "W5_GATE16_P0_STARTED",
        "W5_GATE16_P0_FINISHED",
    ),
    _ProbeDefinition(
        "P1_BCRYPT_STATIC",
        "windows_w5_gate1_6_p1_bcrypt_static.c",
        ("userenv.lib", "Advapi32.lib", "Bcrypt.lib"),
        "W5_GATE16_P1_STARTED",
        "W5_GATE16_P1_FINISHED",
    ),
    _ProbeDefinition(
        "P2_NCRYPT_STATIC",
        "windows_w5_gate1_6_p2_ncrypt_static.c",
        ("userenv.lib", "Advapi32.lib", "Ncrypt.lib"),
        "W5_GATE16_P2_STARTED",
        "W5_GATE16_P2_FINISHED",
    ),
    _ProbeDefinition(
        "P3_DYNAMIC",
        "windows_w5_gate1_6_p3_dynamic.c",
        (),
        "W5_GATE16_P3_STARTED",
        "W5_GATE16_P3_FINISHED",
    ),
)


def _source_path(definition: _ProbeDefinition) -> Path:
    source = Path(__file__).with_name(definition.source_name).resolve(strict=False)
    if not source.is_file():
        raise _Gate16ProbeBuildError(f"probe source unavailable: {definition.source_name}")
    return source


def _discover_vcvars() -> Path:
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
        raise _Gate16ProbeBuildError("vswhere did not find an MSVC installation")
    installation = next(
        (Path(line.strip()) for line in discovery.stdout.splitlines() if line.strip()),
        None,
    )
    if installation is None:
        raise _Gate16ProbeBuildError("vswhere returned no installation path")
    vcvars = installation / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise _Gate16ProbeBuildError("vcvars64.bat is unavailable")
    return vcvars


def _run_vcvars_command(
    vcvars: Path,
    command: str,
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    script = cwd / "gate16_command.cmd"
    script.write_text(
        f'@echo off\r\ncall "{vcvars}"\r\nif errorlevel 1 exit /b 1\r\n{command}\r\n',
        encoding="ascii",
        newline="",
    )
    return subprocess.run(
        ["cmd.exe", "/d", "/c", script.name],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        cwd=str(cwd),
    )


def _normalized_dlls(text: str) -> tuple[str, ...]:
    return tuple(sorted({match.upper() for match in _DLL_RE.findall(text)}))


def _inspect_imports(executable: Path, vcvars: Path, build_directory: Path) -> dict[str, object]:
    dependent = _run_vcvars_command(
        vcvars,
        f'dumpbin /nologo /dependents "{executable}"',
        cwd=build_directory,
        timeout=30,
    )
    imports = _run_vcvars_command(
        vcvars,
        f'dumpbin /nologo /imports "{executable}"',
        cwd=build_directory,
        timeout=30,
    )
    if dependent.returncode != 0 or imports.returncode != 0:
        diagnostic = dependent.stderr or imports.stderr or dependent.stdout or imports.stdout
        raise _Gate16ProbeBuildError(
            f"dumpbin failed (dependents={dependent.returncode}, imports={imports.returncode}): "
            f"{diagnostic.strip()[:512]}"
        )
    dependent_dlls = _normalized_dlls(dependent.stdout)
    import_dlls = _normalized_dlls(imports.stdout)
    return {
        "dumpbin": "PASS",
        "dependents": dependent_dlls,
        "imports_dlls": import_dlls,
        "dependency_dlls": tuple(sorted(set(dependent_dlls) | set(import_dlls))),
    }


def _compile_probe(definition: _ProbeDefinition) -> _ProbeBinary:  # pragma: no cover - Windows CI
    vcvars = _discover_vcvars()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", gettempdir()))
    build_directory = Path(
        mkdtemp(prefix=f"neuro-code-w5-gate16-{definition.name.casefold()}-", dir=runner_temp)
    )
    source = _source_path(definition)
    output = build_directory / f"windows_w5_gate1_6_{definition.name.casefold()}.exe"
    libraries = " ".join(definition.libraries)
    link_suffix = f" {libraries}" if libraries else ""
    result = _run_vcvars_command(
        vcvars,
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}"{link_suffix}',
        cwd=build_directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        diagnostic = (result.stderr or result.stdout or "").strip().replace("\x00", "")[:512]
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _Gate16ProbeBuildError(
            f"MSVC {definition.name} build failed (returncode={result.returncode}): {diagnostic}"
        )
    imports = _inspect_imports(output, vcvars, build_directory)
    return _ProbeBinary(definition, output, imports)


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _parse_probe_output(cell: dict[str, object], definition: _ProbeDefinition) -> None:
    captured = cell.pop("_captured_stdout", None)
    if isinstance(captured, bytes):
        output = captured.decode("utf-8", errors="replace")
    else:
        output = str(cell.get("stdout_preview", ""))
    lines = output.replace("\r", "").splitlines()
    markers: dict[str, str] = {}
    observed: list[str] = []
    for line in lines:
        if not _MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        if separator:
            markers[key] = value[:_MAX_MARKER_VALUE]
        else:
            markers[key] = "OBSERVED"
        observed.append(key)
    started = definition.start_marker in markers
    finished = definition.finish_marker in markers
    cell["probe"] = {
        "started": started,
        "finished": finished,
        "first_marker": observed[0] if observed else None,
        "markers": markers,
    }
    if cell.get("spawn_result") not in {"PASS", "NOT_APPLICABLE"}:
        cell["probe_start"] = "PROCESS_CREATION_FAILED"
    else:
        cell["probe_start"] = (
            "STARTED_AND_FINISHED"
            if started and finished
            else "STARTED_WITHOUT_FINISH"
            if started
            else "PREMAIN_OR_USER_CODE_NOT_OBSERVED"
        )
    cell["probe_result_available"] = started and finished


def _probe_started(cell: object) -> bool:
    if not isinstance(cell, dict):
        return False
    probe = cell.get("probe")
    return isinstance(probe, dict) and probe.get("started") is True


def _static_classification(definition: _ProbeDefinition, matrix: dict[str, object]) -> str:
    unrestricted = all(_probe_started(matrix.get(name)) for name in _AUTHORITIES[:3])
    restricted = any(_probe_started(matrix.get(name)) for name in _AUTHORITIES[3:])
    if definition.name == "P1_BCRYPT_STATIC" and unrestricted and not restricted:
        return "BCRYPT_STATIC_IMPORT_PREMAIN_CORRELATED"
    if definition.name == "P2_NCRYPT_STATIC" and unrestricted and not restricted:
        return "NCRYPT_STATIC_IMPORT_PREMAIN_CORRELATED"
    if definition.name == "P0" and unrestricted and not restricted:
        return "P0_RESTRICTED_PREMAIN_OR_PROCESS_CREATE_INCONCLUSIVE"
    if definition.name == "P3_DYNAMIC" and unrestricted and not restricted:
        return "P3_RESTRICTED_PRE_DYNAMIC_LOAD_INCONCLUSIVE"
    return "NO_PREMAIN_CORRELATION_ESTABLISHED"


def _production_source_diff() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{_BASE}...HEAD", "--", "src/neuro_code"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    if result.returncode != 0:
        raise _Gate16ProbeBuildError("could not inspect production source diff")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _write_artifact(path: str | None, payload: dict[str, object]) -> None:
    if path:
        Path(path).write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


class WindowsW5Gate16LoaderIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Run the four staged PE-loader probes once on the elevated runner."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 1.6 evidence requires the enabled CI gate"
    )
    async def test_gate16_pe_loader_matrix(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        production_diff = _production_source_diff()
        self.assertEqual(production_diff, (), "Gate 1.6 must not modify production source")
        binaries: list[_ProbeBinary] = []
        for definition in _PROBES:
            binary = await asyncio.to_thread(_compile_probe, definition)
            binaries.append(binary)
            self.addAsyncCleanup(_cleanup_probe_directory, binary.executable.parent)

        imports_by_name = {binary.definition.name: binary.imports for binary in binaries}
        artifact_path = os.environ.get("NEURO_CODE_W5_GATE16_EVIDENCE_JSON")
        _write_artifact(
            artifact_path,
            {
                "gate": "W5_GATE1_6",
                "base": _BASE,
                "probe_order": tuple(definition.name for definition in _PROBES),
                "imports": imports_by_name,
                "production_source_diff": production_diff,
            },
        )
        p0_dlls = set(cast(tuple[str, ...], imports_by_name["P0"]["dependency_dlls"]))
        p1_dlls = set(cast(tuple[str, ...], imports_by_name["P1_BCRYPT_STATIC"]["dependency_dlls"]))
        p2_dlls = set(cast(tuple[str, ...], imports_by_name["P2_NCRYPT_STATIC"]["dependency_dlls"]))
        p3_dlls = set(cast(tuple[str, ...], imports_by_name["P3_DYNAMIC"]["dependency_dlls"]))
        self.assertNotIn("BCRYPT.DLL", p0_dlls)
        self.assertNotIn("NCRYPT.DLL", p0_dlls)
        self.assertIn("BCRYPT.DLL", p1_dlls)
        self.assertIn("NCRYPT.DLL", p2_dlls)
        self.assertNotIn("BCRYPT.DLL", p3_dlls)
        self.assertNotIn("NCRYPT.DLL", p3_dlls)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            copied: dict[str, Path] = {}
            for binary in binaries:
                destination = workspace / f"gate16-{binary.definition.name.casefold()}.exe"
                shutil.copy2(binary.executable, destination)
                copied[binary.definition.name] = destination

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
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                runtime_state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
                _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                _diagnostic_create_no_window=True,
            )
            harness = _Gate1DirectProcess()
            original_flags = windows_native_runner._LOGON_FLAGS
            probe_artifacts: dict[str, object] = {}
            try:
                for binary in binaries:
                    definition = binary.definition
                    copied_executable = copied[definition.name]
                    spec = _Workload(
                        f"GATE16_{definition.name}",
                        "fixed",
                        copied_executable,
                        (),
                    )
                    environment = _environment_for(_request(spec, workspace))
                    matrix: dict[str, object] = {
                        "HOST": await asyncio.to_thread(
                            _host_run,
                            spec,
                            workspace,
                            retain_output=True,
                        ),
                        "W2_UNRESTRICTED_NO_PROFILE": await asyncio.to_thread(
                            harness.run,
                            username=online.username,
                            password=online.password.decode("utf-8"),
                            executable=copied_executable,
                            arguments=(),
                            cwd=workspace,
                            environment=environment,
                            logon_flags=0,
                            retain_output=True,
                        ),
                        "W2_UNRESTRICTED_WITH_PROFILE": await asyncio.to_thread(
                            harness.run,
                            username=online.username,
                            password=online.password.decode("utf-8"),
                            executable=copied_executable,
                            arguments=(),
                            cwd=workspace,
                            environment=environment,
                            logon_flags=_LOGON_WITH_PROFILE,
                            retain_output=True,
                        ),
                    }
                    matrix["W2_RESTRICTED_NO_PROFILE"] = await _w3_run(
                        spec,
                        workspace=workspace,
                        adapter=adapter,
                        expected_user_sid=online.user_sid.value,
                        expected_write_sid=record.write_sid.value,
                        retain_output=True,
                    )
                    try:
                        windows_native_runner._LOGON_FLAGS = _LOGON_WITH_PROFILE
                        matrix["W2_RESTRICTED_WITH_PROFILE"] = await _w3_run(
                            spec,
                            workspace=workspace,
                            adapter=adapter,
                            expected_user_sid=online.user_sid.value,
                            expected_write_sid=record.write_sid.value,
                            retain_output=True,
                        )
                    finally:
                        windows_native_runner._LOGON_FLAGS = original_flags
                    for authority_name in _AUTHORITIES:
                        cell = matrix.get(authority_name)
                        if isinstance(cell, dict):
                            _parse_probe_output(cell, definition)
                    probe_artifacts[definition.name] = {
                        "source": definition.source_name,
                        "link_libraries": definition.libraries,
                        "copied_executable": copied_executable.name,
                        "imports": binary.imports,
                        "classification": _static_classification(definition, matrix),
                        "authorities": matrix,
                    }
            finally:
                windows_native_runner._LOGON_FLAGS = original_flags
                await asyncio.to_thread(authority.cleanup, setup_request)

        artifact = {
            "gate": "W5_GATE1_6",
            "base": _BASE,
            "authorities": _AUTHORITIES,
            "probe_order": tuple(definition.name for definition in _PROBES),
            "probes": probe_artifacts,
            "same_copied_executable_per_probe": True,
            "copied_into_authorized_workspace": True,
            "production_source_diff": production_diff,
            "static_import_experiment": {
                "P0": "no Bcrypt.lib/Ncrypt.lib",
                "P1_BCRYPT_STATIC": "P0 baseline plus Bcrypt.lib",
                "P2_NCRYPT_STATIC": "P0 baseline plus Ncrypt.lib",
                "P3_DYNAMIC": "no crypto import library; LoadLibraryW/GetProcAddress",
            },
        }
        _write_artifact(artifact_path, artifact)
        print("W5_GATE1_6_IMPORTS=" + json.dumps(imports_by_name, sort_keys=True), flush=True)
        print("W5_GATE1_6_MATRIX=" + json.dumps(probe_artifacts, sort_keys=True), flush=True)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
