"""Evidence-only probe for Windows AppContainer IPC boundaries.

The harness intentionally lives outside production code.  It tests whether a
full-trust controller can communicate with an AppContainer bootstrap without
crossing that boundary with inherited stdio handles, then tests descendant stdio
inside the already-established AppContainer authority.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
import io
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
_LOGON_WITH_PROFILE = 0x00000001
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
_WIN_CAPABILITY_INTERNET_CLIENT_SID = 85
_SE_GROUP_ENABLED = 0x00000004
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ID_INFO_CLASS = 18

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


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@dataclass(slots=True)
class _CreatedProcess:
    process_handle: int
    thread_handle: int
    pid: int


@dataclass(slots=True)
class _Capabilities:
    structure: _SecurityCapabilities
    keepalive: list[object]


@dataclass(slots=True)
class _NamedPipe:
    handle: int
    client_name: str
    server_name: str
    session_id: int
    appcontainer_named_object_path: str
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
        self.get_appcontainer_named_object_path = _load_function(
            self.kernel32,
            "GetAppContainerNamedObjectPath",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
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
        self.create_job_object = _load_function(
            self.kernel32,
            "CreateJobObjectW",
            [ctypes.c_void_p, ctypes.c_wchar_p],
            ctypes.c_void_p,
        )
        self.set_information_job_object = _load_function(
            self.kernel32,
            "SetInformationJobObject",
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.terminate_job_object = _load_function(
            self.kernel32,
            "TerminateJobObject",
            [ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.create_process_with_logon = _load_function(
            self.advapi32,
            "CreateProcessWithLogonW",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.POINTER(_StartupInfoW),
                ctypes.POINTER(_ProcessInformation),
            ],
            ctypes.c_int32,
        )
        self.get_current_process_id = _load_function(
            self.kernel32,
            "GetCurrentProcessId",
            [],
            ctypes.c_uint32,
        )
        self.process_id_to_session_id = _load_function(
            self.kernel32,
            "ProcessIdToSessionId",
            [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)],
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
        self.create_file = _load_function(
            self.kernel32,
            "CreateFileW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ],
            ctypes.c_void_p,
        )
        self.get_file_information_ex = _load_function(
            self.kernel32,
            "GetFileInformationByHandleEx",
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.get_final_path = _load_function(
            self.kernel32,
            "GetFinalPathNameByHandleW",
            [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_uint32,
        )
        self.get_volume_path = _load_function(
            self.kernel32,
            "GetVolumePathNameW",
            [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.get_volume_information = _load_function(
            self.kernel32,
            "GetVolumeInformationW",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_wchar_p,
                ctypes.c_uint32,
            ],
            ctypes.c_int32,
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


class _EvidenceJob:
    def __init__(self, api: _WinApi, handle: int) -> None:
        self.api = api
        self.handle = handle

    @classmethod
    def create(cls, api: _WinApi) -> _EvidenceJob:
        handle = int(api.create_job_object(None, None) or 0)
        if not handle:
            api.error("CreateJobObjectW")
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.set_information_job_object(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            api.close(handle)
            api.error("SetInformationJobObject")
        return cls(api, handle)

    @property
    def process_creation_handle(self) -> int:
        if not self.handle:
            raise RuntimeError("evidence Job Object is closed")
        return self.handle

    def terminate(self, exit_code: int) -> None:
        if not self.api.terminate_job_object(self.process_creation_handle, exit_code):
            self.api.error("TerminateJobObject")

    def close(self) -> None:
        handle = self.handle
        self.handle = 0
        self.api.close(handle)

    def __enter__(self) -> _EvidenceJob:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


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
                "Neuro Code evidence-only AppContainer runtime POC2B",
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
    def __init__(
        self,
        api: _WinApi,
        profile: _Profile,
        cwd: Path,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.api = api
        self.profile = profile
        self.cwd = cwd
        self.environment = environment

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
        internet: bool = False,
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
        capability_values = self.profile.capabilities(internet=internet) if appcontainer else None
        capabilities = capability_values.structure if capability_values is not None else None
        environment_block = None
        if self.environment is not None:
            entries = [f"{key}={value}" for key, value in self.environment.items()]
            environment_block = ctypes.create_unicode_buffer(
                "\0".join(sorted(entries, key=str.casefold)) + "\0\0"
            )
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
                environment_block,
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
        internet: bool = False,
    ) -> _CreatedProcess:
        return self._spawn(
            application,
            arguments,
            appcontainer=True,
            job_handle=job_handle,
            stdio_handles=stdio_handles,
            use_handle_list=use_handle_list,
            pseudoconsole=pseudoconsole,
            internet=internet,
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


def _appcontainer_named_object_path(api: _WinApi, profile: _Profile) -> str:
    required = ctypes.c_uint32()
    api.get_appcontainer_named_object_path(None, profile.sid, 0, None, ctypes.byref(required))
    if not required.value:
        api.error("GetAppContainerNamedObjectPath(size)")
    buffer = ctypes.create_unicode_buffer(required.value)
    if not api.get_appcontainer_named_object_path(
        None,
        profile.sid,
        len(buffer),
        buffer,
        ctypes.byref(required),
    ):
        api.error("GetAppContainerNamedObjectPath")
    return buffer.value


def _create_named_pipe(api: _WinApi, profile: _Profile) -> _NamedPipe:
    leaf = f"NeuroCode-Poc2B-{uuid.uuid4().hex}"
    client_name = rf"\\.\pipe\LOCAL\{leaf}"
    object_path = _appcontainer_named_object_path(api, profile)
    session_id = ctypes.c_uint32()
    if not api.process_id_to_session_id(api.get_current_process_id(), ctypes.byref(session_id)):
        api.error("ProcessIdToSessionId")
    server_name = rf"\\.\pipe\Sessions\{session_id.value}\{object_path.lstrip(chr(92))}\{leaf}"
    controller_sid = _controller_user_sid()
    configured_sddl = (
        f"D:P(A;;GA;;;SY)(A;;GRGW;;;{controller_sid})(A;;GRGW;;;{profile.sid_text})S:(ML;;NW;;;LW)"
    )
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
            server_name,
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
        return _NamedPipe(
            value,
            client_name,
            server_name,
            int(session_id.value),
            object_path,
            configured_sddl,
            effective,
        )
    finally:
        if descriptor.value:
            api.local_free(descriptor)


def _process_exit_code(api: _WinApi, process: _CreatedProcess) -> int:
    exit_code = ctypes.c_uint32()
    if not api.exit_code(process.process_handle, ctypes.byref(exit_code)):
        api.error("GetExitCodeProcess")
    return int(exit_code.value)


def _connect_named_pipe(
    api: _WinApi,
    pipe: _NamedPipe,
    process: _CreatedProcess,
    timeout_seconds: float = 15,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if api.connect_named_pipe(pipe.handle, None):
            break
        error = cast(Any, ctypes).get_last_error()
        if error == _ERROR_PIPE_CONNECTED:
            break
        if error != _ERROR_PIPE_LISTENING:
            raise OSError(error, f"ConnectNamedPipe failed with Windows error {error}")
        if int(api.wait(process.process_handle, 0)) == _WAIT_OBJECT_0:
            exit_code = _process_exit_code(api, process)
            raise ChildProcessError(
                exit_code,
                f"AppContainer bootstrap exited before pipe connection; "
                f"exit/Win32 code {exit_code}",
            )
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
        controller_sid = _controller_user_sid()
        sddl = (
            f"D:P(A;;GA;;;SY)(A;;GA;;;{controller_sid})(A;;GA;;;{appcontainer_sid})S:(ML;;NW;;;LW)"
        )
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


def _controller_user_sid() -> str:
    completed = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = list(csv.reader(io.StringIO(completed.stdout)))
    if len(rows) != 1 or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise OSError(f"unable to parse controller SID from whoami: {rows!r}")
    return rows[0][1]


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
    job: _EvidenceJob,
) -> _CreatedProcess:
    return launcher.spawn_appcontainer(
        child,
        [mode, pipe.client_name],
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
        pipe = _create_named_pipe(api, launcher.profile)
        with _EvidenceJob.create(api) as job:
            process = _spawn_pipe_bootstrap(launcher, child, "byte-stream", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe, process)
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
                "client_pipe_name": pipe.client_name,
                "server_pipe_name": pipe.server_name,
                "session_id": pipe.session_id,
                "appcontainer_named_object_path": pipe.appcontainer_named_object_path,
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
                    "client_pipe_name": pipe.client_name,
                    "server_pipe_name": pipe.server_name,
                    "session_id": pipe.session_id,
                    "appcontainer_named_object_path": pipe.appcontainer_named_object_path,
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
    unauthorized_profile: _Profile | None = None
    process: _CreatedProcess | None = None
    try:
        pipe = _create_named_pipe(api, launcher.profile)
        unauthorized_profile = _Profile(
            api,
            f"NeuroCode.Poc2B.Unauthorized.{uuid.uuid4().hex}",
        )
        acl_command = _grant_fixture(child.parent, unauthorized_profile.sid_text)
        unauthorized_launcher = _Launcher(api, unauthorized_profile, child.parent)
        with _EvidenceJob.create(api) as job:
            process = unauthorized_launcher.spawn_appcontainer(
                child,
                ["pipe-denied", pipe.server_name],
                job_handle=job.process_creation_handle,
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            exact_job = _is_in_job(api, process.process_handle, job.process_creation_handle)
            exit_code = _wait_exit(api, process, 20000)
        passed = bool(
            exit_code == 0
            and exact_job
            and unauthorized_profile.sid_text.casefold() != launcher.profile.sid_text.casefold()
        )
        return _status(
            passed,
            {
                "probe_is_appcontainer": True,
                "probe_is_target_appcontainer": False,
                "target_appcontainer_sid": launcher.profile.sid_text,
                "probe_appcontainer_sid": unauthorized_profile.sid_text,
                "fixture_acl_command": acl_command,
                "exit_code": exit_code,
                "observed_win32_error_by_child_contract": _ERROR_ACCESS_DENIED,
                "exact_job_membership": exact_job,
                "configured_sddl": pipe.configured_sddl,
                "effective_dacl_sddl": pipe.effective_dacl_sddl,
                "client_pipe_name": pipe.client_name,
                "server_pipe_name": pipe.server_name,
                "session_id": pipe.session_id,
                "appcontainer_named_object_path": pipe.appcontainer_named_object_path,
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
                "target_appcontainer_sid": launcher.profile.sid_text,
                "probe_appcontainer_sid": (
                    unauthorized_profile.sid_text if unauthorized_profile else None
                ),
            },
        )
    finally:
        _close_process(api, process)
        if unauthorized_profile is not None:
            with contextlib.suppress(BaseException):
                unauthorized_profile.close()
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
        pipe = _create_named_pipe(api, launcher.profile)
        with _EvidenceJob.create(api) as job:
            process = _spawn_pipe_bootstrap(launcher, child, "relay-stdio", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe, process)
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
        pipe = _create_named_pipe(api, launcher.profile)
        with _EvidenceJob.create(api) as job:
            process = _spawn_pipe_bootstrap(launcher, child, "relay-mcp", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe, process)
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
        pipe = _create_named_pipe(api, launcher.profile)
        with _EvidenceJob.create(api) as job:
            process = _spawn_pipe_bootstrap(launcher, child, "relay-cancel", pipe, job)
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe, process)
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
        with _EvidenceJob.create(api) as job:
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
        with _EvidenceJob.create(api) as job:
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
        with _EvidenceJob.create(api) as job:
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
            _write_all(api, input_write, b"ipc-poc2b\r\n")
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
                and "CONPTY_POC2B_READY" in output
                and "CONPTY_POC2B_ECHO:ipc-poc2b" in output
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


@dataclass(slots=True)
class _AuthorityRoot:
    path: Path
    mode: str
    purpose: str
    trust: str
    identity: dict[str, object]
    inventory: dict[str, object]
    grant_elapsed_ms: float = 0.0
    cleanup_elapsed_ms: float = 0.0


def _run_checked(
    command: list[str], *, expected: set[int] | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    allowed = {0} if expected is None else expected
    if completed.returncode not in allowed:
        raise OSError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )
    return completed


def _handle_identity(api: _WinApi, path: Path) -> dict[str, object]:
    handle = api.create_file(
        str(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    value = int(handle or 0)
    if not value or value == _INVALID_HANDLE_VALUE:
        api.error(f"CreateFileW(identity: {path})")
    try:
        file_id = _FileIdInfo()
        if not api.get_file_information_ex(
            value,
            _FILE_ID_INFO_CLASS,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            api.error(f"GetFileInformationByHandleEx({path})")
        needed = int(api.get_final_path(value, None, 0, 0))
        if not needed:
            api.error(f"GetFinalPathNameByHandleW(size: {path})")
        canonical = ctypes.create_unicode_buffer(needed + 1)
        if not api.get_final_path(value, canonical, len(canonical), 0):
            api.error(f"GetFinalPathNameByHandleW({path})")
        volume_path = ctypes.create_unicode_buffer(32768)
        if not api.get_volume_path(str(path), volume_path, len(volume_path)):
            api.error(f"GetVolumePathNameW({path})")
        filesystem = ctypes.create_unicode_buffer(64)
        volume_serial = ctypes.c_uint32()
        if not api.get_volume_information(
            volume_path,
            None,
            0,
            ctypes.byref(volume_serial),
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            api.error(f"GetVolumeInformationW({path})")
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return {
            "requested_path": str(path),
            "canonical_handle_path": canonical.value,
            "volume_path": volume_path.value,
            "volume_serial_file_id_info": f"{int(file_id.VolumeSerialNumber):016x}",
            "volume_serial_win32": f"{int(volume_serial.value):08x}",
            "file_id_128": bytes(file_id.FileId).hex(),
            "filesystem": filesystem.value,
            "reparse_point": bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT),
        }
    finally:
        api.close(value)


def _inventory_root(path: Path, *, limit: int = 100_000) -> dict[str, object]:
    objects = 0
    files = 0
    directories = 0
    bytes_exposed = 0
    started = time.perf_counter()
    stack = [path]
    while stack:
        current = stack.pop()
        directories += 1
        objects += 1
        if objects > limit:
            raise OSError(f"runtime authority inventory exceeded hard limit {limit}: {path}")
        with os.scandir(current) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                objects += 1
                if objects > limit:
                    raise OSError(
                        f"runtime authority inventory exceeded hard limit {limit}: {path}"
                    )
                if entry.is_symlink() or bool(
                    getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files += 1
                    bytes_exposed += int(info.st_size)
    return {
        "object_count": objects,
        "file_count": files,
        "directory_count": directories,
        "total_file_bytes": bytes_exposed,
        "inventory_limit": limit,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _authority_root(
    api: _WinApi,
    path: Path,
    mode: str,
    purpose: str,
    trust: str,
) -> _AuthorityRoot:
    resolved = path.resolve(strict=True)
    identity = _handle_identity(api, resolved)
    if str(identity["filesystem"]).casefold() != "ntfs":
        raise OSError(f"runtime authority root is not NTFS: {resolved}")
    if identity["reparse_point"]:
        raise OSError(f"runtime authority root is a reparse point: {resolved}")
    return _AuthorityRoot(
        resolved,
        mode,
        purpose,
        trust,
        identity,
        _inventory_root(resolved),
    )


def _deduplicate_roots(roots: list[_AuthorityRoot]) -> list[_AuthorityRoot]:
    result: list[_AuthorityRoot] = []
    for candidate in sorted(
        roots, key=lambda item: (len(item.path.parts), str(item.path).casefold())
    ):
        duplicate = next(
            (
                item
                for item in result
                if candidate.path == item.path or candidate.path.is_relative_to(item.path)
            ),
            None,
        )
        if duplicate is None:
            result.append(candidate)
            continue
        if duplicate.mode != candidate.mode:
            raise OSError(
                f"conflicting nested runtime authority: {duplicate.path}={duplicate.mode}, "
                f"{candidate.path}={candidate.mode}"
            )
    return result


def _grant_root(root: _AuthorityRoot, sid: str) -> dict[str, object]:
    rights = "(OI)(CI)(RX)" if root.mode == "RX" else "(OI)(CI)(M)"
    started = time.perf_counter()
    completed = _run_checked(["icacls", str(root.path), "/grant", f"*{sid}:{rights}"])
    root.grant_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "path": str(root.path),
        "rights": rights,
        "explicit_acl_api_mutations": 1,
        "output": (completed.stdout + completed.stderr).strip(),
        "elapsed_ms": root.grant_elapsed_ms,
    }


def _cleanup_root(root: _AuthorityRoot, sid: str) -> dict[str, object]:
    started = time.perf_counter()
    completed = _run_checked(["icacls", str(root.path), "/remove:g", f"*{sid}"])
    root.cleanup_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "path": str(root.path),
        "exact_sid": sid,
        "output": (completed.stdout + completed.stderr).strip(),
        "elapsed_ms": root.cleanup_elapsed_ms,
    }


def _runtime_root_record(root: _AuthorityRoot) -> dict[str, object]:
    return {
        "path": str(root.path),
        "mode": root.mode,
        "purpose": root.purpose,
        "trust": root.trust,
        "owner": subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"(Get-Acl -LiteralPath '{root.path}').Owner"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "writable_by_current_user": os.access(root.path, os.W_OK),
        "inside_workspace": root.trust == "WORKSPACE_RUNTIME",
        "inside_userprofile": str(root.path)
        .casefold()
        .startswith(str(Path.home()).casefold() + os.sep.casefold()),
        "identity": root.identity,
        "inventory": root.inventory,
        "grant_elapsed_ms": root.grant_elapsed_ms,
        "cleanup_elapsed_ms": root.cleanup_elapsed_ms,
    }


def _private_environment(private_home: Path, runtime_bins: list[Path]) -> dict[str, str]:
    temp = private_home / "Temp"
    appdata = private_home / "AppData" / "Roaming"
    local_appdata = private_home / "AppData" / "Local"
    for path in (temp, appdata, local_appdata):
        path.mkdir(parents=True, exist_ok=True)
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    return {
        "APPDATA": str(appdata),
        "COMSPEC": str(system_root / "System32" / "cmd.exe"),
        "HOME": str(private_home),
        "HOMEDRIVE": private_home.drive,
        "HOMEPATH": str(private_home)[len(private_home.drive) :],
        "LOCALAPPDATA": str(local_appdata),
        "PATH": os.pathsep.join(str(path) for path in [*runtime_bins, system_root / "System32"]),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "SYSTEMROOT": str(system_root),
        "TEMP": str(temp),
        "TMP": str(temp),
        "USERPROFILE": str(private_home),
        "WINDIR": str(system_root),
    }


def _command_relay_gate(
    api: _WinApi,
    launcher: _Launcher,
    bootstrap: Path,
    command: list[str],
    *,
    input_payload: bytes = b"",
    internet: bool = False,
    timeout_ms: int = 60_000,
) -> dict[str, object]:
    pipe: _NamedPipe | None = None
    process: _CreatedProcess | None = None
    target_handle = 0
    try:
        pipe = _create_named_pipe(api, launcher.profile)
        with _EvidenceJob.create(api) as job:
            process = launcher.spawn_appcontainer(
                bootstrap,
                ["relay-command", pipe.client_name, subprocess.list2cmdline(command)],
                job_handle=job.process_creation_handle,
                internet=internet,
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            client_pid = _connect_named_pipe(api, pipe, process)
            bootstrap_facts = _receive_json_frame(api, pipe.handle, _FRAME_HELLO)
            target = _receive_json_frame(api, pipe.handle, _FRAME_TARGET)
            target_pid = cast(int, target["pid"])
            with contextlib.suppress(OSError):
                target_handle = _open_process(api, target_pid)
            target_exact_job = (
                _is_in_job(api, target_handle, job.process_creation_handle)
                if target_handle
                else bool(target.get("in_job"))
            )
            _send_frame(api, pipe.handle, _FRAME_DATA, input_payload)
            _send_frame(api, pipe.handle, _FRAME_EOF)
            stdout_kind, stdout = _receive_frame(api, pipe.handle)
            stderr_kind, stderr = _receive_frame(api, pipe.handle)
            exit_kind, exit_payload = _receive_frame(api, pipe.handle)
            bootstrap_exit = _wait_exit(api, process, timeout_ms)
            target_exit = struct.unpack("<I", exit_payload)[0] if len(exit_payload) == 4 else None
            passed = bool(
                client_pid == process.pid
                and _valid_appcontainer_facts(bootstrap_facts, launcher.profile.sid_text)
                and target_exact_job
                and stdout_kind == _FRAME_STDOUT
                and stderr_kind == _FRAME_STDERR
                and exit_kind == _FRAME_EXIT
                and bootstrap_exit == 0
            )
            return _status(
                passed,
                {
                    "command": command,
                    "internet_client": internet,
                    "bootstrap_facts": bootstrap_facts,
                    "bootstrap_pid": process.pid,
                    "connected_client_pid": client_pid,
                    "target_pid": target_pid,
                    "target_reported_job": target.get("in_job"),
                    "target_exact_job_membership": target_exact_job,
                    "stdout_hex": stdout.hex(),
                    "stderr_hex": stderr.hex(),
                    "stdout_text": stdout.decode("utf-8", errors="replace"),
                    "stderr_text": stderr.decode("utf-8", errors="replace"),
                    "target_exit_code": target_exit,
                    "bootstrap_exit_code": bootstrap_exit,
                    "controller_to_target_handle_list": False,
                    "bootstrap_internal_handle_list": True,
                },
            )
    except BaseException as error:
        return _status(
            False, {"error_type": type(error).__name__, "error": str(error), "command": command}
        )
    finally:
        api.close(target_handle)
        _close_named_pipe(api, pipe)
        _close_process(api, process)


def _real_shell_cancellation_gate(
    api: _WinApi,
    launcher: _Launcher,
    bootstrap: Path,
    command: list[str],
) -> dict[str, object]:
    pipe: _NamedPipe | None = None
    process: _CreatedProcess | None = None
    target_handle = 0
    try:
        pipe = _create_named_pipe(api, launcher.profile)
        with _EvidenceJob.create(api) as job:
            process = launcher.spawn_appcontainer(
                bootstrap,
                ["relay-command", pipe.client_name, subprocess.list2cmdline(command)],
                job_handle=job.process_creation_handle,
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            _connect_named_pipe(api, pipe, process)
            bootstrap_facts = _receive_json_frame(api, pipe.handle, _FRAME_HELLO)
            target = _receive_json_frame(api, pipe.handle, _FRAME_TARGET)
            target_handle = _open_process(api, cast(int, target["pid"]))
            exact_job = _is_in_job(api, target_handle, job.process_creation_handle)
            job.terminate(79)
            bootstrap_terminated = int(api.wait(process.process_handle, 10_000)) == _WAIT_OBJECT_0
            target_terminated = int(api.wait(target_handle, 10_000)) == _WAIT_OBJECT_0
            return _status(
                bool(
                    _valid_appcontainer_facts(bootstrap_facts, launcher.profile.sid_text)
                    and exact_job
                    and bootstrap_terminated
                    and target_terminated
                ),
                {
                    "command": command,
                    "bootstrap_facts": bootstrap_facts,
                    "target_pid": target["pid"],
                    "target_exact_job_membership": exact_job,
                    "bootstrap_terminated": bootstrap_terminated,
                    "target_terminated": target_terminated,
                    "job_termination_code": 79,
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        api.close(target_handle)
        _close_named_pipe(api, pipe)
        _close_process(api, process)


def _token_facts(api: _WinApi) -> dict[str, object]:
    facts = _controller_token_facts(api)
    facts["user_sid"] = _controller_user_sid()
    groups = subprocess.run(
        ["whoami", "/groups", "/fo", "csv"], capture_output=True, text=True, check=False
    ).stdout
    privileges = subprocess.run(
        ["whoami", "/priv", "/fo", "csv"], capture_output=True, text=True, check=False
    ).stdout
    if "Medium Mandatory Level" in groups:
        integrity = "MEDIUM"
    elif "High Mandatory Level" in groups:
        integrity = "HIGH"
    elif "Low Mandatory Level" in groups:
        integrity = "LOW"
    else:
        integrity = "UNKNOWN"
    facts.update(
        {
            "integrity_level": integrity,
            "whoami": subprocess.run(
                ["whoami"], capture_output=True, text=True, check=False
            ).stdout.strip(),
            "privileges_csv": privileges,
        }
    )
    return facts


def _write_probe_files(
    workspace: Path,
    outside_secret: Path,
    host_marker: Path,
) -> dict[str, Path]:
    workspace_file = workspace / "authorized.txt"
    workspace_file.write_text("WORKSPACE_AUTHORIZED", encoding="utf-8")
    python_probe = workspace / "python_probe.py"
    python_probe.write_text(
        """import asyncio
