"""W5 Gate 1.18 evidence for the NtOpenFile access boundary.

Gate 1.17 established a return-value difference inside
``InitializeSystemPreferredCache``.  This gate follows the already observed
causal call chain to the exact ``NtOpenFile`` invocation, records its semantic
arguments, and repeats that operation with one minimal native probe.  It is
evidence-only: no production sandbox or Windows security state is changed.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from tests.security import test_windows_w5_gate1_16_bcrypt_entry_trace as _gate116
from tests.security import test_windows_w5_gate1_17_system_preferred_cache_trace as _gate117
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

_BASE = "70f6d6d142c60a4e352fe7fed3558d7d3c848caa"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_BASELINE_RUN = 32009556277
_SYN = _gate117._SYN
_SYN_WORLD = _gate117._SYN_WORLD
_VARIANTS = (_SYN, _SYN_WORLD)
_STATUS_ACCESS_DENIED = 0xC0000022
_STATUS_SUCCESS = 0
_MAX_OUTPUT = 64 * 1024
_MAX_QWORDS = 16
_DIRECT_MARKER_RE = re.compile(r"^W5_GATE118_DIRECT_[A-Z0-9_]+(?:=.*)?$")
_QWORD_LINE_RE = re.compile(r"^\s*([0-9a-f`]+)\s+(.+?)\s*$", re.I)
_DU_LINE_RE = re.compile(r"^\s*([0-9a-f`]+)\s+(.*)$", re.I)


def _hex_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value.replace("`", ""), 0)
    except ValueError:
        try:
            return int(value.replace("`", ""), 16)
        except ValueError:
            return None


def _pointer(value: object) -> int | None:
    number = _hex_int(value)
    if number is None or number == 0 or number > 0x00007FFFFFFFFFFF:
        return None
    return number


def _qwords(text: str) -> list[int]:
    """Parse only the bounded hexadecimal words emitted by CDB ``dq``."""

    result: list[int] = []
    for line in text.replace("\r", "").splitlines():
        # CDB prefixes the first data row after a command with its prompt
        # (for example ``0:000> 000000...``).  Strip that prompt before
        # applying the bounded address/qword grammar; otherwise the first
        # stack word is silently discarded and the six-argument NtOpenFile
        # capture is misclassified as unavailable.
        line = re.sub(r"^\s*\d+:\d+>\s*", "", line)
        match = _QWORD_LINE_RE.match(line)
        if match is None:
            continue
        words = re.findall(r"(?i)(?<![a-z0-9])([0-9a-f`]{8,17})(?![a-z0-9])", match.group(2))
        for word in words:
            try:
                result.append(int(word.replace("`", ""), 16))
            except ValueError:
                continue
            if len(result) >= _MAX_QWORDS:
                return result
    return result


def _parse_du(text: str) -> str | None:
    """Extract one bounded quoted/unquoted string from a CDB ``du`` result."""

    chunks: list[str] = []
    for line in text.replace("\r", "").splitlines():
        # As with ``dq``, CDB prefixes the first ``du`` line with the
        # command prompt.  Remove it before applying the bounded address
        # grammar so a one-line UNICODE_STRING is not discarded.
        line = re.sub(r"^\s*\d+:\d+>\s*", "", line)
        match = _DU_LINE_RE.match(line)
        if match is None:
            continue
        payload = match.group(2).strip()
        if not payload or payload.startswith("?"):
            continue
        if '"' in payload:
            first = payload.find('"')
            last = payload.rfind('"')
            if last > first:
                payload = payload[first + 1 : last]
        chunks.append(payload)
    value = "".join(chunks)
    return value if value else None


def _parse_direct_markers(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8", errors="replace").replace("\r", "")
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not _DIRECT_MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        result[key] = value[:512] if separator else "OBSERVED"
    return result


def _marker_int(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _status_text(value: int | None) -> str | None:
    return f"0x{value & 0xFFFFFFFF:08x}" if value is not None else None


def _parse_object_name(session: Any, object_name_pointer: int) -> dict[str, object]:
    words = _qwords(
        session.command(
            f"dq 0x{object_name_pointer:x} L2",
            "W5_GATE118_OBJECT_NAME_STRUCT",
            5.0,
        )
    )
    if len(words) < 2:
        raise RuntimeError("UNICODE_STRING memory unavailable")
    length = words[0] & 0xFFFF
    maximum_length = (words[0] >> 16) & 0xFFFF
    buffer_pointer = _pointer(words[1])
    if buffer_pointer is None:
        raise RuntimeError("UNICODE_STRING buffer unavailable")
    character_limit = max(1, min(256, (maximum_length // 2) + 1))
    decoded = _parse_du(
        session.command(
            f"du 0x{buffer_pointer:x} L{character_limit}",
            "W5_GATE118_OBJECT_NAME_TEXT",
            5.0,
        )
    )
    if decoded is None:
        raise RuntimeError("effective object name unavailable")
    # The captured UNICODE_STRING length is authoritative; reject a debugger
    # rendering which would silently include a longer object name.
    if len(decoded.encode("utf-16-le")) < length:
        raise RuntimeError("effective object name is shorter than Length")
    return {
        "length": length,
        "maximum_length": maximum_length,
        "buffer_pointer": f"0x{buffer_pointer:x}",
        "value": decoded[: length // 2],
    }


def _parse_object_attributes(session: Any, pointer: int) -> dict[str, object]:
    words = _qwords(
        session.command(
            f"dq 0x{pointer:x} L6",
            "W5_GATE118_OBJECT_ATTRIBUTES",
            5.0,
        )
    )
    if len(words) < 6:
        raise RuntimeError("OBJECT_ATTRIBUTES memory unavailable")
    object_name_pointer = _pointer(words[2])
    if object_name_pointer is None:
        raise RuntimeError(
            "OBJECT_ATTRIBUTES.ObjectName unavailable: "
            f"pointer=0x{pointer:x} words={[f'0x{word:x}' for word in words[:6]]}"
        )
    object_name = _parse_object_name(session, object_name_pointer)
    return {
        "length": words[0] & 0xFFFFFFFF,
        "root_directory": f"0x{words[1]:x}",
        "root_directory_value": words[1],
        "object_name_pointer": f"0x{object_name_pointer:x}",
        "attributes": words[3] & 0xFFFFFFFF,
        "security_descriptor": f"0x{words[4]:x}",
        "security_quality_of_service": f"0x{words[5]:x}",
        "object_name": object_name,
    }


def _parse_handle_query(text: str) -> dict[str, object]:
    type_match = re.search(r"(?im)^\s*Type\s+([^\s]+)", text)
    granted_match = re.search(r"(?im)^\s*GrantedAccess\s+([^\s]+)", text)
    return {
        "available": type_match is not None or granted_match is not None,
        "object_type": type_match.group(1) if type_match else None,
        "granted_access": granted_match.group(1) if granted_match else None,
        "preview": text[-2048:],
    }


def _dacl_role(sid: ctypes.c_void_p, *, token_user: str | None, synthetic: str) -> str:
    # This projection deliberately exposes roles only, never machine-specific
    # SID strings.  It is used only if GetNamedSecurityInfoW succeeds.
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert.restype = ctypes.c_int32
    text = ctypes.c_wchar_p()
    if not convert(sid, ctypes.byref(text)) or not text.value:
        return "OTHER"
    value = str(text.value)
    advapi.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    if value == synthetic:
        return "SYNTHETIC_WRITE"
    if token_user and value == token_user:
        return "TOKEN_USER/W2_SANDBOX_USER"
    return {
        "S-1-1-0": "WORLD",
        "S-1-5-32-545": "BUILTIN_USERS",
        "S-1-5-11": "AUTHENTICATED_USERS",
        "S-1-5-18": "SYSTEM",
        "S-1-5-32-544": "ADMINISTRATORS",
    }.get(value, "OTHER")


def _query_dacl(
    object_name: str,
    *,
    token_user: str | None,
    synthetic: str,
) -> dict[str, object]:  # pragma: no cover - Windows CI
    """Attempt a side-effect-free named security query; do not alter state."""

    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        get_named = advapi.GetNamedSecurityInfoW
        get_named.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_named.restype = ctypes.c_uint32
        owner = ctypes.c_void_p()
        group = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        # SE_FILE_OBJECT=1; OWNER|GROUP|DACL security information.
        status = int(
            get_named(
                object_name,
                1,
                0x00000007,
                ctypes.byref(owner),
                ctypes.byref(group),
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
        )
        if status != 0 or not descriptor.value:
            return {
                "available": False,
                "status": "TARGET_DACL_NOT_AVAILABLE_SIDE_EFFECT_FREE",
                "method": "GetNamedSecurityInfoW",
                "error": status,
            }
        # Query the descriptor shape and bounded ACE semantics.  If any native
        # projection is unavailable, retain the non-mutating result as
        # unavailable rather than inventing an ACL interpretation.
        get_dacl = advapi.GetSecurityDescriptorDacl
        get_dacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int32),
        ]
        get_dacl.restype = ctypes.c_int32
        present = ctypes.c_int32()
        native_dacl = ctypes.c_void_p()
        defaulted = ctypes.c_int32()
        if (
            not get_dacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(native_dacl),
                ctypes.byref(defaulted),
            )
            or not present.value
            or not native_dacl.value
        ):
            advapi.LocalFree(descriptor)
            return {
                "available": False,
                "status": "TARGET_DACL_NOT_AVAILABLE_SIDE_EFFECT_FREE",
                "method": "GetNamedSecurityInfoW",
                "error": "DACL_UNAVAILABLE",
            }
        get_acl_info = advapi.GetAclInformation
        get_acl_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        get_acl_info.restype = ctypes.c_int32

        class _AclSize(ctypes.Structure):
            _fields_ = [
                ("ace_count", ctypes.c_uint32),
                ("bytes_in_use", ctypes.c_uint32),
                ("bytes_free", ctypes.c_uint32),
            ]

        info = _AclSize()
        if not get_acl_info(
            native_dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            2,
        ):
            advapi.LocalFree(descriptor)
            return {
                "available": False,
                "status": "TARGET_DACL_NOT_AVAILABLE_SIDE_EFFECT_FREE",
                "method": "GetNamedSecurityInfoW",
                "error": "ACL_INFORMATION_UNAVAILABLE",
            }
        get_ace = advapi.GetAce
        get_ace.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        get_ace.restype = ctypes.c_int32
        aces: list[dict[str, object]] = []
        for index in range(min(int(info.ace_count), 64)):
            ace_pointer = ctypes.c_void_p()
            if not get_ace(native_dacl, index, ctypes.byref(ace_pointer)) or not ace_pointer.value:
                continue
            raw = ctypes.string_at(ace_pointer, 8)
            ace_type = raw[0]
            ace_flags = raw[1]
            mask = int.from_bytes(raw[4:8], "little")
            if ace_type not in (0, 1):
                continue
            sid_pointer = ctypes.c_void_p(ace_pointer.value + 8)
            aces.append(
                {
                    "principal": _dacl_role(
                        sid_pointer,
                        token_user=token_user,
                        synthetic=synthetic,
                    ),
                    "type": "ALLOW" if ace_type == 0 else "DENY",
                    "access_mask": f"0x{mask:08x}",
                    "inheritance": f"0x{ace_flags:02x}",
                }
            )
        owner_role = _dacl_role(owner, token_user=token_user, synthetic=synthetic)
        advapi.LocalFree(descriptor)
        return {
            "available": True,
            "status": "DACL_QUERY_PASS",
            "owner_role": owner_role,
            "aces": aces,
            "world_authority_present": any(ace.get("principal") == "WORLD" for ace in aces),
            "synthetic_authority_present": any(
                ace.get("principal") == "SYNTHETIC_WRITE" for ace in aces
            ),
        }
    except (OSError, AttributeError, ctypes.ArgumentError, ValueError) as error:
        return {
            "available": False,
            "status": "TARGET_DACL_NOT_AVAILABLE_SIDE_EFFECT_FREE",
            "method": "GetNamedSecurityInfoW",
            "error": type(error).__name__,
        }


def _capture_ntopen(
    session: Any,
    *,
    module_base: int | None,
    io_open_address: int,
    nt_open_address: int,
    variant: str,
) -> dict[str, object]:
    """Stop at the call from this _IoOpenDevice invocation only."""

    session.command(
        f"bp /1 0x{io_open_address:x}",
        f"W5_GATE118_IOOPEN_BP_{variant}",
        5.0,
    )
    session.send("g")
    io_entry = session.wait_for(lambda text: "breakpoint" in text.casefold(), 25.0)
    io_stack = session.command("k 8", f"W5_GATE118_IOOPEN_STACK_{variant}", 5.0)
    stack_lower = io_stack.casefold()
    if "bcrypt!_ioopendevice" not in stack_lower or "bcrypt!iocallkerneldriver" not in stack_lower:
        raise RuntimeError(
            "_IoOpenDevice caller stack was not causal: " + io_stack[-1024:].replace("\n", " ")
        )
    # Use an explicitly bounded instruction window.  ``uf`` can follow a
    # private helper's exception/unwind metadata for an unbounded amount of
    # output on some debugger builds; the call-site contract only needs this
    # bounded function window.
    disassembly = session.command(
        f"u 0x{io_open_address:x} L80",
        f"W5_GATE118_U_IOOPEN_{variant}",
        10.0,
    )
    summary = _gate117._disassembly_summary(disassembly, "NtOpenFile")
    call_sites = cast(list[dict[str, object]], summary.get("call_sites", []))
    if not call_sites:
        raise RuntimeError("_IoOpenDevice NtOpenFile call site unavailable")
    call_site = call_sites[0]
    call_va = _hex_int(call_site.get("call_va"))
    return_va = _hex_int(call_site.get("return_site_va"))
    if call_va is None or return_va is None:
        raise RuntimeError("NtOpenFile call/return address unavailable")
    session.command(
        f"bp /1 0x{call_va:x}",
        f"W5_GATE118_CALL_BP_{variant}",
        5.0,
    )
    session.send("g")
    session.wait_for(lambda text: "breakpoint" in text.casefold(), 25.0)
    call_registers = _gate117._registers(
        session.command("r", f"W5_GATE118_CALL_REG_{variant}", 5.0)
    )
    stopped_rip = _hex_int(call_registers.get("rip"))
    if stopped_rip is not None and stopped_rip != call_va:
        raise RuntimeError("stopped at an unrelated NtOpenFile call")
    # Because execution is stopped at this exact call instruction, the next
    # one-shot breakpoint can only observe this invocation (and is verified by
    # the causal caller stack below).
    session.command(
        f"bp /1 0x{nt_open_address:x}",
        f"W5_GATE118_NTOPEN_BP_{variant}",
        5.0,
    )
    session.send("g")
    nt_entry = session.wait_for(lambda text: "breakpoint" in text.casefold(), 25.0)
    nt_registers = _gate117._registers(
        session.command("r", f"W5_GATE118_NTOPEN_REG_{variant}", 5.0)
    )
    nt_stack = session.command("k 8", f"W5_GATE118_NTOPEN_STACK_{variant}", 5.0)
    nt_stack_lower = nt_stack.casefold()
    if (
        "bcrypt!_ioopendevice" not in nt_stack_lower
        or "bcrypt!iocallkerneldriver" not in nt_stack_lower
    ):
        raise RuntimeError("NtOpenFile breakpoint was not this _IoOpenDevice call")
    stack_words = _qwords(
        session.command("dq @rsp L7", f"W5_GATE118_NTOPEN_STACK_ARGS_{variant}", 5.0)
    )
    if len(stack_words) < 7:
        raise RuntimeError("NtOpenFile stack arguments unavailable")
    file_output = _pointer(nt_registers.get("rcx"))
    object_attributes = _pointer(nt_registers.get("r8"))
    io_status = _pointer(nt_registers.get("r9"))
    desired_access = _hex_int(nt_registers.get("rdx"))
    if (
        file_output is None
        or object_attributes is None
        or io_status is None
        or desired_access is None
    ):
        raise RuntimeError("NtOpenFile register arguments unavailable")
    object_projection = _parse_object_attributes(session, object_attributes)
    share_access = stack_words[5] & 0xFFFFFFFF
    open_options = stack_words[6] & 0xFFFFFFFF
    session.send("gu")
    session.wait_for(lambda text: bool(_gate116._PROMPT_RE.search(text)), 20.0)
    return_registers = _gate117._registers(
        session.command("r", f"W5_GATE118_NTOPEN_RETURN_REG_{variant}", 5.0)
    )
    file_words = _qwords(
        session.command(f"dq 0x{file_output:x} L1", f"W5_GATE118_FILE_OUTPUT_{variant}", 5.0)
    )
    iosb_words = _qwords(
        session.command(f"dq 0x{io_status:x} L2", f"W5_GATE118_IOSB_{variant}", 5.0)
    )
    file_handle = file_words[0] if file_words else None
    iosb_status = (iosb_words[0] & 0xFFFFFFFF) if iosb_words else None
    iosb_information = iosb_words[1] if len(iosb_words) > 1 else None
    handle_query: dict[str, object] = {
        "available": False,
        "status": "OBJECT_TYPE_QUERY_UNAVAILABLE",
    }
    if file_handle not in (None, 0, 0xFFFFFFFFFFFFFFFF):
        handle_output = session.command(
            f"!handle 0x{file_handle:x} f",
            f"W5_GATE118_HANDLE_QUERY_{variant}",
            5.0,
        )
        handle_query = _parse_handle_query(handle_output)
        if not handle_query.get("available"):
            handle_query["status"] = "OBJECT_TYPE_QUERY_UNAVAILABLE"
    return {
        "io_entry_preview": io_entry[-4096:],
        "io_stack": io_stack[-4096:],
        "io_open_device_rva": (
            f"0x{io_open_address - module_base:x}" if module_base is not None else None
        ),
        "disassembly": disassembly[-_MAX_OUTPUT:],
        "disassembly_summary": summary,
        "ntopen_call_va": f"0x{call_va:x}",
        "ntopen_call_rva": f"0x{call_va - module_base:x}" if module_base is not None else None,
        "ntopen_return_site_va": f"0x{return_va:x}",
        "ntopen_return_site_rva": (
            f"0x{return_va - module_base:x}" if module_base is not None else None
        ),
        "entry_preview": nt_entry[-4096:],
        "entry_registers": nt_registers,
        "entry_stack": nt_stack[-4096:],
        "file_handle_output_pointer": f"0x{file_output:x}",
        "io_status_block_pointer": f"0x{io_status:x}",
        "desired_access": desired_access,
        "share_access": share_access,
        "open_options": open_options,
        "object_attributes": object_projection,
        "return_registers": return_registers,
        "ntstatus": _status_text(_hex_int(return_registers.get("rax"))),
        "file_handle": f"0x{file_handle:x}" if file_handle is not None else None,
        "io_status": _status_text(iosb_status),
        "io_information": f"0x{iosb_information:x}" if iosb_information is not None else None,
        "caller_rip": return_registers.get("rip"),
        "handle_query": handle_query,
    }


def _direct_projection(raw: dict[str, object]) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    data = captured if isinstance(captured, bytes) else b""
    markers = _parse_direct_markers(data)
    status = markers.get("W5_GATE118_DIRECT_NTOPEN_STATUS")
    return {
        "markers": markers,
        "status": status,
        "status_int": _marker_int(markers, "W5_GATE118_DIRECT_NTOPEN_STATUS"),
        "io_status": markers.get("W5_GATE118_DIRECT_IO_STATUS"),
        "io_information": markers.get("W5_GATE118_DIRECT_IO_INFORMATION"),
        "handle_close": markers.get("W5_GATE118_DIRECT_HANDLE_CLOSE"),
        "finished": "W5_GATE118_DIRECT_FINISHED=OBSERVED" in data.decode("utf-8", errors="replace"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "stdout_preview": str(raw.get("stdout_preview") or "")[:2048],
        "stderr_preview": str(raw.get("stderr_preview") or "")[:512],
        "spawn_result": raw.get("spawn_result"),
        "worker_alive": raw.get("worker_alive"),
    }


class WindowsW5Gate118NtOpenFileTests(unittest.IsolatedAsyncioTestCase):
    """Run the focused Gate 1.18 experiment exactly once."""

    @unittest.skipUnless(_gate116._native_enabled(), "Windows W5 Gate 1.18 is CI-only")
    async def test_gate118_ntopenfile_target(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 Gate 1.18 requires elevation")
        self.assertEqual(_production_source_diff(), ())

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE118_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_18",
            "base": _BASE,
            "main": _MAIN,
            "baseline_run": _BASELINE_RUN,
            "status": "RUNNING",
            "production_source_diff": (),
            "gate117_correction": {
                "earliest_symbol_only_differential": {
                    "index": 83,
                    "syn": "bcrypt!_security_check_cookie",
                    "syn_world": "ntdll!NtDeviceIoControlFile",
                },
                "earliest_semantic_differential": {
                    "callee": "ntdll!NtOpenFile",
                    "syn": _status_text(_STATUS_ACCESS_DENIED),
                    "syn_world": _status_text(_STATUS_SUCCESS),
                },
            },
            "debugger": {},
            "controls": {},
            "ntopenfile": {},
            "direct_probe": {},
            "dacl": {},
            "cleanup": {},
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist()
        broker = await asyncio.to_thread(_compile_broker)
        loader = await asyncio.to_thread(
            _gate116._compile_msvc_probe,
            _source_path("windows_w5_gate1_16_sync_loader.c"),
            "windows_w5_gate118_sync_loader",
            libraries=(),
        )
        direct_probe = await asyncio.to_thread(
            _gate116._compile_msvc_probe,
            _source_path("windows_w5_gate1_18_direct_ntopen.c"),
            "windows_w5_gate118_direct_ntopen",
            libraries=(),
        )
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, loader.parent)
        self.addAsyncCleanup(_remove_directory, direct_probe.parent)

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
            copied_loader = workspace / "gate118-loader.exe"
            copied_broker = workspace / "gate118-token-broker.exe"
            copied_probe = workspace / "gate118-direct-ntopen.exe"
            shutil.copy2(loader, copied_loader)
            shutil.copy2(broker, copied_broker)
            shutil.copy2(direct_probe, copied_probe)
            write_sid = record.write_sid
            harness = _gate116._SynchronizedHarness()

            async def run_cell(
                variant: str,
                executable: Path,
                child_arguments: tuple[str, ...],
                *,
                debug: bool = False,
            ) -> dict[str, object]:
                broker_arguments = (
                    variant,
                    write_sid.value,
                    str(executable),
                    str(workspace),
                    *child_arguments,
                )
                spec = _Workload(
                    "GATE118",
                    variant.casefold(),
                    executable,
                    child_arguments,
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
                    return {"cell": _gate116._safe_result(raw), "raw": raw, "debug": {}}
                if not debug:
                    _gate116._attest_process_token(
                        run.p4_pid,
                        expected_synthetic_sid=write_sid.value,
                    )
                    run.release()
                    raw = await asyncio.to_thread(run.wait, 80.0)
                    return {
                        "cell": _gate116._safe_result(raw),
                        "raw": raw,
                        "debug": {},
                    }
                session: Any | None = None
                debug_data: dict[str, object] = {}
                try:
                    cdb = await asyncio.to_thread(_gate116._discover_cdb)
                    if not bool(cdb.get("available")):
                        run.terminate()
                        raw = await asyncio.to_thread(run.wait, 20.0)
                        return {
                            "cell": _gate116._safe_result(raw),
                            "raw": raw,
                            "debug": {"debugger": cdb},
                        }
                    selected = cast(dict[str, object], cdb["selected"])
                    session = _gate116._CdbSession(Path(str(selected["path"])), run.p4_pid)
                    run.cdb_pid = session.process.pid
                    initial = session.wait_for(
                        lambda text: bool(_gate116._PROMPT_RE.search(text)), 15.0
                    )
                    session.command("sxe ld:bcrypt.dll", "W5_GATE118_SXE_READY", 5.0)
                    session.send(".echo W5_GATE118_DEBUGGER_READY")
                    session.wait_for(lambda text: "W5_GATE118_DEBUGGER_READY" in text, 5.0)
                    debug_data["release_after_attach"] = run.release()
                    session.send("g")
                    module_load = session.wait_for(
                        lambda text: bool(_gate116._MODULE_LOAD_RE.search(text)), 20.0
                    )
                    module_listing = session.command("lm m bcrypt", "W5_GATE118_LM_DONE", 5.0)
                    module_match = _gate116._MODULE_LINE_RE.search(module_listing)
                    module_base = (
                        int(module_match.group(1).replace("`", ""), 16) if module_match else None
                    )
                    symbols: dict[str, object] = {}
                    for index, symbol in enumerate(
                        (
                            "bcrypt!_IoOpenDevice",
                            "bcrypt!IoCallKernelDriver",
                            "ntdll!NtOpenFile",
                        )
                    ):
                        symbols[symbol] = _gate117._symbol_attestation(
                            session,
                            symbol,
                            module_base,
                            f"W5_GATE118_{variant}_{index}",
                        )
                    debug_data.update(
                        {
                            "debugger": selected,
                            "module_load_observed": bool(
                                _gate116._MODULE_LOAD_RE.search(module_load)
                            ),
                            "module_base": (
                                f"0x{module_base:x}" if module_base is not None else None
                            ),
                            "initial_output_preview": initial[-2048:],
                            "symbols": symbols,
                            "post_attach_token_attestation": _gate116._attest_process_token(
                                run.p4_pid,
                                expected_synthetic_sid=write_sid.value,
                            ),
                        }
                    )
                    io_symbol = cast(dict[str, object], symbols["bcrypt!_IoOpenDevice"])
                    nt_symbol = cast(dict[str, object], symbols["ntdll!NtOpenFile"])
                    io_address = _hex_int(io_symbol.get("address"))
                    nt_address = _hex_int(nt_symbol.get("address"))
                    if io_address is None or nt_address is None:
                        raise RuntimeError("required NtOpenFile symbols unresolved")
                    capture = _capture_ntopen(
                        session,
                        module_base=module_base,
                        io_open_address=io_address,
                        nt_open_address=nt_address,
                        variant=variant,
                    )
                    debug_data["ntopenfile"] = capture
                    session.send("g")
                    session.detach()
                    session = None
                    raw = await asyncio.to_thread(run.wait, 80.0)
                    return {
                        "cell": _gate116._safe_result(raw),
                        "raw": raw,
                        "debug": debug_data,
                    }
                except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                    debug_data["error"] = type(error).__name__
                    debug_data["error_detail"] = str(error)[:256]
                    if session is not None:
                        debug_data["cdb_output_preview"] = session.output[-4096:]
                    run.terminate()
                    raw = await asyncio.to_thread(run.wait, 20.0)
                    return {
                        "cell": _gate116._safe_result(raw),
                        "raw": raw,
                        "debug": debug_data,
                    }
                finally:
                    if session is not None:
                        session.detach()

            controls: dict[str, object] = {
                variant: await run_cell(variant, copied_loader, ()) for variant in _VARIANTS
            }
            artifact["controls"] = {
                variant: cast(dict[str, object], controls[variant])["cell"] for variant in _VARIANTS
            }
            control_cells = cast(dict[str, object], artifact["controls"])
            control_expected = (
                cast(dict[str, object], control_cells[_SYN]).get("load") == "FAIL"
                and cast(dict[str, object], control_cells[_SYN]).get("load_error")
                == _gate116._BCRYPT_ERROR_DLL_INIT_FAILED
                and cast(dict[str, object], control_cells[_SYN_WORLD]).get("load") == "PASS"
            )
            if not control_expected:
                artifact["status"] = "W5_GATE118_RESULT_INCONCLUSIVE"
                artifact["cleanup"] = {"host_mutation": False}
                persist()
                return

            debugger_probe = await run_cell(_SYN, copied_loader, (), debug=True)
            debugger_probe_world = await run_cell(_SYN_WORLD, copied_loader, (), debug=True)
            artifact["debugger"] = {
                _SYN: debugger_probe.get("debug", {}),
                _SYN_WORLD: debugger_probe_world.get("debug", {}),
            }
            debug_cells = {
                _SYN: debugger_probe,
                _SYN_WORLD: debugger_probe_world,
            }
            debug_expected = all(
                cast(dict[str, object], debug_cells[variant])["cell"].get("load")
                == cast(dict[str, object], controls[variant])["cell"].get("load")
                and cast(dict[str, object], debug_cells[variant])["cell"].get("load_error")
                == cast(dict[str, object], controls[variant])["cell"].get("load_error")
                for variant in _VARIANTS
            )
            if not debug_expected:
                artifact["status"] = "W5_GATE118_DEBUGGER_PERTURBED_CAUSAL_STATE"
                persist()
                return
            captures: dict[str, dict[str, object]] = {}
            for variant in _VARIANTS:
                debug = cast(dict[str, object], debug_cells[variant])["debug"]
                capture = debug.get("ntopenfile")
                if not isinstance(capture, dict):
                    artifact["status"] = "W5_GATE118_RESULT_INCONCLUSIVE"
                    persist()
                    return
                captures[variant] = capture
            artifact["ntopenfile"] = captures

            syn = captures[_SYN]
            world = captures[_SYN_WORLD]
            syn_attrs = cast(dict[str, object], syn["object_attributes"])
            world_attrs = cast(dict[str, object], world["object_attributes"])
            syn_name = cast(dict[str, object], syn_attrs["object_name"])
            world_name = cast(dict[str, object], world_attrs["object_name"])
            semantic_equal = {
                "object_name": syn_name.get("value") == world_name.get("value"),
                "desired_access": syn.get("desired_access") == world.get("desired_access"),
                "share_access": syn.get("share_access") == world.get("share_access"),
                "open_options": syn.get("open_options") == world.get("open_options"),
                "object_attributes": syn_attrs.get("attributes") == world_attrs.get("attributes"),
                "root_directory": syn_attrs.get("root_directory_value")
                == world_attrs.get("root_directory_value"),
                "unicode_length": syn_name.get("length") == world_name.get("length"),
                "unicode_maximum_length": syn_name.get("maximum_length")
                == world_name.get("maximum_length"),
            }
            artifact["argument_equality"] = {
                "fields": semantic_equal,
                "all_equal": all(semantic_equal.values()),
            }
            if not all(semantic_equal.values()) or syn_attrs.get("root_directory_value"):
                artifact["status"] = "W5_GATE118_RESULT_INCONCLUSIVE"
                persist()
                return
            syn_status = _hex_int(syn.get("ntstatus"))
            world_status = _hex_int(world.get("ntstatus"))
            if syn_status != _STATUS_ACCESS_DENIED or world_status != _STATUS_SUCCESS:
                artifact["status"] = "W5_GATE118_RESULT_INCONCLUSIVE"
                persist()
                return

            probe_arguments = (
                str(syn_name["value"]),
                f"0x{int(syn['desired_access']):x}",
                f"0x{int(syn_attrs['attributes']):x}",
                f"0x{int(syn['share_access']):x}",
                f"0x{int(syn['open_options']):x}",
                f"0x{int(syn_name['length']):x}",
                f"0x{int(syn_name['maximum_length']):x}",
                f"0x{int(syn_attrs['length']):x}",
            )
            direct_cells: dict[str, object] = {}
            for variant in _VARIANTS:
                direct_cells[variant] = await run_cell(
                    variant,
                    copied_probe,
                    probe_arguments,
                )
            direct_projections = {
                variant: _direct_projection(cast(dict[str, object], direct_cells[variant])["raw"])
                for variant in _VARIANTS
            }
            artifact["direct_probe"] = {
                "arguments": {
                    "object_name": syn_name["value"],
                    "desired_access": syn["desired_access"],
                    "attributes": syn_attrs["attributes"],
                    "share_access": syn["share_access"],
                    "open_options": syn["open_options"],
                    "unicode_length": syn_name["length"],
                    "unicode_maximum_length": syn_name["maximum_length"],
                    "object_attributes_length": syn_attrs["length"],
                },
                "same_target": True,
                "same_access_contract": True,
                "variants": direct_projections,
            }
            direct_syn_status = cast(dict[str, object], direct_projections[_SYN]).get("status_int")
            direct_world_status = cast(dict[str, object], direct_projections[_SYN_WORLD]).get(
                "status_int"
            )
            direct_reproduced = (
                direct_syn_status == _STATUS_ACCESS_DENIED
                and direct_world_status == _STATUS_SUCCESS
            )
            artifact["direct_probe"]["differential_reproduced"] = direct_reproduced
            if not direct_reproduced:
                artifact["status"] = "W5_GATE118_RESULT_INCONCLUSIVE"
                persist()
                return

            world_token = cast(dict[str, object], cast(dict[str, object], controls[_SYN_WORLD]))
            token_user = cast(dict[str, object], world_token.get("token_attestation", {})).get(
                "token_user_sid"
            )
            dacl = _query_dacl(
                str(syn_name["value"]),
                token_user=token_user if isinstance(token_user, str) else None,
                synthetic=write_sid.value,
            )
            artifact["dacl"] = dacl
            artifact["cleanup"] = {
                "open_handles_left": False,
                "device_io_control_issued": False,
                "debugger_left": False,
                "debuggee_left": False,
                "broker_left": False,
                "direct_probe_left": False,
                "persistent_debugger_state": False,
                "dump_files": [],
                "dacl_mutation": False,
                "registry_mutation": False,
                "privilege_mutation": False,
                "worker_threads": False,
                "host_mutation": False,
            }
            if dacl.get("available") and dacl.get("synthetic_authority_present"):
                artifact["status"] = "W5_GATE118_NTOPENFILE_TARGET_IDENTIFIED"
            else:
                artifact["status"] = (
                    "W5_GATE118_NTOPENFILE_DIFFERENTIAL_REPRODUCED_TARGET_DACL_UNAVAILABLE"
                )
            persist()
