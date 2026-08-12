"""Evidence-only Windows AppContainer composition probe.

This harness is intentionally independent from the production sandbox adapter. It
uses stable public Win32 process APIs and the existing production Job Object owner,
but it does not wire AppContainer into composition or any runtime workload.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from neuro_code.infrastructure.sandbox.windows_job import WindowsJobObject

_ERROR_ALREADY_EXISTS_HRESULT = 0x800700B7
_ERROR_BROKEN_PIPE = 109
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_NO_DATA = 232
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_SE_GROUP_ENABLED = 0x00000004
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_TYPE = 18
_TOKEN_ELEVATION = 20
_TOKEN_IS_APPCONTAINER = 29
_WIN_BUILTIN_ADMINISTRATORS_SID = 26
_WIN_CAPABILITY_INTERNET_CLIENT_SID = 85
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int32),
    ]


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _StartupInfoW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SidAndAttributes)),
        ("CapabilityCount", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
    ]


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_int16), ("Y", ctypes.c_int16)]


@dataclass(slots=True)
class _CreatedProcess:
    process_handle: int
    thread_handle: int
    pid: int


@dataclass(slots=True)
class _Capabilities:
    structure: _SecurityCapabilities
    keepalive: list[object]


def _load_function(
    library: object,
    name: str,
    argtypes: list[object],
    restype: object,
) -> Any:
    function = getattr(library, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


class _WinApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows AppContainer evidence must run on Windows")
        loader = cast(Any, ctypes).WinDLL
        self.kernel32 = loader("kernel32.dll", use_last_error=True)
        self.advapi32 = loader("advapi32.dll", use_last_error=True)
        self.userenv = loader("userenv.dll", use_last_error=True)

        self.create_profile = _load_function(
            self.userenv,
            "CreateAppContainerProfile",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.POINTER(_SidAndAttributes),
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_long,
        )
        self.derive_profile_sid = _load_function(
            self.userenv,
            "DeriveAppContainerSidFromAppContainerName",
            [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_long,
        )
        self.delete_profile = _load_function(
            self.userenv,
            "DeleteAppContainerProfile",
            [ctypes.c_wchar_p],
            ctypes.c_long,
        )
        self.free_sid = _load_function(self.advapi32, "FreeSid", [ctypes.c_void_p], ctypes.c_void_p)
        self.local_free = _load_function(
            self.kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p
        )
        self.convert_sid = _load_function(
            self.advapi32,
            "ConvertSidToStringSidW",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)],
            ctypes.c_int32,
        )
        self.create_well_known_sid = _load_function(
            self.advapi32,
            "CreateWellKnownSid",
            [
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
        )
        self.open_process_token = _load_function(
            self.advapi32,
            "OpenProcessToken",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int32,
        )
        self.get_token_information = _load_function(
            self.advapi32,
            "GetTokenInformation",
            [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
        )
        self.check_token_membership = _load_function(
            self.advapi32,
            "CheckTokenMembership",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)],
            ctypes.c_int32,
        )
        self.initialize_attributes = _load_function(
            self.kernel32,
            "InitializeProcThreadAttributeList",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self.update_attribute = _load_function(
            self.kernel32,
            "UpdateProcThreadAttribute",
            [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self.delete_attributes = _load_function(
            self.kernel32,
            "DeleteProcThreadAttributeList",
            [ctypes.c_void_p],
            None,
        )
        self.create_process = _load_function(
            self.kernel32,
            "CreateProcessW",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self.create_pipe = _load_function(
            self.kernel32,
            "CreatePipe",
            [
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(_SecurityAttributes),
                ctypes.c_uint32,
            ],
            ctypes.c_int32,
        )
        self.set_handle_information = _load_function(
            self.kernel32,
            "SetHandleInformation",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.create_event = _load_function(
            self.kernel32,
            "CreateEventW",
            [ctypes.POINTER(_SecurityAttributes), ctypes.c_int32, ctypes.c_int32, ctypes.c_wchar_p],
            ctypes.c_void_p,
        )
        self.write_file = _load_function(
            self.kernel32,
            "WriteFile",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self.read_file = _load_function(
            self.kernel32,
            "ReadFile",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self.wait = _load_function(
            self.kernel32,
            "WaitForSingleObject",
            [ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_uint32,
        )
        self.exit_code = _load_function(
            self.kernel32,
            "GetExitCodeProcess",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int32,
        )
        self.close_handle_api = _load_function(
            self.kernel32, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32
        )
        self.open_process = _load_function(
            self.kernel32,
            "OpenProcess",
            [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32],
            ctypes.c_void_p,
        )
        self.is_process_in_job = _load_function(
            self.kernel32,
            "IsProcessInJob",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)],
            ctypes.c_int32,
        )
        self.create_pseudoconsole = _load_function(
            self.kernel32,
            "CreatePseudoConsole",
            [
                _Coord,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_long,
        )
        self.resize_pseudoconsole = _load_function(
            self.kernel32,
            "ResizePseudoConsole",
            [ctypes.c_void_p, _Coord],
            ctypes.c_long,
        )
        self.close_pseudoconsole = _load_function(
            self.kernel32, "ClosePseudoConsole", [ctypes.c_void_p], None
        )
        self.load_library = _load_function(
            self.kernel32,
            "LoadLibraryExW",
            [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_void_p,
        )
        self.get_proc_address = _load_function(
            self.kernel32,
            "GetProcAddress",
            [ctypes.c_void_p, ctypes.c_char_p],
            ctypes.c_void_p,
        )
        self.free_library = _load_function(
            self.kernel32, "FreeLibrary", [ctypes.c_void_p], ctypes.c_int32
        )

    @staticmethod
    def error(operation: str) -> NoReturn:
        code = cast(Any, ctypes).get_last_error()
        raise OSError(code, f"{operation} failed with Windows error {code}")

    def close(self, handle: int | None) -> None:
        if handle:
            self.close_handle_api(handle)


class _AttributeList:
    def __init__(self, api: _WinApi, count: int) -> None:
        self._api = api
        size = ctypes.c_size_t()
        api.initialize_attributes(None, count, 0, ctypes.byref(size))
        if size.value == 0:
            api.error("InitializeProcThreadAttributeList(size)")
        self._storage = ctypes.create_string_buffer(size.value)
        self.pointer = ctypes.cast(self._storage, ctypes.c_void_p)
        if not api.initialize_attributes(self.pointer, count, 0, ctypes.byref(size)):
            api.error("InitializeProcThreadAttributeList")
        self._keepalive: list[object] = []

    def add(self, key: int, value: Any, size: int, operation: str) -> None:
        self._keepalive.append(value)
        pointer = ctypes.cast(value, ctypes.c_void_p) if not isinstance(value, int) else value
        if not self._api.update_attribute(self.pointer, 0, key, pointer, size, None, None):
            self._api.error(f"UpdateProcThreadAttribute({operation})")

    def close(self) -> None:
        self._api.delete_attributes(self.pointer)


class _Profile:
    def __init__(self, api: _WinApi, name: str) -> None:
        self.api = api
        self.name = name
        self.sid = ctypes.c_void_p()
        result = int(
            api.create_profile(
                name, name, "Neuro Code evidence-only POC1", None, 0, ctypes.byref(self.sid)
            )
        )
        unsigned = result & 0xFFFFFFFF
        if unsigned == _ERROR_ALREADY_EXISTS_HRESULT:
            result = int(api.derive_profile_sid(name, ctypes.byref(self.sid)))
            unsigned = result & 0xFFFFFFFF
        if unsigned != 0 or not self.sid.value:
            raise OSError(unsigned, f"Create/derive AppContainer profile failed: 0x{unsigned:08x}")
        converted = ctypes.c_wchar_p()
        if not api.convert_sid(self.sid, ctypes.byref(converted)):
            api.error("ConvertSidToStringSidW")
        try:
            self.sid_text = converted.value or ""
        finally:
            api.local_free(converted)

    def capabilities(self, *, internet: bool = False) -> _Capabilities:
        keepalive: list[object] = []
        capability_pointer: Any = None
        capability_count = 0
        if internet:
            sid_storage = ctypes.create_string_buffer(68)
            sid_size = ctypes.c_uint32(len(sid_storage))
            if not self.api.create_well_known_sid(
                _WIN_CAPABILITY_INTERNET_CLIENT_SID,
                None,
                sid_storage,
                ctypes.byref(sid_size),
            ):
                self.api.error("CreateWellKnownSid(internetClient)")
            sid_entry = (_SidAndAttributes * 1)(
                _SidAndAttributes(ctypes.cast(sid_storage, ctypes.c_void_p), _SE_GROUP_ENABLED)
            )
            keepalive.extend([sid_storage, sid_entry])
            capability_pointer = ctypes.cast(sid_entry, ctypes.POINTER(_SidAndAttributes))
            capability_count = 1
        structure = _SecurityCapabilities(
            self.sid,
            capability_pointer,
            capability_count,
            0,
        )
        keepalive.append(structure)
        return _Capabilities(structure, keepalive)

    def close(self) -> int:
        if not self.sid.value:
            return 0
        self.api.free_sid(self.sid)
        self.sid = ctypes.c_void_p()
        return int(self.api.delete_profile(self.name)) & 0xFFFFFFFF


class _Launcher:
    def __init__(self, api: _WinApi, profile: _Profile, cwd: Path) -> None:
        self.api = api
        self.profile = profile
        self.cwd = cwd

    def spawn(
        self,
        application: Path,
        arguments: list[str],
        *,
        internet: bool = False,
        job_handle: int | None = None,
        handles: tuple[int, int, int] | None = None,
        pseudoconsole: int | None = None,
    ) -> _CreatedProcess:
        capabilities = self.profile.capabilities(internet=internet)
        count = (
            1
            + int(job_handle is not None)
            + int(handles is not None)
            + int(pseudoconsole is not None)
        )
        attributes = _AttributeList(self.api, count)
        try:
            attributes.add(
                _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(capabilities.structure),
                ctypes.sizeof(capabilities.structure),
                "security capabilities",
            )
            if job_handle is not None:
                job_values = (ctypes.c_void_p * 1)(job_handle)
                attributes.add(
                    _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                    job_values,
                    ctypes.sizeof(job_values),
                    "job list",
                )
            if handles is not None:
                handle_values = (ctypes.c_void_p * len(handles))(*handles)
                attributes.add(
                    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    handle_values,
                    ctypes.sizeof(handle_values),
                    "handle list",
                )
            if pseudoconsole is not None:
                attributes.add(
                    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                    pseudoconsole,
                    ctypes.sizeof(ctypes.c_void_p),
                    "pseudoconsole",
                )

            startup = _StartupInfoExW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            inherit_handles = handles is not None
            if handles is not None:
                startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput = handles[0]
                startup.StartupInfo.hStdOutput = handles[1]
                startup.StartupInfo.hStdError = handles[2]
            elif pseudoconsole is not None:
                startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.lpAttributeList = attributes.pointer.value
            process = _ProcessInformation()
            command_line = ctypes.create_unicode_buffer(
                subprocess.list2cmdline([str(application), *arguments])
            )
            created = self.api.create_process(
                str(application),
                command_line,
                None,
                None,
                inherit_handles,
                _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
                None,
                str(self.cwd),
                ctypes.byref(startup),
                ctypes.byref(process),
            )
            if not created:
                self.api.error("CreateProcessW")
        finally:
            attributes.close()
        if not process.hProcess or not process.hThread or not process.dwProcessId:
            raise OSError("CreateProcessW returned incomplete process information")
        return _CreatedProcess(
            int(process.hProcess), int(process.hThread), int(process.dwProcessId)
        )


def _status(passed: bool, *, detail: object | None = None) -> dict[str, object]:
    result: dict[str, object] = {"status": "PASS" if passed else "FAIL"}
    if detail is not None:
        result["detail"] = detail
    return result


def _unresolved(detail: object) -> dict[str, object]:
    return {"status": "UNRESOLVED", "detail": detail}


def _wait_exit(api: _WinApi, process: _CreatedProcess, timeout_ms: int = 30000) -> int:
    result = int(api.wait(process.process_handle, timeout_ms))
    if result == _WAIT_TIMEOUT:
        raise TimeoutError(f"process {process.pid} did not exit within {timeout_ms}ms")
    if result != _WAIT_OBJECT_0:
        api.error("WaitForSingleObject(process)")
    exit_code = ctypes.c_uint32()
    if not api.exit_code(process.process_handle, ctypes.byref(exit_code)):
        api.error("GetExitCodeProcess")
    if exit_code.value == _STILL_ACTIVE:
        raise RuntimeError("process still active after signalled wait")
    return int(exit_code.value)


def _close_process(api: _WinApi, process: _CreatedProcess) -> None:
    api.close(process.thread_handle)
    api.close(process.process_handle)


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _single_record(
    launcher: _Launcher,
    child: Path,
    report: Path,
    *,
    internet: bool = False,
    job_handle: int | None = None,
) -> tuple[dict[str, object], _CreatedProcess]:
    report.unlink(missing_ok=True)
    process = launcher.spawn(
        child,
        ["record", str(report)],
        internet=internet,
        job_handle=job_handle,
    )
    exit_code = _wait_exit(launcher.api, process)
    rows = _read_json_lines(report)
    if exit_code != 0 or len(rows) != 1:
        raise RuntimeError(f"record child failed: exit={exit_code}, rows={rows!r}")
    return rows[0], process


def _make_pipe(api: _WinApi, *, parent_reads: bool) -> tuple[int, int]:
    security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
    read_handle = ctypes.c_void_p()
    write_handle = ctypes.c_void_p()
    if not api.create_pipe(
        ctypes.byref(read_handle),
        ctypes.byref(write_handle),
        ctypes.byref(security),
        0,
    ):
        api.error("CreatePipe")
    read_value = int(read_handle.value or 0)
    write_value = int(write_handle.value or 0)
    parent_handle = read_value if parent_reads else write_value
    if not api.set_handle_information(parent_handle, _HANDLE_FLAG_INHERIT, 0):
        api.error("SetHandleInformation")
    return read_value, write_value


def _write(api: _WinApi, handle: int, data: bytes) -> None:
    buffer = ctypes.create_string_buffer(data)
    written = ctypes.c_uint32()
    if not api.write_file(handle, buffer, len(data), ctypes.byref(written), None):
        api.error("WriteFile")
    if written.value != len(data):
        raise OSError(f"short pipe write: {written.value}/{len(data)}")


def _read_all(api: _WinApi, handle: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        buffer = ctypes.create_string_buffer(4096)
        read = ctypes.c_uint32()
        if not api.read_file(handle, buffer, len(buffer), ctypes.byref(read), None):
            error = cast(Any, ctypes).get_last_error()
            if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
                break
            raise OSError(error, f"ReadFile failed with Windows error {error}")
        if read.value == 0:
            break
        chunks.append(buffer.raw[: read.value])
    return b"".join(chunks)


def _is_in_job(api: _WinApi, process_handle: int, job_handle: int) -> bool:
    result = ctypes.c_int32()
    if not api.is_process_in_job(process_handle, job_handle, ctypes.byref(result)):
        api.error("IsProcessInJob")
    return bool(result.value)


def _grant_fixture(path: Path, sid: str, rights: str) -> list[str]:
    command = ["icacls", str(path), "/grant", f"*{sid}:{rights}"]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise OSError(
            f"icacls failed ({completed.returncode}): {completed.stdout} {completed.stderr}"
        )
    return command


def _controller_token_facts(api: _WinApi) -> dict[str, object]:
    token = ctypes.c_void_p()
    if not api.open_process_token(
        api.kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        api.error("OpenProcessToken(controller)")
    try:
        elevation = ctypes.c_uint32()
        elevation_type = ctypes.c_uint32()
        returned = ctypes.c_uint32()
        if not api.get_token_information(
            token,
            _TOKEN_ELEVATION,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            api.error("GetTokenInformation(TokenElevation)")
        if not api.get_token_information(
            token,
            _TOKEN_ELEVATION_TYPE,
            ctypes.byref(elevation_type),
            ctypes.sizeof(elevation_type),
            ctypes.byref(returned),
        ):
            api.error("GetTokenInformation(TokenElevationType)")
    finally:
        api.close(int(token.value or 0))
    admin_sid = ctypes.create_string_buffer(68)
    admin_size = ctypes.c_uint32(len(admin_sid))
    if not api.create_well_known_sid(
        _WIN_BUILTIN_ADMINISTRATORS_SID,
        None,
        admin_sid,
        ctypes.byref(admin_size),
    ):
        api.error("CreateWellKnownSid(Administrators)")
    is_admin = ctypes.c_int32()
    if not api.check_token_membership(None, admin_sid, ctypes.byref(is_admin)):
        api.error("CheckTokenMembership(Administrators)")
    return {
        "elevated": bool(elevation.value),
        "elevation_type": int(elevation_type.value),
        "administrator_group_enabled": bool(is_admin.value),
    }


def _processmodel_probe(api: _WinApi) -> dict[str, object]:
    module = api.load_library("processmodel.dll", None, _LOAD_LIBRARY_SEARCH_SYSTEM32)
    if not module:
        return {
            "status": "UNRESOLVED",
            "available": False,
            "classification": "EXPERIMENTAL_ONLY",
            "error": cast(Any, ctypes).get_last_error(),
        }
    try:
        address = api.get_proc_address(module, b"Experimental_CreateProcessInSandbox")
        return {
            "status": "PASS" if address else "UNRESOLVED",
            "available": bool(address),
            "classification": "EXPERIMENTAL_ONLY",
        }
    finally:
        api.free_library(module)


def _executable_gate(
    launcher: _Launcher, application: str | None, arguments: list[str]
) -> dict[str, object]:
    if application is None:
        return _unresolved("executable not installed on runner")
    try:
        process = launcher.spawn(Path(application), arguments)
        try:
            exit_code = _wait_exit(launcher.api, process)
        finally:
            _close_process(launcher.api, process)
    except BaseException as error:
        return {
            "status": "FAILED",
            "application": application,
            "error_type": type(error).__name__,
            "error": str(error),
            "note": "No broad install-root ACL was granted.",
        }
    return {
        "status": "STARTED" if exit_code == 0 else "FAILED",
        "application": application,
        "exit_code": exit_code,
    }


def _run(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    api = _WinApi()
    output_report = Path(args.report).resolve()
    output_report.parent.mkdir(parents=True, exist_ok=True)
    profile_name = f"NeuroCode.Poc1.{uuid.uuid4().hex}"
    runner: dict[str, object] = {
        "platform": platform.platform(),
        "version": platform.version(),
        "release": platform.release(),
        "architecture": platform.machine(),
        "python": sys.version,
        "github_runner_name": os.environ.get("RUNNER_NAME"),
        "github_runner_os": os.environ.get("RUNNER_OS"),
    }
    runner.update(_controller_token_facts(api))
    result: dict[str, object] = {
        "classification": "EVIDENCE_ONLY_DO_NOT_MERGE",
        "profile_name": profile_name,
        "runner": runner,
        "gates": {},
    }
    controller_is_admin = bool(runner["elevated"] or runner["administrator_group_enabled"])
    result["non_admin_acceptance"] = (
        "NON_ADMIN_ACCEPTANCE_UNRESOLVED"
        if controller_is_admin
        else "NON_ADMIN_CONTROLLER_OBSERVED"
    )

    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-appcontainer-poc1-"))
    authorized = fixture / "authorized"
    outside = fixture / "outside"
    authorized.mkdir()
    outside.mkdir()
    (authorized / "authorized.txt").write_text("authorized", encoding="utf-8")
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    child = authorized / "windows_appcontainer_poc1_child.exe"
    shutil.copy2(Path(args.child).resolve(), child)
    profile: _Profile | None = None
    critical_failures: list[str] = []
    try:
        profile = _Profile(api, profile_name)
        result["appcontainer_sid"] = profile.sid_text
        result["acl_commands"] = [
            _grant_fixture(fixture, profile.sid_text, "(RX)"),
            _grant_fixture(authorized, profile.sid_text, "(OI)(CI)(M)"),
        ]
        launcher = _Launcher(api, profile, authorized)
        gates = cast(dict[str, object], result["gates"])

        basic_report = authorized / "gate-a.jsonl"
        try:
            facts, process = _single_record(launcher, child, basic_report)
            _close_process(api, process)
            basic_pass = bool(
                facts.get("token_is_appcontainer")
                and str(facts.get("appcontainer_sid", "")).casefold() == profile.sid_text.casefold()
                and cast(int, facts.get("integrity_rid", 0xFFFFFFFF)) <= 0x1000
            )
            gates["A_appcontainer_creation"] = _status(basic_pass, detail=facts)
            if not basic_pass:
                critical_failures.append("A")
        except BaseException as error:
            gates["A_appcontainer_creation"] = _status(False, detail=str(error))
            critical_failures.append("A")

        gates["B_executables"] = {
            "python": _executable_gate(
                launcher,
                str(getattr(sys, "_base_executable", sys.executable)),
                ["-I", "-S", "-c", "raise SystemExit(0)"],
            ),
            "cmd": _executable_gate(
                launcher,
                str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe"),
                ["/d", "/c", "exit", "0"],
            ),
            "powershell_or_pwsh": _executable_gate(
                launcher,
                shutil.which("pwsh") or shutil.which("powershell"),
                ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
            ),
            "git": _executable_gate(launcher, shutil.which("git"), ["--version"]),
            "node": _executable_gate(launcher, shutil.which("node"), ["--version"]),
        }

        tree_report = authorized / "gate-c.jsonl"
        tree_report.unlink(missing_ok=True)
        tree_process: _CreatedProcess | None = None
        descendant_handles: list[int] = []
        try:
            with WindowsJobObject.create() as job:
                tree_process = launcher.spawn(
                    child,
                    ["tree", str(tree_report), "0"],
                    job_handle=job.process_creation_handle,
                )
                api.close(tree_process.thread_handle)
                tree_process.thread_handle = 0
                deadline = time.monotonic() + 20
                rows: list[dict[str, object]] = []
                while time.monotonic() < deadline:
                    rows = _read_json_lines(tree_report)
                    if len(rows) >= 3:
                        break
                    time.sleep(0.1)
                if len(rows) != 3:
                    raise RuntimeError(f"expected three descendant records, got {rows!r}")
                token_inheritance = all(
                    row.get("token_is_appcontainer") is True
                    and str(row.get("appcontainer_sid", "")).casefold()
                    == profile.sid_text.casefold()
                    for row in rows
                )
                job_ownership = True
                for row in rows:
                    handle = api.open_process(
                        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
                        False,
                        cast(int, row["pid"]),
                    )
                    if not handle:
                        api.error(f"OpenProcess({row['pid']})")
                    descendant_handles.append(int(handle))
                    job_ownership = job_ownership and _is_in_job(
                        api, int(handle), job.process_creation_handle
                    )
                composition = _is_in_job(
                    api, tree_process.process_handle, job.process_creation_handle
                )
                job.terminate(73)
                terminated = all(
                    int(api.wait(handle, 10000)) == _WAIT_OBJECT_0 for handle in descendant_handles
                )
                gates["C_appcontainer_job"] = _status(
                    composition,
                    detail={
                        "records": rows,
                        "exact_job_membership": job_ownership,
                        "token_inheritance": token_inheritance,
                        "termination": terminated,
                    },
                )
                if not (composition and token_inheritance and job_ownership and terminated):
                    critical_failures.append("C")
        except BaseException as error:
            gates["C_appcontainer_job"] = _status(False, detail=str(error))
            critical_failures.append("C")
        finally:
            for handle in descendant_handles:
                api.close(handle)
            if tree_process is not None:
                api.close(tree_process.thread_handle)
                api.close(tree_process.process_handle)

        stdin_read, stdin_write = _make_pipe(api, parent_reads=False)
        stdout_read, stdout_write = _make_pipe(api, parent_reads=True)
        stderr_read, stderr_write = _make_pipe(api, parent_reads=True)
        sentinel_security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        sentinel = int(api.create_event(ctypes.byref(sentinel_security), True, False, None) or 0)
        pipe_process: _CreatedProcess | None = None
        try:
            with WindowsJobObject.create() as job:
                pipe_process = launcher.spawn(
                    child,
                    ["stdio", str(sentinel)],
                    job_handle=job.process_creation_handle,
                    handles=(stdin_read, stdout_write, stderr_write),
                )
                api.close(pipe_process.thread_handle)
                pipe_process.thread_handle = 0
                api.close(stdin_read)
                stdin_read = 0
                api.close(stdout_write)
                stdout_write = 0
                api.close(stderr_write)
                stderr_write = 0
                _write(api, stdin_write, b"poc1-input\n")
                api.close(stdin_write)
                stdin_write = 0
                exit_code = _wait_exit(api, pipe_process)
                stdout = _read_all(api, stdout_read).decode("utf-8", errors="replace")
                stderr = _read_all(api, stderr_read).decode("utf-8", errors="replace")
                handle_list_pass = (
                    exit_code == 0
                    and "STDOUT:poc1-input" in stdout
                    and "STDERR:ok" in stderr
                    and "SENTINEL_VISIBLE:false" in stdout
                    and _is_in_job(api, pipe_process.process_handle, job.process_creation_handle)
                )
                gates["D_handle_list"] = _status(
                    handle_list_pass,
                    detail={"exit_code": exit_code, "stdout": stdout, "stderr": stderr},
                )
                if not handle_list_pass:
                    critical_failures.append("D")
        except BaseException as error:
            gates["D_handle_list"] = _status(False, detail=str(error))
            critical_failures.append("D")
        finally:
            for handle in (
                stdin_read,
                stdin_write,
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
                sentinel,
            ):
                api.close(handle)
            if pipe_process is not None:
                api.close(pipe_process.thread_handle)
                api.close(pipe_process.process_handle)

        conpty_input_read, conpty_input_write = _make_pipe(api, parent_reads=False)
        conpty_output_read, conpty_output_write = _make_pipe(api, parent_reads=True)
        pseudoconsole = ctypes.c_void_p()
        conpty_process: _CreatedProcess | None = None
        conpty_report = authorized / "gate-e.jsonl"
        conpty_report.unlink(missing_ok=True)
        try:
            create_result = int(
                api.create_pseudoconsole(
                    _Coord(80, 24),
                    conpty_input_read,
                    conpty_output_write,
                    0,
                    ctypes.byref(pseudoconsole),
                )
            )
            if create_result != 0 or not pseudoconsole.value:
                raise OSError(
                    create_result & 0xFFFFFFFF,
                    f"CreatePseudoConsole failed: 0x{create_result & 0xFFFFFFFF:08x}",
                )
            with WindowsJobObject.create() as job:
                conpty_process = launcher.spawn(
                    child,
                    ["conpty", str(conpty_report)],
                    job_handle=job.process_creation_handle,
                    pseudoconsole=int(pseudoconsole.value),
                )
                api.close(conpty_process.thread_handle)
                conpty_process.thread_handle = 0
                exact_job = _is_in_job(
                    api, conpty_process.process_handle, job.process_creation_handle
                )
                resize_result = int(api.resize_pseudoconsole(pseudoconsole, _Coord(100, 35)))
                _write(api, conpty_input_write, b"poc1-conpty\r\n")
                exit_code = _wait_exit(api, conpty_process, 30000)
                api.close_pseudoconsole(pseudoconsole)
                pseudoconsole = ctypes.c_void_p()
                api.close(conpty_output_write)
                conpty_output_write = 0
                output = _read_all(api, conpty_output_read).decode("utf-8", errors="replace")
                rows = _read_json_lines(conpty_report)
                confinement = len(rows) == 2 and all(
                    row.get("token_is_appcontainer") is True
                    and row.get("in_job") is True
                    and str(row.get("appcontainer_sid", "")).casefold()
                    == profile.sid_text.casefold()
                    for row in rows
                )
                conpty_pass = (
                    exit_code == 0
                    and exact_job
                    and resize_result == 0
                    and "CONPTY_READY" in output
                    and "CONPTY_ECHO:poc1-conpty" in output
                    and confinement
                )
                gates["E_conpty"] = _status(
                    conpty_pass,
                    detail={
                        "exit_code": exit_code,
                        "exact_job_membership": exact_job,
                        "resize_hresult": resize_result & 0xFFFFFFFF,
                        "output": output,
                        "records": rows,
                        "descendant_confinement": confinement,
                    },
                )
                if not conpty_pass:
                    critical_failures.append("E")
        except BaseException as error:
            gates["E_conpty"] = _status(False, detail=str(error))
            critical_failures.append("E")
        finally:
            if pseudoconsole.value:
                api.close_pseudoconsole(pseudoconsole)
            for handle in (
                conpty_input_read,
                conpty_input_write,
                conpty_output_read,
                conpty_output_write,
            ):
                api.close(handle)
            if conpty_process is not None:
                api.close(conpty_process.thread_handle)
                api.close(conpty_process.process_handle)

        def network_attempt(
            name: str, host: str, port: int, *, internet: bool
        ) -> dict[str, object]:
            report = authorized / f"network-{name}.jsonl"
            report.unlink(missing_ok=True)
            process = launcher.spawn(
                child,
                ["network", host, str(port), str(report)],
                internet=internet,
            )
            try:
                exit_code = _wait_exit(api, process, 30000)
            finally:
                _close_process(api, process)
            rows = _read_json_lines(report)
            if exit_code != 0 or len(rows) != 1:
                raise RuntimeError(f"network helper failed: exit={exit_code}, rows={rows!r}")
            return rows[0]

        try:
            denied = network_attempt("denied", "www.microsoft.com", 443, internet=False)
            gates["F_network_no_capability"] = _status(
                denied.get("connected") is False, detail=denied
            )
        except BaseException as error:
            gates["F_network_no_capability"] = _status(False, detail=str(error))

        try:
            allowed = network_attempt("allowed", "www.microsoft.com", 443, internet=True)
            gates["F_network_internet_client"] = (
                _status(True, detail=allowed)
                if allowed.get("connected") is True
                else _unresolved(allowed)
            )
        except BaseException as error:
            gates["F_network_internet_client"] = _unresolved(str(error))

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        local_port = int(listener.getsockname()[1])
        accepted: list[bool] = []

        def accept_once() -> None:
            try:
                connection, _ = listener.accept()
                connection.close()
                accepted.append(True)
            except OSError:
                accepted.append(False)

        accept_thread = threading.Thread(target=accept_once, daemon=True)
        accept_thread.start()
        try:
            localhost = network_attempt("localhost", "127.0.0.1", local_port, internet=True)
            gates["F_localhost_observation"] = {
                "status": "PASS",
                "classification": "OBSERVATION_ONLY",
                "child": localhost,
            }
        except BaseException as error:
            gates["F_localhost_observation"] = {
                "status": "UNRESOLVED",
                "classification": "OBSERVATION_ONLY",
                "error": str(error),
            }
        finally:
            listener.close()
            accept_thread.join(timeout=1)
            cast(dict[str, object], gates["F_localhost_observation"])[
                "controller_accepted_connection"
            ] = bool(accepted and accepted[0])

        filesystem_report = authorized / "filesystem.jsonl"
        filesystem_report.unlink(missing_ok=True)
        filesystem_process = launcher.spawn(
            child,
            [
                "filesystem",
                str(authorized / "authorized.txt"),
                str(outside / "outside.txt"),
                str(authorized / "written.txt"),
                str(filesystem_report),
            ],
        )
        try:
            filesystem_exit = _wait_exit(api, filesystem_process)
        finally:
            _close_process(api, filesystem_process)
        filesystem_rows = _read_json_lines(filesystem_report)
        filesystem_detail = filesystem_rows[0] if len(filesystem_rows) == 1 else {}
        filesystem_pass = bool(
            filesystem_exit == 0
            and filesystem_detail.get("authorized_read") is True
            and filesystem_detail.get("authorized_write") is True
            and filesystem_detail.get("outside_read") is False
        )
        gates["minimal_filesystem"] = _status(
            filesystem_pass,
            detail={"exit_code": filesystem_exit, **filesystem_detail},
        )
        result["experimental_bfs"] = _processmodel_probe(api)
    except BaseException as error:
        result["harness_error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        critical_failures.append("HARNESS")
    finally:
        if profile is not None:
            with contextlib.suppress(BaseException):
                result["profile_delete_hresult"] = profile.close()
        try:
            shutil.rmtree(fixture)
            result["fixture_cleanup"] = "PASS"
        except BaseException as error:
            result["fixture_cleanup"] = f"FAIL: {error}"
            critical_failures.append("CLEANUP")

    result["critical_failures"] = sorted(set(critical_failures))
    result["overall"] = "PASS" if not critical_failures else "FAIL"
    output_report.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return result, not critical_failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    _, passed = _run(args)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
