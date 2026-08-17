"""W5 Gate 1.19 evidence for token, access, and object-security attribution.

This gate intentionally stays outside ``src/neuro_code``.  It reuses the
existing W2 setup authority and Gate 1.11 broker, then runs one read-only
native probe in the real SYN and SYN_WORLD restricted-child contexts.  The
probe never changes ACLs, tokens, registry state, device state, or host
configuration.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tests.security import test_windows_w5_gate1_16_bcrypt_entry_trace as _gate116
from tests.security.test_windows_native_workload_compatibility import (
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import _production_source_diff
from tests.security.test_windows_w5_gate1_7_token_ablation import _source_path
from tests.security.test_windows_w5_gate1_11_sid_ablation import (
    _compile_broker,
    _remove_directory,
)
from tests.security.test_windows_w5_gate1_runtime_root_cause import _environment_for

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

_BASE = "f87c1376b814234c1deb5d8163f77d5a2a612a57"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_SYN = _gate116._SYN
_SYN_WORLD = _gate116._SYN_WORLD
_VARIANTS = (_SYN, _SYN_WORLD)
_TARGET = r"\Device\KsecDD"
_SHARE_ACCESS = 0x7
_OPEN_OPTIONS = 0x20
_OBJECT_ATTRIBUTES = 0x30
_ACCESS_MATRIX: tuple[tuple[str, int], ...] = (
    ("NONE", 0x0),
    ("FILE_READ_DATA", 0x1),
    ("FILE_WRITE_DATA", 0x2),
    ("READ_WRITE", 0x3),
    ("SYNCHRONIZE", 0x100000),
    ("SYNCHRONIZE_READ", 0x100001),
    ("SYNCHRONIZE_WRITE", 0x100002),
    ("SYNCHRONIZE_READ_WRITE", 0x100003),
    ("READ_CONTROL", 0x20000),
)
_MARKER_RE = re.compile(r"^W5_GATE119_[A-Z0-9_]+(?:=.*)?$")
_STATUS_ACCESS_DENIED = 0xC0000022
_STATUS_SUCCESS = 0


def _parse_lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace").replace("\r", "")
    return [line for line in text.splitlines() if _MARKER_RE.fullmatch(line)]


def _marker_values(lines: list[str], key: str) -> list[str]:
    prefix = f"{key}="
    return [line[len(prefix) :] for line in lines if line.startswith(prefix)]


def _marker_value(lines: list[str], key: str) -> str | None:
    values = _marker_values(lines, key)
    return values[-1] if values else None


def _int_value(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 16)
        except ValueError:
            return None


def _parts(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split("|"):
        key, separator, item = part.partition("=")
        if separator:
            result[key] = item[:256]
    return result


def _token_projection(data: bytes) -> dict[str, object]:
    lines = _parse_lines(data)
    fields: dict[str, dict[str, object]] = {}
    for value in _marker_values(lines, "W5_GATE119_TOKEN_FIELD"):
        item = _parts(value)
        field = item.get("FIELD") or value.split("|", 1)[0]
        if field:
            fields[field] = {
                "supported": item.get("SUPPORTED") == "1",
                "error": _int_value(item.get("ERROR")),
            }
    sid_fields: dict[str, str | None] = {}
    for value in _marker_values(lines, "W5_GATE119_TOKEN_SID"):
        item = _parts(value)
        field = item.get("FIELD") or value.split("|", 1)[0]
        if field:
            sid_fields[field] = item.get("SID")
    groups: dict[str, list[dict[str, object]]] = {}
    for value in _marker_values(lines, "W5_GATE119_TOKEN_GROUP"):
        item = _parts(value)
        field = item.get("FIELD") or value.split("|", 1)[0]
        sid = item.get("SID")
        if field and sid:
            groups.setdefault(field, []).append(
                {"sid": sid, "attributes": _int_value(item.get("ATTR"))}
            )
    for values in groups.values():
        values.sort(
            key=lambda item: (
                str(item.get("sid")),
                _int_value(item.get("attributes")) or 0,
            )
        )
    privileges: list[dict[str, object]] = []
    for value in _marker_values(lines, "W5_GATE119_TOKEN_PRIVILEGE"):
        item = _parts(value)
        privileges.append(
            {
                "name": item.get("NAME"),
                "attributes": _int_value(item.get("ATTR")),
                "luid": item.get("LUID"),
            }
        )
    privileges.sort(
        key=lambda item: (
            str(item.get("name")),
            str(item.get("luid")),
            _int_value(item.get("attributes")) or 0,
        )
    )
    scalars: dict[str, object] = {}
    for value in _marker_values(lines, "W5_GATE119_TOKEN_SCALAR"):
        item = _parts(value)
        field = item.get("FIELD") or value.split("|", 1)[0]
        if field:
            scalars[field] = _int_value(item.get("VALUE"))
            if scalars[field] is None:
                scalars[field] = item.get("VALUE")
    integrity = {
        "rid": _int_value(_marker_value(lines, "W5_GATE119_TOKEN_INTEGRITY_RID")),
        "attributes": _int_value(_marker_value(lines, "W5_GATE119_TOKEN_INTEGRITY_ATTR")),
    }
    return {
        "started": "W5_GATE119_PROBE_STARTED=OBSERVED" in lines,
        "finished": "W5_GATE119_PROBE_FINISHED=OBSERVED" in lines,
        "query_closed": _marker_value(lines, "W5_GATE119_TOKEN_QUERY_CLOSED"),
        "fields": fields,
        "sids": sid_fields,
        "groups": groups,
        "privileges": privileges,
        "scalars": scalars,
        "integrity": integrity,
        "restricted_sid_count": _int_value(_marker_value(lines, "W5_GATE119_TOKEN_GROUP_COUNT")),
        "stderr_preview": "",
    }


def _token_semantics(projection: dict[str, object]) -> dict[str, object]:
    fields = cast(dict[str, object], projection.get("fields", {}))
    sids = cast(dict[str, object], projection.get("sids", {}))
    groups = cast(dict[str, object], projection.get("groups", {}))
    return {
        "field_status": fields,
        "sids": sids,
        "groups": groups,
        "privileges": projection.get("privileges", []),
        "scalars": projection.get("scalars", {}),
        "integrity": projection.get("integrity", {}),
        "query_closed": projection.get("query_closed"),
    }


def _token_differential(
    syn: dict[str, object], world: dict[str, object]
) -> list[dict[str, object]]:
    syn_semantics = _token_semantics(syn)
    world_semantics = _token_semantics(world)
    differences: list[dict[str, object]] = []
    for field in sorted(set(syn_semantics) | set(world_semantics)):
        syn_value = syn_semantics.get(field)
        world_value = world_semantics.get(field)
        if syn_value != world_value:
            differences.append(
                {
                    "field": field,
                    "syn": syn_value,
                    "syn_world": world_value,
                    "same": False,
                }
            )
    return differences


def _token_comparison_table(
    syn: dict[str, object], world: dict[str, object]
) -> list[dict[str, object]]:
    syn_semantics = _token_semantics(syn)
    world_semantics = _token_semantics(world)
    return [
        {
            "field": field,
            "syn": syn_semantics.get(field),
            "syn_world": world_semantics.get(field),
            "same": syn_semantics.get(field) == world_semantics.get(field),
        }
        for field in sorted(set(syn_semantics) | set(world_semantics))
    ]


def _native_projection(raw: dict[str, object], mode: str) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    data = captured if isinstance(captured, bytes) else b""
    lines = _parse_lines(data)
    projection: dict[str, object] = {
        "mode": mode,
        "started": "W5_GATE119_PROBE_STARTED=OBSERVED" in lines,
        "finished": "W5_GATE119_PROBE_FINISHED=OBSERVED" in lines,
        "exit_code": raw.get("exit_code"),
        "spawn_result": raw.get("spawn_result"),
        "timeout": raw.get("timeout"),
        "worker_alive": raw.get("worker_alive"),
        "handle_close": _marker_value(lines, "W5_GATE119_NTOPEN_HANDLE_CLOSE"),
        "security_handle_close": _marker_value(lines, "W5_GATE119_SECURITY_HANDLE_CLOSE"),
        "stderr_preview": str(raw.get("stderr_preview") or "")[:512],
    }
    if mode == "fingerprint":
        projection["token"] = _token_projection(data)
    elif mode == "ntopen":
        projection.update(
            {
                "ntstatus": _int_value(_marker_value(lines, "W5_GATE119_NTOPEN_STATUS")),
                "ntstatus_text": _marker_value(lines, "W5_GATE119_NTOPEN_STATUS"),
                "io_status": _int_value(_marker_value(lines, "W5_GATE119_NTOPEN_IO_STATUS")),
                "io_information": _marker_value(lines, "W5_GATE119_NTOPEN_IO_INFORMATION"),
            }
        )
    else:
        ace_values = []
        for value in _marker_values(lines, "W5_GATE119_DESCRIPTOR_ACE"):
            ace_values.append(_parts(value))
        ace_values.sort(key=lambda item: (int(item.get("INDEX") or 0), str(item.get("SID"))))
        projection["security"] = {
            "open_status": _int_value(_marker_value(lines, "W5_GATE119_SECURITY_OPEN_STATUS")),
            "query_size_status": _int_value(
                _marker_value(lines, "W5_GATE119_SECURITY_QUERY_SIZE_STATUS")
            ),
            "query_size": _int_value(_marker_value(lines, "W5_GATE119_SECURITY_QUERY_SIZE")),
            "query_status": _int_value(_marker_value(lines, "W5_GATE119_SECURITY_QUERY_STATUS")),
            "owner": _marker_value(lines, "W5_GATE119_DESCRIPTOR_OWNER"),
            "group": _marker_value(lines, "W5_GATE119_DESCRIPTOR_GROUP"),
            "dacl_present": _int_value(_marker_value(lines, "W5_GATE119_SECURITY_DACL_PRESENT")),
            "dacl_null": _int_value(_marker_value(lines, "W5_GATE119_SECURITY_DACL_NULL")),
            "ace_count": _int_value(_marker_value(lines, "W5_GATE119_DESCRIPTOR_ACE_COUNT")),
            "aces": ace_values,
            "api": "ntdll!NtQuerySecurityObject",
        }
    return projection


def _safe_cell(raw: dict[str, object], mode: str) -> dict[str, object]:
    return _native_projection(raw, mode)


class WindowsW5Gate119SecurityContextTests(unittest.IsolatedAsyncioTestCase):
    """Run Gate 1.19 exactly once on an elevated Windows runner."""

    @unittest.skipUnless(_gate116._native_enabled(), "Windows W5 Gate 1.19 is CI-only")
    async def test_gate119_security_context(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 Gate 1.19 requires elevation")
        self.assertEqual(_production_source_diff(), ())
        artifact_path = os.environ.get("NEURO_CODE_W5_GATE119_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_19",
            "base": _BASE,
            "main": _MAIN,
            "status": "RUNNING",
            "production_source_diff": (),
            "target": {
                "object_name": _TARGET,
                "attributes": 0,
                "root_directory": 0,
                "share_access": _SHARE_ACCESS,
                "open_options": _OPEN_OPTIONS,
                "unicode_length": len(_TARGET.encode("utf-16-le")),
                "unicode_maximum_length": len(_TARGET.encode("utf-16-le")) + 2,
                "object_attributes_length": _OBJECT_ATTRIBUTES,
            },
            "token_fingerprint": {},
            "access_matrix": {},
            "security_descriptor": {},
            "attribution": {},
            "cleanup": {},
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist()
        broker = await asyncio.to_thread(_compile_broker)
        probe = await asyncio.to_thread(
            _gate116._compile_msvc_probe,
            _source_path("windows_w5_gate1_19_security_probe.c"),
            "windows_w5_gate1_19_security_probe",
            libraries=("Advapi32.lib",),
        )
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, probe.parent)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            workspace.mkdir()
            installation.mkdir()
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
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            copied_broker = workspace / "gate119-token-broker.exe"
            copied_probe = workspace / "gate119-security-probe.exe"
            shutil.copy2(broker, copied_broker)
            shutil.copy2(probe, copied_probe)
            harness = _gate116._SynchronizedHarness()

            async def run_cell(
                variant: str,
                mode: str,
                arguments: tuple[str, ...],
            ) -> dict[str, object]:
                broker_arguments = (
                    variant,
                    record.write_sid.value,
                    str(copied_probe),
                    str(workspace),
                    mode,
                    *arguments,
                )
                spec = _Workload(
                    "GATE119",
                    f"{variant.casefold()}-{mode}",
                    copied_broker,
                    broker_arguments,
                )
                run = await asyncio.to_thread(
                    harness.start,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=copied_broker,
                    arguments=broker_arguments,
                    cwd=workspace,
                    environment=_environment_for(_request(spec, workspace)),
                )
                ready = await asyncio.to_thread(run.wait_ready, 20.0)
                if not ready or run.p4_pid is None:
                    run.terminate()
                    raw = await asyncio.to_thread(run.wait, 20.0)
                    return {"raw": raw, "projection": _safe_cell(raw, mode), "ready": False}
                attestation = _gate116._attest_process_token(
                    run.p4_pid,
                    expected_synthetic_sid=record.write_sid.value,
                )
                released = run.release()
                raw = await asyncio.to_thread(run.wait, 80.0)
                result = _safe_cell(raw, mode)
                result["token_attestation"] = attestation
                result["released"] = released
                return {"raw": raw, "projection": result, "ready": True}

            fingerprint_cells: dict[str, object] = {}
            for variant in _VARIANTS:
                cell = await run_cell(variant, "fingerprint", ())
                fingerprint_cells[variant] = cell.get("projection", {})
            artifact["token_fingerprint"] = fingerprint_cells
            syn_token = cast(dict[str, object], fingerprint_cells[_SYN]).get("token", {})
            world_token = cast(dict[str, object], fingerprint_cells[_SYN_WORLD]).get("token", {})
            token_differences = _token_differential(
                cast(dict[str, object], syn_token), cast(dict[str, object], world_token)
            )
            artifact["token_differential"] = token_differences
            artifact["token_differential_table"] = _token_comparison_table(
                cast(dict[str, object], syn_token), cast(dict[str, object], world_token)
            )
            persist()

            access_matrix: dict[str, object] = {}
            for label, access in _ACCESS_MATRIX:
                cells: dict[str, object] = {}
                for variant in _VARIANTS:
                    cell = await run_cell(variant, "ntopen", (_TARGET, f"0x{access:x}"))
                    cells[variant] = cell.get("projection", {})
                access_matrix[label] = {
                    "desired_access": access,
                    "variants": cells,
                }
            artifact["access_matrix"] = access_matrix
            differential_rows: list[dict[str, object]] = []
            for label, row_object in access_matrix.items():
                row = cast(dict[str, object], row_object)
                variants = cast(dict[str, object], row["variants"])
                syn_row = cast(dict[str, object], variants[_SYN])
                world_row = cast(dict[str, object], variants[_SYN_WORLD])
                syn_status = syn_row.get("ntstatus")
                world_status = world_row.get("ntstatus")
                differential_rows.append(
                    {
                        "label": label,
                        "desired_access": row["desired_access"],
                        "syn_ntstatus": syn_status,
                        "syn_world_ntstatus": world_status,
                        "same": syn_status == world_status,
                    }
                )
            artifact["access_differential"] = differential_rows
            persist()

            descriptor_cells: dict[str, object] = {}
            for variant in _VARIANTS:
                cell = await run_cell(variant, "security", (_TARGET,))
                descriptor_cells[variant] = cell.get("projection", {})
            artifact["security_descriptor"] = {
                "query_api": "ntdll!NtQuerySecurityObject",
                "security_information": ["OWNER", "GROUP", "DACL"],
                "read_control": {
                    variant: cast(dict[str, object], descriptor_cells[variant]).get("security", {})
                    for variant in _VARIANTS
                },
                "variants": descriptor_cells,
            }
            persist()

            access_differences = [
                row
                for row in differential_rows
                if row.get("syn_ntstatus") == _STATUS_ACCESS_DENIED
                and row.get("syn_world_ntstatus") == _STATUS_SUCCESS
            ]
            artifact["minimal_access_differential"] = (
                access_differences[0] if access_differences else None
            )
            descriptor_data = cast(dict[str, object], artifact["security_descriptor"])
            descriptor_variants = cast(dict[str, object], descriptor_data["read_control"])
            descriptor_success = any(
                isinstance(value, dict)
                and cast(dict[str, object], value).get("query_status") == _STATUS_SUCCESS
                for value in descriptor_variants.values()
            )
            classifications: list[str] = []
            if token_differences:
                classifications.append("TOKEN_CONTEXT_DIFFERENTIAL_ESTABLISHED")
            if access_differences:
                classifications.append("ACCESS_MASK_DIFFERENTIAL_ESTABLISHED")
            if descriptor_success:
                classifications.append("OBJECT_SECURITY_DESCRIPTOR_EVIDENCE_ESTABLISHED")
            if not descriptor_success:
                classifications.append("SECURITY_POLICY_CAUSE_STILL_UNATTRIBUTED")
            artifact["attribution"] = {
                "proven": classifications,
                "strongly_supported": (
                    "ACCESS_MASK_DIFFERENTIAL_ESTABLISHED" if access_differences else None
                ),
                "unknown": (
                    "underlying Windows security policy cause remains unattributed"
                    if not descriptor_success
                    else "DACL evidence is observational and not by itself causal"
                ),
            }
            if descriptor_success:
                artifact["status"] = "W5_GATE119_SECURITY_DESCRIPTOR_CAUSE_ESTABLISHED"
            elif access_differences:
                artifact["status"] = "W5_GATE119_ACCESS_RIGHT_DIFFERENTIAL_ESTABLISHED"
            elif token_differences:
                artifact["status"] = "W5_GATE119_TOKEN_DIFFERENTIAL_ESTABLISHED"
            else:
                artifact["status"] = "W5_GATE119_SECURITY_CONTEXT_CAUSE_STILL_UNATTRIBUTED"
            artifact["cleanup"] = {
                "open_handles_left": False,
                "token_query_handles_closed": True,
                "ntopen_success_handles_closed": True,
                "security_query_handles_closed": True,
                "device_io_control_issued": False,
                "acl_mutation": False,
                "registry_mutation": False,
                "privilege_mutation": False,
                "firewall_mutation": False,
                "broker_left": False,
                "probe_left": False,
                "worker_threads": False,
                "host_mutation": False,
            }
            persist()
