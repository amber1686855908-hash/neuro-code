"""W5 Gate 1.14 exact World oracle and Procmon bcrypt differential evidence.

This gate is intentionally evidence-only.  It builds disposable files with
protected, explicit DACLs, evaluates them with ``AccessCheck``, and attempts
two bounded Process Monitor captures.  It never changes the production token,
ACL, firewall, runner, or Windows sandbox implementation.
"""

from __future__ import annotations

import asyncio
import csv
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import time
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
    _BUILTIN_USERS,
    _SYN,
    _SYN_WORLD,
    _WORLD,
    _AccessCheckOracle,
    _canonical,
    _environment_roots,
    _object_fingerprint,
    _scan_roots,
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
    WRITE_ACCESS_MASK,
    WRITE_ONLY_ACCESS_MASK,
    _AceHeader,
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

_BASE = "2781aa39f9f52f4963a068ca75721fb99cf2d88e"
_MAXIMUM_ALLOWED = 0x02000000
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_PROCMON_URL = "https://download.sysinternals.com/files/ProcessMonitor.zip"
_MAX_FILTERED_EVENTS = 200


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


def _error_text(operation: str, code: int | None = None) -> str:
    return f"{operation} failed with Windows error {code or 0}"


def _set_protected_acl(
    api: _NativeWindowsAclApi,
    path: Path,
    entries: tuple[tuple[object, int], ...],
) -> None:  # pragma: no cover - Windows native CI
    """Replace a disposable file DACL with an explicit protected ACL."""

    buffer = ctypes.create_string_buffer(4096)
    if not api._initialize_acl(buffer, ctypes.sizeof(buffer), api._ACL_REVISION):
        raise RuntimeError(_error_text("InitializeAcl"))
    sid_pointers: list[int] = []
    try:
        for sid, access_mask in entries:
            pointer = api._sid_pointer(cast(Any, sid))
            sid_pointers.append(pointer)
            if not api._add_allowed_ace(
                ctypes.cast(buffer, ctypes.c_void_p),
                api._ACL_REVISION,
                0,
                access_mask,
                pointer,
            ):
                raise RuntimeError(_error_text("AddAccessAllowedAceEx"))
        flags = api._DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        result = api._set_named_security_info(
            str(path),
            api._SE_FILE_OBJECT,
            flags,
            None,
            None,
            ctypes.cast(buffer, ctypes.c_void_p),
            None,
        )
        if result != 0:
            raise RuntimeError(_error_text("SetNamedSecurityInfoW", cast(int, result)))
    finally:
        for pointer in sid_pointers:
            api._local_free(pointer)


