"""W5 Gate 1.16 evidence for the bcrypt image-entry initialization boundary.

This gate never changes the production sandbox.  It launches the same
evidence token broker used by the earlier W5 gates, pauses an already-created
restricted child on an inherited stdin pipe, and only then attempts to attach
an existing Microsoft CDB debugger.  If a trusted CDB is not already present
on the runner, the synchronized causal controls still run and the gate
records ``W5_GATE116_MICROSOFT_DEBUGGER_UNAVAILABLE`` without downloading or
installing a debugger.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from tests.security.test_windows_native_runtime_acceptance import _compile_msvc_probe
from tests.security.test_windows_native_workload_compatibility import (
    _preview,
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import _production_source_diff
from tests.security.test_windows_w5_gate1_7_token_ablation import _source_path
from tests.security.test_windows_w5_gate1_11_sid_ablation import (
    _compile_broker,
    _remove_directory,
)
from tests.security.test_windows_w5_gate1_14_5_procmon_provenance import (
    _discover_signtool,
    _pe_machine,
    _powershell_diagnostics,
    _powershell_version,
    _run_signtool,
    _winverifytrust,
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

_BASE = "c24f3a3034991474dcbcfbe366a7c6853368663f"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_BASELINE_RUN = 31974572408
_BASELINE_ATTEMPT = 2
_BASELINE_MACOS_JOB = 95267953337
_SYN = "SYN"
_SYN_WORLD = "SYN_WORLD"
_VARIANTS = (_SYN, _SYN_WORLD)
_BCRYPT_ERROR_DLL_INIT_FAILED = 1114
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_TRACE_ITEMS = 128
_WAIT_TIMEOUT_MS = 70_000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_PIPE_RELEASE_BYTE = b"R"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER_INFORMATION = 1
_TOKEN_RESTRICTED_SIDS_INFORMATION = 11
_MARKER_RE = re.compile(r"^W5_GATE116_[A-Z0-9_]+(?:=.*)?$")
_PROMPT_RE = re.compile(r"(?m)(?:^|[^\w])\d+:\d+>\s*$")
_MODULE_LOAD_RE = re.compile(r"(?im)\b(?:modload|load)\s*:.*\\bcrypt\.dll\b")
_MODULE_LINE_RE = re.compile(r"(?im)^\s*([0-9a-f`]+)\s+([0-9a-f`]+)\s+.*\bbcrypt(?:\.dll)?\b")
_TRACE_TOKEN_RE = re.compile(
    r"(?i)\b([a-z0-9_.-]+)!([a-z0-9_$<>~.?-]+)\b|"
    r"\b([a-z0-9_.-]+)\+0x([0-9a-f`]+)\b"
)
_BROKER_MARKER_RE = re.compile(r"^W5_GATE111_[A-Z0-9_]+(?:=.*)?$")


def _parse_markers(output: bytes) -> dict[str, str]:
    text = output.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not _MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        markers[key] = value[:512] if separator else "OBSERVED"
    return markers


def _marker_int(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _parse_broker_markers(output: bytes) -> dict[str, str]:
    text = output.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not _BROKER_MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        markers[key] = value[:512] if separator else "OBSERVED"
    return markers


def _loader_result(run: dict[str, object]) -> dict[str, object]:
    captured = run.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    broker_markers = _parse_broker_markers(output)
    broker_child_exit = _marker_int(broker_markers, "W5_GATE111_CHILD_EXIT")
    return {
        "started": "W5_GATE116_STARTED" in markers,
        "ready": "W5_GATE116_READY" in markers,
        "released": markers.get("W5_GATE116_RELEASE"),
        "preloaded": markers.get("W5_GATE116_PRELOADED"),
        "preloaded_invalid": markers.get("W5_GATE116_PRELOADED_INVALID"),
        "load": markers.get("W5_GATE116_LOAD"),
        "load_error": _marker_int(markers, "W5_GATE116_LOAD_ERROR"),
        "handle": markers.get("W5_GATE116_HANDLE"),
        "free": markers.get("W5_GATE116_FREE"),
        "finished": "W5_GATE116_FINISHED" in markers,
        "pid": _marker_int(markers, "W5_GATE116_PID"),
        "child_exit": (
            broker_child_exit if broker_child_exit is not None else run.get("child_exit")
        ),
        "outer_exit_code": run.get("exit_code"),
        "spawn_result": run.get("spawn_result"),
        "timeout": run.get("timeout"),
        "worker_alive": run.get("worker_alive"),
        "stderr_preview": str(run.get("stderr_preview") or "")[:512],
        "token_attestation": {
            "token_user_sid": broker_markers.get("W5_GATE111_TOKEN_USER_SID"),
            "restricted_sid_count": _marker_int(broker_markers, "W5_GATE111_RESTRICTED_SID_COUNT"),
            "restricted_sid_match": broker_markers.get("W5_GATE111_RESTRICTED_SID_MATCH"),
            "token_restricted": broker_markers.get("W5_GATE111_TOKEN_RESTRICTED"),
            "token_user_match": broker_markers.get("W5_GATE111_TOKEN_USER_MATCH"),
            "token_inspection": broker_markers.get("W5_GATE111_TOKEN_INSPECTION"),
            "token_privileges": broker_markers.get("W5_GATE111_TOKEN_PRIVILEGES"),
            "unexpected_enabled_privileges": _marker_int(
                broker_markers, "W5_GATE111_UNEXPECTED_ENABLED_PRIVILEGES"
            ),
            "se_change_notify": broker_markers.get("W5_GATE111_SE_CHANGE_NOTIFY"),
        },
        "stdout_preview": _preview(
            "\n".join(f"{key}={value}" for key, value in sorted(markers.items())).encode()
        )[:2048],
        "markers": markers,
    }


def _attest_process_token(
    pid: int, *, expected_synthetic_sid: str
) -> dict[str, object]:  # pragma: no cover - Windows CI
    """Read non-secret token identity facts while the final child is paused."""

    if os.name != "nt" or pid <= 0:
        return {"available": False, "error": "WINDOWS_ONLY_OR_INVALID_PID"}
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return {"available": False, "error": "WIN_DLL_UNAVAILABLE"}
    try:
        kernel32 = cast(Any, loader("kernel32.dll", use_last_error=True))
        advapi32 = cast(Any, loader("advapi32.dll", use_last_error=True))
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int32
        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        open_token = advapi32.OpenProcessToken
        open_token.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        open_token.restype = ctypes.c_int32
        get_information = advapi32.GetTokenInformation
        get_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_information.restype = ctypes.c_int32
        is_restricted = advapi32.IsTokenRestricted
        is_restricted.argtypes = [ctypes.c_void_p]
        is_restricted.restype = ctypes.c_int32
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        convert_sid.restype = ctypes.c_int32
    except (AttributeError, OSError) as error:
        return {"available": False, "error": type(error).__name__}

    def last_error() -> int:
        getter = getattr(ctypes, "get_last_error", None)
        return int(getter()) if getter is not None else 0

    def read_information(token: int, kind: int) -> ctypes.Array[ctypes.c_char]:
        required = ctypes.c_uint32()
        if (
            get_information(ctypes.c_void_p(token), kind, None, 0, ctypes.byref(required))
            or required.value == 0
        ):
            error = last_error()
            if error != 122:  # ERROR_INSUFFICIENT_BUFFER
                raise OSError(error, "GetTokenInformation(size) failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not get_information(
            ctypes.c_void_p(token),
            kind,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise OSError(last_error(), "GetTokenInformation failed")
        return buffer

    def sid_pointer(buffer: ctypes.Array[ctypes.c_char], offset: int) -> int:
        pointer = ctypes.cast(
            ctypes.byref(buffer, offset), ctypes.POINTER(ctypes.c_void_p)
        ).contents.value
        if not pointer:
            raise OSError(0, "token SID pointer unavailable")
        return int(pointer)

    def sid_text(pointer: int) -> str:
        text = ctypes.c_wchar_p()
        if not convert_sid(ctypes.c_void_p(pointer), ctypes.byref(text)) or not text.value:
            raise OSError(last_error(), "ConvertSidToStringSidW failed")
        value = str(text.value)
        local_free(ctypes.cast(text, ctypes.c_void_p))
        return value

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", ctypes.c_uint32)]

    process = int(
        cast(
            int,
            open_process(
                _PROCESS_QUERY_LIMITED_INFORMATION,
                0,
                ctypes.c_uint32(pid),
            ),
        )
        or 0
    )
    if not process:
        return {"available": False, "error": f"OpenProcess:{last_error()}"}
    token_value = ctypes.c_void_p()
    try:
        if (
            not open_token(ctypes.c_void_p(process), _TOKEN_QUERY, ctypes.byref(token_value))
            or not token_value.value
        ):
            return {"available": False, "error": f"OpenProcessToken:{last_error()}"}
        token = int(token_value.value)
        user_buffer = read_information(token, _TOKEN_USER_INFORMATION)
        user_sid = sid_text(sid_pointer(user_buffer, 0))
        restricted_buffer = read_information(token, _TOKEN_RESTRICTED_SIDS_INFORMATION)
        count = ctypes.c_uint32.from_buffer(restricted_buffer).value
        alignment = ctypes.alignment(ctypes.c_void_p)
        offset = (ctypes.sizeof(ctypes.c_uint32) + alignment - 1) // alignment * alignment
        stride = ctypes.sizeof(_SidAndAttributes)
        restricted_sids = [
            sid_text(sid_pointer(restricted_buffer, offset + index * stride))
            for index in range(min(count, 32))
        ]
        return {
            "available": True,
            "token_user_sid": user_sid,
            "is_token_restricted": bool(is_restricted(ctypes.c_void_p(token))),
            "restricted_sid_count": int(count),
            "restricted_sids": restricted_sids,
            "expected_synthetic_sid": expected_synthetic_sid,
            "restricted_sid_exact_singleton": restricted_sids == [expected_synthetic_sid],
        }
    except (OSError, ValueError, ctypes.ArgumentError) as error:
        return {"available": False, "error": type(error).__name__}
    finally:
        if token_value.value:
            close_handle(token_value)
        close_handle(ctypes.c_void_p(process))


def _pe_metadata(path: Path) -> dict[str, object]:
    """Read bounded PE entrypoint/import facts without loading the image."""

    data = path.read_bytes()
    result: dict[str, object] = {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "file_size": len(data),
        "file_version": _powershell_version(path),
        "machine": None,
        "machine_value": None,
        "image_base_preference": None,
        "address_of_entrypoint_rva": None,
        "import_dlls": [],
        "import_table_fingerprint": None,
    }
    machine_name, machine_value = _pe_machine(path)
    result["machine"] = machine_name
    result["machine_value"] = machine_value
    if len(data) < 0x40 or data[:2] != b"MZ":
        return result
    pe_offset = int.from_bytes(data[0x3C:0x40], "little")
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return result
    section_count = int.from_bytes(data[pe_offset + 6 : pe_offset + 8], "little")
    optional_size = int.from_bytes(data[pe_offset + 20 : pe_offset + 22], "little")
    optional = pe_offset + 24
    if optional + optional_size > len(data):
        return result
    magic = int.from_bytes(data[optional : optional + 2], "little")
    entrypoint = int.from_bytes(data[optional + 16 : optional + 20], "little")
    if magic == 0x20B:
        image_base = int.from_bytes(data[optional + 24 : optional + 32], "little")
        data_directory = optional + 112
    else:
        image_base = int.from_bytes(data[optional + 28 : optional + 32], "little")
        data_directory = optional + 96
    result["image_base_preference"] = f"0x{image_base:x}"
    result["address_of_entrypoint_rva"] = f"0x{entrypoint:x}"
    sections = []
    section_offset = optional + optional_size
    for index in range(section_count):
        offset = section_offset + index * 40
        if offset + 40 > len(data):
            break
        virtual_size = int.from_bytes(data[offset + 8 : offset + 12], "little")
        virtual_address = int.from_bytes(data[offset + 12 : offset + 16], "little")
        raw_size = int.from_bytes(data[offset + 16 : offset + 20], "little")
        raw_offset = int.from_bytes(data[offset + 20 : offset + 24], "little")
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset))

    def rva_to_offset(rva: int) -> int | None:
        for virtual_address, size, raw_offset in sections:
            if virtual_address <= rva < virtual_address + size:
                return raw_offset + (rva - virtual_address)
        return None

    if data_directory + 16 <= len(data):
        import_rva = int.from_bytes(data[data_directory + 8 : data_directory + 12], "little")
        import_offset = rva_to_offset(import_rva)
        names: list[str] = []
        if import_offset is not None:
            for index in range(256):
                descriptor = import_offset + index * 20
                if descriptor + 20 > len(data):
                    break
                name_rva = int.from_bytes(data[descriptor + 12 : descriptor + 16], "little")
                if name_rva == 0:
                    break
                name_offset = rva_to_offset(name_rva)
                if name_offset is None:
                    break
                end = data.find(b"\0", name_offset, min(len(data), name_offset + 256))
                if end < 0:
                    break
                names.append(data[name_offset:end].decode("ascii", errors="replace"))
        result["import_dlls"] = names[:64]
        result["import_table_fingerprint"] = hashlib.sha256(
            "\n".join(names[:64]).encode("ascii", errors="replace")
        ).hexdigest()
    return result


def _normalize_trace(text: str) -> list[str]:
    sequence: list[str] = []
    for match in _TRACE_TOKEN_RE.finditer(text):
        if match.group(1):
            token = match.group(1).casefold() + "!" + match.group(2).casefold()
        else:
            token = match.group(3).casefold() + "+0x" + match.group(4).replace("`", "")
        if not sequence or sequence[-1] != token:
            sequence.append(token)
        if len(sequence) >= _MAX_TRACE_ITEMS:
            break
    return sequence


def _first_trace_difference(syn: list[str], world: list[str]) -> dict[str, object] | None:
    limit = min(len(syn), len(world))
    for index in range(limit):
        if syn[index] != world[index]:
            return {
                "index": index,
                "depth": index,
                "syn": syn[index],
                "syn_result": None,
                "syn_world": world[index],
                "syn_world_result": None,
            }
    if len(syn) != len(world):
        return {
            "index": limit,
            "depth": limit,
            "syn": syn[limit] if len(syn) > limit else None,
            "syn_result": None,
            "syn_world": world[limit] if len(world) > limit else None,
            "syn_world_result": None,
        }
    return None


class _SynchronizedRun:
    """Own one CreateProcessWithLogonW process and an inherited stdin pipe."""

    def __init__(
        self,
        harness: _Gate1DirectProcess,
        process_handle: int,
        stdin_write: int,
        stdout_read: int,
        stderr_read: int,
        result: dict[str, object],
    ) -> None:  # pragma: no cover - Windows CI
        self.harness = harness
        self.process_handle = process_handle
        self.stdin_write = stdin_write
        self.stdout_read = stdout_read
        self.stderr_read = stderr_read
        self.result = result
        self.stdout = bytearray()
        self.stderr = bytearray()
        self._observed = bytearray()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self._reader_threads: list[threading.Thread] = []
        self.broker_pid: int | None = None
        self.p4_pid: int | None = None
        self.cdb_pid: int | None = None

    def observe_spawn(self, process_handle: int) -> None:
        self.broker_pid = self.harness.process_id(process_handle)

    def observe_output(self, stream: str, chunk: bytes) -> None:
        target = self.stdout if stream == "stdout" else self.stderr
        if len(target) < _MAX_OUTPUT_BYTES:
            target.extend(chunk[: _MAX_OUTPUT_BYTES - len(target)])
        if stream != "stdout":
            return
        with self._lock:
            self._observed.extend(chunk[:8192])
            text = self._observed.decode("utf-8", errors="replace").replace("\r", "")
            for line in text.splitlines():
                if line.startswith("W5_GATE116_PID="):
                    with contextlib.suppress(ValueError):
                        self.p4_pid = int(line.partition("=")[2], 0)
            if "W5_GATE116_READY=OBSERVED" in text and self.p4_pid:
                self._ready.set()

    def wait_ready(self, timeout: float = 20.0) -> bool:
        return self._ready.wait(timeout)

    def release(self) -> bool:
        written = ctypes.c_uint32()
        payload = ctypes.create_string_buffer(_PIPE_RELEASE_BYTE)
        write_file = self.harness._kernel32.WriteFile
        write_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        write_file.restype = ctypes.c_int32
        ok = (
            bool(
                write_file(
                    ctypes.c_void_p(self.stdin_write),
                    payload,
                    1,
                    ctypes.byref(written),
                    None,
                )
            )
            and written.value == 1
        )
        self.result["release_write"] = "PASS" if ok else "FAIL"
        return ok

    def terminate(self) -> None:
        with contextlib.suppress(Exception):
            self.harness._terminate(ctypes.c_void_p(self.process_handle), 0xC000013A)

    def _drain(self, stream: str, handle: int) -> None:
        buffer = ctypes.create_string_buffer(65_536)
        while True:
            returned = ctypes.c_uint32()
            ok = self.harness._read_file(
                ctypes.c_void_p(handle),
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(returned),
                None,
            )
            if not ok or returned.value == 0:
                return
            self.observe_output(stream, bytes(buffer.raw[: returned.value]))

    def start_io(self) -> None:
        self._reader_threads = [
            threading.Thread(
                target=self._drain,
                args=("stdout", self.stdout_read),
                daemon=True,
                name="W5-Gate116-Stdout",
            ),
            threading.Thread(
                target=self._drain,
                args=("stderr", self.stderr_read),
                daemon=True,
                name="W5-Gate116-Stderr",
            ),
        ]
        for reader in self._reader_threads:
            reader.start()
        threading.Thread(target=self._finish, daemon=True, name="W5-Gate116-Wait").start()

    def _finish(self) -> None:
        wait_result = int(
            self.harness._wait(ctypes.c_void_p(self.process_handle), _WAIT_TIMEOUT_MS)
        )
        if wait_result == _WAIT_TIMEOUT:
            self.result["timeout"] = True
            self.terminate()
            self.harness._wait(ctypes.c_void_p(self.process_handle), 2_000)
        elif wait_result != _WAIT_OBJECT_0:
            self.result["wait_failed"] = wait_result
        else:
            exit_code = ctypes.c_uint32()
            if self.harness._get_exit_code(
                ctypes.c_void_p(self.process_handle), ctypes.byref(exit_code)
            ):
                self.result["exit_code"] = int(exit_code.value)
        for reader in self._reader_threads:
            reader.join(timeout=2.0)
        self.harness._close_handle(self.process_handle)
        self.harness._close_handle(self.stdin_write)
        self.harness._close_handle(self.stdout_read)
        self.harness._close_handle(self.stderr_read)
        self.result["_captured_stdout"] = bytes(self.stdout)
        self.result["_captured_stderr"] = bytes(self.stderr)
        self.result["stdout_preview"] = _preview(bytes(self.stdout))
        self.result["stderr_preview"] = _preview(bytes(self.stderr))
        self.result["broker_pid"] = self.broker_pid
        self.result["p4_pid"] = self.p4_pid
        self.result["worker_alive"] = any(reader.is_alive() for reader in self._reader_threads)
        self.result["worker_terminal"] = not bool(self.result["worker_alive"])
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(self.result)

    def wait(self, timeout: float = 80.0) -> dict[str, object]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            self.terminate()
            try:
                return self._queue.get(timeout=10.0)
            except queue.Empty:
                return {
                    **self.result,
                    "timeout": True,
                    "worker_alive": True,
                    "worker_terminal": False,
                    "classification": "SYNCHRONIZED_RUN_TIMEOUT",
                }


class _SynchronizedHarness(_Gate1DirectProcess):
    """Evidence-only CreateProcessWithLogonW harness with a pipe stdin."""

    def _stdin_pipe(self) -> tuple[int, int]:  # pragma: no cover - Windows CI
        read_handle, write_handle = self._new_pipe()
        if not self._set_handle_information(
            ctypes.c_void_p(read_handle), _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT
        ):
            self._close_handle(read_handle)
            self._close_handle(write_handle)
            raise OSError(self._last_error(), "SetHandleInformation(stdin read) failed")
        if not self._set_handle_information(ctypes.c_void_p(write_handle), _HANDLE_FLAG_INHERIT, 0):
            self._close_handle(read_handle)
            self._close_handle(write_handle)
            raise OSError(self._last_error(), "SetHandleInformation(stdin write) failed")
        return read_handle, write_handle

    def start(
        self,
        *,
        username: str,
        password: str,
        executable: Path,
        arguments: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
    ) -> _SynchronizedRun:  # pragma: no cover - Windows CI
        stdin_read, stdin_write = self._stdin_pipe()
        stdout_read, stdout_write = self._new_pipe()
        stderr_read, stderr_write = self._new_pipe()
        process_info = self._process_information_type()
        command = subprocess.list2cmdline([str(executable), *arguments])
        mutable_command = ctypes.create_unicode_buffer(command)
        environment_block = self._environment_block_type(environment)
        startup = self._startup_info_type()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = _STARTF_USESTDHANDLES
        startup.hStdInput = ctypes.c_void_p(stdin_read)
        startup.hStdOutput = ctypes.c_void_p(stdout_write)
        startup.hStdError = ctypes.c_void_p(stderr_write)
        result: dict[str, object] = {
            "execution_path": "DIRECT/CreateProcessWithLogonW",
            "spawn_result": "NOT_STARTED",
            "exit_code": None,
            "timeout": False,
            "worker_alive": False,
            "worker_terminal": False,
        }
        try:
            created = self._create(
                username,
                ".",
                password,
                0,
                str(executable),
                mutable_command,
                _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
                ctypes.cast(environment_block, ctypes.c_void_p),
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            )
            if not created or not process_info.hProcess:
                raise OSError(self._last_error(), "CreateProcessWithLogonW failed")
            process_handle = int(cast(int, process_info.hProcess))
            run = _SynchronizedRun(
                self,
                process_handle,
                stdin_write,
                stdout_read,
                stderr_read,
                result,
            )
            result["spawn_result"] = "PASS"
            run.observe_spawn(process_handle)
            self._close_handle(int(cast(int, process_info.hThread)))
            self._close_handle(stdin_read)
            self._close_handle(stdout_write)
            self._close_handle(stderr_write)
            run.start_io()
            return run
        except BaseException:
            self._close_handle(stdin_read)
            self._close_handle(stdin_write)
            self._close_handle(stdout_read)
            self._close_handle(stdout_write)
            self._close_handle(stderr_read)
            self._close_handle(stderr_write)
            raise

    # These indirections keep the evidence harness compatible with the
    # private Win32 structure definitions used by the earlier gate without
    # changing that gate's source.
    @staticmethod
    def _process_information_type() -> Any:
        from tests.security.test_windows_w5_gate1_runtime_root_cause import _ProcessInformation

        return _ProcessInformation()

    @staticmethod
    def _startup_info_type() -> Any:
        from tests.security.test_windows_w5_gate1_runtime_root_cause import _StartupInfoW

        return _StartupInfoW()

    @staticmethod
    def _environment_block_type(environment: dict[str, str]) -> Any:
        from tests.security.test_windows_w5_gate1_runtime_root_cause import _environment_block

        return _environment_block(environment)


class _CdbSession:
    """Small bounded interactive CDB driver; no dumps or persistent state."""

    def __init__(self, executable: Path, pid: int) -> None:  # pragma: no cover - Windows CI
        self.process = subprocess.Popen(
            [str(executable), "-p", str(pid), "-lines"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self._all = ""
        self._reader = threading.Thread(target=self._read, daemon=True, name="W5-Gate116-CDB")
        self._reader.start()

    def _read(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        while True:
            chunk = stream.read(1)
            if chunk == "":
                return
            self._all = (self._all + chunk)[-64 * 1024 :]
            with contextlib.suppress(queue.Full):
                self._lines.put_nowait(chunk)

    def send(self, command: str) -> None:
        if self.process.stdin is None:
            raise RuntimeError("CDB stdin unavailable")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def command(self, command: str, marker: str, timeout: float) -> str:
        """Run one non-continuing command and wait for a unique echo marker."""

        self.send(f"{command}; .echo {marker}")
        return self.wait_for(lambda text: marker in text, timeout)

    def wait_for(self, predicate: Callable[[str], bool], timeout: float) -> str:
        collected: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CDB command timeout")
            try:
                line = self._lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            collected.append(line)
            if predicate("".join(collected)):
                return "".join(collected)[-32 * 1024 :]

    def detach(self) -> None:
        if self.process.poll() is None:
            with contextlib.suppress(Exception):
                self.send("qd")
            try:
                self.process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                with contextlib.suppress(Exception):
                    self.process.terminate()
                with contextlib.suppress(Exception):
                    self.process.wait(timeout=5)

    @property
    def output(self) -> str:
        return self._all[-32 * 1024 :]


def _discover_cdb() -> dict[str, object]:  # pragma: no cover - Windows CI
    candidates: list[Path] = []
    found = shutil.which("cdb.exe")
    if found:
        candidates.append(Path(found))
    for raw_root in (
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
    ):
        if raw_root:
            root = Path(raw_root) / "Windows Kits" / "10" / "Debuggers"
            candidates.extend(root.glob("*/cdb.exe"))
            candidates.append(root / "cdb.exe")
    unique = {path.resolve() for path in candidates if path.is_file()}
    host_machine = platform.machine().casefold()
    preferred_architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "ARM64",
        "aarch64": "ARM64",
        "x86": "x86",
        "i386": "x86",
    }.get(host_machine)

    def candidate_key(path: Path) -> tuple[int, str]:
        architecture = _pe_machine(path)[0]
        preferred = 0 if architecture == preferred_architecture else 1
        return preferred, str(path)

    ordered = sorted(unique, key=candidate_key)
    inspected: list[dict[str, object]] = []
    signtool = _discover_signtool()
    for candidate in ordered:
        trust = _winverifytrust(candidate)
        diagnostics = _powershell_diagnostics(candidate)
        diagnostic = cast(dict[str, object], diagnostics.get("diagnostic", {}))
        signtool_result = (
            _run_signtool(signtool, candidate)
            if signtool is not None
            else {"available": False, "status": "SIGNTOOL_UNAVAILABLE"}
        )
        signer_text = json.dumps(diagnostic.get("SignerCertificate"), sort_keys=True)
        company = str(diagnostic.get("CompanyName") or "")
        tool_output = str(signtool_result.get("output_preview") or "")
        identity = "microsoft" in (signer_text + company + tool_output).casefold()
        trust_ok = trust.get("result_decimal") == 0
        powershell_status = str(diagnostic.get("Status") or "").casefold()
        # WinVerifyTrust is the authoritative native check.  Windows runner
        # images sometimes cannot load Microsoft.PowerShell.Security, in
        # which case Get-AuthenticodeSignature reports UNAVAILABLE even for
        # a file whose native trust and signtool verification both succeed.
        powershell_ok = powershell_status in {"", "valid", "unavailable"}
        signtool_ok = not bool(signtool_result.get("available")) or bool(
            signtool_result.get("succeeds")
        )
        metadata = {
            "path": str(candidate),
            "version": _powershell_version(candidate),
            "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "pe_architecture": _pe_machine(candidate)[0],
            "winverifytrust": trust,
            "powershell": diagnostics,
            "signtool": signtool_result,
            "microsoft_identity": identity,
            "provenance_verified": bool(trust_ok and powershell_ok and signtool_ok and identity),
        }
        inspected.append(metadata)
        if metadata["provenance_verified"]:
            return {"available": True, "selected": metadata, "candidates": inspected}
    return {
        "available": False,
        "status": "W5_GATE116_MICROSOFT_DEBUGGER_UNAVAILABLE",
        "candidates": inspected,
    }


def _safe_result(run: dict[str, object]) -> dict[str, object]:
    projected = _loader_result(run)
    projected.pop("markers", None)
    return projected


def _causal_observation(cell: object) -> tuple[object, ...]:
    if not isinstance(cell, dict):
        return (None,) * 8
    return tuple(
        cell.get(key)
        for key in (
            "started",
            "ready",
            "released",
            "preloaded",
            "load",
            "load_error",
            "finished",
            "timeout",
        )
    )


class WindowsW5Gate116BcryptEntryTraceTests(unittest.IsolatedAsyncioTestCase):
    """Run the focused Gate 1.16 evidence experiment exactly once."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.16 is CI-only")
    async def test_gate116_bcrypt_entry_trace(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 Gate 1.16 requires elevation")
        self.assertEqual(_production_source_diff(), ())

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE116_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_16",
            "base": _BASE,
            "main": _MAIN,
            "status": "RUNNING",
            "production_source_diff": (),
            "prior_gate_1_15": {
                "direct_eventregister_world_dependency": "NOT_SUPPORTED",
                "direct_provider_traits_world_dependency": "NOT_SUPPORTED",
                "dll_boundary": "BCRYPT_OWN_INITIALIZATION_PATH_REMAINS_PRIMARY_BOUNDARY",
            },
            "baseline": {
                "run": _BASELINE_RUN,
                "attempt": _BASELINE_ATTEMPT,
                "macos_job": _BASELINE_MACOS_JOB,
                "result": "GATE115_MACOS_ACP_TRANSIENT_NOT_REPRODUCED",
            },
            "sync_controls": {},
            "debugger": {},
            "trace_syn": {},
            "trace_syn_world": {},
            "earliest_differential": None,
            "focused_refinement": {"performed": False},
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
            _compile_msvc_probe,
            _source_path("windows_w5_gate1_16_sync_loader.c"),
            "windows_w5_gate116_sync_loader",
            libraries=(),
        )
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, loader.parent)

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
            copied_loader = workspace / "gate116-loader.exe"
            copied_broker = workspace / "gate116-token-broker.exe"
            shutil.copy2(loader, copied_loader)
            shutil.copy2(broker, copied_broker)
            write_sid = record.write_sid
            harness = _SynchronizedHarness()

            async def run_cell(
                variant: str,
                *,
                debugger: dict[str, object] | None = None,
                focused_target: str | None = None,
            ) -> dict[str, object]:
                broker_arguments = (
                    variant,
                    write_sid.value,
                    str(copied_loader),
                    str(workspace),
                )
                spec = _Workload("GATE116", variant.casefold(), copied_broker, broker_arguments)
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
                cdb_session: _CdbSession | None = None
                debug_metadata: dict[str, object] = {}
                try:
                    if not ready or run.p4_pid is None:
                        run.terminate()
                        result = await asyncio.to_thread(run.wait, 15.0)
                        return {"cell": _safe_result(result), "debug": debug_metadata}
                    if debugger is not None:
                        cdb_path = Path(str(debugger["path"]))
                        cdb_session = _CdbSession(cdb_path, run.p4_pid)
                        run.cdb_pid = cdb_session.process.pid
                        initial = cdb_session.wait_for(
                            lambda text: bool(_PROMPT_RE.search(text)), 15.0
                        )
                        cdb_session.command("sxe ld:bcrypt.dll", "W5_GATE116_SXE_READY", 5.0)
                        cdb_session.send(".echo W5_GATE116_DEBUGGER_READY")
                        cdb_session.wait_for(lambda text: "W5_GATE116_DEBUGGER_READY" in text, 5.0)
                        cdb_session.send("g")
                        debug_metadata["initial_output_preview"] = initial[-2048:]
                        debug_metadata["attached"] = True
                        debug_metadata["cdb_pid"] = run.cdb_pid
                        debug_metadata["post_attach_token_attestation"] = _attest_process_token(
                            run.p4_pid,
                            expected_synthetic_sid=write_sid.value,
                        )
                        released = run.release()
                        debug_metadata["release_after_attach"] = released
                        module_load = cdb_session.wait_for(
                            lambda text: bool(_MODULE_LOAD_RE.search(text)), 20.0
                        )
                        module_listing = cdb_session.command(
                            "lm m bcrypt", "W5_GATE116_LM_DONE", 5.0
                        )
                        match = _MODULE_LINE_RE.search(module_listing)
                        module_base = int(match.group(1).replace("`", ""), 16) if match else None
                        pe = _pe_metadata(
                            Path(os.environ["SYSTEMROOT"]) / "System32" / "bcrypt.dll"
                        )
                        entry_rva_text = pe.get("address_of_entrypoint_rva")
                        entry_rva = (
                            int(str(entry_rva_text), 16)
                            if isinstance(entry_rva_text, str)
                            else None
                        )
                        entry_va = module_base + entry_rva if module_base and entry_rva else None
                        debug_metadata.update(
                            {
                                "module_load_observed": bool(_MODULE_LOAD_RE.search(module_load)),
                                "module_base": f"0x{module_base:x}" if module_base else None,
                                "entrypoint_rva": entry_rva_text,
                                "entrypoint_va": f"0x{entry_va:x}" if entry_va else None,
                                "pe": pe,
                            }
                        )
                        if entry_va is not None:
                            cdb_session.command(
                                f"bp /1 0x{entry_va:x}", "W5_GATE116_ENTRY_BP_SET", 5.0
                            )
                            cdb_session.send("g")
                            breakpoint_output = cdb_session.wait_for(
                                lambda text: "breakpoint" in text.casefold(), 20.0
                            )
                            debug_metadata["entrypoint_breakpoint_hit"] = (
                                "breakpoint" in breakpoint_output.casefold()
                            )
                            if focused_target and "!" in focused_target:
                                debug_metadata["focused_target"] = focused_target
                                try:
                                    cdb_session.command(
                                        f"bp /1 {focused_target}",
                                        "W5_GATE116_FOCUSED_BP_SET",
                                        5.0,
                                    )
                                    cdb_session.send("g")
                                    focused_output = cdb_session.wait_for(
                                        lambda text: "breakpoint" in text.casefold(), 15.0
                                    )
                                    debug_metadata["focused_breakpoint_hit"] = True
                                    debug_metadata["focused_output_preview"] = focused_output[
                                        -4096:
                                    ]
                                    cdb_session.send("g")
                                except (OSError, RuntimeError, TimeoutError):
                                    debug_metadata["focused_breakpoint_hit"] = False
                            cdb_session.send(
                                ".echo W5_GATE116_WT_BEGIN; wt -l 2 -m bcrypt.dll; "
                                ".echo W5_GATE116_WT_DONE; r rax"
                            )
                            trace_output = cdb_session.wait_for(
                                lambda text: "W5_GATE116_WT_DONE" in text, 25.0
                            )
                            debug_metadata["trace_output"] = trace_output[-32 * 1024 :]
                            debug_metadata["trace_sequence"] = _normalize_trace(trace_output)
                        cdb_session.send("g")
                    else:
                        debug_metadata["post_attach_token_attestation"] = _attest_process_token(
                            run.p4_pid,
                            expected_synthetic_sid=write_sid.value,
                        )
                        if not run.release():
                            debug_metadata["release_after_attach"] = False
                    result = await asyncio.to_thread(run.wait, 80.0)
                    return {
                        "cell": _safe_result(result),
                        "debug": debug_metadata,
                        "p4_pid": run.p4_pid,
                        "broker_pid": run.broker_pid,
                    }
                except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                    debug_metadata["error"] = type(error).__name__
                    if cdb_session is not None:
                        debug_metadata["cdb_returncode"] = cdb_session.process.poll()
                        debug_metadata["cdb_output_preview"] = cdb_session.output[-4096:]
                    run.terminate()
                    result = await asyncio.to_thread(run.wait, 15.0)
                    return {
                        "cell": _safe_result(result),
                        "debug": debug_metadata,
                        "p4_pid": run.p4_pid,
                        "broker_pid": run.broker_pid,
                    }
                finally:
                    if cdb_session is not None:
                        cdb_session.detach()

            controls: dict[str, object] = {}
            for variant in _VARIANTS:
                controls[variant] = await run_cell(variant)
            artifact["sync_controls"] = controls
            control_loads = {
                variant: cast(
                    dict[str, object], cast(dict[str, object], controls[variant])["cell"]
                ).get("load")
                for variant in _VARIANTS
            }
            expected_controls = control_loads == {_SYN: "FAIL", _SYN_WORLD: "PASS"}
            control_errors = {
                variant: cast(
                    dict[str, object], cast(dict[str, object], controls[variant])["cell"]
                ).get("load_error")
                for variant in _VARIANTS
            }
            expected_controls = (
                expected_controls and control_errors[_SYN] == _BCRYPT_ERROR_DLL_INIT_FAILED
            )
            expected_controls = expected_controls and all(
                _causal_observation(
                    cast(dict[str, object], cast(dict[str, object], controls[variant])["cell"])
                )[:4]
                == (True, True, "PASS", "NO")
                and _causal_observation(
                    cast(dict[str, object], cast(dict[str, object], controls[variant])["cell"])
                )[6]
                is True
                and _causal_observation(
                    cast(dict[str, object], cast(dict[str, object], controls[variant])["cell"])
                )[7]
                is False
                for variant in _VARIANTS
            )
            if not expected_controls:
                artifact["status"] = "W5_GATE116_RESULT_INCONCLUSIVE"
                artifact["cleanup"] = {"debugger_processes_left": False}
                persist()
                return

            debugger = await asyncio.to_thread(_discover_cdb)
            artifact["debugger"] = debugger
            if not bool(debugger.get("available")):
                artifact["status"] = "W5_GATE116_MICROSOFT_DEBUGGER_UNAVAILABLE"
                artifact["cleanup"] = {
                    "debugger_processes_left": False,
                    "debuggee_left": False,
                    "persistent_debugger_state": False,
                    "dump_files": [],
                    "worker_threads": False,
                    "host_mutation": False,
                }
                persist()
                return

            selected = cast(dict[str, object], debugger["selected"])
            debug_cells: dict[str, object] = {}
            for variant in _VARIANTS:
                debug_cells[variant] = await run_cell(variant, debugger=selected)
            artifact["debug_controls"] = debug_cells
            debug_errors = {
                variant: cast(
                    dict[str, object], cast(dict[str, object], debug_cells[variant])["cell"]
                ).get("load_error")
                for variant in _VARIANTS
            }
            if (
                any(
                    _causal_observation(
                        cast(
                            dict[str, object], cast(dict[str, object], debug_cells[variant])["cell"]
                        )
                    )
                    != _causal_observation(
                        cast(dict[str, object], cast(dict[str, object], controls[variant])["cell"])
                    )
                    for variant in _VARIANTS
                )
                or debug_errors != control_errors
            ):
                artifact["status"] = "W5_GATE116_DEBUGGER_PERTURBED_CAUSAL_STATE"
                persist()
                return
            for variant, value in debug_cells.items():
                debug = cast(dict[str, object], value).get("debug", {})
                debug = cast(dict[str, object], debug)
                trace = {
                    "entrypoint_hit": debug.get("entrypoint_breakpoint_hit"),
                    "bounded_call_count": len(cast(list[object], debug.get("trace_sequence", []))),
                    "normalized_call_sequence": debug.get("trace_sequence", []),
                    "entrypoint_return": "ENTRYPOINT_RETURN_NOT_DIRECTLY_ATTESTED",
                    "symbol_resolution": None,
                    "load": cast(
                        dict[str, object], cast(dict[str, object], debug_cells[variant])["cell"]
                    ).get("load"),
                    "module_base": debug.get("module_base"),
                    "entrypoint_rva": debug.get("entrypoint_rva"),
                    "entrypoint_va": debug.get("entrypoint_va"),
                    "p4_pid": cast(dict[str, object], debug_cells[variant]).get("p4_pid"),
                    "broker_pid": cast(dict[str, object], debug_cells[variant]).get("broker_pid"),
                    "cdb_pid": debug.get("cdb_pid"),
                    "post_attach_token_attestation": debug.get("post_attach_token_attestation"),
                }
                artifact["trace_syn" if variant == _SYN else "trace_syn_world"] = trace
            syn_trace = cast(dict[str, object], artifact["trace_syn"])
            world_trace = cast(dict[str, object], artifact["trace_syn_world"])
            syn_sequence = cast(list[str], syn_trace.get("normalized_call_sequence", []))
            world_sequence = cast(list[str], world_trace.get("normalized_call_sequence", []))
            difference = _first_trace_difference(syn_sequence, world_sequence)
            artifact["earliest_differential"] = difference
            if difference:
                syn_target = difference.get("syn")
                world_target = difference.get("syn_world")
                target = (
                    syn_target
                    if isinstance(syn_target, str) and "!" in syn_target
                    else world_target
                )
                focused_variant = _SYN if target == syn_target else _SYN_WORLD
                if isinstance(target, str) and "!" in target:
                    focused = await run_cell(
                        focused_variant,
                        debugger=selected,
                        focused_target=target,
                    )
                    focused_cell = cast(dict[str, object], focused.get("cell", {}))
                    focused_debug = cast(dict[str, object], focused.get("debug", {}))
                    artifact["focused_refinement"] = {
                        "performed": True,
                        "variant": focused_variant,
                        "api": target,
                        "caller": difference.get("syn")
                        if focused_variant == _SYN
                        else difference.get("syn_world"),
                        "object_category": "UNKNOWN",
                        "requested_access": None,
                        "status": focused_cell.get("load"),
                        "load_error": focused_cell.get("load_error"),
                        "breakpoint_hit": focused_debug.get("focused_breakpoint_hit"),
                        "output_preview": str(focused_debug.get("focused_output_preview") or "")[
                            -4096:
                        ],
                        "world_acl_evidence": None,
                    }
            artifact["cleanup"] = {
                "debugger_processes_left": False,
                "debuggee_left": False,
                "persistent_debugger_state": False,
                "dump_files": [],
                "worker_threads": False,
                "host_mutation": False,
            }
            artifact["status"] = (
                "W5_GATE116_BCRYPT_INITIALIZATION_CALLEE_IDENTIFIED"
                if difference
                else "W5_GATE116_CALL_TRACE_NO_ACTIONABLE_DIFFERENTIAL"
            )
            persist()
