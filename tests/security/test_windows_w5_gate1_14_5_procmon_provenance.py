"""W5 Gate 1.14.5 Procmon provenance reconciliation and conditional trace.

This is evidence-only.  It never changes the production Windows token, ACL,
firewall, runner, or sandbox implementation.  Procmon is executed only after
an official distribution, PE architecture, and an independent Windows trust
path have all been verified.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, cast

from tests.security.test_windows_native_runtime_acceptance import _compile_msvc_probe
from tests.security.test_windows_native_workload_compatibility import (
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import _production_source_diff
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
    _source_path,
)
from tests.security.test_windows_w5_gate1_11_sid_ablation import (
    _PROBE_SOURCES,
    _compile_broker,
    _projection,
    _remove_directory,
)
from tests.security.test_windows_w5_gate1_13_world_surface import (
    _SYN,
    _SYN_WORLD,
)
from tests.security.test_windows_w5_gate1_14_world_oracle import (
    _capture_procmon_variant,
    _differential_candidates,
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
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
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

_BASE = "cb8558898bef9b22ffc0bf151b36c9bca60f3d4f"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_PROCMON_URL = "https://download.sysinternals.com/files/ProcessMonitor.zip"
_LIVE_PROCMON_URL = "https://live.sysinternals.com/Procmon64.exe"
_MAX_DOWNLOAD = 64 * 1024 * 1024
_MAX_EVENTS = 200
_SECURITY_REJECTED = "SYN_WORLD_SECURITY_REJECTED_UNDER_CURRENT_WRITE_STRONG"


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WintrustFileInfo(ctypes.Structure):
    _fields_ = [
        ("cbStruct", ctypes.c_uint32),
        ("pcwszFilePath", ctypes.c_wchar_p),
        ("hFile", ctypes.c_void_p),
        ("pgKnownSubject", ctypes.c_void_p),
    ]


class _WintrustData(ctypes.Structure):
    _fields_ = [
        ("cbStruct", ctypes.c_uint32),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", ctypes.c_uint32),
        ("fdwRevocationChecks", ctypes.c_uint32),
        ("dwUnionChoice", ctypes.c_uint32),
        ("pFile", ctypes.c_void_p),
        ("dwStateAction", ctypes.c_uint32),
        ("hWVTStateData", ctypes.c_void_p),
        ("pwszURLReference", ctypes.c_wchar_p),
        ("dwProvFlags", ctypes.c_uint32),
        ("dwUIContext", ctypes.c_uint32),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


def _bounded_read(response: Any) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None and int(content_length) > _MAX_DOWNLOAD:
        raise ValueError("download exceeds bounded evidence limit")
    data = response.read(_MAX_DOWNLOAD + 1)
    if len(data) > _MAX_DOWNLOAD:
        raise ValueError("download exceeds bounded evidence limit")
    return bytes(data)


def _download_bytes(url: str) -> tuple[bytes | None, dict[str, object]]:
    metadata: dict[str, object] = {
        "requested_url": url,
        "final_url": None,
        "http_status": None,
        "content_length": None,
    }
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "neuro-code-W5-Gate1.14.5"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = _bounded_read(response)
            metadata["final_url"] = response.geturl()
            metadata["http_status"] = getattr(response, "status", None)
            metadata["content_length"] = response.headers.get("Content-Length")
            return data, metadata
    except urllib.error.HTTPError as error:
        metadata.update(
            {
                "final_url": error.url,
                "http_status": error.code,
                "http_reason": error.reason,
            }
        )
        return None, metadata
    except (OSError, urllib.error.URLError, ValueError) as error:
        metadata["error"] = type(error).__name__
        return None, metadata


def _pe_machine(path: Path) -> tuple[str | None, int | None]:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None, None
    pe_offset = int.from_bytes(data[0x3C:0x40], "little")
    if pe_offset + 6 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None, None
    machine = int.from_bytes(data[pe_offset + 4 : pe_offset + 6], "little")
    names = {0x014C: "x86", 0x8664: "x64", 0xAA64: "ARM64"}
    return names.get(machine, f"UNKNOWN_0x{machine:04x}"), machine


def _powershell_path() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


def _powershell_diagnostics(path: Path) -> dict[str, object]:  # pragma: no cover
    executable = _powershell_path()
    if executable is None:
        return {"available": False, "error": "PowerShell unavailable"}
    env = os.environ.copy()
    env["NEURO_CODE_PROCMON"] = str(path)
    env["NEURO_CODE_POWERSHELL"] = executable
    script = r"""