class _ExactDaclAttestor:
    """Read protected DACL metadata without changing the object."""

    def __init__(self, api: _NativeWindowsAclApi) -> None:  # pragma: no cover
        self._api = api
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise RuntimeError("Win32 ctypes is unavailable")
        advapi = loader("advapi32.dll", use_last_error=True)
        self._get_control = advapi.GetSecurityDescriptorControl
        self._get_control.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._get_control.restype = ctypes.c_int32

    def attest(
        self,
        path: Path,
        expected: tuple[tuple[str, int, int, int], ...],
        known_sids: dict[str, str],
    ) -> dict[str, object]:  # pragma: no cover - Windows native CI
        owner = ctypes.c_void_p()
        group = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        sacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        info = self._api._DACL_SECURITY_INFORMATION | 0x00000001 | 0x00000002
        result = self._api._get_named_security_info(
            str(path),
            self._api._SE_FILE_OBJECT,
            info,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(descriptor),
        )
        if result != 0 or not descriptor.value or not dacl.value:
            return {
                "protected_dacl": False,
                "unexpected_inherited_ace_count": 0,
                "semantic_ace_set": [],
                "expected_semantic_ace_set": _semantic_rows(expected),
                "acl_ready": False,
                "error": cast(int, result or 0),
            }
        try:
            control = ctypes.c_uint16()
            revision = ctypes.c_uint32()
            if not self._get_control(
                descriptor,
                ctypes.byref(control),
                ctypes.byref(revision),
            ):
                return {
                    "protected_dacl": False,
                    "unexpected_inherited_ace_count": 0,
                    "semantic_ace_set": [],
                    "expected_semantic_ace_set": _semantic_rows(expected),
                    "acl_ready": False,
                    "error": "GetSecurityDescriptorControl",
                }
            size = _AclSizeInformation()
            if not self._api._get_acl_information(
                dacl,
                ctypes.byref(size),
                ctypes.sizeof(size),
                self._api._ACL_SIZE_INFORMATION,
            ):
                raise RuntimeError(_error_text("GetAclInformation"))
            ace_count = int(size.AceCount)
            semantic: list[tuple[str, int, int, int]] = []
            inherited = 0
            for index in range(ace_count):
                ace = ctypes.c_void_p()
                if not self._api._get_ace(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise RuntimeError(_error_text("GetAce"))
                header = _AceHeader.from_address(ace.value)
                if header.AceFlags & self._api._INHERITED_ACE:
                    inherited += 1
                sid_role = "AMBIGUOUS"
                if header.AceType in (
                    self._api._ACCESS_ALLOWED_ACE_TYPE,
                    self._api._ACCESS_DENIED_ACE_TYPE,
                ):
                    # ACCESS_ALLOWED/ACCESS_DENIED ACEs contain a 4-byte
                    # header followed by a 4-byte access mask, then the SID.
                    sid_offset = 8
                    sid_length = int(header.AceSize) - sid_offset
                    sid_buffer = ctypes.create_string_buffer(
                        ctypes.string_at(ace.value + sid_offset, sid_length)
                    )
                    try:
                        sid_role = known_sids.get(
                            self._api._sid_string(ctypes.addressof(sid_buffer)),
                            "AMBIGUOUS",
                        )
                    except Exception:
                        sid_role = "AMBIGUOUS"
                mask = int.from_bytes(ctypes.string_at(ace.value + 4, 4), "little")
                semantic.append((sid_role, int(header.AceType), mask, int(header.AceFlags)))
            observed = tuple(sorted(semantic))
            expected_sorted = tuple(sorted(expected))
            protected = bool(control.value & _SE_DACL_PROTECTED)
            return {
                "protected_dacl": protected,
                "unexpected_inherited_ace_count": inherited,
                "semantic_ace_set": _semantic_rows(observed),
                "expected_semantic_ace_set": _semantic_rows(expected_sorted),
                "acl_ready": protected and inherited == 0 and observed == expected_sorted,
                "control": int(control.value),
                "revision": int(revision.value),
            }
        finally:
            self._api._local_free(descriptor)


def _semantic_rows(
    entries: tuple[tuple[str, int, int, int], ...] | list[tuple[str, int, int, int]],
) -> list[dict[str, object]]:
    return [
        {
            "sid_role": role,
            "ace_type": ace_type,
            "mask": mask,
            "ace_flags": ace_flags,
        }
        for role, ace_type, mask, ace_flags in sorted(entries)
    ]


def _expected_entries(*rows: tuple[str, int]) -> tuple[tuple[str, int, int, int], ...]:
    return tuple((role, 0, mask, 0) for role, mask in rows)


def _redact_trace_path(value: str) -> str:
    result = value.replace("/", "\\")
    for label, env_name in (
        ("<TEMP>", "TEMP"),
        ("<USERPROFILE>", "USERPROFILE"),
        ("<PROGRAMDATA>", "PROGRAMDATA"),
        ("<SYSTEMROOT>", "SYSTEMROOT"),
    ):
        raw = os.environ.get(env_name)
        if raw:
            result = result.replace(raw.replace("/", "\\"), label)
    return result[-240:]


def _trace_category(operation: str, path: str) -> str:
    lower = operation.casefold()
    if "registry" in lower or lower.startswith("reg"):
        return "REGISTRY"
    if path.startswith(("\\\\.\\", "\\\\?\\globalroot")):
        return "DEVICE"
    if "named" in lower or "event" in lower or "mutex" in lower or "section" in lower:
        return "NAMED_OBJECT"
    if "file" in lower or "image" in lower or "load" in lower:
        return "FILE"
    return "OTHER"


def _parse_procmon_csv(path: Path, child_pid: int) -> list[dict[str, object]]:
    """Parse only a bounded, PID-filtered Procmon CSV export."""

    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return events
        fields = {field.casefold().strip(): field for field in reader.fieldnames if field}
        pid_field = next(
            (fields[key] for key in ("pid", "process id", "processid") if key in fields),
            None,
        )
        if pid_field is None:
            return events
        for row in reader:
            try:
                pid = int(str(row.get(pid_field, "")).strip())
            except ValueError:
                continue
            if pid != child_pid:
                continue
            operation = str(row.get(fields.get("operation", ""), ""))
            result = str(row.get(fields.get("result", ""), ""))
            object_path = str(row.get(fields.get("path", fields.get("detail", "")), ""))
            detail = str(row.get(fields.get("detail", ""), ""))
            events.append(
                {
                    "order": len(events),
                    "pid": pid,
                    "operation": operation[:120],
                    "path": _redact_trace_path(object_path),
                    "result": result[:120],
                    "detail": detail[:240],
                }
            )
            if len(events) >= _MAX_FILTERED_EVENTS:
                break
    return events


def _differential_candidates(
    syn_events: list[dict[str, object]],
    world_events: list[dict[str, object]],
) -> list[dict[str, object]]:
    def index(events: list[dict[str, object]]) -> dict[tuple[str, str, str], set[str]]:
        output: dict[tuple[str, str, str], set[str]] = {}
        for event in events:
            key = (
                str(event.get("operation", "")),
                str(event.get("path", "")),
                str(event.get("detail", "")),
            )
            output.setdefault(key, set()).add(str(event.get("result", "")))
        return output

    left = index(syn_events)
    right = index(world_events)
    candidates: list[dict[str, object]] = []
    for key in sorted(set(left) & set(right)):
        syn_results = sorted(left[key])
        world_results = sorted(right[key])
        if not any("ACCESS DENIED" in value.upper() for value in syn_results):
            continue
        if not any(value.upper() == "SUCCESS" for value in world_results):
            continue
        operation, path, detail = key
        candidates.append(
            {
                "category": _trace_category(operation, path),
                "normalized_target": path,
                "operation": operation,
                "detail": detail,
                "TRACE_SYN_result": syn_results,
                "TRACE_SYN_WORLD_result": world_results,
                "temporal_distance": None,
                "world_acl_evidence": "NOT_INSPECTED",
            }
        )
        if len(candidates) >= 20:
            break
    return candidates


def _powershell_signature(path: Path) -> dict[str, object]:  # pragma: no cover
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        return {"status": "UNAVAILABLE", "publisher": None, "error": "PowerShell unavailable"}
    env = os.environ.copy()
    env["NEURO_CODE_PROCMON"] = str(path)
    command = (
        "$i=Get-Item -LiteralPath $env:NEURO_CODE_PROCMON; "
        "$s=Get-AuthenticodeSignature -LiteralPath $env:NEURO_CODE_PROCMON; "
        "[pscustomobject]@{Status=$s.Status;Publisher=$s.SignerCertificate.Subject;"
        "FileVersion=$i.VersionInfo.FileVersion}|ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    try:
        payload = json.loads((result.stdout or "{}").strip())
    except json.JSONDecodeError:
        payload = {}
    return {
        "status": str(payload.get("Status", "UNKNOWN")),
        "publisher": str(payload.get("Publisher", ""))[:256],
        "file_version": str(payload.get("FileVersion", ""))[:128],
        "exit": result.returncode,
        "raw_preview": (result.stdout or result.stderr)[:512],
    }


def _download_procmon(directory: Path) -> tuple[Path | None, dict[str, object]]:  # pragma: no cover
    metadata: dict[str, object] = {
        "official_source": _PROCMON_URL,
        "official_source_verified": False,
        "authenticode": None,
        "publisher": None,
        "file_version": None,
        "sha256": None,
        "runtime_help_captured": False,
    }
    zip_path = directory / "Procmon.zip"
    try:
        request = urllib.request.Request(
            _PROCMON_URL,
            headers={"User-Agent": "neuro-code-W5-Gate1.14"},
        )
        with urllib.request.urlopen(request, timeout=60) as response, zip_path.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        with zipfile.ZipFile(zip_path) as archive:
            members = {Path(name).name.casefold(): name for name in archive.namelist()}
            member = members.get("procmon64.exe") or members.get("procmon.exe")
            if member is None:
                metadata["error"] = "official archive did not contain Procmon executable"
                return None, metadata
            archive.extract(member, directory)
            executable = directory / member
            if not executable.is_file():
                metadata["error"] = "extracted Procmon executable missing"
                return None, metadata
        signature = _powershell_signature(executable)
        metadata["authenticode"] = signature.get("status")
        metadata["publisher"] = signature.get("publisher")
        metadata["file_version"] = signature.get("file_version")
        metadata["sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
        publisher = str(signature.get("publisher", ""))
        verified = signature.get("status") == "Valid" and "Microsoft" in publisher
        metadata["official_source_verified"] = bool(verified)
        if not verified:
            metadata["error"] = "Authenticode status or Microsoft publisher validation failed"
            return None, metadata
        try:
            help_process = subprocess.Popen(
                [str(executable), "/?"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = help_process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                help_process.terminate()
                stdout, stderr = help_process.communicate(timeout=10)
                metadata["runtime_help_timeout"] = True
            help_text = stdout or stderr
            metadata["runtime_help_captured"] = bool(help_text)
            metadata["runtime_help_preview"] = help_text[:2048]
            metadata["runtime_help_exit"] = help_process.returncode
            required_switches = ("/BackingFile", "/Terminate", "/OpenLog", "/SaveAs")
            metadata["runtime_switches_validated"] = all(
                switch.casefold() in help_text.casefold() for switch in required_switches
            )
            if not metadata["runtime_switches_validated"]:
                metadata["error"] = "Procmon runtime help did not validate all capture switches"
                return None, metadata
        except (OSError, subprocess.SubprocessError) as error:
            metadata["error"] = f"runtime help failed: {type(error).__name__}"
            return None, metadata
        return executable, metadata
    except urllib.error.HTTPError as error:
        metadata["error"] = "official Procmon acquisition HTTP error"
        metadata["http_status"] = error.code
        metadata["http_reason"] = error.reason
        metadata["http_url"] = error.url
        return None, metadata
    except (
        OSError,
        urllib.error.URLError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as error:
        metadata["error"] = f"official Procmon acquisition failed: {type(error).__name__}"
        return None, metadata


def _capture_procmon_variant(
    executable: Path,
    variant: str,
    broker: Path,
    p4: Path,
    workspace: Path,
    username: str,
    password: str,
    write_sid: str,
    trace_directory: Path,
) -> dict[str, object]:  # pragma: no cover - Windows native CI
    pml = trace_directory / f"{variant.casefold()}.pml"
    csv_path = trace_directory / f"{variant.casefold()}.csv"
    arguments = (variant, write_sid, str(p4), str(workspace))
    spec = _Workload("GATE114_TRACE", variant.casefold(), broker, arguments)
    environment = _environment_for(_request(spec, workspace))
    command = [
        str(executable),
        "/AcceptEula",
        "/Quiet",
        "/Minimized",
        "/NoFilter",
        "/BackingFile",
        str(pml),
    ]
    procmon: subprocess.Popen[str] | None = None
    record: dict[str, object] = {
        "variant": variant,
        "trace_started": False,
        "trace_stopped": False,
        "filtered_events": [],
        "event_count_after_pid_filter": 0,
        "p4": None,
        "procmon_process_left": False,
    }
    try:
        procmon = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(3)
        if procmon.poll() is not None:
            record["start_exit"] = procmon.returncode
        else:
            record["trace_started"] = True
            raw = _run_harness_bounded(
                _Gate1DirectProcess(),
                username=username,
                password=password,
                executable=broker,
                arguments=arguments,
                cwd=workspace,
                environment=environment,
                logon_flags=0,
                timeout=35.0,
            )
            record["p4"] = _projection(raw, variant, "P4")
    except (OSError, subprocess.SubprocessError) as error:
        record["error"] = f"Procmon launch failed: {type(error).__name__}"
    finally:
        try:
            subprocess.run(
                [str(executable), "/Terminate"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            record["trace_stopped"] = True
        except (OSError, subprocess.SubprocessError) as error:
            record["terminate_error"] = type(error).__name__
        if procmon is not None and procmon.poll() is None:
            try:
                procmon.terminate()
                procmon.wait(timeout=20)
            except (OSError, subprocess.SubprocessError):
                try:
                    procmon.kill()
                    procmon.wait(timeout=10)
                except (OSError, subprocess.SubprocessError):
                    record["procmon_process_left"] = True
    child = cast(dict[str, object] | None, record.get("p4"))
    broker_projection = cast(dict[str, object] | None, child.get("broker") if child else None)
    child_pid = broker_projection.get("child_pid") if broker_projection else None
    if pml.is_file() and record.get("trace_stopped"):
        try:
            export = subprocess.run(
                [str(executable), "/OpenLog", str(pml), "/SaveAs", str(csv_path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            record["export_exit"] = export.returncode
            record["export_preview"] = (export.stdout or export.stderr)[:512]
        except (OSError, subprocess.SubprocessError) as error:
            record["export_error"] = type(error).__name__
    if isinstance(child_pid, int) and csv_path.is_file():
        try:
            events = _parse_procmon_csv(csv_path, child_pid)
            record["filtered_events"] = events
            record["event_count_after_pid_filter"] = len(events)
        except (OSError, csv.Error) as error:
            record["csv_error"] = type(error).__name__
    record["pml_created"] = pml.is_file()
    # Raw PML and exported CSV are intentionally not uploaded.
    for path in (pml, csv_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            cleanup_errors = record.setdefault("cleanup_errors", [])
            if isinstance(cleanup_errors, list):
                cleanup_errors.append(path.name)
    return record


class WindowsW5Gate114WorldOracleTests(unittest.IsolatedAsyncioTestCase):
    """Verify exact World ACL semantics and attempt bounded Procmon traces."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.14 is CI-only")
    async def test_gate114_world_oracle_and_procmon(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        self.assertEqual(_production_source_diff(), ())
        artifact_path = os.environ.get("NEURO_CODE_W5_GATE114_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_14",
            "base": _BASE,
            "status": "RUNNING",
            "production_source_diff": (),
            "fixture_reconciliation": {},
            "world_double_pass": {},
            "security_candidate_classification": None,
            "host_surface_classification": None,
            "bcrypt_trace_classification": None,
            "procmon": {},
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist()
        broker = await asyncio.to_thread(_compile_broker)
        p4 = await asyncio.to_thread(
            _compile_msvc_probe,
            _source_path(_PROBE_SOURCES["P4"]),
            "windows_w5_gate114_p4",
            libraries=("Advapi32.lib", "Userenv.lib"),
        )
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, p4.parent)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            readonly = root / "readonly"
            installation = root / "installation"
            outside = root / "outside"
            for path in (workspace, readonly, installation, outside):
                path.mkdir()
            sensitive = installation / "sensitive-state.bin"
            sensitive.write_bytes(b"W5_GATE114_SENSITIVE\n")
            readonly_file = readonly / "readonly.bin"
            readonly_file.write_bytes(b"W5_GATE114_READONLY\n")
            installation_file = installation / "installation.bin"
            installation_file.write_bytes(b"W5_GATE114_INSTALLATION\n")
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
            write_sid = record.write_sid
            acl_api = _NativeWindowsAclApi()
            attestor = _ExactDaclAttestor(acl_api)
            known_sids = {
                online.user_sid.value: "ONLINE_USER",
                write_sid.value: "SYNTHETIC_WRITE",
                _WORLD.value: "WORLD",
                _BUILTIN_USERS.value: "BUILTIN_USERS",
            }
            fixtures: dict[
                str,
                tuple[Path, tuple[tuple[object, int], ...], tuple[tuple[str, int, int, int], ...]],
            ] = {
                "OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS": (
                    outside / "outside-synthetic-with-normal-pass.bin",
                    ((online.user_sid, WRITE_ACCESS_MASK), (write_sid, WRITE_ONLY_ACCESS_MASK)),
                    _expected_entries(
                        ("ONLINE_USER", WRITE_ACCESS_MASK),
                        ("SYNTHETIC_WRITE", WRITE_ONLY_ACCESS_MASK),
                    ),
                ),
                "OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS": (
                    outside / "outside-synthetic-without-normal-pass.bin",
                    ((write_sid, WRITE_ONLY_ACCESS_MASK),),
                    _expected_entries(("SYNTHETIC_WRITE", WRITE_ONLY_ACCESS_MASK)),
                ),
                "OUTSIDE_WORLD_ONLY": (
                    outside / "outside-world-only.bin",
                    ((_WORLD, WRITE_ACCESS_MASK),),
                    _expected_entries(("WORLD", WRITE_ACCESS_MASK)),
                ),
                "OUTSIDE_NO_SECOND_PASS": (
                    outside / "outside-no-second-pass.bin",
                    ((_BUILTIN_USERS, WRITE_ACCESS_MASK),),
                    _expected_entries(("BUILTIN_USERS", WRITE_ACCESS_MASK)),
                ),
            }
            for path, _, _ in fixtures.values():
                path.write_bytes(b"W5_GATE114_FIXTURE\n")
            for path, entries, _ in fixtures.values():
                _set_protected_acl(acl_api, path, entries)

            attestations = {
                name: attestor.attest(path, expected, known_sids)
                for name, (path, _, expected) in fixtures.items()
            }
            artifact["fixture_reconciliation"] = {
                name: {
                    "acl_semantics": {
                        "OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS": "explicit W2 Online user WRITE + synthetic WRITE",
                        "OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS": "explicit synthetic WRITE only",
                        "OUTSIDE_WORLD_ONLY": "explicit World WRITE only",
                        "OUTSIDE_NO_SECOND_PASS": "explicit BUILTIN Users WRITE only",
                    }[name],
                    **attestation,
                }
                for name, attestation in attestations.items()
            }
            persist()
            for attestation in attestations.values():
                self.assertTrue(attestation["protected_dacl"])
                self.assertEqual(attestation["unexpected_inherited_ace_count"], 0)
                self.assertTrue(attestation["acl_ready"])

            audited_objects = {
                str(path): _object_fingerprint(path)
                for path in (
                    *(fixture[0] for fixture in fixtures.values()),
                    installation_file,
                    store.path,
                    readonly_file,
                )
            }
            oracle = _AccessCheckOracle(
                online.username,
                online.password.decode("utf-8"),
                write_sid.value,
            )
            try:
                fixture_results = {
                    name: {variant: oracle.check(variant, path) for variant in (_SYN, _SYN_WORLD)}
                    for name, (path, _, _) in fixtures.items()
                }
                ordinary_results = {
                    name: oracle.check_unrestricted(path) for name, (path, _, _) in fixtures.items()
                }
                world_result = fixture_results["OUTSIDE_WORLD_ONLY"]
                artifact["fixture_reconciliation"]["results"] = fixture_results
                artifact["fixture_reconciliation"]["ordinary_access_check"] = ordinary_results
                artifact["world_double_pass"] = {
                    "dacl_semantics": "protected explicit World WRITE ACE; no synthetic or inherited WRITE ACE",
                    "SYN": world_result[_SYN],
                    "SYN_WORLD": world_result[_SYN_WORLD],
                    "ordinary_access_check": ordinary_results["OUTSIDE_WORLD_ONLY"],
                    "attribution_method": "BY_CONSTRUCTION_EXACT_DACL",
                    "ordinary_pass_attribution": "BY_CONSTRUCTION_EXACT_DACL",
                    "restricted_pass_attribution": "BY_CONSTRUCTION_EXACT_DACL",
                    "principal": "WORLD",
                }
                artifact["security_candidate_classification"] = (
                    "SYN_WORLD_SECURITY_REJECTED_UNDER_CURRENT_WRITE_STRONG"
                )
                persist()
                self.assertEqual(world_result[_SYN]["oracle"], "PASS")
                self.assertFalse(world_result[_SYN]["write_allowed"])
                self.assertEqual(world_result[_SYN_WORLD]["oracle"], "PASS")
                self.assertTrue(world_result[_SYN_WORLD]["write_allowed"])
                self.assertTrue(ordinary_results["OUTSIDE_WORLD_ONLY"]["write_allowed"])
                for name, result in fixture_results.items():
                    for cell in result.values():
                        self.assertEqual(cell["oracle"], "PASS", name)
                self.assertTrue(
                    fixture_results["OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS"][_SYN]["write_allowed"]
                )
                self.assertTrue(
                    fixture_results["OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS"][_SYN_WORLD][
                        "write_allowed"
                    ]
                )
                self.assertFalse(
                    fixture_results["OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS"][_SYN]["write_allowed"]
                )
                self.assertFalse(
                    fixture_results["OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS"][_SYN_WORLD][
                        "write_allowed"
                    ]
                )
                self.assertFalse(
                    fixture_results["OUTSIDE_NO_SECOND_PASS"][_SYN_WORLD]["write_allowed"]
                )

                env_roots, redaction_roots = _environment_roots(online.username)
                audit_roots = list(env_roots)
                for path, label in (
                    (installation, "Installation"),
                    (readonly, "ReadOnlyRoot"),
                    (workspace, "WorkspaceExcluded"),
                ):
                    canonical = _canonical(path)
                    redaction_roots.setdefault(canonical, label)
                    if canonical not in audit_roots:
                        audit_roots.append(canonical)
                surface = _scan_roots(
                    tuple(audit_roots),
                    (_canonical(workspace),),
                    oracle,
                    redaction_roots,
                )
                artifact["host_surface"] = surface
                artifact["host_surface_classification"] = surface["surface_classification"]
                persist()

                broker_destination = workspace / "gate114-token-broker.exe"
                p4_destination = workspace / "gate114-p4.exe"
                shutil.copy2(broker, broker_destination)
                shutil.copy2(p4, p4_destination)
                harness = _Gate1DirectProcess()

                async def run_control(variant: str) -> dict[str, object]:
                    arguments = (variant, write_sid.value, str(p4_destination), str(workspace))
                    spec = _Workload(
                        "GATE114_CONTROL", variant.casefold(), broker_destination, arguments
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
                artifact["control_TRACE_SYN"] = control_syn
                artifact["control_TRACE_SYN_WORLD"] = control_world
                syn_bcrypt = cast(
                    dict[str, object],
                    cast(dict[str, object], control_syn["probe_result"])["bcrypt"],
                )
                world_bcrypt = cast(
                    dict[str, object],
                    cast(dict[str, object], control_world["probe_result"])["bcrypt"],
                )
                controls_reproduced = (
                    syn_bcrypt.get("load") == "FAIL"
                    and syn_bcrypt.get("load_error") == 1114
                    and world_bcrypt.get("load") == "PASS"
                    and world_bcrypt.get("gen_random_status") == "0x00000000"
                )
                trace_directory = Path(
                    mkdtemp(
                        prefix="neuro-code-w5-gate114-procmon-", dir=os.environ.get("RUNNER_TEMP")
                    )
                )
                self.addAsyncCleanup(_remove_directory, trace_directory)
                procmon_executable, procmon_metadata = await asyncio.to_thread(
                    _download_procmon, trace_directory
                )
                procmon_artifact: dict[str, object] = {
                    **procmon_metadata,
                    "TRACE_SYN": control_syn,
                    "TRACE_SYN_WORLD": control_world,
                    "controls_reproduced": controls_reproduced,
                }
                if procmon_executable is None:
                    procmon_artifact["status"] = "PROCMON_TRACE_BLOCKED"
                    artifact["bcrypt_trace_classification"] = "PROCMON_TRACE_BLOCKED"
                    primary_status = "W5_GATE114_PROCMON_TRACE_BLOCKED"
                elif not controls_reproduced:
                    procmon_artifact["status"] = "TRACE_INCONCLUSIVE"
                    artifact["bcrypt_trace_classification"] = "TRACE_INCONCLUSIVE"
                    primary_status = "W5_GATE114_RESULT_INCONCLUSIVE"
                else:
                    trace_syn = await asyncio.to_thread(
                        _capture_procmon_variant,
                        procmon_executable,
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
                        procmon_executable,
                        _SYN_WORLD,
                        broker_destination,
                        p4_destination,
                        workspace,
                        online.username,
                        online.password.decode("utf-8"),
                        write_sid.value,
                        trace_directory,
                    )
                    procmon_artifact["TRACE_SYN"] = trace_syn
                    procmon_artifact["TRACE_SYN_WORLD"] = trace_world
                    candidates = _differential_candidates(
                        cast(list[dict[str, object]], trace_syn.get("filtered_events", [])),
                        cast(list[dict[str, object]], trace_world.get("filtered_events", [])),
                    )
                    procmon_artifact["candidate_differential_operations"] = candidates
                    if candidates:
                        procmon_artifact["status"] = "CANDIDATES_RECORDED_NOT_PROVEN"
                        artifact["bcrypt_trace_classification"] = "BCRYPT_TARGET_NOT_YET_IDENTIFIED"
                        primary_status = "W5_GATE114_BCRYPT_TARGET_NOT_IDENTIFIED"
                    else:
                        procmon_artifact["status"] = "CAPTURED_NO_HIGH_CONFIDENCE_TARGET"
                        artifact["bcrypt_trace_classification"] = "BCRYPT_TARGET_NOT_YET_IDENTIFIED"
                        primary_status = "W5_GATE114_BCRYPT_TARGET_NOT_IDENTIFIED"
                artifact["procmon"] = procmon_artifact
                artifact["production_source_diff"] = _production_source_diff()
                unchanged = {
                    path: _object_fingerprint(Path(path)) == fingerprint
                    for path, fingerprint in audited_objects.items()
                }
                artifact["object_mutation_check"] = {
                    "all_unchanged": all(unchanged.values()),
                    "objects": unchanged,
                }
                artifact["trace_cleanup"] = {
                    "procmon_process_left": any(
                        bool(
                            cast(dict[str, object], procmon_artifact.get(key, {})).get(
                                "procmon_process_left"
                            )
                        )
                        for key in ("TRACE_SYN", "TRACE_SYN_WORLD")
                    ),
                    "ambiguous_handle_ownership": False,
                    "live_worker_threads": False,
                }
                artifact["status"] = "COMPLETED"
                artifact["primary_status"] = primary_status
                persist()
                self.assertEqual(artifact["production_source_diff"], ())
                self.assertTrue(artifact["object_mutation_check"]["all_unchanged"])
                self.assertFalse(artifact["trace_cleanup"]["procmon_process_left"])
            finally:
                oracle.close()
                await asyncio.to_thread(authority.cleanup, setup_request)
        self.assertEqual(artifact.get("status"), "COMPLETED")
        self.assertEqual(artifact.get("production_source_diff"), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
