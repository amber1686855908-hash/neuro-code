"""Evidence-only probe for Windows AppContainer IPC boundaries.

The harness intentionally lives outside production code.  It tests whether a
full-trust controller can communicate with an AppContainer bootstrap without
crossing that boundary with inherited stdio handles, then tests descendant stdio
inside the already-established AppContainer authority.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from neuro_code.infrastructure.sandbox.windows_job import WindowsJobObject

_ERROR_ALREADY_EXISTS_HRESULT = 0x800700B7
_ERROR_ACCESS_DENIED = 5
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_CONNECTED = 535
_ERROR_PIPE_LISTENING = 536
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_WAIT = 0x00000000
_PIPE_NOWAIT = 0x00000001
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_UNLIMITED_INSTANCES = 255
_INVALID_HANDLE_VALUE = cast(int, ctypes.c_void_p(-1).value)
_SDDL_REVISION_1 = 1
_SE_KERNEL_OBJECT = 6
_DACL_SECURITY_INFORMATION = 0x00000004
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_TYPE = 18
_TOKEN_ELEVATION = 20
_WIN_BUILTIN_ADMINISTRATORS_SID = 26
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000

_FRAME_HELLO = b"H"
_FRAME_TARGET = b"T"
_FRAME_DATA = b"D"
_FRAME_EOF = b"E"
_FRAME_STDOUT = b"O"
_FRAME_STDERR = b"R"
_FRAME_EXIT = b"X"
_FRAME_READY = b"Q"
_MAX_FRAME = 1024 * 1024


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
class _NamedPipe:
    handle: int
    name: str
    configured_sddl: str
    effective_dacl_sddl: str


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
            raise OSError("Windows AppContainer IPC evidence must run on Windows")
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
            self.userenv, "DeleteAppContainerProfile", [ctypes.c_wchar_p], ctypes.c_long
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
        self.convert_sddl = _load_function(
            self.advapi32,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
        )
        self.convert_sd_to_sddl = _load_function(
            self.advapi32,
            "ConvertSecurityDescriptorToStringSecurityDescriptorW",
            [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_wchar_p),
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
        )
        self.get_security_info = _load_function(
            self.advapi32,
            "GetSecurityInfo",
            [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_uint32,
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
            self.kernel32, "DeleteProcThreadAttributeList", [ctypes.c_void_p], None
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
        self.create_named_pipe = _load_function(
            self.kernel32,
            "CreateNamedPipeW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(_SecurityAttributes),
            ],
            ctypes.c_void_p,
        )
        self.connect_named_pipe = _load_function(
            self.kernel32,
            "ConnectNamedPipe",
            [ctypes.c_void_p, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self.disconnect_named_pipe = _load_function(
            self.kernel32, "DisconnectNamedPipe", [ctypes.c_void_p], ctypes.c_int32
        )
        self.set_named_pipe_state = _load_function(
            self.kernel32,
            "SetNamedPipeHandleState",
            [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )
        self.get_named_pipe_client_pid = _load_function(
            self.kernel32,
            "GetNamedPipeClientProcessId",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
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
        self.check_token_membership = _load_function(
            self.advapi32,
            "CheckTokenMembership",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)],
            ctypes.c_int32,
        )

    @staticmethod
    def error(operation: str) -> NoReturn:
        code = cast(Any, ctypes).get_last_error()
        raise OSError(code, f"{operation} failed with Windows error {code}")

    def close(self, handle: int | None) -> None:
        if handle and handle != _INVALID_HANDLE_VALUE:
            self.close_handle_api(handle)


class _AttributeList:
    def __init__(self, api: _WinApi, count: int) -> None:
        self.api = api
        size = ctypes.c_size_t()
        api.initialize_attributes(None, count, 0, ctypes.byref(size))
        if not size.value:
            api.error("InitializeProcThreadAttributeList(size)")
        self.storage = ctypes.create_string_buffer(size.value)
        self.pointer = ctypes.cast(self.storage, ctypes.c_void_p)
        if not api.initialize_attributes(self.pointer, count, 0, ctypes.byref(size)):
            api.error("InitializeProcThreadAttributeList")
        self.keepalive: list[object] = []

    def add(self, key: int, value: Any, size: int, label: str) -> None:
        self.keepalive.append(value)
        pointer = ctypes.cast(value, ctypes.c_void_p) if not isinstance(value, int) else value
        if not self.api.update_attribute(self.pointer, 0, key, pointer, size, None, None):
            self.api.error(f"UpdateProcThreadAttribute({label})")

    def close(self) -> None:
        self.api.delete_attributes(self.pointer)


class _Profile:
    def __init__(self, api: _WinApi, name: str) -> None:
        self.api = api
        self.name = name
        self.sid = ctypes.c_void_p()
        result = int(
            api.create_profile(
                name,
                name,
                "Neuro Code evidence-only AppContainer IPC POC1B",
                None,
                0,
                ctypes.byref(self.sid),
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

    def capabilities(self) -> _SecurityCapabilities:
        return _SecurityCapabilities(self.sid, None, 0, 0)

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

    def _spawn(
        self,
        application: Path,
        arguments: list[str],
        *,
        appcontainer: bool,
        job_handle: int | None,
        stdio_handles: tuple[int, int, int] | None = None,
        use_handle_list: bool = False,
        pseudoconsole: int | None = None,
    ) -> _CreatedProcess:
        count = (
            int(appcontainer)
            + int(job_handle is not None)
            + int(use_handle_list)
            + int(pseudoconsole is not None)
        )
        if count == 0:
            raise ValueError("evidence launcher requires at least one creation attribute")
        attributes = _AttributeList(self.api, count)
        capabilities = self.profile.capabilities() if appcontainer else None
        try:
            if capabilities is not None:
                attributes.add(
                    _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                    ctypes.byref(capabilities),
                    ctypes.sizeof(capabilities),
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
            if use_handle_list:
                if stdio_handles is None:
                    raise ValueError("HANDLE_LIST requires stdio handles")
                handle_values = (ctypes.c_void_p * 3)(*stdio_handles)
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
            if stdio_handles is not None:
                startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput = stdio_handles[0]
                startup.StartupInfo.hStdOutput = stdio_handles[1]
                startup.StartupInfo.hStdError = stdio_handles[2]
            elif pseudoconsole is not None:
                startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.lpAttributeList = attributes.pointer.value
            process = _ProcessInformation()
            command_line = ctypes.create_unicode_buffer(
                subprocess.list2cmdline([str(application), *arguments])
            )
            flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT
            if pseudoconsole is None:
                flags |= _CREATE_NO_WINDOW
            created = self.api.create_process(
                str(application),
                command_line,
                None,
                None,
                stdio_handles is not None,
                flags,
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

    def spawn_appcontainer(
        self,
        application: Path,
        arguments: list[str],
        *,
        job_handle: int,
        stdio_handles: tuple[int, int, int] | None = None,
        use_handle_list: bool = False,
        pseudoconsole: int | None = None,
    ) -> _CreatedProcess:
        return self._spawn(
            application,
            arguments,
            appcontainer=True,
            job_handle=job_handle,
            stdio_handles=stdio_handles,
            use_handle_list=use_handle_list,
            pseudoconsole=pseudoconsole,
        )

    def spawn_trusted_launcher(
        self,
        application: Path,
        arguments: list[str],
        *,
        job_handle: int,
        stdio_handles: tuple[int, int, int],
    ) -> _CreatedProcess:
        return self._spawn(
            application,
            arguments,
            appcontainer=False,
            job_handle=job_handle,
            stdio_handles=stdio_handles,
            use_handle_list=True,
        )


def _status(passed: bool, detail: object) -> dict[str, object]:
    return {"status": "PASS" if passed else "FAIL", "detail": detail}


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
        raise RuntimeError("process remained active after signalled wait")
    return int(exit_code.value)


def _close_process(api: _WinApi, process: _CreatedProcess | None) -> None:
    if process is not None:
        api.close(process.thread_handle)
        api.close(process.process_handle)


def _is_in_job(api: _WinApi, process_handle: int, job_handle: int) -> bool:
    result = ctypes.c_int32()
    if not api.is_process_in_job(process_handle, job_handle, ctypes.byref(result)):
        api.error("IsProcessInJob")
    return bool(result.value)


def _open_process(api: _WinApi, pid: int) -> int:
    handle = api.open_process(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
        False,
        pid,
    )
    if not handle:
        api.error(f"OpenProcess({pid})")
    return int(handle)


def _valid_appcontainer_facts(facts: dict[str, object], sid: str) -> bool:
    return bool(
        facts.get("token_is_appcontainer") is True
        and str(facts.get("appcontainer_sid", "")).casefold() == sid.casefold()
        and cast(int, facts.get("integrity_rid", 0xFFFFFFFF)) <= 0x1000
        and facts.get("in_job") is True
    )


def _write_all(api: _WinApi, handle: int, data: bytes) -> None:
    cursor = 0
    while cursor < len(data):
        chunk = data[cursor : cursor + 4093]
        buffer = ctypes.create_string_buffer(chunk)
        written = ctypes.c_uint32()
        if not api.write_file(handle, buffer, len(chunk), ctypes.byref(written), None):
            api.error("WriteFile")
        if written.value == 0:
            raise OSError("WriteFile made no progress")
        cursor += int(written.value)


def _read_exact(api: _WinApi, handle: int, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        buffer = ctypes.create_string_buffer(min(remaining, 4096))
        received = ctypes.c_uint32()
        if not api.read_file(handle, buffer, len(buffer), ctypes.byref(received), None):
            api.error("ReadFile(exact)")
        if received.value == 0:
            raise EOFError(f"unexpected EOF with {remaining} bytes remaining")
        chunks.append(buffer.raw[: received.value])
        remaining -= int(received.value)
    return b"".join(chunks)


def _read_all(api: _WinApi, handle: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        buffer = ctypes.create_string_buffer(4096)
        received = ctypes.c_uint32()
        if not api.read_file(handle, buffer, len(buffer), ctypes.byref(received), None):
            error = cast(Any, ctypes).get_last_error()
            if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
                break
            raise OSError(error, f"ReadFile failed with Windows error {error}")
        if received.value == 0:
            break
        chunks.append(buffer.raw[: received.value])
    return b"".join(chunks)


def _read_line(api: _WinApi, handle: int) -> bytes:
    output = bytearray()
    while len(output) <= _MAX_FRAME:
        value = _read_exact(api, handle, 1)
        output.extend(value)
        if value == b"\n":
            return bytes(output)
    raise OSError("line exceeded evidence limit")


def _send_frame(api: _WinApi, handle: int, kind: bytes, payload: bytes = b"") -> None:
    if len(kind) != 1 or len(payload) > _MAX_FRAME:
        raise ValueError("invalid evidence frame")
    _write_all(api, handle, kind + struct.pack("<I", len(payload)) + payload)


def _receive_frame(api: _WinApi, handle: int) -> tuple[bytes, bytes]:
    header = _read_exact(api, handle, 5)
    length = struct.unpack("<I", header[1:])[0]
    if length > _MAX_FRAME:
        raise OSError(f"frame exceeds evidence limit: {length}")
    return header[:1], _read_exact(api, handle, length)


def _receive_json_frame(
    api: _WinApi,
    handle: int,
    expected_kind: bytes,
) -> dict[str, object]:
    kind, payload = _receive_frame(api, handle)
    if kind != expected_kind:
        raise OSError(f"expected frame {expected_kind!r}, got {kind!r}")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise OSError("facts frame was not a JSON object")
    return cast(dict[str, object], value)


def _effective_dacl_sddl(api: _WinApi, handle: int) -> str:
    descriptor = ctypes.c_void_p()
    error = int(
        api.get_security_info(
            handle,
            _SE_KERNEL_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
    )
    if error != 0 or not descriptor.value:
        raise OSError(error, f"GetSecurityInfo failed with Windows error {error}")
    text = ctypes.c_wchar_p()
    try:
        if not api.convert_sd_to_sddl(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        ):
            api.error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        return text.value or ""
    finally:
        if text:
            api.local_free(text)
        api.local_free(descriptor)


def _create_named_pipe(api: _WinApi, appcontainer_sid: str) -> _NamedPipe:
    name = rf"\\.\pipe\LOCAL\NeuroCode-Poc1B-{uuid.uuid4().hex}"
    configured_sddl = f"D:P(A;;GA;;;SY)(A;;GRGW;;;{appcontainer_sid})S:(ML;;NW;;;LW)"
    descriptor = ctypes.c_void_p()
    if not api.convert_sddl(
        configured_sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        api.error("ConvertStringSecurityDescriptorToSecurityDescriptorW(named pipe)")
    security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, False)
    try:
        handle = api.create_named_pipe(
            name,
            _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_NOWAIT | _PIPE_REJECT_REMOTE_CLIENTS,
            _PIPE_UNLIMITED_INSTANCES,
            65536,
            65536,
            15000,
            ctypes.byref(security),
        )
        value = int(handle or 0)
        if not value or value == _INVALID_HANDLE_VALUE:
            api.error("CreateNamedPipeW")
        effective = _effective_dacl_sddl(api, value)
        return _NamedPipe(value, name, configured_sddl, effective)
    finally:
        if descriptor.value:
            api.local_free(descriptor)


def _connect_named_pipe(api: _WinApi, pipe: _NamedPipe, timeout_seconds: float = 15) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if api.connect_named_pipe(pipe.handle, None):
            break
        error = cast(Any, ctypes).get_last_error()
        if error == _ERROR_PIPE_CONNECTED:
            break
        if error != _ERROR_PIPE_LISTENING:
            raise OSError(error, f"ConnectNamedPipe failed with Windows error {error}")
        time.sleep(0.025)
    else:
        raise TimeoutError("bounded named-pipe connection timed out")
    wait_mode = ctypes.c_uint32(_PIPE_WAIT)
    if not api.set_named_pipe_state(pipe.handle, ctypes.byref(wait_mode), None, None):
        api.error("SetNamedPipeHandleState(PIPE_WAIT)")
    pid = ctypes.c_uint32()
    if not api.get_named_pipe_client_pid(pipe.handle, ctypes.byref(pid)):
        api.error("GetNamedPipeClientProcessId")
    return int(pid.value)


def _close_named_pipe(api: _WinApi, pipe: _NamedPipe | None) -> None:
    if pipe is not None and pipe.handle:
        with contextlib.suppress(BaseException):
            api.disconnect_named_pipe(pipe.handle)
        api.close(pipe.handle)
        pipe.handle = 0


def _make_anonymous_pipe(
    api: _WinApi,
    *,
    parent_reads: bool,
    appcontainer_sid: str | None = None,
) -> tuple[int, int]:
    descriptor = ctypes.c_void_p()
    if appcontainer_sid is not None:
        sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{appcontainer_sid})S:(ML;;NW;;;LW)"
        if not api.convert_sddl(sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None):
            api.error("ConvertStringSecurityDescriptorToSecurityDescriptorW(anonymous pipe)")
    security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, True)
    read_handle = ctypes.c_void_p()
    write_handle = ctypes.c_void_p()
    try:
        if not api.create_pipe(
            ctypes.byref(read_handle),
            ctypes.byref(write_handle),
            ctypes.byref(security),
            0,
        ):
            api.error("CreatePipe")
    finally:
        if descriptor.value:
            api.local_free(descriptor)
    read_value = int(read_handle.value or 0)
    write_value = int(write_handle.value or 0)
    parent_handle = read_value if parent_reads else write_value
    if not api.set_handle_information(parent_handle, _HANDLE_FLAG_INHERIT, 0):
        api.error("SetHandleInformation(parent pipe end)")
    return read_value, write_value


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


def _grant_fixture(path: Path, sid: str) -> list[str]:
    command = ["icacls", str(path), "/grant", f"*{sid}:(OI)(CI)(RX)"]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise OSError(
            f"icacls failed ({completed.returncode}): {completed.stdout} {completed.stderr}"
        )
    return command


def _spawn_pipe_bootstrap(
    launcher: _Launcher,
    child: Path,
    mode: str,
    pipe: _NamedPipe,
    job: WindowsJobObject,
) -> _CreatedProcess:
    return launcher.spawn_appcontainer(
        child,
        [mode, pipe.name],
        job_handle=job.process_creation_handle,
    )


def _named_pipe_connect_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    pipe: _NamedPipe | None = None
    process: _CreatedProcess | None = None
    try:
        pipe = _create_named_pipe(api, launcher.profile.sid_text)
        with WindowsJobObject.create() as job:
            process = _spawn_pipe_bootstrap(launcher, child, "byte-stream", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe)
            facts = _receive_json_frame(api, pipe.handle, _FRAME_HELLO)
            exact_job = _is_in_job(api, process.process_handle, job.process_creation_handle)
            payload = (
                b"prefix\x00utf8:" + "神经编码".encode() + b"\r|\n|\r\n|" + bytes(range(256)) * 321
            )
            _send_frame(api, pipe.handle, _FRAME_DATA, payload)
            kind, echoed = _receive_frame(api, pipe.handle)
            exit_code = _wait_exit(api, process)
            valid_facts = _valid_appcontainer_facts(facts, launcher.profile.sid_text)
            connect_pass = bool(
                client_pid == process.pid and exact_job and valid_facts and exit_code == 0
            )
            stream_pass = kind == _FRAME_DATA and echoed == payload and len(payload) > 65536
            detail = {
                "pipe_name_entropy_bits": 128,
                "pipe_name": pipe.name,
                "configured_sddl": pipe.configured_sddl,
                "effective_dacl_sddl": pipe.effective_dacl_sddl,
                "pipe_reject_remote_clients": True,
                "bounded_timeout_seconds": 15,
                "connected_client_pid": client_pid,
                "expected_client_pid": process.pid,
                "bootstrap_facts": facts,
                "exact_job_membership": exact_job,
                "exit_code": exit_code,
            }
            stream_detail = {
                "payload_size": len(payload),
                "exact_echo": echoed == payload,
                "binary_zero_preserved": b"\x00" in echoed,
                "utf8_preserved": "神经编码".encode() in echoed,
                "cr_preserved": b"\r|" in echoed,
                "lf_preserved": b"\n|" in echoed,
                "crlf_preserved": b"\r\n|" in echoed,
                "byte_stream_mode": True,
            }
            return _status(connect_pass, detail), _status(stream_pass, stream_detail)
    except BaseException as error:
        detail = {"error_type": type(error).__name__, "error": str(error)}
        if pipe is not None:
            detail.update(
                {
                    "configured_sddl": pipe.configured_sddl,
                    "effective_dacl_sddl": pipe.effective_dacl_sddl,
                    "pipe_name": pipe.name,
                }
            )
        return _status(False, detail), _status(False, detail)
    finally:
        _close_named_pipe(api, pipe)
        _close_process(api, process)


def _unauthorized_client_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> dict[str, object]:
    pipe: _NamedPipe | None = None
    completed: subprocess.CompletedProcess[str] | None = None
    try:
        pipe = _create_named_pipe(api, launcher.profile.sid_text)
        completed = subprocess.run(
            [str(child), "pipe-denied", pipe.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        observation = json.loads(completed.stdout.strip())
        passed = bool(
            completed.returncode == 0
            and isinstance(observation, dict)
            and observation.get("connected") is False
            and observation.get("error") == _ERROR_ACCESS_DENIED
        )
        return _status(
            passed,
            {
                "probe_is_target_appcontainer": False,
                "exit_code": completed.returncode,
                "observation": observation,
                "expected_win32_error": _ERROR_ACCESS_DENIED,
                "configured_sddl": pipe.configured_sddl,
                "effective_dacl_sddl": pipe.effective_dacl_sddl,
                "no_everyone_or_broad_users_ace": ";;;WD)" not in pipe.effective_dacl_sddl
                and ";;;BU)" not in pipe.effective_dacl_sddl,
            },
        )
    except BaseException as error:
        return _status(
            False,
            {
                "error_type": type(error).__name__,
                "error": str(error),
                "exit_code": completed.returncode if completed else None,
            },
        )
    finally:
        _close_named_pipe(api, pipe)


def _stdio_relay_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> dict[str, object]:
    pipe: _NamedPipe | None = None
    process: _CreatedProcess | None = None
    target_handle = 0
    input_payload = b"stdin:\x00utf8:" + "桥接".encode() + b"\r|\n|\r\n|" + bytes(range(251)) * 300
    try:
        pipe = _create_named_pipe(api, launcher.profile.sid_text)
        with WindowsJobObject.create() as job:
            process = _spawn_pipe_bootstrap(launcher, child, "relay-stdio", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe)
            bootstrap = _receive_json_frame(api, pipe.handle, _FRAME_HELLO)
            target = _receive_json_frame(api, pipe.handle, _FRAME_TARGET)
            target_pid = cast(int, target["pid"])
            target_handle = _open_process(api, target_pid)
            target_exact_job = _is_in_job(api, target_handle, job.process_creation_handle)
            _send_frame(api, pipe.handle, _FRAME_DATA, input_payload)
            _send_frame(api, pipe.handle, _FRAME_EOF)
            stdout_kind, stdout = _receive_frame(api, pipe.handle)
            stderr_kind, stderr = _receive_frame(api, pipe.handle)
            exit_kind, exit_payload = _receive_frame(api, pipe.handle)
            bootstrap_exit = _wait_exit(api, process)
            target_exit = struct.unpack("<I", exit_payload)[0] if len(exit_payload) == 4 else None
            bootstrap_valid = _valid_appcontainer_facts(bootstrap, launcher.profile.sid_text)
            target_valid = _valid_appcontainer_facts(target, launcher.profile.sid_text)
            passed = bool(
                client_pid == process.pid
                and bootstrap_valid
                and target_valid
                and target_exact_job
                and stdout_kind == _FRAME_STDOUT
                and stdout == input_payload
                and stderr_kind == _FRAME_STDERR
                and stderr == b"TARGET_STDERR:diagnostic\r\n"
                and exit_kind == _FRAME_EXIT
                and target_exit == 37
                and bootstrap_exit == 0
            )
            return _status(
                passed,
                {
                    "connected_client_pid": client_pid,
                    "bootstrap_facts": bootstrap,
                    "target_facts": target,
                    "target_exact_job_membership": target_exact_job,
                    "target_stdin_exact": stdout == input_payload,
                    "stdout_exact": stdout == input_payload,
                    "stdout_size": len(stdout),
                    "stderr_exact": stderr == b"TARGET_STDERR:diagnostic\r\n",
                    "stderr_separate_frame": stderr_kind == _FRAME_STDERR,
                    "eof_propagated": target_exit == 37,
                    "target_exit_code": target_exit,
                    "bootstrap_exit_code": bootstrap_exit,
                    "internal_handle_list_boundary": (
                        "AppContainer bootstrap -> AppContainer descendant"
                    ),
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        api.close(target_handle)
        _close_named_pipe(api, pipe)
        _close_process(api, process)


def _mcp_relay_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> dict[str, object]:
    pipe: _NamedPipe | None = None
    process: _CreatedProcess | None = None
    target_handle = 0
    requests = [
        b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n',
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
        b'{"jsonrpc":"2.0","id":3,"method":"tools/call"}\n',
    ]
    responses: list[bytes] = []
    diagnostics: list[bytes] = []
    try:
        pipe = _create_named_pipe(api, launcher.profile.sid_text)
        with WindowsJobObject.create() as job:
            process = _spawn_pipe_bootstrap(launcher, child, "relay-mcp", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe)
            bootstrap = _receive_json_frame(api, pipe.handle, _FRAME_HELLO)
            target = _receive_json_frame(api, pipe.handle, _FRAME_TARGET)
            target_handle = _open_process(api, cast(int, target["pid"]))
            target_job = _is_in_job(api, target_handle, job.process_creation_handle)
            for request in requests:
                _send_frame(api, pipe.handle, _FRAME_DATA, request)
                response_kind, response = _receive_frame(api, pipe.handle)
                diagnostic_kind, diagnostic = _receive_frame(api, pipe.handle)
                if response_kind != _FRAME_STDOUT or diagnostic_kind != _FRAME_STDERR:
                    raise OSError("MCP stdout/stderr frame separation failed")
                responses.append(response)
                diagnostics.append(diagnostic)
            _send_frame(api, pipe.handle, _FRAME_EOF)
            exit_kind, exit_payload = _receive_frame(api, pipe.handle)
            bootstrap_exit = _wait_exit(api, process)
            target_exit = struct.unpack("<I", exit_payload)[0] if len(exit_payload) == 4 else None
            expected_responses = [
                f"response-{index}:".encode() + value for index, value in enumerate(requests)
            ]
            expected_diagnostics = [f"diagnostic-{index}\n".encode() for index in range(3)]
            passed = bool(
                client_pid == process.pid
                and _valid_appcontainer_facts(bootstrap, launcher.profile.sid_text)
                and _valid_appcontainer_facts(target, launcher.profile.sid_text)
                and target_job
                and responses == expected_responses
                and diagnostics == expected_diagnostics
                and exit_kind == _FRAME_EXIT
                and target_exit == 0
                and bootstrap_exit == 0
            )
            return _status(
                passed,
                {
                    "connected_client_pid": client_pid,
                    "bootstrap_facts": bootstrap,
                    "target_facts": target,
                    "target_exact_job_membership": target_job,
                    "requests_hex": [value.hex() for value in requests],
                    "responses_hex": [value.hex() for value in responses],
                    "stderr_hex": [value.hex() for value in diagnostics],
                    "stdout_protocol_only": responses == expected_responses,
                    "stderr_isolated": diagnostics == expected_diagnostics,
                    "terminal_translation_absent": responses == expected_responses,
                    "eof_propagated": target_exit == 0,
                    "target_exit_code": target_exit,
                    "bootstrap_exit_code": bootstrap_exit,
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        api.close(target_handle)
        _close_named_pipe(api, pipe)
        _close_process(api, process)


def _cancellation_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> dict[str, object]:
    pipe: _NamedPipe | None = None
    process: _CreatedProcess | None = None
    target_handle = 0
    try:
        pipe = _create_named_pipe(api, launcher.profile.sid_text)
        with WindowsJobObject.create() as job:
            process = _spawn_pipe_bootstrap(launcher, child, "relay-cancel", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe)
            bootstrap = _receive_json_frame(api, pipe.handle, _FRAME_HELLO)
            target = _receive_json_frame(api, pipe.handle, _FRAME_TARGET)
            ready_kind, _ = _receive_frame(api, pipe.handle)
            target_handle = _open_process(api, cast(int, target["pid"]))
            target_job = _is_in_job(api, target_handle, job.process_creation_handle)
            job.terminate(74)
            bootstrap_terminated = int(api.wait(process.process_handle, 10000)) == _WAIT_OBJECT_0
            target_terminated = int(api.wait(target_handle, 10000)) == _WAIT_OBJECT_0
            passed = bool(
                ready_kind == _FRAME_READY
                and client_pid == process.pid
                and _valid_appcontainer_facts(bootstrap, launcher.profile.sid_text)
                and _valid_appcontainer_facts(target, launcher.profile.sid_text)
                and target_job
                and bootstrap_terminated
                and target_terminated
            )
            return _status(
                passed,
                {
                    "connected_client_pid": client_pid,
                    "bootstrap_facts": bootstrap,
                    "target_facts": target,
                    "target_exact_job_membership": target_job,
                    "bootstrap_terminated": bootstrap_terminated,
                    "target_terminated": target_terminated,
                    "job_termination_exit_code": 74,
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        api.close(target_handle)
        _close_named_pipe(api, pipe)
        _close_process(api, process)


def _plain_inheritance_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> dict[str, object]:
    stdin_read, stdin_write = _make_anonymous_pipe(
        api, parent_reads=False, appcontainer_sid=launcher.profile.sid_text
    )
    stdout_read, stdout_write = _make_anonymous_pipe(
        api, parent_reads=True, appcontainer_sid=launcher.profile.sid_text
    )
    stderr_read, stderr_write = _make_anonymous_pipe(
        api, parent_reads=True, appcontainer_sid=launcher.profile.sid_text
    )
    process: _CreatedProcess | None = None
    payload = b"plain-inheritance\x00" + "机械路径".encode() + b"\r\n"
    try:
        with WindowsJobObject.create() as job:
            process = launcher.spawn_appcontainer(
                child,
                ["plain-target"],
                job_handle=job.process_creation_handle,
                stdio_handles=(stdin_read, stdout_write, stderr_write),
                use_handle_list=False,
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            exact_job = _is_in_job(api, process.process_handle, job.process_creation_handle)
            api.close(stdin_read)
            stdin_read = 0
            api.close(stdout_write)
            stdout_write = 0
            api.close(stderr_write)
            stderr_write = 0
            _write_all(api, stdin_write, payload)
            api.close(stdin_write)
            stdin_write = 0
            exit_code = _wait_exit(api, process)
            stdout = _read_all(api, stdout_read)
            stderr = _read_all(api, stderr_read)
            lines = stderr.splitlines()
            facts = json.loads(lines[0].decode("utf-8")) if lines else {}
            passed = bool(
                exit_code == 37
                and exact_job
                and stdout == payload
                and b"PLAIN_STDERR:diagnostic" in stderr
                and isinstance(facts, dict)
                and _valid_appcontainer_facts(facts, launcher.profile.sid_text)
            )
            return _status(
                passed,
                {
                    "classification": (
                        "PLAIN_INHERITANCE_MECHANICALLY_WORKS" if passed else "MECHANIC_FAILED"
                    ),
                    "handle_list_used": False,
                    "b_inherit_handles": True,
                    "exact_job_membership": exact_job,
                    "stdout_exact": stdout == payload,
                    "stderr_hex": stderr.hex(),
                    "target_facts": facts,
                    "exit_code": exit_code,
                    "production_recommendation": False,
                    "controller_handle_table_race_risk": True,
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        for handle in (
            stdin_read,
            stdin_write,
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        ):
            api.close(handle)
        _close_process(api, process)


def _trusted_launcher_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> dict[str, object]:
    stdin_read, stdin_write = _make_anonymous_pipe(
        api, parent_reads=False, appcontainer_sid=launcher.profile.sid_text
    )
    stdout_read, stdout_write = _make_anonymous_pipe(
        api, parent_reads=True, appcontainer_sid=launcher.profile.sid_text
    )
    stderr_read, stderr_write = _make_anonymous_pipe(
        api, parent_reads=True, appcontainer_sid=launcher.profile.sid_text
    )
    process: _CreatedProcess | None = None
    payload = b"trusted-launcher\x00" + "最小可信".encode() + b"\n"
    try:
        with WindowsJobObject.create() as job:
            process = launcher.spawn_trusted_launcher(
                child,
                ["trusted-launcher", launcher.profile.name, str(child)],
                job_handle=job.process_creation_handle,
                stdio_handles=(stdin_read, stdout_write, stderr_write),
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            launcher_job = _is_in_job(api, process.process_handle, job.process_creation_handle)
            api.close(stdin_read)
            stdin_read = 0
            api.close(stdout_write)
            stdout_write = 0
            api.close(stderr_write)
            stderr_write = 0
            _write_all(api, stdin_write, payload)
            api.close(stdin_write)
            stdin_write = 0
            launcher_exit = _wait_exit(api, process)
            stdout = _read_all(api, stdout_read)
            stderr = _read_all(api, stderr_read)
            lines = stderr.splitlines()
            facts = json.loads(lines[0].decode("utf-8")) if lines else {}
            passed = bool(
                launcher_exit == 37
                and launcher_job
                and stdout == payload
                and b"PLAIN_STDERR:diagnostic" in stderr
                and isinstance(facts, dict)
                and _valid_appcontainer_facts(facts, launcher.profile.sid_text)
            )
            return _status(
                passed,
                {
                    "classification": "TRUSTED_LAUNCHER_MECHANICALLY_WORKS" if passed else "FAIL",
                    "controller_to_launcher_handle_list": True,
                    "launcher_to_appcontainer_plain_inheritance": True,
                    "launcher_exact_job_membership": launcher_job,
                    "target_facts": facts,
                    "stdout_exact": stdout == payload,
                    "stderr_hex": stderr.hex(),
                    "target_exit_propagated_by_launcher": launcher_exit,
                    "production_implementation": False,
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        for handle in (
            stdin_read,
            stdin_write,
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        ):
            api.close(handle)
        _close_process(api, process)


def _conpty_smoke_gate(
    api: _WinApi,
    launcher: _Launcher,
    child: Path,
) -> dict[str, object]:
    input_read, input_write = _make_anonymous_pipe(api, parent_reads=False)
    output_read, output_write = _make_anonymous_pipe(api, parent_reads=True)
    pseudoconsole = ctypes.c_void_p()
    process: _CreatedProcess | None = None
    try:
        result = int(
            api.create_pseudoconsole(
                _Coord(80, 24),
                input_read,
                output_write,
                0,
                ctypes.byref(pseudoconsole),
            )
        )
        if result != 0 or not pseudoconsole.value:
            raise OSError(result & 0xFFFFFFFF, f"CreatePseudoConsole failed: 0x{result:08x}")
        with WindowsJobObject.create() as job:
            process = launcher.spawn_appcontainer(
                child,
                ["conpty-smoke"],
                job_handle=job.process_creation_handle,
                pseudoconsole=int(pseudoconsole.value),
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            exact_job = _is_in_job(api, process.process_handle, job.process_creation_handle)
            resize_hresult = int(api.resize_pseudoconsole(pseudoconsole, _Coord(100, 30)))
            _write_all(api, input_write, b"ipc-poc1b\r\n")
            exit_code = _wait_exit(api, process)
            api.close_pseudoconsole(pseudoconsole)
            pseudoconsole = ctypes.c_void_p()
            api.close(output_write)
            output_write = 0
            output = _read_all(api, output_read).decode("utf-8", errors="replace")
            passed = bool(
                exit_code == 0
                and exact_job
                and resize_hresult == 0
                and "CONPTY_POC1B_READY" in output
                and "CONPTY_POC1B_ECHO:ipc-poc1b" in output
            )
            return _status(
                passed,
                {
                    "exit_code": exit_code,
                    "exact_job_membership": exact_job,
                    "resize_hresult": resize_hresult & 0xFFFFFFFF,
                    "output": output,
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        if pseudoconsole.value:
            api.close_pseudoconsole(pseudoconsole)
        for handle in (input_read, input_write, output_read, output_write):
            api.close(handle)
        _close_process(api, process)


def _run(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    api = _WinApi()
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    profile_name = f"NeuroCode.Poc1B.{uuid.uuid4().hex}"
    runner: dict[str, object] = {
        "platform": platform.platform(),
        "version": platform.version(),
        "release": platform.release(),
        "edition": platform.win32_edition(),
        "win32_version": platform.win32_ver(),
        "architecture": platform.machine(),
        "python": sys.version,
        "github_runner_name": os.environ.get("RUNNER_NAME"),
        "github_runner_os": os.environ.get("RUNNER_OS"),
    }
    runner.update(_controller_token_facts(api))
    controller_is_admin = bool(runner["elevated"] or runner["administrator_group_enabled"])
    result: dict[str, object] = {
        "classification": "EVIDENCE_ONLY_DO_NOT_MERGE",
        "profile_name": profile_name,
        "runner": runner,
        "non_admin_acceptance": (
            "NON_ADMIN_ACCEPTANCE_UNRESOLVED"
            if controller_is_admin
            else "NON_ADMIN_CONTROLLER_OBSERVED"
        ),
        "runtime_authority": {
            "python": "RUNTIME_AUTHORITY_UNRESOLVED_FROM_POC1",
            "git": "RUNTIME_AUTHORITY_UNRESOLVED_FROM_POC1",
            "expanded_in_this_poc": False,
        },
        "gates": {},
    }
    gates = cast(dict[str, object], result["gates"])
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-appcontainer-ipc-poc1b-"))
    child = fixture / "windows_appcontainer_ipc_poc1b_child.exe"
    shutil.copy2(Path(args.child).resolve(), child)
    profile: _Profile | None = None
    critical_failures: list[str] = []
    try:
        profile = _Profile(api, profile_name)
        result["appcontainer_sid"] = profile.sid_text
        result["fixture_acl_command"] = _grant_fixture(fixture, profile.sid_text)
        launcher = _Launcher(api, profile, fixture)

        connect, stream = _named_pipe_connect_gate(api, launcher, child)
        gates["A_named_pipe_connect"] = connect
        gates["A_byte_stream"] = stream
        gates["A_unauthorized_client"] = _unauthorized_client_gate(api, launcher, child)
        gates["A2_descendant_stdio"] = _stdio_relay_gate(api, launcher, child)
        gates["A2_mcp_semantic"] = _mcp_relay_gate(api, launcher, child)
        gates["A2_cancellation"] = _cancellation_gate(api, launcher, child)
        gates["B_plain_inheritance"] = _plain_inheritance_gate(api, launcher, child)
        if cast(dict[str, object], gates["B_plain_inheritance"])["status"] == "PASS":
            gates["B2_trusted_launcher"] = _trusted_launcher_gate(api, launcher, child)
        else:
            gates["B2_trusted_launcher"] = {
                "status": "UNRESOLVED",
                "detail": "not executed because plain inheritance did not pass",
            }
        gates["conpty_regression"] = _conpty_smoke_gate(api, launcher, child)

        named_requirements = (
            "A_named_pipe_connect",
            "A_byte_stream",
            "A_unauthorized_client",
            "A2_descendant_stdio",
            "A2_mcp_semantic",
            "A2_cancellation",
            "conpty_regression",
        )
        for name in named_requirements:
            if cast(dict[str, object], gates[name])["status"] != "PASS":
                critical_failures.append(name)
        if not critical_failures:
            decision = "WINDOWS_APPCONTAINER_IPC_VIABLE"
        elif cast(dict[str, object], gates["B2_trusted_launcher"])["status"] == "PASS":
            decision = "WINDOWS_APPCONTAINER_TRUSTED_LAUNCHER_POC_REQUIRED"
        else:
            decision = "WINDOWS_APPCONTAINER_IPC_BLOCKED"
        result["architecture_decision"] = decision
    except BaseException as error:
        result["harness_error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        critical_failures.append("HARNESS")
        result["architecture_decision"] = "WINDOWS_APPCONTAINER_IPC_INCONCLUSIVE"
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
    report_path.write_text(
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