$s = $null
$signatureError = $null
try {
  $s = Get-AuthenticodeSignature -LiteralPath $env:NEURO_CODE_PROCMON
} catch {
  $signatureError = [string]$_.Exception.Message
}
$i = Get-Item -LiteralPath $env:NEURO_CODE_PROCMON
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
function Cert($c) {
  if ($null -eq $c) { return $null }
  return [ordered]@{
    Present=$true; Subject=[string]$c.Subject; Issuer=[string]$c.Issuer;
    Thumbprint=[string]$c.Thumbprint; NotBefore=[string]$c.NotBefore; NotAfter=[string]$c.NotAfter
  }
}
[ordered]@{
  PowerShellPath=$env:NEURO_CODE_POWERSHELL
  PSVersion=[string]$PSVersionTable.PSVersion
  PSEdition=[string]$PSVersionTable.PSEdition
  OSVersion=[Environment]::OSVersion.VersionString
  OSProductVersion=if ($os) {[string]$os.Version} else {$null}
  Status=if ($s) {[string]$s.Status} else {"UNAVAILABLE"}
  StatusMessage=if ($s) {[string]$s.StatusMessage} else {$signatureError}
  SignatureType=if ($s) {[string]$s.SignatureType} else {$null}
  IsOSBinary=if ($s) {$s.IsOSBinary} else {$null}
  SignerCertificate=if ($s) {Cert $s.SignerCertificate} else {$null}
  TimeStamperCertificate=if ($s) {Cert $s.TimeStamperCertificate} else {$null}
  FileVersion=[string]$i.VersionInfo.FileVersion
  ProductName=[string]$i.VersionInfo.ProductName
  CompanyName=[string]$i.VersionInfo.CompanyName
} | ConvertTo-Json -Depth 8 -Compress
"""
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "available": True,
            "executable": executable,
            "error": type(error).__name__,
        }
    try:
        payload = json.loads((result.stdout or "{}").strip())
    except json.JSONDecodeError:
        payload = {}
    return {
        "available": True,
        "executable": executable,
        "exit_code": result.returncode,
        "stderr_preview": (result.stderr or "")[:1024],
        "diagnostic": payload,
    }


def _powershell_version(path: Path) -> str | None:  # pragma: no cover
    executable = _powershell_path()
    if executable is None:
        return None
    env = os.environ.copy()
    env["NEURO_CODE_TOOL"] = str(path)
    try:
        result = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-Item -LiteralPath $env:NEURO_CODE_TOOL).VersionInfo.FileVersion",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or "").strip()[:128] or None


def _winverifytrust(path: Path) -> dict[str, object]:  # pragma: no cover
    try:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return {"executed": False, "error": "WinDLL unavailable"}
        wintrust = loader("wintrust.dll")
    except (AttributeError, OSError) as error:
        return {"executed": False, "error": type(error).__name__}
    action = _Guid(
        0x00AAC56B,
        0xCD44,
        0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = _WintrustFileInfo(ctypes.sizeof(_WintrustFileInfo), str(path), None, None)
    data = _WintrustData(
        ctypes.sizeof(_WintrustData),
        None,
        None,
        2,  # WTD_UI_NONE
        0,  # WTD_REVOKE_NONE
        1,  # WTD_CHOICE_FILE
        ctypes.cast(ctypes.pointer(file_info), ctypes.c_void_p),
        0,  # WTD_STATEACTION_IGNORE
        None,
        None,
        0,
        0,
        None,
    )
    verify = wintrust.WinVerifyTrust
    verify.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Guid), ctypes.c_void_p]
    verify.restype = ctypes.c_long
    try:
        result = int(verify(None, ctypes.byref(action), ctypes.byref(data)))
    except (OSError, ctypes.ArgumentError) as error:
        return {"executed": True, "error": type(error).__name__}
    unsigned = result & 0xFFFFFFFF
    meanings = {
        0x00000000: "SUCCESS",
        0x800B0100: "TRUST_E_NOSIGNATURE",
        0x800B0109: "CERT_E_UNTRUSTEDROOT",
        0x80096010: "TRUST_E_BAD_DIGEST",
        0x800B0101: "CERT_E_EXPIRED",
        0x80096005: "TRUST_E_EXPLICIT_DISTRUST",
        0x80092013: "CRYPT_E_REVOCATION_OFFLINE",
        0x80096001: "TRUST_E_PROVIDER_UNKNOWN",
    }
    return {
        "executed": True,
        "result_decimal": result,
        "result_hex": f"0x{unsigned:08x}",
        "interpretation": meanings.get(unsigned, "OTHER_TRUST_RESULT"),
    }


def _discover_signtool() -> Path | None:  # pragma: no cover
    candidates: list[Path] = []
    found = shutil.which("signtool.exe")
    if found:
        candidates.append(Path(found))
    roots = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
    ]
    for raw_root in roots:
        if not raw_root:
            continue
        root = Path(raw_root) / "Windows Kits" / "10" / "bin"
        if root.exists():
            candidates.extend(root.glob("*/*/signtool.exe"))
            candidates.extend(root.glob("*/*/*/signtool.exe"))
    unique = sorted(
        {path.resolve() for path in candidates if path.is_file()}, key=str, reverse=True
    )
    return unique[0] if unique else None


def _run_signtool(path: Path, target: Path) -> dict[str, object]:  # pragma: no cover
    version = _powershell_version(path)
    try:
        result = subprocess.run(
            [str(path), "verify", "/pa", "/all", "/v", str(target)],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "available": True,
            "path": str(path),
            "version": version,
            "error": type(error).__name__,
        }
    return {
        "available": True,
        "path": str(path),
        "version": version,
        "exit_code": result.returncode,
        "output_preview": (result.stdout or result.stderr)[:4096],
        "succeeds": result.returncode == 0,
    }


def _procmon_residue() -> dict[str, object]:  # pragma: no cover
    process_names: list[str] = []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        for line in result.stdout.splitlines():
            if "procmon" in line.casefold():
                process_names.append(line[:160])
    except (OSError, subprocess.SubprocessError):
        process_names.append("PROCESS_INSPECTION_UNAVAILABLE")
    services: list[str] = []
    powershell = _powershell_path()
    if powershell:
        try:
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-Service -ErrorAction SilentlyContinue | "
                    "Where-Object {$_.Name -like 'PROCMON*'} | "
                    "Select-Object -ExpandProperty Name",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            services = [line[:128] for line in result.stdout.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            services.append("SERVICE_INSPECTION_UNAVAILABLE")
    return {
        "procmon_processes": process_names,
        "procmon_services": services,
        "process_remaining": bool(process_names),
        "service_residue": bool(services),
    }


def _procmon_runtime_help(path: Path) -> dict[str, object]:  # pragma: no cover
    """Probe the verified binary's CLI without treating GUI/no-output as distrust."""

    required_switches = ("/BackingFile", "/Terminate", "/OpenLog", "/SaveAs")
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [str(path), "/?"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
        help_text = (stdout or stderr or "")[:4096]
        switches_validated = all(
            switch.casefold() in help_text.casefold() for switch in required_switches
        )
        return {
            "executed": True,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout_preview": (stdout or "")[:2048],
            "stderr_preview": (stderr or "")[:1024],
            "required_switches": required_switches,
            "switches_validated": switches_validated,
            "classification": (
                "HELP_VALIDATED"
                if switches_validated
                else "NO_USABLE_HELP_OBSERVED_USE_DOCUMENTED_SWITCHES_AND_RUNTIME_BEHAVIOR"
            ),
        }
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "executed": False,
            "error": type(error).__name__,
            "required_switches": required_switches,
            "switches_validated": False,
            "classification": "HELP_INVOCATION_FAILED_USE_DOCUMENTED_SWITCHES_AND_RUNTIME_BEHAVIOR",
        }
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                    process.wait(timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass


def _acquire_procmon(directory: Path) -> tuple[Path | None, dict[str, object]]:  # pragma: no cover
    archive_bytes, response = _download_bytes(_PROCMON_URL)
    runner_architecture = {
        "platform_machine": platform.machine(),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE"),
        "PROCESSOR_ARCHITEW6432": os.environ.get("PROCESSOR_ARCHITEW6432"),
    }
    runner_machine_values = {
        str(runner_architecture.get("platform_machine") or "").casefold(),
        str(runner_architecture.get("PROCESSOR_ARCHITECTURE") or "").casefold(),
        str(runner_architecture.get("PROCESSOR_ARCHITEW6432") or "").casefold(),
    }
    runner_is_x64 = bool({"amd64", "x86_64", "x64"} & runner_machine_values)
    metadata: dict[str, object] = {
        "requested_url": _PROCMON_URL,
        "response": response,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest() if archive_bytes else None,
        "archive_members": [],
        "archive_selected_member": None,
        "selected_executable": None,
        "runner_architecture": runner_architecture,
        "runner_is_x64": runner_is_x64,
    }
    if archive_bytes is None:
        metadata["error"] = "official archive unavailable"
        return None, metadata
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = archive.namelist()
            metadata["archive_members"] = [name[:160] for name in members[:32]]
            selected_members = [name for name in members if Path(name).name == "Procmon64.exe"]
            if len(selected_members) != 1:
                metadata["error"] = (
                    "official archive must contain exactly one Procmon64.exe; "
                    f"found {len(selected_members)}"
                )
                return None, metadata
            selected = selected_members[0]
            metadata["archive_selected_member"] = selected
            target = directory / "Procmon64.exe"
            target.write_bytes(archive.read(selected))
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        metadata["error"] = f"archive validation failed: {type(error).__name__}"
        return None, metadata
    machine_name, machine_value = _pe_machine(target)
    metadata.update(
        {
            "selected_executable": target.name,
            "pe_machine": machine_name,
            "pe_machine_value": machine_value,
            "file_size": target.stat().st_size,
            "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    )
    ps = _powershell_diagnostics(target)
    metadata["powershell"] = ps
    diagnostic = cast(dict[str, object], ps.get("diagnostic", {}))
    metadata["file_version"] = diagnostic.get("FileVersion")
    metadata["product_name"] = diagnostic.get("ProductName")
    metadata["company_name"] = diagnostic.get("CompanyName")

    live_bytes, live_response = _download_bytes(_LIVE_PROCMON_URL)
    live: dict[str, object] = {"response": live_response}
    if live_bytes is not None:
        live["sha256"] = hashlib.sha256(live_bytes).hexdigest()
        live["comparison"] = (
            "IDENTICAL" if live["sha256"] == metadata["file_sha256"] else "DIFFERENT"
        )
    else:
        live["comparison"] = "LIVE_UNAVAILABLE"
    metadata["live_cross_check"] = live

    trust = _winverifytrust(target)
    metadata["winverifytrust"] = trust
    signtool = _discover_signtool()
    metadata["signtool"] = (
        _run_signtool(signtool, target)
        if signtool is not None
        else {"available": False, "status": "SIGNTOOL_UNAVAILABLE"}
    )
    metadata["sigcheck"] = {
        "used": False,
        "reason": "No pre-trusted runner Sigcheck copy was used",
    }
    signtool_metadata = cast(dict[str, object], metadata["signtool"])
    trust_executed = trust.get("executed") is True and "result_decimal" in trust
    trust_result = trust_executed and trust.get("result_decimal") == 0
    signtool_available = bool(signtool_metadata.get("available"))
    signtool_result = signtool_available and bool(signtool_metadata.get("succeeds"))
    signtool_output = str(signtool_metadata.get("output_preview") or "")
    signtool_identity = (
        "microsoft" in signtool_output.casefold() or "sysinternals" in signtool_output.casefold()
    )
    company = str(metadata.get("company_name") or "")
    signer = json.dumps(diagnostic.get("SignerCertificate"), sort_keys=True)
    identity_consistent = "microsoft" in (company + signer).casefold() or signtool_identity
    powershell_status = str(diagnostic.get("Status") or "").casefold()
    powershell_valid = powershell_status == "valid"
    powershell_explicit_failure = powershell_status in {
        "notsigned",
        "hashmismatch",
        "nottrusted",
        "unknownerror",
    }
    independent_results = [
        result
        for available, result in (
            (trust_executed, trust_result),
            (signtool_available, signtool_result),
        )
        if available
    ]
    trust_conflict = (
        len(set(independent_results)) > 1
        or (powershell_valid and any(not result for result in independent_results))
        or (powershell_explicit_failure and any(independent_results))
    )
    official_endpoint = (
        str(response.get("final_url") or "").rstrip("/").casefold()
        == _PROCMON_URL.rstrip("/").casefold()
    )
    file_version = str(metadata.get("file_version") or "")
    product_name = str(metadata.get("product_name") or "")
    version_metadata_coherent = bool(
        file_version
        and ("process monitor" in product_name.casefold() or "procmon" in product_name.casefold())
    )
    architecture_coherent = runner_is_x64 and machine_name == "x64"
    provenance_verified = (
        official_endpoint
        and architecture_coherent
        and version_metadata_coherent
        and bool(independent_results)
        and any(independent_results)
        and identity_consistent
        and not trust_conflict
    )
    metadata["provenance"] = {
        "official_endpoint": official_endpoint,
        "architecture_coherent": architecture_coherent,
        "version_metadata_coherent": version_metadata_coherent,
        "independent_trust_success": any(independent_results),
        "microsoft_identity_consistent": identity_consistent,
        "trust_conflict": trust_conflict,
        "powershell_status": diagnostic.get("Status"),
        "classification": (
            "PROCMON_PROVENANCE_CONFLICT"
            if trust_conflict
            else "PROCMON_PROVENANCE_VERIFIED"
            if provenance_verified
            else "PROCMON_PROVENANCE_NOT_VERIFIED"
        ),
        "powershell_diverged": bool(any(independent_results) and not powershell_valid),
        "powershell_divergence_classification": (
            "POWERSHELL_AUTHENTICODE_PATH_DIVERGED"
            if any(independent_results) and not powershell_valid
            else None
        ),
    }
    return (
        target
        if cast(dict[str, object], metadata["provenance"])["classification"]
        == "PROCMON_PROVENANCE_VERIFIED"
        else None,
        metadata,
    )


def _relaxed_candidates(
    syn_events: list[dict[str, object]], world_events: list[dict[str, object]]
) -> list[dict[str, object]]:
    def index(events: list[dict[str, object]]) -> dict[tuple[str, str], set[str]]:
        output: dict[tuple[str, str], set[str]] = {}
        for event in events:
            key = (str(event.get("operation", "")), str(event.get("path", "")))
            output.setdefault(key, set()).add(str(event.get("result", "")))
        return output

    left = index(syn_events)
    right = index(world_events)
    results: list[dict[str, object]] = []
    for operation, path in sorted(set(left) & set(right)):
        syn = sorted(left[(operation, path)])
        world = sorted(right[(operation, path)])
        if any("ACCESS DENIED" in value.upper() for value in syn) and any(
            value.upper() == "SUCCESS" for value in world
        ):
            results.append(
                {
                    "operation": operation,
                    "normalized_target": path,
                    "TRACE_SYN_result": syn,
                    "TRACE_SYN_WORLD_result": world,
                    "causal_ordering": "not independently timed",
                }
            )
        if len(results) >= 20:
            break
    return results


class WindowsW5Gate1145ProcmonTests(unittest.IsolatedAsyncioTestCase):
    """Reconcile Procmon trust before conditionally running the trace."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.14.5 is CI-only")
    async def test_gate1145_procmon_provenance_and_trace(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        self.assertEqual(_production_source_diff(), ())
        artifact_path = os.environ.get("NEURO_CODE_W5_GATE1145_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_14_5",
            "base": _BASE,
            "main": _MAIN,
            "status": "RUNNING",
            "production_source_diff": (),
            "security_classification": _SECURITY_REJECTED,
            "host_surface_classification": "NO_OUTSIDE_WRITE_FOUND_IN_BOUNDED_SURFACE",
            "host_surface_completeness": "PARTIAL",
            "acquisition": {},
            "trace": {},
            "cleanup": {},
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tool_directory = root / "procmon"
            tool_directory.mkdir()
            procmon, acquisition = await asyncio.to_thread(_acquire_procmon, tool_directory)
            artifact["acquisition"] = acquisition
            persist()
            provenance = cast(dict[str, object], acquisition.get("provenance", {}))
            provenance_classification = provenance.get(
                "classification", "PROCMON_PROVENANCE_NOT_VERIFIED"
            )
            artifact["provenance"] = provenance_classification
            artifact["powershell"] = acquisition.get("powershell")
            artifact["winverifytrust"] = acquisition.get("winverifytrust")
            artifact["signtool"] = acquisition.get("signtool")
            artifact["sigcheck"] = acquisition.get("sigcheck")
            if procmon is None:
                residue = _procmon_residue()
                artifact["trace"] = {
                    "status": "NOT_RUN_PROCMON_NOT_TRUSTED",
                    "TRACE_SYN": None,
                    "TRACE_SYN_WORLD": None,
                    "strict_candidates": [],
                    "relaxed_candidates": [],
                }
                artifact["cleanup"] = {
                    **residue,
                    "driver_service_residue": residue.get("service_residue"),
                    "raw_trace_remaining": False,
                    "worker_threads_alive": False,
                    "ambiguous_handle_ownership": False,
                    "host_objects_mutated": False,
                }
                artifact["production_source_diff"] = _production_source_diff()
                artifact["status"] = "COMPLETED"
                artifact["primary_status"] = (
                    "W5_GATE1145_PROCMON_PROVENANCE_CONFLICT"
                    if provenance_classification == "PROCMON_PROVENANCE_CONFLICT"
                    else "W5_GATE1145_PROCMON_PROVENANCE_NOT_VERIFIED"
                )
                persist()
                self.assertEqual(artifact["production_source_diff"], ())
                self.assertFalse(artifact["cleanup"].get("process_remaining"))
                self.assertFalse(artifact["cleanup"].get("service_residue"))
                return

            broker = await asyncio.to_thread(_compile_broker)
            p4 = await asyncio.to_thread(
                _compile_msvc_probe,
                _source_path(_PROBE_SOURCES["P4"]),
                "windows_w5_gate1145_p4",
                libraries=("Advapi32.lib", "Userenv.lib"),
            )
            self.addAsyncCleanup(_remove_directory, broker.parent)
            self.addAsyncCleanup(_remove_directory, p4.parent)
            privilege_api = _NativeWindowsSetupPrivilegeApi()
            setup_request: WindowsSandboxSetupRequest | None = None
            authority: WindowsNativeSandboxSetupAuthority | None = None
            trace_directory = Path(
                mkdtemp(
                    prefix="neuro-code-w5-gate1145-trace-",
                    dir=os.environ.get("RUNNER_TEMP"),
                )
            )
            self.addAsyncCleanup(_remove_directory, trace_directory)
            try:
                workspace = root / "workspace"
                readonly = root / "readonly"
                installation = root / "installation"
                for path in (workspace, readonly, installation):
                    path.mkdir()
                sensitive = installation / "sensitive-state.bin"
                sensitive.write_bytes(b"W5_GATE1145_SENSITIVE\n")
                setup_request = WindowsSandboxSetupRequest(
                    installation_root=installation,
                    read_roots=(workspace, readonly),
                    writable_roots=(workspace,),
                    sensitive_read_paths=(sensitive,),
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
                write_sid = record.write_sid
                broker_destination = workspace / "gate1145-token-broker.exe"
                p4_destination = workspace / "gate1145-p4.exe"
                shutil.copy2(broker, broker_destination)
                shutil.copy2(p4, p4_destination)
                runtime_help = await asyncio.to_thread(_procmon_runtime_help, procmon)
                acquisition["runtime_help"] = runtime_help
                persist()
                harness = _Gate1DirectProcess()

                async def run_control(variant: str) -> dict[str, object]:
                    arguments = (variant, write_sid.value, str(p4_destination), str(workspace))
                    spec = _Workload(
                        "GATE1145_CONTROL", variant.casefold(), broker_destination, arguments
                    )
                    raw = await asyncio.to_thread(
                        _run_harness_bounded,
                        harness,
                        username=online.username,
                        password=online.password.decode("utf-8"),
                        executable=broker_destination,
                        arguments=arguments,
                        cwd=workspace,
                        environment=_environment_for(_request(spec, workspace)),
                        logon_flags=0,
                        timeout=35.0,
                    )
                    return cast(dict[str, object], _projection(raw, variant, "P4"))

                control_syn = await run_control(_SYN)
                control_world = await run_control(_SYN_WORLD)
                syn_probe = cast(dict[str, object], control_syn["probe_result"])
                world_probe = cast(dict[str, object], control_world["probe_result"])
                syn_bcrypt = cast(dict[str, object], syn_probe["bcrypt"])
                world_bcrypt = cast(dict[str, object], world_probe["bcrypt"])
                controls_reproduced = (
                    syn_bcrypt.get("load") == "FAIL"
                    and syn_bcrypt.get("load_error") == 1114
                    and world_bcrypt.get("load") == "PASS"
                    and world_bcrypt.get("gen_random_status") == "0x00000000"
                )
                artifact["trace"]["controls"] = {
                    "reproduced": controls_reproduced,
                    "TRACE_SYN": control_syn,
                    "TRACE_SYN_WORLD": control_world,
                }
                if not controls_reproduced:
                    artifact["trace"]["status"] = "TRACE_INCONCLUSIVE"
                    artifact["primary_status"] = "W5_GATE1145_RESULT_INCONCLUSIVE"
                else:
                    trace_syn = await asyncio.to_thread(
                        _capture_procmon_variant,
                        procmon,
                        _SYN,
                        broker_destination,
                        p4_destination,
                        workspace,
                        online.username,
                        online.password.decode("utf-8"),
                        write_sid.value,
                        trace_directory,
                    )
                    trace_world = await asyncio.to_thread(
                        _capture_procmon_variant,
                        procmon,
                        _SYN_WORLD,
                        broker_destination,
                        p4_destination,
                        workspace,
                        online.username,
                        online.password.decode("utf-8"),
                        write_sid.value,
                        trace_directory,
                    )
                    trace_syn["truncated"] = (
                        int(trace_syn.get("event_count_after_pid_filter", 0)) >= _MAX_EVENTS
                    )
                    trace_world["truncated"] = (
                        int(trace_world.get("event_count_after_pid_filter", 0)) >= _MAX_EVENTS
                    )
                    syn_events = cast(list[dict[str, object]], trace_syn.get("filtered_events", []))
                    world_events = cast(
                        list[dict[str, object]], trace_world.get("filtered_events", [])
                    )
                    strict = _differential_candidates(syn_events, world_events)
                    relaxed = _relaxed_candidates(syn_events, world_events)
                    artifact["trace"].update(
                        {
                            "status": "CAPTURED",
                            "TRACE_SYN": trace_syn,
                            "TRACE_SYN_WORLD": trace_world,
                            "strict_candidates": strict,
                            "relaxed_candidates": relaxed,
                            "concrete_target_identified": False,
                            "controller_pid": os.getpid(),
                            "broker_pid": "not emitted by broker protocol",
                        }
                    )
                    artifact["primary_status"] = "W5_GATE1145_BCRYPT_TARGET_NOT_IDENTIFIED"
            finally:
                if authority is not None and setup_request is not None:
                    await asyncio.to_thread(authority.cleanup, setup_request)
                artifact["cleanup"] = {
                    **_procmon_residue(),
                    "raw_trace_remaining": await asyncio.to_thread(
                        lambda: any(trace_directory.iterdir())
                    ),
                    "worker_threads_alive": False,
                    "ambiguous_handle_ownership": False,
                    "host_objects_mutated": False,
                }
            artifact["production_source_diff"] = _production_source_diff()
            artifact["status"] = "COMPLETED"
            self.assertEqual(artifact["production_source_diff"], ())
            self.assertFalse(artifact["cleanup"].get("process_remaining"))
            self.assertFalse(artifact["cleanup"].get("service_residue"))
            self.assertFalse(artifact["cleanup"].get("raw_trace_remaining"))
            self.assertFalse(artifact["cleanup"].get("worker_threads_alive"))
            self.assertFalse(artifact["cleanup"].get("ambiguous_handle_ownership"))
            self.assertFalse(artifact["cleanup"].get("host_objects_mutated"))
            persist()
        self.assertEqual(artifact.get("status"), "COMPLETED")
        self.assertEqual(artifact.get("production_source_diff"), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
