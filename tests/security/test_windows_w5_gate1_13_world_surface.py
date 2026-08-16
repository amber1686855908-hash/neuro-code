"""W5 Gate 1.13 evidence for World authority and the bcrypt target.

This gate is deliberately evidence-only.  It does not change the Windows
runtime token, ACL, firewall, or runner implementation.  The native access
oracle evaluates a candidate restricted token with ``AccessCheck`` and never
mutates an audited object.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import stat
import subprocess
import unittest
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
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
    _reconcile_file,
    _remove_directory,
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

_BASE = "ab59db6c78d290256e8f2bb241d90bb8c2f45660"
_WORLD = WindowsAccountSid("S-1-1-0")
_BUILTIN_USERS = WindowsAccountSid("S-1-5-32-545")
_SYN = "SYN"
_SYN_WORLD = "SYN_WORLD"
_FLAGS = 0xD
_MAXIMUM_ALLOWED = 0x02000000
_WRITE_LIKE_MASK = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
)
_REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_DEPTH = 3
_MAX_ENTRIES = 2500
_MAX_SAMPLES = 12


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _GenericMapping(ctypes.Structure):
    _fields_ = [
        ("GenericRead", ctypes.c_uint32),
        ("GenericWrite", ctypes.c_uint32),
        ("GenericExecute", ctypes.c_uint32),
        ("GenericAll", ctypes.c_uint32),
    ]


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _LuidAndAttributes(ctypes.Structure):
    _fields_ = [("Luid", _Luid), ("Attributes", ctypes.c_uint32)]


class _PrivilegeSet(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", ctypes.c_uint32),
        ("Control", ctypes.c_uint32),
        ("Privilege", _LuidAndAttributes * 1),
    ]


class _AccessCheckOracle:
    """Create candidate tokens and perform side-effect-free ``AccessCheck``."""

    def __init__(
        self, username: str, password: str, synthetic_sid: str
    ) -> None:  # pragma: no cover
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise RuntimeError("Win32 ctypes is unavailable")
        self._advapi = loader("advapi32.dll", use_last_error=True)
        self._kernel = loader("kernel32.dll", use_last_error=True)
        self._close = self._kernel.CloseHandle
        self._close.argtypes = [ctypes.c_void_p]
        self._close.restype = ctypes.c_int32
        self._local_free = self._kernel.LocalFree
        self._local_free.argtypes = [ctypes.c_void_p]
        self._local_free.restype = ctypes.c_void_p
        self._logon_user = self._advapi.LogonUserW
        self._logon_user.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._logon_user.restype = ctypes.c_int32
        self._convert_sid = self._advapi.ConvertStringSidToSidW
        self._convert_sid.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._convert_sid.restype = ctypes.c_int32
        self._create_restricted = self._advapi.CreateRestrictedToken
        self._create_restricted.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_SidAndAttributes),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._create_restricted.restype = ctypes.c_int32
        self._duplicate = self._advapi.DuplicateTokenEx
        self._duplicate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._duplicate.restype = ctypes.c_int32
        self._get_named = self._advapi.GetNamedSecurityInfoW
        self._get_named.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._get_named.restype = ctypes.c_uint32
        self._access_check = self._advapi.AccessCheck
        self._access_check.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_GenericMapping),
            ctypes.POINTER(_PrivilegeSet),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        self._access_check.restype = ctypes.c_int32
        self._tokens: dict[str, ctypes.c_void_p] = {}
        self._unrestricted: ctypes.c_void_p | None = None
        self._username = username
        self._password = password
        self._synthetic_sid = synthetic_sid

    @staticmethod
    def _last_error() -> int:
        getter = getattr(ctypes, "get_last_error", None)
        return int(getter()) if getter is not None else 0

    def _sid_pointer(self, text: str) -> ctypes.c_void_p:
        pointer = ctypes.c_void_p()
        if not self._convert_sid(text, ctypes.byref(pointer)) or not pointer.value:
            raise OSError(self._last_error(), "ConvertStringSidToSidW")
        return pointer

    def _restricted_token(self, variant: str) -> ctypes.c_void_p:
        source = ctypes.c_void_p()
        if not self._logon_user(
            self._username,
            ".",
            self._password,
            2,
            0,
            ctypes.byref(source),
        ):
            raise OSError(self._last_error(), "LogonUserW")
        synthetic = self._sid_pointer(self._synthetic_sid)
        world = self._sid_pointer(_WORLD.value) if variant == _SYN_WORLD else None
        try:
            sid_count = 1 if world is None else 2
            restricted = (_SidAndAttributes * sid_count)()
            restricted[0].Sid = synthetic
            restricted[0].Attributes = 0
            if world is not None:
                restricted[1].Sid = world
                restricted[1].Attributes = 0
            child = ctypes.c_void_p()
            if (
                not self._create_restricted(
                    source,
                    _FLAGS,
                    0,
                    None,
                    0,
                    None,
                    sid_count,
                    restricted,
                    ctypes.byref(child),
                )
                or not child.value
            ):
                raise OSError(self._last_error(), "CreateRestrictedToken")
            impersonation = ctypes.c_void_p()
            if (
                not self._duplicate(
                    child,
                    0x0008 | 0x0004,
                    None,
                    2,
                    2,
                    ctypes.byref(impersonation),
                )
                or not impersonation.value
            ):
                self._close(child)
                raise OSError(self._last_error(), "DuplicateTokenEx")
            self._close(child)
            return impersonation
        finally:
            self._close(source)
            self._local_free(synthetic)
            if world is not None:
                self._local_free(world)

    def token(self, variant: str) -> ctypes.c_void_p:
        if variant not in self._tokens:
            self._tokens[variant] = self._restricted_token(variant)
        return self._tokens[variant]

    def unrestricted_token(self) -> ctypes.c_void_p:
        if self._unrestricted is not None:
            return self._unrestricted
        source = ctypes.c_void_p()
        if not self._logon_user(
            self._username,
            ".",
            self._password,
            2,
            0,
            ctypes.byref(source),
        ):
            raise OSError(self._last_error(), "LogonUserW")
        impersonation = ctypes.c_void_p()
        try:
            if (
                not self._duplicate(
                    source,
                    0x0008 | 0x0004,
                    None,
                    2,
                    2,
                    ctypes.byref(impersonation),
                )
                or not impersonation.value
            ):
                raise OSError(self._last_error(), "DuplicateTokenEx")
            self._unrestricted = impersonation
            return impersonation
        finally:
            self._close(source)

    def _check_handle(
        self, token: ctypes.c_void_p, path: Path
    ) -> dict[str, object]:  # pragma: no cover
        owner = ctypes.c_void_p()
        group = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        sacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self._get_named(
            str(path),
            1,
            0x00000004,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(descriptor),
        )
        if result != 0 or not descriptor.value:
            return {
                "oracle": "UNAVAILABLE",
                "security_descriptor_error": int(result or self._last_error()),
                "requested_mask": _MAXIMUM_ALLOWED,
                "granted_mask": 0,
                "write_allowed": False,
            }
        try:
            privilege_buffer = ctypes.create_string_buffer(1024)
            privilege_set = ctypes.cast(privilege_buffer, ctypes.POINTER(_PrivilegeSet))
            privilege_size = ctypes.c_uint32(ctypes.sizeof(privilege_buffer))
            mapping = _GenericMapping(
                0x00120089,
                0x00120116,
                0x001200A0,
                0x001F01FF,
            )
            granted = ctypes.c_uint32()
            access_status = ctypes.c_int32()
            ok = self._access_check(
                descriptor,
                token,
                _MAXIMUM_ALLOWED,
                ctypes.byref(mapping),
                privilege_set,
                ctypes.byref(privilege_size),
                ctypes.byref(granted),
                ctypes.byref(access_status),
            )
            if not ok:
                return {
                    "oracle": "UNAVAILABLE",
                    "access_check_error": self._last_error(),
                    "requested_mask": _MAXIMUM_ALLOWED,
                    "granted_mask": int(granted.value),
                    "write_allowed": False,
                }
            granted_mask = int(granted.value)
            return {
                "oracle": "PASS",
                "access_status": bool(access_status.value),
                "requested_mask": _MAXIMUM_ALLOWED,
                "granted_mask": granted_mask,
                "write_granted_mask": granted_mask & _WRITE_LIKE_MASK,
                "write_allowed": bool(granted_mask & _WRITE_LIKE_MASK),
            }
        finally:
            self._local_free(descriptor)

    def check(self, variant: str, path: Path) -> dict[str, object]:  # pragma: no cover
        return self._check_handle(self.token(variant), path)

    def check_unrestricted(self, path: Path) -> dict[str, object]:  # pragma: no cover
        return self._check_handle(self.unrestricted_token(), path)

    def close(self) -> None:
        for token in self._tokens.values():
            self._close(token)
        self._tokens.clear()
        if self._unrestricted is not None:
            self._close(self._unrestricted)
            self._unrestricted = None


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(path: Path, roots: Iterable[Path]) -> bool:
    canonical = _canonical(path)
    return any(canonical == root or root in canonical.parents for root in roots)


def _redacted_path(path: Path, roots: dict[Path, str]) -> str:
    canonical = _canonical(path)
    for root, label in sorted(roots.items(), key=lambda item: len(str(item[0])), reverse=True):
        if canonical == root:
            return f"<{label}>"
        if root in canonical.parents:
            relative = canonical.relative_to(root)
            return f"<{label}>/{str(relative).replace(chr(92), '/')}"
    return "<outside>" + str(canonical).replace(chr(92), "/")[-160:]


def _scan_roots(
    roots: tuple[Path, ...],
    authorized_writable: tuple[Path, ...],
    oracle: _AccessCheckOracle,
    redaction_roots: dict[Path, str],
) -> dict[str, object]:  # pragma: no cover - Windows CI
    entries_examined = 0
    directories_examined = 0
    files_examined = 0
    reparse_skipped = 0
    permission_failures = 0
    acl_failures = 0
    cap_reached = False
    world_writable_count = 0
    samples: list[dict[str, object]] = []
    queue: list[tuple[Path, int]] = [(root, 0) for root in roots]
    seen: set[Path] = set()
    while queue:
        current, depth = queue.pop(0)
        current = _canonical(current)
        if current in seen:
            continue
        seen.add(current)
        try:
            current_stat = current.stat(follow_symlinks=False)
        except OSError:
            permission_failures += 1
            continue
        if int(getattr(current_stat, "st_file_attributes", 0)) & _REPARSE_POINT:
            reparse_skipped += 1
            continue
        if entries_examined >= _MAX_ENTRIES:
            cap_reached = True
            break
        entries_examined += 1
        is_directory = stat.S_ISDIR(current_stat.st_mode)
        if is_directory:
            directories_examined += 1
        else:
            files_examined += 1
        if not _is_within(current, authorized_writable):
            observed = oracle.check(_SYN_WORLD, current)
            if observed.get("oracle") != "PASS":
                acl_failures += 1
            if observed.get("write_allowed"):
                world_writable_count += 1
                sample = {
                    "path": _redacted_path(current, redaction_roots),
                    "granted_mask": observed.get("granted_mask"),
                    "write_granted_mask": observed.get("write_granted_mask"),
                    "object_type": "directory" if is_directory else "file",
                }
                if len(samples) < _MAX_SAMPLES:
                    samples.append(sample)
        if is_directory and depth < _MAX_DEPTH:
            try:
                with os.scandir(current) as iterator:
                    for entry in iterator:
                        if entries_examined + len(queue) >= _MAX_ENTRIES:
                            cap_reached = True
                            break
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            permission_failures += 1
                            continue
                        if int(getattr(entry_stat, "st_file_attributes", 0)) & _REPARSE_POINT:
                            reparse_skipped += 1
                            continue
                        queue.append((Path(entry.path), depth + 1))
            except OSError:
                permission_failures += 1
    return {
        "roots": [_redacted_path(root, redaction_roots) for root in roots],
        "max_depth": _MAX_DEPTH,
        "max_entries": _MAX_ENTRIES,
        "entries_examined": entries_examined,
        "directories_examined": directories_examined,
        "files_examined": files_examined,
        "reparse_points_skipped": reparse_skipped,
        "permission_read_failures": permission_failures,
        "acl_failures": acl_failures,
        "cap_reached": cap_reached,
        "world_writable_outside_count": world_writable_count,
        "world_writable_outside_samples": samples,
        "surface_classification": (
            "REAL_HOST_WORLD_WRITE_SURFACE_FOUND"
            if world_writable_count
            else "NO_OUTSIDE_WRITE_FOUND_IN_BOUNDED_SURFACE"
        ),
        "surface_completeness": "PARTIAL" if cap_reached or permission_failures else "BOUNDED",
    }


def _environment_roots(online_username: str) -> tuple[tuple[Path, ...], dict[Path, str]]:
    values = (
        ("SystemRoot", os.environ.get("SYSTEMROOT")),
        ("ProgramFiles", os.environ.get("PROGRAMFILES")),
        ("ProgramFilesX86", os.environ.get("PROGRAMFILES(X86)")),
        ("ProgramData", os.environ.get("PROGRAMDATA")),
        ("Public", os.environ.get("PUBLIC")),
        ("Temp", os.environ.get("TEMP")),
        ("Tmp", os.environ.get("TMP")),
        ("UserProfile", os.environ.get("USERPROFILE")),
        ("AllUsersProfile", os.environ.get("ALLUSERSPROFILE")),
        ("LocalAppData", os.environ.get("LOCALAPPDATA")),
        ("AppData", os.environ.get("APPDATA")),
    )
    roots: list[Path] = []
    labels: dict[Path, str] = {}
    for label, value in values:
        if not value:
            continue
        path = _canonical(Path(value))
        if path.exists() and path not in labels:
            roots.append(path)
            labels[path] = label
    system_drive = os.environ.get("SYSTEMDRIVE")
    if system_drive:
        profile = _canonical(Path(system_drive) / "Users" / online_username)
        if profile.exists() and profile not in labels:
            roots.append(profile)
            labels[profile] = "SandboxProfile"
    return tuple(roots), labels


def _object_fingerprint(path: Path) -> tuple[int, int]:
    observed = path.stat()
    return int(observed.st_size), int(observed.st_mtime_ns)


def _trace_tool_facts() -> dict[str, object]:  # pragma: no cover - Windows CI
    candidates = [
        shutil.which("procmon.exe"),
        shutil.which("wpr.exe"),
        shutil.which("xperf.exe"),
        shutil.which("logman.exe"),
    ]
    available = next((Path(candidate) for candidate in candidates if candidate), None)
    if available is None:
        return {
            "attempted": True,
            "mechanism": "NONE",
            "status": "UNAVAILABLE",
            "limitation": "No Procmon/WPR/xperf/logman executable was present on the runner",
            "classification": "BCRYPT_TARGET_NOT_YET_IDENTIFIED",
        }
    metadata: dict[str, object] = {
        "attempted": True,
        "mechanism": available.name,
        "path": available.name,
        "status": "DISCOVERED",
    }
    try:
        metadata["sha256"] = hashlib.sha256(available.read_bytes()).hexdigest()
    except OSError as error:
        metadata["sha256"] = None
        metadata["status"] = "HASH_BLOCKED"
        metadata["limitation"] = f"tool hash failed: {type(error).__name__}"
    if available.name.casefold() == "procmon.exe":
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if powershell is None:
            metadata["authenticode"] = "UNAVAILABLE"
            metadata["limitation"] = "PowerShell signature verifier unavailable"
        else:
            env = os.environ.copy()
            env["NEURO_CODE_TRACE_TOOL"] = str(available)
            try:
                signature = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-Command",
                        "$s=Get-AuthenticodeSignature -LiteralPath $env:NEURO_CODE_TRACE_TOOL; "
                        "[pscustomobject]@{Status=$s.Status;Subject=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                    env=env,
                )
                metadata["authenticode"] = (signature.stdout or signature.stderr)[:512]
                metadata["authenticode_exit"] = signature.returncode
            except (OSError, subprocess.SubprocessError) as error:
                metadata["authenticode"] = "ERROR"
                metadata["limitation"] = f"signature verification failed: {type(error).__name__}"
    if available.name.casefold() == "wpr.exe":
        try:
            listed = subprocess.run(
                [str(available), "-profiles"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            metadata["profile_inventory_exit"] = listed.returncode
            metadata["profile_inventory_preview"] = (listed.stdout or listed.stderr)[:512]
            metadata["status"] = "PROFILE_INVENTORY_ONLY"
            metadata["limitation"] = "WPR object-access differential parser is unavailable"
        except (OSError, subprocess.SubprocessError) as error:
            metadata["status"] = "BLOCKED"
            metadata["limitation"] = f"WPR profile inventory failed: {type(error).__name__}"
    else:
        metadata["status"] = "DISCOVERED_NO_OBJECT_TRACE"
        metadata["limitation"] = "No bounded object-access trace was captured"
    metadata["classification"] = "BCRYPT_TARGET_NOT_YET_IDENTIFIED"
    return metadata


class WindowsW5Gate113WorldSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """Bound World access and attempt the minimal bcrypt differential trace."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.13 is CI-only")
    async def test_gate113_world_surface_and_bcrypt_target(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        self.assertEqual(_production_source_diff(), ())

        broker = await asyncio.to_thread(_compile_broker)
        p4 = await asyncio.to_thread(
            _compile_msvc_probe,
            _source_path(_PROBE_SOURCES["P4"]),
            "windows_w5_gate113_p4",
            libraries=("Advapi32.lib", "Userenv.lib"),
        )
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, p4.parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE113_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_13",
            "base": _BASE,
            "production_source_diff": (),
            "status": "RUNNING",
            "fixture_reconciliation": {},
            "world_double_pass": {},
            "host_surface": {},
            "trace": {},
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
            readonly = root / "readonly"
            installation = root / "installation"
            outside = root / "outside"
            for path in (workspace, readonly, installation, outside):
                path.mkdir()
            sensitive = installation / "sensitive-state.bin"
            sensitive.write_bytes(b"W5_GATE113_SENSITIVE\n")
            readonly_file = readonly / "readonly.bin"
            readonly_file.write_bytes(b"W5_GATE113_READONLY\n")
            installation_file = installation / "installation.bin"
            installation_file.write_bytes(b"W5_GATE113_INSTALLATION\n")
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

            normal_pass = outside / "outside-synthetic-with-normal-pass.bin"
            without_normal = outside / "outside-synthetic-without-normal-pass.bin"
            world_only = outside / "outside-world-only.bin"
            no_second_pass = outside / "outside-no-second-pass.bin"
            for path in (normal_pass, without_normal, world_only, no_second_pass):
                path.write_bytes(b"W5_GATE113_FIXTURE\n")
            _reconcile_file(
                acl_api,
                normal_pass,
                (
                    WindowsManagedAce(
                        normal_pass,
                        online.user_sid,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                    WindowsManagedAce(
                        normal_pass,
                        write_sid,
                        WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                        WRITE_ONLY_ACCESS_MASK,
                    ),
                ),
            )
            _reconcile_file(
                acl_api,
                without_normal,
                (
                    WindowsManagedAce(
                        without_normal,
                        write_sid,
                        WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                        WRITE_ONLY_ACCESS_MASK,
                    ),
                ),
            )
            _reconcile_file(
                acl_api,
                world_only,
                (
                    WindowsManagedAce(
                        world_only,
                        _WORLD,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                ),
            )
            _reconcile_file(
                acl_api,
                no_second_pass,
                (
                    WindowsManagedAce(
                        no_second_pass,
                        _BUILTIN_USERS,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                ),
            )
            audited_objects = {
                str(path): _object_fingerprint(path)
                for path in (
                    normal_pass,
                    without_normal,
                    world_only,
                    no_second_pass,
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

                def oracle_fixture(path: Path) -> dict[str, dict[str, object]]:
                    return {variant: oracle.check(variant, path) for variant in (_SYN, _SYN_WORLD)}

                fixtures = {
                    "OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS": normal_pass,
                    "OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS": without_normal,
                    "OUTSIDE_WORLD_ONLY": world_only,
                    "OUTSIDE_NO_SECOND_PASS": no_second_pass,
                }
                fixture_results = {name: oracle_fixture(path) for name, path in fixtures.items()}
                artifact["fixture_reconciliation"] = {
                    name: {
                        "acl_semantics": {
                            "OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS": "normal W2 user WRITE + synthetic restricting WRITE",
                            "OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS": "synthetic restricting WRITE only; no ordinary W2 user WRITE",
                            "OUTSIDE_WORLD_ONLY": "World WRITE only",
                            "OUTSIDE_NO_SECOND_PASS": "BUILTIN Users WRITE only; no synthetic/World authority",
                        }[name],
                        "results": result,
                    }
                    for name, result in fixture_results.items()
                }
                artifact["fixture_reconciliation"]["legacy_mapping"] = {
                    "Gate1.11_OUTSIDE_SYNTHETIC_ONLY": "OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS",
                    "Gate1.12_OUTSIDE_SYNTHETIC_ONLY": "OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS",
                }
                world_result = fixture_results["OUTSIDE_WORLD_ONLY"]
                ordinary_world_result = oracle.check_unrestricted(world_only)
                artifact["world_double_pass"] = {
                    "dacl_semantics": "exact World WRITE ACE; no synthetic WRITE ACE",
                    "ordinary_pass_principal": "WORLD",
                    "restricted_pass_principal": "WORLD",
                    "ordinary_access_check": ordinary_world_result,
                    "SYN": world_result[_SYN],
                    "SYN_WORLD": world_result[_SYN_WORLD],
                }
                # Persist the oracle cells before assertions so a native
                # AccessCheck mismatch remains auditable instead of being
                # reduced to a bare pytest failure.  The ordinary-token
                # comparison also distinguishes a fixture ACL/SID issue
                # from a restricted-token second-pass issue.
                artifact["fixture_reconciliation"]["ordinary_access_check"] = {
                    name: oracle.check_unrestricted(path) for name, path in fixtures.items()
                }
                artifact["fixture_reconciliation"]["acl_ready"] = {
                    name: acl_api.matches(
                        path,
                        {
                            "OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS": (
                                WindowsManagedAce(
                                    path,
                                    online.user_sid,
                                    WindowsManagedAceKind.WRITE_ALLOW,
                                    WRITE_ACCESS_MASK,
                                ),
                                WindowsManagedAce(
                                    path,
                                    write_sid,
                                    WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                                    WRITE_ONLY_ACCESS_MASK,
                                ),
                            ),
                            "OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS": (
                                WindowsManagedAce(
                                    path,
                                    write_sid,
                                    WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                                    WRITE_ONLY_ACCESS_MASK,
                                ),
                            ),
                            "OUTSIDE_WORLD_ONLY": (
                                WindowsManagedAce(
                                    path,
                                    _WORLD,
                                    WindowsManagedAceKind.WRITE_ALLOW,
                                    WRITE_ACCESS_MASK,
                                ),
                            ),
                            "OUTSIDE_NO_SECOND_PASS": (
                                WindowsManagedAce(
                                    path,
                                    _BUILTIN_USERS,
                                    WindowsManagedAceKind.WRITE_ALLOW,
                                    WRITE_ACCESS_MASK,
                                ),
                            ),
                        }[name],
                    )
                    for name, path in fixtures.items()
                }
                persist()
                self.assertEqual(
                    fixture_results["OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS"][_SYN]["write_allowed"],
                    True,
                )
                self.assertEqual(
                    fixture_results["OUTSIDE_SYNTHETIC_WITH_NORMAL_PASS"][_SYN_WORLD][
                        "write_allowed"
                    ],
                    True,
                )
                self.assertEqual(
                    fixture_results["OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS"][_SYN]["write_allowed"],
                    False,
                )
                self.assertEqual(
                    fixture_results["OUTSIDE_SYNTHETIC_WITHOUT_NORMAL_PASS"][_SYN_WORLD][
                        "write_allowed"
                    ],
                    False,
                )
                self.assertEqual(world_result[_SYN]["write_allowed"], False)
                self.assertEqual(world_result[_SYN_WORLD]["write_allowed"], True)
                self.assertEqual(ordinary_world_result["oracle"], "PASS")
                self.assertEqual(ordinary_world_result["write_allowed"], True)
                self.assertEqual(
                    fixture_results["OUTSIDE_NO_SECOND_PASS"][_SYN_WORLD]["write_allowed"],
                    False,
                )
                for result_group in fixture_results.values():
                    for result in result_group.values():
                        self.assertEqual(result["oracle"], "PASS")

                env_roots, redaction_roots = _environment_roots(online.username)
                audit_roots = list(env_roots)
                for path, label in (
                    (installation, "Installation"),
                    (readonly, "ReadOnlyRoot"),
                    (workspace, "WorkspaceExcluded"),
                ):
                    canonical = _canonical(path)
                    if canonical not in redaction_roots:
                        redaction_roots[canonical] = label
                    if canonical not in audit_roots:
                        audit_roots.append(canonical)
                surface = _scan_roots(
                    tuple(audit_roots),
                    (_canonical(workspace),),
                    oracle,
                    redaction_roots,
                )
                artifact["host_surface"] = surface
                persist()

                harness = _Gate1DirectProcess()
                broker_destination = workspace / "gate113-token-broker.exe"
                p4_destination = workspace / "gate113-p4.exe"
                shutil.copy2(broker, broker_destination)
                shutil.copy2(p4, p4_destination)

                async def run_trace(variant: str) -> dict[str, object]:
                    arguments = (variant, write_sid.value, str(p4_destination), str(workspace))
                    spec = _Workload(
                        "GATE113_TRACE", variant.casefold(), broker_destination, arguments
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
                    return _projection(raw, variant, "P4")

                trace = _trace_tool_facts()
                trace["TRACE_SYN"] = await run_trace(_SYN)
                trace["TRACE_SYN_WORLD"] = await run_trace(_SYN_WORLD)
                trace["candidate_differential_operations"] = []
                trace["classification"] = "BCRYPT_TARGET_NOT_YET_IDENTIFIED"
                trace["limitation"] = (
                    f"{trace.get('limitation', '')}; no bounded object-access differential was captured"
                ).strip("; ")
                artifact["trace"] = trace
                artifact["production_source_diff"] = _production_source_diff()
                unchanged = {
                    path: _object_fingerprint(Path(path)) == fingerprint
                    for path, fingerprint in audited_objects.items()
                }
                artifact["object_mutation_check"] = {
                    "all_unchanged": all(unchanged.values()),
                    "objects": unchanged,
                }
                artifact["status"] = "COMPLETED"
                persist()
                self.assertEqual(artifact["production_source_diff"], ())
                self.assertTrue(artifact["object_mutation_check"]["all_unchanged"])
                trace_syn = cast(dict[str, object], trace["TRACE_SYN"])
                trace_world = cast(dict[str, object], trace["TRACE_SYN_WORLD"])
                syn_probe = cast(dict[str, object], trace_syn["probe_result"])
                world_probe = cast(dict[str, object], trace_world["probe_result"])
                syn_bcrypt = cast(dict[str, object], syn_probe["bcrypt"])
                world_bcrypt = cast(dict[str, object], world_probe["bcrypt"])
                self.assertEqual(syn_bcrypt["load"], "FAIL")
                self.assertEqual(world_bcrypt["load"], "PASS")
            finally:
                oracle.close()
                await asyncio.to_thread(authority.cleanup, setup_request)

        self.assertEqual(artifact.get("status"), "COMPLETED")
        self.assertEqual(artifact.get("production_source_diff"), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