import json
import os
import pathlib
import sys

def attempt(path):
    try:
        return {\"read\": True, \"value\": pathlib.Path(path).read_text(encoding=\"utf-8\")}
    except OSError as error:
        return {\"read\": False, \"errno\": error.errno, \"winerror\": getattr(error, \"winerror\", None)}

result = {
    \"imports\": [sys.__name__, os.__name__, json.__name__, pathlib.__name__, asyncio.__name__],
    \"workspace\": attempt(sys.argv[1]),
    \"outside\": attempt(sys.argv[2]),
    \"host\": attempt(sys.argv[3]),
    \"environment\": {key: os.environ.get(key) for key in (\"USERPROFILE\", \"HOME\", \"TEMP\", \"TMP\")},
    \"secret_sentinel\": os.environ.get(\"NEURO_SECRET_SENTINEL"),
    \"prefix\": sys.prefix,
    \"base_prefix\": sys.base_prefix,
}
print(json.dumps(result, sort_keys=True))
""",
        encoding="utf-8",
    )
    mcp_server = workspace / "mcp_server.py"
    mcp_server.write_text(
        """import json
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

server = FastMCP(\"neuro-poc2b\", log_level=\"ERROR\")

def attempt(name):
    try:
        return {\"read\": True, \"value\": Path(os.environ[name]).read_text(encoding=\"utf-8\")}
    except OSError as error:
        return {\"read\": False, \"errno\": error.errno, \"winerror\": getattr(error, \"winerror\", None)}

@server.tool()
def echo(text: str) -> str:
    return json.dumps({
        \"echo\": text,
        \"workspace\": attempt(\"NEURO_WORKSPACE_FILE\"),
        \"outside\": attempt(\"NEURO_OUTSIDE_SECRET\"),
        \"host\": attempt(\"NEURO_HOST_MARKER\"),
        \"home\": os.environ.get(\"HOME\"),
    }, sort_keys=True)

if __name__ == \"__main__\":
    server.run(transport=\"stdio\")
""",
        encoding="utf-8",
    )
    return {
        "workspace_file": workspace_file,
        "python_probe": python_probe,
        "mcp_server": mcp_server,
        "outside_secret": outside_secret,
        "host_marker": host_marker,
    }


def _command_expect(
    gate: dict[str, object],
    *,
    exit_code: int,
    stdout_contains: list[str] | None = None,
    stderr_contains: list[str] | None = None,
) -> dict[str, object]:
    detail = cast(dict[str, object], gate["detail"])
    passed = gate["status"] == "PASS" and detail.get("target_exit_code") == exit_code
    stdout = str(detail.get("stdout_text", ""))
    stderr = str(detail.get("stderr_text", ""))
    for value in stdout_contains or []:
        passed = passed and value in stdout
    for value in stderr_contains or []:
        passed = passed and value in stderr
    detail["expected_target_exit_code"] = exit_code
    detail["expected_stdout_contains"] = stdout_contains or []
    detail["expected_stderr_contains"] = stderr_contains or []
    gate["status"] = "PASS" if passed else "FAIL"
    return gate


def _python_gate(
    api: _WinApi,
    launcher: _Launcher,
    bootstrap: Path,
    python: Path,
    probes: dict[str, Path],
) -> dict[str, object]:
    gate = _command_relay_gate(
        api,
        launcher,
        bootstrap,
        [
            str(python),
            str(probes["python_probe"]),
            str(probes["workspace_file"]),
            str(probes["outside_secret"]),
            str(probes["host_marker"]),
        ],
    )
    detail = cast(dict[str, object], gate["detail"])
    parsed: dict[str, object] = {}
    if gate["status"] == "PASS" and detail.get("target_exit_code") == 0:
        try:
            parsed = cast(dict[str, object], json.loads(str(detail["stdout_text"]).strip()))
        except (ValueError, TypeError):
            parsed = {}
    detail["parsed_result"] = parsed
    environment = cast(dict[str, object], parsed.get("environment", {}))
    passed = bool(
        gate["status"] == "PASS"
        and detail.get("target_exit_code") == 0
        and cast(dict[str, object], parsed.get("workspace", {})).get("read") is True
        and cast(dict[str, object], parsed.get("outside", {})).get("read") is False
        and cast(dict[str, object], parsed.get("host", {})).get("read") is False
        and parsed.get("secret_sentinel") is None
        and len(cast(list[object], parsed.get("imports", []))) == 5
        and environment.get("HOME") == environment.get("USERPROFILE")
        and environment.get("TEMP") == environment.get("TMP")
    )
    gate["status"] = "PASS" if passed else "FAIL"
    return gate


def _node_gate(
    api: _WinApi,
    launcher: _Launcher,
    bootstrap: Path,
    node: Path,
    probes: dict[str, Path],
) -> dict[str, object]:
    script = (
        "const fs=require('fs');"
        "let denied=false;try{fs.readFileSync(process.argv[2],'utf8')}catch(e){denied=true;}"
        "console.log(JSON.stringify({ok:'ok',denied,home:process.env.HOME,secret:process.env.NEURO_SECRET_SENTINEL||null}));"
    )
    gate = _command_relay_gate(
        api,
        launcher,
        bootstrap,
        [str(node), "-e", script, "ignored", str(probes["outside_secret"])],
    )
    detail = cast(dict[str, object], gate["detail"])
    parsed: dict[str, object] = {}
    if gate["status"] == "PASS" and detail.get("target_exit_code") == 0:
        with contextlib.suppress(ValueError, TypeError):
            parsed = cast(dict[str, object], json.loads(str(detail["stdout_text"]).strip()))
    detail["parsed_result"] = parsed
    gate["status"] = (
        "PASS"
        if gate["status"] == "PASS"
        and detail.get("target_exit_code") == 0
        and parsed.get("ok") == "ok"
        and parsed.get("denied") is True
        and parsed.get("secret") is None
        else "FAIL"
    )
    return gate


def _mcp_gate(
    api: _WinApi,
    launcher: _Launcher,
    bootstrap: Path,
    python: Path,
    mcp_server: Path,
) -> dict[str, object]:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "neuro-poc2b", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "runtime-poc2b"}},
        },
    ]
    payload = b"".join(
        json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n" for request in requests
    )
    gate = _command_relay_gate(
        api,
        launcher,
        bootstrap,
        [str(python), str(mcp_server)],
        input_payload=payload,
    )
    detail = cast(dict[str, object], gate["detail"])
    responses: list[dict[str, object]] = []
    clean = True
    for line in str(detail.get("stdout_text", "")).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            clean = False
            continue
        if not isinstance(value, dict):
            clean = False
            continue
        responses.append(cast(dict[str, object], value))
    by_id = {value.get("id"): value for value in responses}
    tool_result_text = ""
    call_result = cast(dict[str, object], by_id.get(3, {}).get("result", {}))
    contents = cast(list[object], call_result.get("content", []))
    if contents and isinstance(contents[0], dict):
        tool_result_text = str(cast(dict[str, object], contents[0]).get("text", ""))
    isolation: dict[str, object] = {}
    with contextlib.suppress(ValueError):
        isolation = cast(dict[str, object], json.loads(tool_result_text))
    detail.update(
        {
            "requests": requests,
            "response_ids": sorted(value for value in by_id if isinstance(value, int)),
            "stdout_protocol_clean": clean,
            "stderr_isolated": True,
            "tool_result": isolation,
        }
    )
    passed = bool(
        gate["status"] == "PASS"
        and detail.get("target_exit_code") == 0
        and clean
        and set(by_id) >= {1, 2, 3}
        and "echo" in json.dumps(by_id[2])
        and isolation.get("echo") == "runtime-poc2b"
        and cast(dict[str, object], isolation.get("workspace", {})).get("read") is True
        and cast(dict[str, object], isolation.get("outside", {})).get("read") is False
        and cast(dict[str, object], isolation.get("host", {})).get("read") is False
    )
    gate["status"] = "PASS" if passed else "FAIL"
    return gate


def _real_conpty_gate(
    api: _WinApi,
    launcher: _Launcher,
    command: Path,
) -> dict[str, object]:
    input_read, input_write = _make_anonymous_pipe(api, parent_reads=False)
    output_read, output_write = _make_anonymous_pipe(api, parent_reads=True)
    pseudoconsole = ctypes.c_void_p()
    process: _CreatedProcess | None = None
    try:
        result = int(
            api.create_pseudoconsole(
                _Coord(80, 24), input_read, output_write, 0, ctypes.byref(pseudoconsole)
            )
        )
        if result != 0 or not pseudoconsole.value:
            raise OSError(result & 0xFFFFFFFF, f"CreatePseudoConsole failed: 0x{result:08x}")
        with _EvidenceJob.create(api) as job:
            process = launcher.spawn_appcontainer(
                command,
                [
                    "/d",
                    "/v:on",
                    "/q",
                    "/s",
                    "/c",
                    "echo CONPTY_RUNTIME_READY&set /p neuro=&echo CONPTY_RUNTIME_ECHO:!neuro!&exit /b 23",
                ],
                job_handle=job.process_creation_handle,
                pseudoconsole=int(pseudoconsole.value),
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            exact_job = _is_in_job(api, process.process_handle, job.process_creation_handle)
            resize = int(api.resize_pseudoconsole(pseudoconsole, _Coord(100, 30)))
            _write_all(api, input_write, b"runtime-poc2b\r\n")
            exit_code = _wait_exit(api, process)
            api.close_pseudoconsole(pseudoconsole)
            pseudoconsole = ctypes.c_void_p()
            api.close(output_write)
            output_write = 0
            output = _read_all(api, output_read).decode("utf-8", errors="replace")
            return _status(
                bool(
                    exit_code == 23
                    and exact_job
                    and resize == 0
                    and "CONPTY_RUNTIME_READY" in output
                    and "CONPTY_RUNTIME_ECHO:runtime-poc2b" in output
                ),
                {
                    "target": str(command),
                    "appcontainer_sid": launcher.profile.sid_text,
                    "security_capabilities_attribute": True,
                    "job_list_attribute": True,
                    "pseudoconsole_attribute": True,
                    "exact_job_membership": exact_job,
                    "resize_hresult": resize & 0xFFFFFFFF,
                    "exit_code": exit_code,
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


def _network_gate(
    api: _WinApi,
    launcher: _Launcher,
    bootstrap: Path,
    *,
    internet: bool,
) -> dict[str, object]:
    pipe: _NamedPipe | None = None
    process: _CreatedProcess | None = None
    try:
        pipe = _create_named_pipe(api, launcher.profile)
        with _EvidenceJob.create(api) as job:
            process = launcher.spawn_appcontainer(
                bootstrap,
                ["network-pipe", pipe.client_name, "www.microsoft.com", "443"],
                job_handle=job.process_creation_handle,
                internet=internet,
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            _connect_named_pipe(api, pipe, process)
            facts = _receive_json_frame(api, pipe.handle, _FRAME_HELLO)
            kind, payload = _receive_frame(api, pipe.handle)
            exit_code = _wait_exit(api, process)
            network = cast(dict[str, object], json.loads(payload.decode("utf-8")))
            expected = network.get("connected") is internet
            return _status(
                bool(
                    kind == _FRAME_DATA
                    and exit_code == 0
                    and _valid_appcontainer_facts(facts, launcher.profile.sid_text)
                    and expected
                ),
                {
                    "internet_client": internet,
                    "target": "www.microsoft.com:443",
                    "result": network,
                    "facts": facts,
                    "exit_code": exit_code,
                    "firewall_or_wfp_mutation": False,
                    "loopback_exemption": False,
                },
            )
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        _close_named_pipe(api, pipe)
        _close_process(api, process)


def _standard_user_run(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    api = _WinApi()
    Path(os.environ["TEMP"]).mkdir(parents=True, exist_ok=True)
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_source = Path(args.bootstrap).resolve(strict=True)
    token = _token_facts(api)
    result: dict[str, object] = {
        "classification": "EVIDENCE_ONLY_DO_NOT_MERGE",
        "mode": "REAL_STANDARD_USER_LOGON",
        "runner": {
            "platform": platform.platform(),
            "version": platform.version(),
            "python": sys.version,
        },
        "token": token,
        "controller_secret_sentinel_present": "NEURO_SECRET_SENTINEL" in os.environ,
        "gates": {},
    }
    gates = cast(dict[str, object], result["gates"])
    critical: list[str] = []
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-runtime-poc2b-standard-"))
    helper = fixture / "trusted-helper"
    workspace = fixture / "workspace"
    private_home = fixture / "private-home"
    outside = fixture / "outside"
    for path in (helper, workspace, private_home, outside):
        path.mkdir(parents=True)
    bootstrap = helper / "runtime-bootstrap.exe"
    shutil.copy2(bootstrap_source, bootstrap)
    (workspace / "authorized.txt").write_text("STANDARD_WORKSPACE", encoding="utf-8")
    outside_secret = outside / "outside-secret.txt"
    outside_secret.write_text("STANDARD_OUTSIDE_SECRET", encoding="utf-8")
    host_marker = Path.home() / "host-profile-marker.txt"
    host_marker.write_text("STANDARD_HOST_PROFILE", encoding="utf-8")
    profile: _Profile | None = None
    roots: list[_AuthorityRoot] = []
    cleanup: list[dict[str, object]] = []
    try:
        token_pass = bool(
            token.get("administrator_group_enabled") is False
            and token.get("elevated") is False
            and token.get("integrity_level") == "MEDIUM"
        )
        gates["standard_token"] = _status(token_pass, token)
        profile = _Profile(api, f"NeuroCode.Poc2B.Standard.{uuid.uuid4().hex}")
        result["appcontainer_sid"] = profile.sid_text
        roots = _deduplicate_roots(
            [
                _authority_root(api, helper, "RX", "trusted bootstrap helper", "USER_INSTALL"),
                _authority_root(api, workspace, "RW", "authorized workspace", "WORKSPACE_RUNTIME"),
                _authority_root(api, private_home, "RW", "sandbox-private profile", "USER_INSTALL"),
            ]
        )
        grants = [_grant_root(root, profile.sid_text) for root in roots]
        result["authority_grants"] = grants
        environment = _private_environment(private_home, [])
        launcher = _Launcher(api, profile, workspace, environment)
        command = Path(environment["COMSPEC"])
        shell = _command_relay_gate(
            api,
            launcher,
            bootstrap,
            [
                str(command),
                "/d",
                "/v:on",
                "/s",
                "/c",
                "set /p neuro=&echo STDOUT:!neuro!&echo STDERR:standard 1>&2&"
                "if defined NEURO_SECRET_SENTINEL echo SECRET_LEAK&exit /b 29",
            ],
            input_payload=b"standard-user-input\r\n",
        )
        gates["cmd_shell"] = _command_expect(
            shell,
            exit_code=29,
            stdout_contains=["STDOUT:standard-user-input"],
            stderr_contains=["STDERR:standard"],
        )
        standard_shell_gate = cast(dict[str, object], gates["cmd_shell"])
        standard_shell_detail = cast(dict[str, object], standard_shell_gate["detail"])
        if "SECRET_LEAK" in str(standard_shell_detail.get("stdout_text", "")):
            standard_shell_gate["status"] = "FAIL"
        filesystem = _command_relay_gate(
            api,
            launcher,
            bootstrap,
            [
                str(command),
                "/d",
                "/s",
                "/c",
                f'type "{workspace / "authorized.txt"}" & '
                f'type "{outside_secret}" & type "{host_marker}"',
            ],
        )
        filesystem_detail = cast(dict[str, object], filesystem["detail"])
        filesystem["status"] = (
            "PASS"
            if filesystem["status"] == "PASS"
            and filesystem_detail.get("target_exit_code") != 0
            and "STANDARD_WORKSPACE" in str(filesystem_detail.get("stdout_text", ""))
            and "STANDARD_OUTSIDE_SECRET" not in str(filesystem_detail.get("stdout_text", ""))
            and "STANDARD_HOST_PROFILE" not in str(filesystem_detail.get("stdout_text", ""))
            else "FAIL"
        )
        gates["filesystem_authority"] = filesystem
        gates["network_no_capability"] = _network_gate(api, launcher, bootstrap, internet=False)
        gates["network_internet_client"] = _network_gate(api, launcher, bootstrap, internet=True)
        python_gate = _command_relay_gate(
            api,
            launcher,
            bootstrap,
            [sys.executable, "-c", "print('standard-python-ok')"],
        )
        if (
            python_gate["status"] == "PASS"
            and cast(dict[str, object], python_gate["detail"]).get("target_exit_code") == 0
        ):
            python_gate = _command_expect(
                python_gate, exit_code=0, stdout_contains=["standard-python-ok"]
            )
            cast(dict[str, object], python_gate["detail"])["classification"] = "PASS"
        else:
            python_gate["status"] = "RUNNER_RUNTIME_LIMITATION"
            cast(dict[str, object], python_gate["detail"])["classification"] = (
                "RUNNER_RUNTIME_LIMITATION"
            )
        gates["python_runtime"] = python_gate
        required = (
            "standard_token",
            "cmd_shell",
            "filesystem_authority",
            "network_no_capability",
            "network_internet_client",
        )
        critical.extend(
            name for name in required if cast(dict[str, object], gates[name])["status"] != "PASS"
        )
        result["profile_created_by_standard_user"] = True
        result["named_pipe_created_by_standard_user"] = True
        result["job_created_by_standard_user"] = True
    except BaseException as error:
        result["harness_error"] = {"type": type(error).__name__, "message": str(error)}
        critical.append("STANDARD_USER_HARNESS")
    finally:
        if profile is not None:
            for root in reversed(roots):
                try:
                    cleanup.append(_cleanup_root(root, profile.sid_text))
                except BaseException as error:
                    cleanup.append({"path": str(root.path), "status": "FAIL", "error": str(error)})
                    critical.append("ACL_CLEANUP")
            profile_delete = profile.close()
            result["profile_delete_hresult"] = profile_delete
            if profile_delete != 0:
                critical.append("PROFILE_CLEANUP")
        result["authority_cleanup"] = cleanup
        result["authority_roots"] = [_runtime_root_record(root) for root in roots]
        host_marker.unlink(missing_ok=True)
        with contextlib.suppress(BaseException):
            shutil.rmtree(fixture)
    result["critical_failures"] = sorted(set(critical))
    result["overall"] = "PASS" if not critical else "FAIL"
    report_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return result, not critical


def _create_standard_user_and_run(
    api: _WinApi,
    args: argparse.Namespace,
    staging: Path,
) -> dict[str, object]:
    username = f"neuro_poc2b_{uuid.uuid4().hex[:8]}"
    password = f"N3uro!{uuid.uuid4().hex}aA"
    public_root = (
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / f"NeuroPoc2B-{uuid.uuid4().hex}"
    )
    public_root.mkdir(parents=True)
    script = public_root / "windows_appcontainer_runtime_poc2b.py"
    bootstrap = public_root / "runtime-bootstrap.exe"
    child_report = public_root / "standard-user-report.json"
    standard_profile = Path(os.environ.get("SYSTEMDRIVE", "C:")) / "Users" / username
    shutil.copy2(Path(__file__).resolve(), script)
    shutil.copy2(Path(args.bootstrap).resolve(), bootstrap)
    created = False
    process: _CreatedProcess | None = None
    try:
        _run_checked(["net", "user", username, password, "/add", "/expires:never"])
        created = True
        _run_checked(["icacls", str(public_root), "/grant", f"{username}:(OI)(CI)(M)"])
        command = ctypes.create_unicode_buffer(
            subprocess.list2cmdline(
                [
                    sys.executable,
                    str(script),
                    "--standard-user",
                    "--bootstrap",
                    str(bootstrap),
                    "--report",
                    str(child_report),
                ]
            )
        )
        startup = _StartupInfoW()
        startup.cb = ctypes.sizeof(startup)
        info = _ProcessInformation()
        standard_temp = standard_profile / "AppData" / "Local" / "Temp"
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        environment = {
            "COMSPEC": str(system_root / "System32" / "cmd.exe"),
            "HOME": str(standard_profile),
            "PATH": os.pathsep.join(
                [str(Path(sys.executable).parent), str(system_root / "System32")]
            ),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "NEURO_SECRET_SENTINEL": uuid.uuid4().hex,
            "SYSTEMDRIVE": standard_profile.drive,
            "SYSTEMROOT": str(system_root),
            "TEMP": str(standard_temp),
            "TMP": str(standard_temp),
            "USERPROFILE": str(standard_profile),
            "WINDIR": str(system_root),
        }
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0"
        )
        if not api.create_process_with_logon(
            username,
            ".",
            password,
            _LOGON_WITH_PROFILE,
            str(sys.executable),
            command,
            _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
            environment_block,
            str(public_root),
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            api.error("CreateProcessWithLogonW(real standard user)")
        process = _CreatedProcess(int(info.hProcess), int(info.hThread), int(info.dwProcessId))
        api.close(process.thread_handle)
        process.thread_handle = 0
        exit_code = _wait_exit(api, process, 180_000)
        child = (
            json.loads(child_report.read_text(encoding="utf-8")) if child_report.exists() else {}
        )
        return {
            "status": "PASS" if exit_code == 0 and child.get("overall") == "PASS" else "FAIL",
            "username": username,
            "created_local_user": True,
            "real_logon_process": True,
            "create_restricted_token_only": False,
            "exit_code": exit_code,
            "report": child,
        }
    except BaseException as error:
        return {
            "status": "FAIL",
            "username": username,
            "created_local_user": created,
            "real_logon_process": process is not None,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    finally:
        _close_process(api, process)
        if created:
            subprocess.run(["net", "user", username, "/delete"], capture_output=True, check=False)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$p=$args[0]; Get-CimInstance Win32_UserProfile | "
                "Where-Object {$_.LocalPath -eq $p -and -not $_.Loaded} | Remove-CimInstance",
                str(standard_profile),
            ],
            capture_output=True,
            check=False,
        )
        with contextlib.suppress(BaseException):
            shutil.rmtree(public_root)


def _run_legacy(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    api = _WinApi()
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    profile_name = f"NeuroCode.Poc2B.{uuid.uuid4().hex}"
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
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-appcontainer-ipc-poc2b-"))
    child = fixture / "windows_appcontainer_runtime_poc2b_bootstrap.exe"
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
            profile_delete = profile.close()
            result["profile_delete_hresult"] = profile_delete
            if profile_delete != 0:
                critical_failures.append("CLEANUP")
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


def _durable_journal(path: Path, record: dict[str, object]) -> int:
    encoded = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with path.open("ab", buffering=0) as stream:
        stream.write(encoded)
        os.fsync(stream.fileno())
    return len(encoded)


def _classify_runtime_root(path: Path, workspace_root: Path) -> str:
    text = str(path).casefold()
    if text.startswith(str(workspace_root).casefold() + os.sep.casefold()):
        return "WORKSPACE_RUNTIME"
    if text.startswith(str(Path.home()).casefold() + os.sep.casefold()):
        return "USER_INSTALL"
    if text.startswith(str(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))).casefold()):
        return "MACHINE_INSTALL"
    if text.startswith(str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))).casefold()):
        return "SYSTEM_TRUSTED"
    return "UNKNOWN"


def _distribution_root(executable: Path, kind: str) -> Path:
    resolved = executable.resolve(strict=True)
    if kind == "git":
        for parent in resolved.parents:
            if (parent / "mingw64").is_dir() and (parent / "cmd").is_dir():
                return parent
    return resolved.parent


def _main_run(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    api = _WinApi()
    original_secret_sentinel = os.environ.get("NEURO_SECRET_SENTINEL")
    controller_secret_sentinel = uuid.uuid4().hex
    os.environ["NEURO_SECRET_SENTINEL"] = controller_secret_sentinel
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_source = Path(args.bootstrap).resolve(strict=True)
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-appcontainer-runtime-poc2b-"))
    helper = fixture / "trusted-helper"
    controller_state = fixture / "controller-state"
    workspace = fixture / "workspace"
    private_home = fixture / "private-home"
    outside = fixture / "outside"
    for path in (helper, controller_state, workspace, private_home, outside):
        path.mkdir(parents=True)
    bootstrap = helper / "runtime-bootstrap.exe"
    shutil.copy2(bootstrap_source, bootstrap)
    controller_state_file = controller_state / "controller-state-secret.txt"
    controller_state_file.write_text("CONTROLLER_STATE_SECRET", encoding="utf-8")
    outside_secret = outside / "outside_secret.txt"
    outside_secret.write_text("OUTSIDE_SECRET_POC2B", encoding="utf-8")
    host_marker = Path.home() / "neuro-poc2b-host-marker.txt"
    host_marker.write_text("HOST_PROFILE_SECRET_POC2B", encoding="utf-8")
    probes = _write_probe_files(workspace, outside_secret, host_marker)
    (workspace / "git-file.txt").write_text("git-runtime", encoding="utf-8")
    host_gitconfig = Path.home() / ".gitconfig"
    original_gitconfig = host_gitconfig.read_bytes() if host_gitconfig.exists() else None
    host_gitconfig.write_text("[neuro]\n\tmarker = HOST_GIT_CONFIG_POC2B\n", encoding="utf-8")
    host_ps_profile = Path.home() / "Documents" / "WindowsPowerShell" / "profile.ps1"
    original_ps_profile = host_ps_profile.read_bytes() if host_ps_profile.exists() else None
    host_ps_profile.parent.mkdir(parents=True, exist_ok=True)
    host_ps_profile.write_text("Write-Output 'HOST_POWERSHELL_PROFILE_POC2B'\n", encoding="utf-8")
    journal = fixture / "authority-journal.jsonl"
    profile: _Profile | None = None
    roots: list[_AuthorityRoot] = []
    cleanup: list[dict[str, object]] = []
    critical: list[str] = []
    result: dict[str, object] = {
        "classification": "EVIDENCE_ONLY_DO_NOT_MERGE",
        "controller_secret_sentinel": {
            "present": True,
            "length": len(controller_secret_sentinel),
            "value_redacted": True,
        },
        "runner": {
            "platform": platform.platform(),
            "version": platform.version(),
            "release": platform.release(),
            "edition": platform.win32_edition(),
            "architecture": platform.machine(),
            "python": sys.version,
            "controller_token": _token_facts(api),
            "github_runner_name": os.environ.get("RUNNER_NAME"),
        },
        "gates": {},
    }
    gates = cast(dict[str, object], result["gates"])
    try:
        profile = _Profile(api, f"NeuroCode.Poc2B.Runtime.{uuid.uuid4().hex}")
        result["appcontainer_sid"] = profile.sid_text
        executable_paths: dict[str, Path | None] = {
            "python": Path(sys.executable),
            "git": Path(shutil.which("git") or "") if shutil.which("git") else None,
            "node": Path(shutil.which("node") or "") if shutil.which("node") else None,
            "powershell": Path(shutil.which("powershell") or "")
            if shutil.which("powershell")
            else None,
            "pwsh": Path(shutil.which("pwsh") or "") if shutil.which("pwsh") else None,
            "cmd": Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")),
        }
        result["resolved_executables"] = {
            key: str(value.resolve(strict=True)) if value is not None else "NOT_INSTALLED"
            for key, value in executable_paths.items()
        }
        root_specs: list[tuple[Path, str, str, str]] = [
            (helper, "RX", "controller-owned trusted bootstrap helper", "USER_INSTALL"),
            (workspace, "RW", "disposable authorized workspace", "WORKSPACE_RUNTIME"),
            (private_home, "RW", "sandbox-private HOME/TEMP", "USER_INSTALL"),
        ]
        python_roots = {Path(sys.prefix), Path(sys.base_prefix)}
        for python_root in python_roots:
            root_specs.append(
                (
                    python_root,
                    "RX",
                    "Python 3.12 distribution/venv runtime",
                    _classify_runtime_root(python_root, Path.cwd()),
                )
            )
        for kind in ("git", "node", "pwsh"):
            executable = executable_paths[kind]
            if executable is not None:
                distribution_root = _distribution_root(executable, kind)
                root_specs.append(
                    (
                        distribution_root,
                        "RX",
                        f"{kind} runtime distribution",
                        _classify_runtime_root(distribution_root, Path.cwd()),
                    )
                )
        roots = _deduplicate_roots([_authority_root(api, *spec) for spec in root_specs])
        journal_bytes = 0
        grants: list[dict[str, object]] = []
        for authority_root in roots:
            journal_bytes += _durable_journal(
                journal,
                {
                    "phase": "BEFORE_MUTATION",
                    "sid": profile.sid_text,
                    "path": str(authority_root.path),
                    "mode": authority_root.mode,
                    "identity": authority_root.identity,
                },
            )
            grants.append(_grant_root(authority_root, profile.sid_text))
        result["authority_journal"] = {
            "path": str(journal),
            "durable_before_mutation": True,
            "size_bytes": journal_bytes,
            "records": len(roots),
        }
        result["authority_grants"] = grants
        environment = _private_environment(
            private_home,
            [path.parent for path in executable_paths.values() if path is not None],
        )
        environment.update(
            {
                "NEURO_HOST_MARKER": str(host_marker),
                "NEURO_OUTSIDE_SECRET": str(outside_secret),
                "NEURO_WORKSPACE_FILE": str(probes["workspace_file"]),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        launcher = _Launcher(api, profile, workspace, environment)
        cmd = cast(Path, executable_paths["cmd"])
        cmd_gate = _command_relay_gate(
            api,
            launcher,
            bootstrap,
            [
                str(cmd),
                "/d",
                "/v:on",
                "/s",
                "/c",
                "set /p neuro=&echo CMD_STDOUT:!neuro!&echo CMD_STDERR:diag 1>&2&"
                "if defined NEURO_SECRET_SENTINEL echo SECRET_LEAK&exit /b 31",
            ],
            input_payload=b"cmd-runtime-input\r\n",
        )
        gates["cmd_shell"] = _command_expect(
            cmd_gate,
            exit_code=31,
            stdout_contains=["CMD_STDOUT:cmd-runtime-input"],
            stderr_contains=["CMD_STDERR:diag"],
        )
        cmd_shell_gate = cast(dict[str, object], gates["cmd_shell"])
        cmd_detail = cast(dict[str, object], cmd_shell_gate["detail"])
        if "SECRET_LEAK" in str(cmd_detail.get("stdout_text", "")):
            cmd_shell_gate["status"] = "FAIL"
        gates["shell_cancellation"] = _real_shell_cancellation_gate(
            api,
            launcher,
            bootstrap,
            [str(cmd), "/d", "/s", "/c", "ping -n 30 127.0.0.1 >nul"],
        )
        helper_boundary = _command_relay_gate(
            api,
            launcher,
            bootstrap,
            [str(cmd), "/d", "/s", "/c", f'type "{controller_state_file}"'],
        )
        helper_detail = cast(dict[str, object], helper_boundary["detail"])
        helper_boundary["status"] = (
            "PASS"
            if helper_boundary["status"] == "PASS"
            and helper_detail.get("target_exit_code") != 0
            and "CONTROLLER_STATE_SECRET" not in str(helper_detail.get("stdout_text", ""))
            else "FAIL"
        )
        helper_detail.update(
            {
                "bootstrap_location": str(bootstrap),
                "authorized_root": str(helper),
                "controller_state_sibling": str(controller_state),
                "exact_authority": "(OI)(CI)(RX) exact one-shot SID",
            }
        )
        gates["bootstrap_helper_boundary"] = helper_boundary
        python = cast(Path, executable_paths["python"])
        gates["python_runtime"] = _python_gate(api, launcher, bootstrap, python, probes)
        git = executable_paths["git"]
        if git is None:
            gates["git_runtime"] = _status(False, "git NOT_INSTALLED")
        else:
            git_version = _command_expect(
                _command_relay_gate(api, launcher, bootstrap, [str(git), "--version"]),
                exit_code=0,
                stdout_contains=["git version"],
            )
            repo_path = workspace / "repo"
            repo_path.mkdir()
            (repo_path / "tracked.txt").write_text("tracked", encoding="utf-8")
            git_init = _command_expect(
                _command_relay_gate(api, launcher, bootstrap, [str(git), "init", str(repo_path)]),
                exit_code=0,
                stdout_contains=["Initialized empty Git repository"],
            )
            git_add = _command_expect(
                _command_relay_gate(
                    api,
                    launcher,
                    bootstrap,
                    [str(git), "-C", str(repo_path), "add", "tracked.txt"],
                ),
                exit_code=0,
            )
            git_status = _command_expect(
                _command_relay_gate(
                    api,
                    launcher,
                    bootstrap,
                    [str(git), "-C", str(repo_path), "status", "--short"],
                ),
                exit_code=0,
                stdout_contains=["A  tracked.txt"],
            )
            host_config = _command_relay_gate(
                api,
                launcher,
                bootstrap,
                [str(git), "config", "--global", "--get", "neuro.marker"],
            )
            host_detail = cast(dict[str, object], host_config["detail"])
            host_config["status"] = (
                "PASS"
                if host_config["status"] == "PASS"
                and host_detail.get("target_exit_code") == 1
                and "HOST_GIT_CONFIG_POC2B" not in str(host_detail.get("stdout_text", ""))
                else "FAIL"
            )
            local_write = _command_expect(
                _command_relay_gate(
                    api,
                    launcher,
                    bootstrap,
                    [str(git), "config", "--global", "neuro.marker", "SANDBOX_LOCAL"],
                ),
                exit_code=0,
            )
            local_read = _command_expect(
                _command_relay_gate(
                    api,
                    launcher,
                    bootstrap,
                    [str(git), "config", "--global", "--get", "neuro.marker"],
                ),
                exit_code=0,
                stdout_contains=["SANDBOX_LOCAL"],
            )
            parts = {
                "version": git_version,
                "init": git_init,
                "add": git_add,
                "status": git_status,
                "host_config_denied": host_config,
                "private_config_write": local_write,
                "private_config_read": local_read,
            }
            gates["git_runtime"] = _status(
                all(value["status"] == "PASS" for value in parts.values()),
                parts,
            )
        node = executable_paths["node"]
        gates["node_runtime"] = (
            _node_gate(api, launcher, bootstrap, node, probes)
            if node is not None
            else {"status": "NOT_INSTALLED", "detail": "node NOT_INSTALLED"}
        )
        for key in ("powershell", "pwsh"):
            executable = executable_paths[key]
            if executable is None:
                gates[f"{key}_runtime"] = {"status": "NOT_INSTALLED", "detail": "NOT_INSTALLED"}
                continue
            shell_gate = _command_relay_gate(
                api,
                launcher,
                bootstrap,
                [
                    str(executable),
                    "-Command",
                    "$v=[Console]::In.ReadToEnd(); [Console]::Out.Write('PS_OUT:'+ $v); "
                    "[Console]::Error.Write('PS_ERR:diag'); exit 33",
                ],
                input_payload=b"powershell-runtime-input",
            )
            shell_gate = _command_expect(
                shell_gate,
                exit_code=33,
                stdout_contains=["PS_OUT:powershell-runtime-input"],
                stderr_contains=["PS_ERR:diag"],
            )
            detail = cast(dict[str, object], shell_gate["detail"])
            profile_isolated = "HOST_POWERSHELL_PROFILE_POC2B" not in (
                str(detail.get("stdout_text", "")) + str(detail.get("stderr_text", ""))
            )
            detail["host_profile_marker_absent"] = profile_isolated
            if not profile_isolated:
                shell_gate["status"] = "FAIL"
            gates[f"{key}_runtime"] = shell_gate
        gates["real_mcp_stdio"] = _mcp_gate(api, launcher, bootstrap, python, probes["mcp_server"])
        gates["conpty_real_cmd"] = _real_conpty_gate(api, launcher, cmd)
        result["standard_user"] = _create_standard_user_and_run(api, args, fixture)
        required = (
            "cmd_shell",
            "shell_cancellation",
            "bootstrap_helper_boundary",
            "python_runtime",
            "git_runtime",
            "real_mcp_stdio",
            "conpty_real_cmd",
        )
        critical.extend(
            name for name in required if cast(dict[str, object], gates[name])["status"] != "PASS"
        )
        if cast(dict[str, object], result["standard_user"])["status"] != "PASS":
            critical.append("standard_user")
        result["windows11_acceptance"] = "WINDOWS11_USER_ACCEPTANCE_PENDING"
        result["server_acceptance"] = (
            "NON_ADMIN_SERVER_ACCEPTANCE_PASS"
            if "standard_user" not in critical
            else "NON_ADMIN_SERVER_ACCEPTANCE_FAILED"
        )
    except BaseException as error:
        result["harness_error"] = {"type": type(error).__name__, "message": str(error)}
        critical.append("HARNESS")
    finally:
        if profile is not None:
            for authority_root in reversed(roots):
                try:
                    cleanup.append(_cleanup_root(authority_root, profile.sid_text))
                except BaseException as error:
                    cleanup.append(
                        {
                            "path": str(authority_root.path),
                            "error": str(error),
                            "status": "FAIL",
                        }
                    )
                    critical.append("CLEANUP")
            try:
                profile_delete = profile.close()
                result["profile_delete_hresult"] = profile_delete
                if profile_delete != 0:
                    critical.append("CLEANUP")
            except BaseException as error:
                result["profile_delete_error"] = str(error)
                critical.append("CLEANUP")
        result["authority_cleanup"] = cleanup
        result["runtime_roots"] = [_runtime_root_record(root) for root in roots]
        result["runtime_authority_scale"] = {
            "root_count": len(roots),
            "object_count": sum(cast(int, root.inventory["object_count"]) for root in roots),
            "file_bytes": sum(cast(int, root.inventory["total_file_bytes"]) for root in roots),
            "acl_mutation_count": len(roots),
            "grant_elapsed_ms": round(sum(root.grant_elapsed_ms for root in roots), 3),
            "cleanup_elapsed_ms": round(sum(root.cleanup_elapsed_ms for root in roots), 3),
            "observed_only_not_production_limit": True,
            "recommended_production_hard_limit": {
                "roots": 16,
                "objects": 100_000,
                "bytes": 5 * 1024 * 1024 * 1024,
            },
        }
        if original_gitconfig is None:
            host_gitconfig.unlink(missing_ok=True)
        else:
            host_gitconfig.write_bytes(original_gitconfig)
        if original_ps_profile is None:
            host_ps_profile.unlink(missing_ok=True)
        else:
            host_ps_profile.write_bytes(original_ps_profile)
        host_marker.unlink(missing_ok=True)
        if original_secret_sentinel is None:
            os.environ.pop("NEURO_SECRET_SENTINEL", None)
        else:
            os.environ["NEURO_SECRET_SENTINEL"] = original_secret_sentinel
        with contextlib.suppress(BaseException):
            shutil.rmtree(fixture)
    if not critical:
        decision = "WINDOWS_RUNTIME_NONADMIN_VIABLE"
    elif "standard_user" in critical and all(
        name not in critical
        for name in ("python_runtime", "git_runtime", "cmd_shell", "real_mcp_stdio")
    ):
        decision = "WINDOWS_RUNTIME_VIABLE_NONADMIN_ACCEPTANCE_REQUIRED"
    elif any(name in critical for name in ("python_runtime", "git_runtime", "real_mcp_stdio")):
        decision = "WINDOWS_RUNTIME_AUTHORITY_BLOCKED"
    elif "standard_user" in critical:
        decision = "WINDOWS_NONADMIN_ARCHITECTURE_BLOCKED"
    else:
        decision = "WINDOWS_POC2B_INCONCLUSIVE"
    result["architecture_decision"] = decision
    result["critical_failures"] = sorted(set(critical))
    result["overall"] = "PASS" if not critical else "FAIL"
    report_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return result, not critical


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--standard-user", action="store_true")
    args = parser.parse_args()
    _, passed = _standard_user_run(args) if args.standard_user else _main_run(args)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
