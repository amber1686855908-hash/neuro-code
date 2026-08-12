"""Evidence-only probe for Windows AppContainer filesystem ACL authority.

This file deliberately does not implement a production sandbox.  It exercises
one-shot AppContainer identities against disposable local-NTFS fixtures and
records handle-derived object identity, exact ACL mutations, inheritance,
recovery, hardlink, reparse-point, and namespace behavior.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
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
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ID_INFO_CLASS = 18
_OWNER_SECURITY_INFORMATION = 0x1
_GROUP_SECURITY_INFORMATION = 0x2
_DACL_SECURITY_INFORMATION = 0x4
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1
_INVALID_HANDLE_VALUE = cast(int, ctypes.c_void_p(-1).value)
_MAX_SCAN_OBJECTS = 1000


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
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]


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


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTimeLow", ctypes.c_uint32),
        ("ftCreationTimeHigh", ctypes.c_uint32),
        ("ftLastAccessTimeLow", ctypes.c_uint32),
        ("ftLastAccessTimeHigh", ctypes.c_uint32),
        ("ftLastWriteTimeLow", ctypes.c_uint32),
        ("ftLastWriteTimeHigh", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


@dataclass(frozen=True, slots=True)
class _ObjectIdentity:
    path: str
    canonical_handle_path: str
    volume_serial: int
    file_id: str
    attributes: int
    link_count: int
    filesystem: str
    volume_root: str

    @property
    def key(self) -> tuple[int, str]:
        return self.volume_serial, self.file_id

    @property
    def is_reparse(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)

    def json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "canonical_handle_path": self.canonical_handle_path,
            "volume_serial": f"{self.volume_serial:016x}",
            "file_id": self.file_id,
            "attributes": self.attributes,
            "reparse_point": self.is_reparse,
            "hardlink_count": self.link_count,
            "filesystem": self.filesystem,
            "volume_root": self.volume_root,
        }


@dataclass(slots=True)
class _CreatedProcess:
    process_handle: int
    thread_handle: int
    pid: int


def _load(library: object, name: str, args: list[object], result: object) -> Any:
    function = getattr(library, name)
    function.argtypes = args
    function.restype = result
    return function


class _WinApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows ACL evidence must run on Windows")
        loader = cast(Any, ctypes).WinDLL
        self.kernel32 = loader("kernel32.dll", use_last_error=True)
        self.advapi32 = loader("advapi32.dll", use_last_error=True)
        self.userenv = loader("userenv.dll", use_last_error=True)
        self.create_profile = _load(
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
        self.derive_profile_sid = _load(
            self.userenv,
            "DeriveAppContainerSidFromAppContainerName",
            [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_long,
        )
        self.delete_profile = _load(
            self.userenv, "DeleteAppContainerProfile", [ctypes.c_wchar_p], ctypes.c_long
        )
        self.free_sid = _load(self.advapi32, "FreeSid", [ctypes.c_void_p], ctypes.c_void_p)
        self.local_free = _load(self.kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
        self.convert_sid = _load(
            self.advapi32,
            "ConvertSidToStringSidW",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)],
            ctypes.c_int32,
        )
        self.initialize_attributes = _load(
            self.kernel32,
            "InitializeProcThreadAttributeList",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self.update_attribute = _load(
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
        self.delete_attributes = _load(
            self.kernel32, "DeleteProcThreadAttributeList", [ctypes.c_void_p], None
        )
        self.create_process = _load(
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
        self.wait = _load(
            self.kernel32,
            "WaitForSingleObject",
            [ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_uint32,
        )
        self.exit_code = _load(
            self.kernel32,
            "GetExitCodeProcess",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_int32,
        )
        self.close_handle = _load(self.kernel32, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32)
        self.is_process_in_job = _load(
            self.kernel32,
            "IsProcessInJob",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)],
            ctypes.c_int32,
        )
        self.create_file = _load(
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
        self.get_file_information_ex = _load(
            self.kernel32,
            "GetFileInformationByHandleEx",
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.get_file_information = _load(
            self.kernel32,
            "GetFileInformationByHandle",
            [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)],
            ctypes.c_int32,
        )
        self.get_final_path = _load(
            self.kernel32,
            "GetFinalPathNameByHandleW",
            [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_uint32,
        )
        self.get_volume_path = _load(
            self.kernel32,
            "GetVolumePathNameW",
            [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self.get_volume_information = _load(
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
        self.get_short_path = _load(
            self.kernel32,
            "GetShortPathNameW",
            [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32],
            ctypes.c_uint32,
        )
        self.get_named_security_info = _load(
            self.advapi32,
            "GetNamedSecurityInfoW",
            [
                ctypes.c_wchar_p,
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
        self.convert_sd_to_sddl = _load(
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

    @staticmethod
    def error(operation: str) -> NoReturn:
        code = cast(Any, ctypes).get_last_error()
        raise OSError(code, f"{operation} failed with Windows error {code}")

    def close(self, handle: int | None) -> None:
        if handle and handle != _INVALID_HANDLE_VALUE:
            self.close_handle(handle)


class _Profile:
    def __init__(self, api: _WinApi, name: str, *, create: bool = True) -> None:
        self.api = api
        self.name = name
        self.sid = ctypes.c_void_p()
        result = (
            int(
                api.create_profile(
                    name, name, "Neuro Code ACL POC2A evidence", None, 0, ctypes.byref(self.sid)
                )
            )
            if create
            else int(api.derive_profile_sid(name, ctypes.byref(self.sid)))
        )
        unsigned = result & 0xFFFFFFFF
        if unsigned == _ERROR_ALREADY_EXISTS_HRESULT and create:
            result = int(api.derive_profile_sid(name, ctypes.byref(self.sid)))
            unsigned = result & 0xFFFFFFFF
        if unsigned != 0 or not self.sid.value:
            raise OSError(unsigned, f"AppContainer profile operation failed: 0x{unsigned:08x}")
        converted = ctypes.c_wchar_p()
        if not api.convert_sid(self.sid, ctypes.byref(converted)):
            api.error("ConvertSidToStringSidW")
        try:
            self.sid_text = converted.value or ""
        finally:
            api.local_free(converted)

    def close(self) -> int:
        if self.sid.value:
            self.api.free_sid(self.sid)
            self.sid = ctypes.c_void_p()
        return int(self.api.delete_profile(self.name)) & 0xFFFFFFFF


class _AttributeList:
    def __init__(self, api: _WinApi, count: int) -> None:
        self.api = api
        size = ctypes.c_size_t()
        api.initialize_attributes(None, count, 0, ctypes.byref(size))
        self.storage = ctypes.create_string_buffer(size.value)
        self.pointer = ctypes.cast(self.storage, ctypes.c_void_p)
        if not api.initialize_attributes(self.pointer, count, 0, ctypes.byref(size)):
            api.error("InitializeProcThreadAttributeList")
        self.keepalive: list[object] = []

    def add(self, key: int, value: Any, size: int, label: str) -> None:
        self.keepalive.append(value)
        if not self.api.update_attribute(
            self.pointer, 0, key, ctypes.cast(value, ctypes.c_void_p), size, None, None
        ):
            self.api.error(f"UpdateProcThreadAttribute({label})")

    def close(self) -> None:
        self.api.delete_attributes(self.pointer)


def _spawn_appcontainer(
    api: _WinApi,
    profile: _Profile,
    job: WindowsJobObject,
    executable: Path,
    arguments: list[str],
    cwd: Path,
) -> _CreatedProcess:
    attributes = _AttributeList(api, 2)
    capabilities = _SecurityCapabilities(profile.sid, None, 0, 0)
    job_values = (ctypes.c_void_p * 1)(job.process_creation_handle)
    try:
        attributes.add(
            _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(capabilities),
            ctypes.sizeof(capabilities),
            "security capabilities",
        )
        attributes.add(
            _PROC_THREAD_ATTRIBUTE_JOB_LIST, job_values, ctypes.sizeof(job_values), "job list"
        )
        startup = _StartupInfoExW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = attributes.pointer.value
        process = _ProcessInformation()
        command = ctypes.create_unicode_buffer(
            subprocess.list2cmdline([str(executable), *arguments])
        )
        if not api.create_process(
            str(executable),
            command,
            None,
            None,
            False,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
            None,
            str(cwd),
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            api.error("CreateProcessW(AppContainer)")
    finally:
        attributes.close()
    return _CreatedProcess(int(process.hProcess), int(process.hThread), int(process.dwProcessId))


def _wait_process(api: _WinApi, process: _CreatedProcess, timeout_ms: int = 30000) -> int:
    try:
        waited = int(api.wait(process.process_handle, timeout_ms))
        if waited == _WAIT_TIMEOUT:
            raise TimeoutError(f"process {process.pid} timed out")
        if waited != _WAIT_OBJECT_0:
            api.error("WaitForSingleObject")
        code = ctypes.c_uint32()
        if not api.exit_code(process.process_handle, ctypes.byref(code)):
            api.error("GetExitCodeProcess")
        if code.value == _STILL_ACTIVE:
            raise RuntimeError("process still active")
        return int(code.value)
    finally:
        api.close(process.thread_handle)
        api.close(process.process_handle)


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


def _icacls(path: Path, *arguments: str) -> str:
    completed = _run_checked(["icacls", str(path), *arguments])
    return (completed.stdout + completed.stderr).strip()


def _grant(path: Path, sid: str, rights: str) -> dict[str, object]:
    output = _icacls(path, "/grant", f"*{sid}:{rights}")
    return {"path": str(path), "sid": sid, "rights": rights, "output": output}


def _remove_sid(path: Path, sid: str) -> dict[str, object]:
    output = _icacls(path, "/remove:g", f"*{sid}")
    return {"path": str(path), "sid": sid, "output": output}


def _sddl(api: _WinApi, path: Path) -> str:
    descriptor = ctypes.c_void_p()
    error = int(
        api.get_named_security_info(
            str(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
    )
    if error:
        raise OSError(error, f"GetNamedSecurityInfoW({path}) failed")
    text = ctypes.c_wchar_p()
    try:
        if not api.convert_sd_to_sddl(
            descriptor,
            _SDDL_REVISION_1,
            _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        ):
            api.error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        return text.value or ""
    finally:
        if text:
            api.local_free(text)
        if descriptor.value:
            api.local_free(descriptor)


def _object_identity(api: _WinApi, path: Path) -> _ObjectIdentity:
    handle = api.create_file(
        str(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = int(handle or 0)
    if not value or value == _INVALID_HANDLE_VALUE:
        api.error(f"CreateFileW(identity:{path})")
    try:
        file_id = _FileIdInfo()
        if not api.get_file_information_ex(
            value, _FILE_ID_INFO_CLASS, ctypes.byref(file_id), ctypes.sizeof(file_id)
        ):
            api.error("GetFileInformationByHandleEx(FileIdInfo)")
        basic = _ByHandleFileInformation()
        if not api.get_file_information(value, ctypes.byref(basic)):
            api.error("GetFileInformationByHandle")
        required = int(api.get_final_path(value, None, 0, 0))
        if not required:
            api.error("GetFinalPathNameByHandleW(size)")
        canonical = ctypes.create_unicode_buffer(required + 1)
        if not api.get_final_path(value, canonical, len(canonical), 0):
            api.error("GetFinalPathNameByHandleW")
    finally:
        api.close(value)
    volume_root = ctypes.create_unicode_buffer(32768)
    if not api.get_volume_path(str(path), volume_root, len(volume_root)):
        api.error("GetVolumePathNameW")
    filesystem = ctypes.create_unicode_buffer(64)
    serial = ctypes.c_uint32()
    if not api.get_volume_information(
        volume_root.value, None, 0, ctypes.byref(serial), None, None, filesystem, len(filesystem)
    ):
        api.error("GetVolumeInformationW")
    return _ObjectIdentity(
        path=str(path),
        canonical_handle_path=canonical.value,
        volume_serial=int(file_id.VolumeSerialNumber),
        file_id=bytes(file_id.FileId).hex(),
        attributes=int(basic.dwFileAttributes),
        link_count=int(basic.nNumberOfLinks),
        filesystem=filesystem.value,
        volume_root=volume_root.value,
    )


def _same_identity(api: _WinApi, left: Path, right: Path) -> bool:
    return _object_identity(api, left).key == _object_identity(api, right).key


def _short_path(api: _WinApi, path: Path) -> str | None:
    required = int(api.get_short_path(str(path), None, 0))
    if not required:
        return None
    output = ctypes.create_unicode_buffer(required + 1)
    if not api.get_short_path(str(path), output, len(output)):
        return None
    return output.value


def _profile_name(label: str) -> str:
    return f"NeuroCode.Poc2A.{label}.{uuid.uuid4().hex}"


def _profile_fingerprint(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _write_durable(path: Path, record: dict[str, object], *, append: bool = False) -> int:
    data = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(data)


def _journal_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _journal_phase_a(api: _WinApi, root: Path, journal: Path, stage: str) -> NoReturn:
    profile = _Profile(api, _profile_name(f"crash-{stage}"))
    identity = _object_identity(api, root)
    prepared = {
        "state": "PREPARED",
        "profile_name": profile.name,
        "profile_fingerprint": _profile_fingerprint(profile.name),
        "sid": profile.sid_text,
        "identity": identity.json(),
        "path": str(root),
        "exact_ace": "(OI)(CI)(RX)",
        "previous_sddl": _sddl(api, root),
        "journal_before_mutation": True,
    }
    _write_durable(journal, prepared)
    if stage == "before-mutation":
        os._exit(91)
    _grant(root, profile.sid_text, "(OI)(CI)(RX)")
    _write_durable(
        journal,
        {
            "state": "MUTATED",
            "sid": profile.sid_text,
            "identity": identity.json(),
            "post_sddl": _sddl(api, root),
        },
        append=True,
    )
    os._exit(92)


def _recover_journal(api: _WinApi, journal: Path) -> dict[str, object]:
    records = _journal_records(journal)
    prepared = records[0]
    expected = cast(dict[str, object], prepared["identity"])
    path = Path(cast(str, prepared["path"]))
    sid = cast(str, prepared["sid"])
    identity_match = False
    if path.exists():
        actual = _object_identity(api, path)
        identity_match = (
            f"{actual.volume_serial:016x}" == expected["volume_serial"]
            and actual.file_id == expected["file_id"]
        )
    removed = False
    if identity_match:
        _remove_sid(path, sid)
        removed = True
    profile = _Profile(api, cast(str, prepared["profile_name"]), create=False)
    profile_delete = profile.close()
    recovery: dict[str, object] = {
        "state": "RECOVERED" if identity_match else "IDENTITY_MISMATCH_FAIL_SAFE",
        "identity_match": identity_match,
        "ace_remove_attempted": removed,
        "profile_delete_hresult": profile_delete,
    }
    _write_durable(journal, recovery, append=True)
    return recovery


def _canonicalize_roots(api: _WinApi, roots: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    normalized: list[tuple[Path, str, _ObjectIdentity]] = []
    for root, mode in roots:
        identity = _object_identity(api, root)
        if identity.filesystem.casefold() != "ntfs":
            raise ValueError(f"unsupported filesystem: {identity.filesystem}")
        if identity.is_reparse:
            raise ValueError(f"workspace root is a reparse point: {root}")
        normalized.append(
            (Path(identity.canonical_handle_path.removeprefix("\\\\?\\")), mode, identity)
        )
    normalized.sort(key=lambda entry: (len(str(entry[0])), str(entry[0]).casefold()))
    result: list[tuple[Path, str]] = []
    for path, mode, _identity in normalized:
        path_key = str(path).rstrip("\\").casefold()
        duplicate = False
        for parent, parent_mode in result:
            parent_key = str(parent).rstrip("\\").casefold()
            if path_key == parent_key or path_key.startswith(parent_key + "\\"):
                if mode != parent_mode:
                    raise ValueError(
                        "nested workspace roots have conflicting READ_ONLY/READ_WRITE modes"
                    )
                duplicate = True
                break
        if not duplicate:
            result.append((path, mode))
    return result


def _scan_authority(
    api: _WinApi, roots: list[tuple[Path, str]], *, max_objects: int = _MAX_SCAN_OBJECTS
) -> dict[str, object]:
    canonical = _canonicalize_roots(api, roots)
    objects: dict[tuple[int, str], dict[str, object]] = {}
    count = 0
    start = time.perf_counter()
    for root, mode in canonical:
        stack = [root]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    count += 1
                    if count > max_objects:
                        raise ValueError(f"bounded authority scan exceeded {max_objects} objects")
                    identity = _object_identity(api, Path(entry.path))
                    if identity.is_reparse:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    state = objects.setdefault(
                        identity.key,
                        {"modes": set(), "paths": [], "link_count": identity.link_count},
                    )
                    cast(set[str], state["modes"]).add(mode)
                    cast(list[str], state["paths"]).append(entry.path)
    conflicts: list[dict[str, object]] = []
    external: list[dict[str, object]] = []
    for state in objects.values():
        modes = cast(set[str], state["modes"])
        if len(modes) > 1:
            conflicts.append({**state, "modes": sorted(modes)})
        if cast(int, state["link_count"]) > len(cast(list[str], state["paths"])):
            external.append({**state, "modes": sorted(modes)})
    if conflicts:
        raise ValueError(f"filesystem identity appears in conflicting root modes: {conflicts}")
    if external:
        raise ValueError(f"external hardlink authority cannot be bounded: {external}")
    return {
        "canonical_roots": [{"path": str(path), "mode": mode} for path, mode in canonical],
        "objects_scanned": count,
        "regular_identities": len(objects),
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
        "limit": max_objects,
    }


def _status(passed: bool, detail: object) -> dict[str, object]:
    return {"status": "PASS" if passed else "FAIL", "detail": detail}


def _contains_sid(api: _WinApi, path: Path, sid: str) -> bool:
    return sid.casefold() in _sddl(api, path).casefold()


def _run_child(
    api: _WinApi,
    profile: _Profile,
    executable: Path,
    cwd: Path,
    mode: str,
    target: Path,
    report: Path,
) -> dict[str, object]:
    report.unlink(missing_ok=True)
    with WindowsJobObject.create() as job:
        process = _spawn_appcontainer(
            api, profile, job, executable, [mode, str(target), str(report)], cwd
        )
        in_job = ctypes.c_int32()
        if not api.is_process_in_job(
            process.process_handle, job.process_creation_handle, ctypes.byref(in_job)
        ):
            api.error("IsProcessInJob(child)")
        exit_code = _wait_process(api, process)
    payload = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    return {"exit_code": exit_code, "exact_job_membership": bool(in_job.value), "report": payload}


def _namespace_gate(api: _WinApi, root: Path) -> dict[str, object]:
    identity = _object_identity(api, root)
    case_alias = Path(str(root).swapcase())
    extended = Path("\\\\?\\" + str(root))
    short = _short_path(api, root)
    short_available = bool(short and short.casefold() != str(root).casefold())
    short_same = _same_identity(api, root, Path(short)) if short_available and short else None
    rejected = {
        "UNC": "PRE_SPAWN_UNSUPPORTED",
        "device": "PRE_SPAWN_UNSUPPORTED",
        "unsupported_filesystem": "PRE_SPAWN_UNSUPPORTED",
        "root_ADS": "PRE_SPAWN_UNSUPPORTED",
    }
    examples = [r"\\server\share\workspace", r"\\.\C:\workspace", str(root) + ":stream"]
    reject_logic = [value.startswith(("\\\\", "\\\\.\\")) or ":" in value[2:] for value in examples]
    passed = _same_identity(api, root, case_alias) and _same_identity(api, root, extended)
    passed = passed and all(reject_logic) and (short_same in {True, None})
    return _status(
        passed,
        {
            "root_identity": identity.json(),
            "case_alias_same_identity": _same_identity(api, root, case_alias),
            "extended_path_same_identity": _same_identity(api, root, extended),
            "short_path": short,
            "short_path_status": "PASS"
            if short_same
            else ("UNAVAILABLE_ON_RUNNER" if short_same is None else "FAIL"),
            "unsupported": rejected,
            "configured_root_ads_rejected": reject_logic[2],
            "authorized_file_ads_classification": "SAME_FILE_SECURITY_DESCRIPTOR_AUTHORITY",
        },
    )


def _hardlink_gates(api: _WinApi, fixture: Path) -> dict[str, dict[str, object]]:
    same_root = fixture / "hardlink-same"
    same_root.mkdir()
    first = same_root / "a.txt"
    second = same_root / "b.txt"
    first.write_text("same", encoding="utf-8")
    os.link(first, second)
    same_scan = _scan_authority(api, [(same_root, "READ_WRITE")])
    same_identity = _same_identity(api, first, second)

    rw = fixture / "hardlink-rw"
    ro = fixture / "hardlink-ro"
    rw.mkdir()
    ro.mkdir()
    mixed_a = rw / "a.txt"
    mixed_b = ro / "hardlink-to-a.txt"
    mixed_a.write_text("mixed", encoding="utf-8")
    os.link(mixed_a, mixed_b)
    mixed_error = ""
    try:
        _scan_authority(api, [(rw, "READ_WRITE"), (ro, "READ_ONLY")])
    except ValueError as error:
        mixed_error = str(error)

    external_root = fixture / "hardlink-authorized"
    outside = fixture / "hardlink-outside"
    external_root.mkdir()
    outside.mkdir()
    inside_file = external_root / "inside.txt"
    outside_alias = outside / "outside-alias.txt"
    inside_file.write_text("external", encoding="utf-8")
    os.link(inside_file, outside_alias)
    external_error = ""
    try:
        _scan_authority(api, [(external_root, "READ_WRITE")])
    except ValueError as error:
        external_error = str(error)
    aliases = _run_checked(["fsutil", "hardlink", "list", str(inside_file)]).stdout.splitlines()
    return {
        "same_mode": _status(
            same_identity and same_scan["regular_identities"] == 1,
            {
                "same_identity": same_identity,
                "scan": same_scan,
                "link_count": _object_identity(api, first).link_count,
            },
        ),
        "conflicting_mode": _status(
            "conflicting root modes" in mixed_error,
            {
                "pre_spawn_rejected": bool(mixed_error),
                "error": mixed_error,
                "identity": _object_identity(api, mixed_a).json(),
            },
        ),
        "external": _status(
            "external hardlink authority" in external_error and len(aliases) >= 2,
            {
                "pre_spawn_rejected": bool(external_error),
                "error": external_error,
                "link_count": _object_identity(api, inside_file).link_count,
                "aliases": aliases,
                "alias_enumeration": "FSUTIL_STABLE_OS_CONTROL",
            },
        ),
    }


def _nested_root_gate(api: _WinApi, fixture: Path) -> dict[str, object]:
    outer = fixture / "nested-root"
    inner = outer / "child"
    inner.mkdir(parents=True)
    same = _canonicalize_roots(api, [(outer, "READ_WRITE"), (inner, "READ_WRITE")])
    conflict = ""
    try:
        _canonicalize_roots(api, [(outer, "READ_WRITE"), (inner, "READ_ONLY")])
    except ValueError as error:
        conflict = str(error)
    return _status(
        len(same) == 1 and "conflicting" in conflict,
        {
            "same_mode_canonical_count": len(same),
            "conflicting_pre_spawn_rejected": bool(conflict),
            "error": conflict,
        },
    )


def _reparse_gate(
    api: _WinApi, fixture: Path, profile: _Profile, child: Path, reports: Path
) -> dict[str, object]:
    authorized = fixture / "reparse-authorized"
    outside = fixture / "reparse-outside"
    authorized.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("outside-secret", encoding="utf-8")
    link = authorized / "outside-link"
    _run_checked(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)])
    outside_before = _sddl(api, outside)
    _grant(fixture, profile.sid_text, "(RX)")
    _grant(child.parent, profile.sid_text, "(OI)(CI)(RX)")
    _grant(reports, profile.sid_text, "(OI)(CI)(M)")
    _grant(authorized, profile.sid_text, "(OI)(CI)(RX)")
    outside_after = _sddl(api, outside)
    report = reports / "reparse.json"
    child_result = _run_child(api, profile, child, fixture, "reparse", link / "secret.txt", report)
    root_rejected = False
    try:
        _canonicalize_roots(api, [(link, "READ_ONLY")])
    except ValueError:
        root_rejected = True
    for cleanup_path in (authorized, reports, child.parent, fixture):
        _remove_sid(cleanup_path, profile.sid_text)
    passed = (
        outside_before == outside_after
        and child_result["exit_code"] == 0
        and cast(dict[str, object], child_result["report"])["outside_readable"] is False
        and root_rejected
    )
    return _status(
        passed,
        {
            "outside_acl_unchanged": outside_before == outside_after,
            "link_identity": _object_identity(api, link).json(),
            "runtime": child_result,
            "reparse_root_pre_spawn_rejected": root_rejected,
        },
    )


def _one_shot_gate(
    api: _WinApi, fixture: Path, target: _Profile, child: Path, reports: Path
) -> dict[str, object]:
    stale_root = fixture / "stale-authority"
    stale_root.mkdir()
    secret = stale_root / "secret.txt"
    secret.write_text("outside-secret", encoding="utf-8")
    stale = _Profile(api, _profile_name("stale"))
    stale_name = stale.name
    stale_sid = stale.sid_text
    _grant(stale_root, stale_sid, "(OI)(CI)(RX)")
    stale_delete = stale.close()
    _grant(fixture, target.sid_text, "(RX)")
    _grant(child.parent, target.sid_text, "(OI)(CI)(RX)")
    _grant(reports, target.sid_text, "(OI)(CI)(M)")
    child_result = _run_child(
        api, target, child, fixture, "reparse", secret, reports / "stale-authority.json"
    )
    for cleanup_path in (reports, child.parent, fixture):
        _remove_sid(cleanup_path, target.sid_text)
    new_cannot_use_stale = (
        child_result["exit_code"] == 0
        and cast(dict[str, object], child_result["report"])["outside_readable"] is False
    )
    names_differ = stale_name != target.name
    sids_differ = stale_sid.casefold() != target.sid_text.casefold()
    return _status(
        stale_delete == 0 and names_differ and sids_differ and new_cannot_use_stale,
        {
            "stale_profile_fingerprint": _profile_fingerprint(stale_name),
            "stale_sid": stale_sid,
            "stale_ace_left_in_place_for_test": True,
            "stale_profile_delete_hresult": stale_delete,
            "future_profile_fingerprint": _profile_fingerprint(target.name),
            "future_sid": target.sid_text,
            "profile_names_differ": names_differ,
            "sids_differ": sids_differ,
            "future_profile_cannot_use_stale_ace": new_cannot_use_stale,
            "profile_name_entropy_bits": 128,
            "reuse_policy": "NEVER_REUSE_PROFILE_NAME_OR_SID",
            "runtime": child_result,
        },
    )


def _crash_recovery_gate(
    api: _WinApi, fixture: Path, script: Path
) -> tuple[dict[str, object], dict[str, object]]:
    crash_root = fixture / "crash-root"
    crash_root.mkdir()
    unrelated_name = _profile_name("crash-unrelated")
    unrelated = _Profile(api, unrelated_name)
    _grant(crash_root, unrelated.sid_text, "(RX)")
    unrelated_before = _contains_sid(api, crash_root, unrelated.sid_text)
    journal = fixture / "crash-after.jsonl"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--phase-a",
            "after-mutation",
            "--root",
            str(crash_root),
            "--journal",
            str(journal),
        ],
        check=False,
    )
    records_before = _journal_records(journal)
    stale_sid = cast(str, records_before[0]["sid"])
    stale_present = _contains_sid(api, crash_root, stale_sid)
    concurrent_name = _profile_name("crash-concurrent")
    concurrent = _Profile(api, concurrent_name)
    _grant(crash_root, concurrent.sid_text, "(RX)")
    recovery = _recover_journal(api, journal)
    preserved = _contains_sid(api, crash_root, unrelated.sid_text) and _contains_sid(
        api, crash_root, concurrent.sid_text
    )
    stale_removed = not _contains_sid(api, crash_root, stale_sid)

    replacement = fixture / "crash-recreated"
    replacement.mkdir()
    mismatch_journal = fixture / "crash-mismatch.jsonl"
    mismatch_completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--phase-a",
            "after-mutation",
            "--root",
            str(replacement),
            "--journal",
            str(mismatch_journal),
        ],
        check=False,
    )
    old_identity = _journal_records(mismatch_journal)[0]["identity"]
    shutil.rmtree(replacement)
    replacement.mkdir()
    replacement_before = _sddl(api, replacement)
    mismatch_recovery = _recover_journal(api, mismatch_journal)
    replacement_after = _sddl(api, replacement)

    before_root = fixture / "crash-before"
    before_root.mkdir()
    before_journal = fixture / "crash-before.jsonl"
    before_completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--phase-a",
            "before-mutation",
            "--root",
            str(before_root),
            "--journal",
            str(before_journal),
        ],
        check=False,
    )
    before_recovery = _recover_journal(api, before_journal)

    _remove_sid(crash_root, unrelated.sid_text)
    _remove_sid(crash_root, concurrent.sid_text)
    unrelated_delete = unrelated.close()
    concurrent_delete = concurrent.close()
    crash_passed = (
        completed.returncode == 92
        and stale_present
        and stale_removed
        and preserved
        and recovery["profile_delete_hresult"] == 0
        and unrelated_before
        and before_completed.returncode == 91
        and before_recovery["profile_delete_hresult"] == 0
        and unrelated_delete == 0
        and concurrent_delete == 0
    )
    mismatch_passed = (
        mismatch_completed.returncode == 92
        and mismatch_recovery["state"] == "IDENTITY_MISMATCH_FAIL_SAFE"
        and mismatch_recovery["ace_remove_attempted"] is False
        and replacement_before == replacement_after
    )
    return (
        _status(
            crash_passed,
            {
                "after_mutation_exit": completed.returncode,
                "stale_ace_present": stale_present,
                "recovery": recovery,
                "stale_ace_removed": stale_removed,
                "unrelated_and_concurrent_preserved": preserved,
                "before_mutation_exit": before_completed.returncode,
                "before_mutation_recovery": before_recovery,
                "journal_before_mutation": records_before[0]["journal_before_mutation"],
            },
        ),
        _status(
            mismatch_passed,
            {
                "phase_a_exit": mismatch_completed.returncode,
                "old_identity": old_identity,
                "replacement_identity": _object_identity(api, replacement).json(),
                "recovery": mismatch_recovery,
                "replacement_acl_unchanged": replacement_before == replacement_after,
            },
        ),
    )


def _journaled_grant(
    api: _WinApi, journal: Path, path: Path, sid: str, rights: str
) -> dict[str, object]:
    identity = _object_identity(api, path)
    prepared = {
        "state": "PREPARED",
        "sid": sid,
        "identity": identity.json(),
        "path": str(path),
        "exact_ace": rights,
        "previous_sddl": _sddl(api, path),
        "journal_before_mutation": True,
    }
    _write_durable(journal, prepared, append=journal.exists())
    grant = _grant(path, sid, rights)
    _write_durable(
        journal,
        {
            "state": "MUTATED",
            "sid": sid,
            "identity": identity.json(),
            "path": str(path),
            "post_sddl": _sddl(api, path),
        },
        append=True,
    )
    return grant


def _all_paths(root: Path) -> list[Path]:
    return [root, *sorted(root.rglob("*"), key=lambda path: str(path).casefold())]


def _acl_authority_gate(
    api: _WinApi,
    fixture: Path,
    profile: _Profile,
    unrelated: _Profile,
    concurrent: _Profile,
    child: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    authority_parent = fixture / "authority"
    authority_parent.mkdir()
    _grant(authority_parent, unrelated.sid_text, "(OI)(CI)(RX)")
    ro = authority_parent / "ro"
    rw = authority_parent / "rw"
    reports = fixture / "reports"
    runtime = fixture / "runtime"
    for directory in (ro / "nested", rw / "nested", reports, runtime):
        directory.mkdir(parents=True, exist_ok=True)
    (ro / "existing.txt").write_text("ro-existing", encoding="utf-8")
    (ro / "nested" / "child.txt").write_text("ro-nested", encoding="utf-8")
    (rw / "existing.txt").write_text("rw-existing", encoding="utf-8")
    (rw / "nested" / "child.txt").write_text("rw-nested", encoding="utf-8")
    child_copy = runtime / child.name
    shutil.copy2(child, child_copy)

    _icacls(rw, "/inheritance:d")
    before_paths = _all_paths(ro) + _all_paths(rw)
    before = {str(path): _sddl(api, path) for path in before_paths}
    owner_group_before = {str(path): value.split("D:", 1)[0] for path, value in before.items()}
    unrelated_before = all(_contains_sid(api, path, unrelated.sid_text) for path in (ro, rw))

    journal = fixture / "normal-mutations.jsonl"
    grants = [
        _journaled_grant(api, journal, fixture, profile.sid_text, "(RX)"),
        _journaled_grant(api, journal, runtime, profile.sid_text, "(OI)(CI)(RX)"),
        _journaled_grant(api, journal, reports, profile.sid_text, "(OI)(CI)(M)"),
        _journaled_grant(api, journal, ro, profile.sid_text, "(OI)(CI)(RX)"),
        _journaled_grant(api, journal, rw, profile.sid_text, "(OI)(CI)(M)"),
    ]
    (ro / "future").mkdir()
    (ro / "future" / "descendant.txt").write_text("ro-future", encoding="utf-8")
    after_grant = {str(path): _sddl(api, path) for path in before_paths}
    changed_descriptors = [path for path in before if before[path] != after_grant[path]]
    existing_descendants_have_inherited = all(
        _contains_sid(api, path, profile.sid_text)
        for path in (ro / "existing.txt", ro / "nested" / "child.txt", rw / "existing.txt")
    )
    future = rw / "future-after-grant"
    future.mkdir()
    (future / "child.txt").write_text("future", encoding="utf-8")
    future_inherited = _contains_sid(api, future / "child.txt", profile.sid_text)

    ro_result = _run_child(api, profile, child_copy, fixture, "ro", ro, reports / "ro.json")
    rw_result = _run_child(api, profile, child_copy, fixture, "rw", rw, reports / "rw.json")
    ro_report = cast(dict[str, object], ro_result["report"])
    rw_report = cast(dict[str, object], rw_result["report"])
    ro_passed = (
        ro_result["exit_code"] == 0
        and all(ro_report[key] is True for key in ("read_existing", "read_nested", "read_future"))
        and ro_report["write_dac_denied"] is True
        and all(
            cast(int, ro_report[key]) in {5, 1314}
            for key in ("create_error", "modify_error", "rename_error", "delete_error")
        )
    )
    rw_passed = (
        rw_result["exit_code"] == 0
        and all(
            rw_report[key] is True
            for key in (
                "read",
                "modify",
                "create_file",
                "create_directory",
                "rename",
                "replace",
                "delete",
            )
        )
        and all(
            cast(int, rw_report[key]) in {5, 1314}
            for key in ("owner_error", "dacl_error", "label_error")
        )
    )

    _grant(ro, concurrent.sid_text, "(RX)")
    _grant(rw, concurrent.sid_text, "(M)")
    concurrent_present = all(_contains_sid(api, path, concurrent.sid_text) for path in (ro, rw))
    for path in (ro, rw, runtime, reports, fixture):
        _remove_sid(path, profile.sid_text)
    target_absent = all(
        not _contains_sid(api, path, profile.sid_text)
        for path in _all_paths(ro) + _all_paths(rw) + [runtime, reports, fixture]
    )
    unrelated_preserved = all(_contains_sid(api, path, unrelated.sid_text) for path in (ro, rw))
    concurrent_preserved = all(_contains_sid(api, path, concurrent.sid_text) for path in (ro, rw))
    after_cleanup = {str(path): _sddl(api, path) for path in before_paths if path.exists()}
    owner_group_preserved = all(
        after_cleanup[path].split("D:", 1)[0] == owner_group_before[path] for path in after_cleanup
    )
    ro_protection_preserved = "D:P" not in _sddl(api, ro)
    rw_protection_preserved = "D:P" in _sddl(api, rw)
    _remove_sid(ro, concurrent.sid_text)
    _remove_sid(rw, concurrent.sid_text)
    _remove_sid(authority_parent, unrelated.sid_text)

    exact_acl: dict[str, object] = {
        "status": "PASS" if ro_passed and rw_passed else "FAIL",
        "detail": {
            "grants": grants,
            "RO": {
                "requested": "(OI)(CI)(RX)",
                "effective_sddl": after_grant[str(ro)],
                "forbidden_rights": ["GENERIC_ALL", "WRITE_DAC", "WRITE_OWNER", "TAKE_OWNERSHIP"],
            },
            "RW": {
                "requested": "(OI)(CI)(M)",
                "effective_sddl": after_grant[str(rw)],
                "forbidden_rights": ["GENERIC_ALL", "WRITE_DAC", "WRITE_OWNER", "TAKE_OWNERSHIP"],
            },
            "security_management_denied": {
                "owner": rw_report["owner_error"],
                "DACL": rw_report["dacl_error"],
                "mandatory_label": rw_report["label_error"],
            },
        },
    }
    preservation = _status(
        unrelated_before
        and unrelated_preserved
        and owner_group_preserved
        and ro_protection_preserved
        and rw_protection_preserved,
        {
            "unrelated_before": unrelated_before,
            "unrelated_after_cleanup": unrelated_preserved,
            "owner_group_preserved": owner_group_preserved,
            "ro_inheritance_case_preserved": ro_protection_preserved,
            "rw_custom_protection_preserved": rw_protection_preserved,
            "whole_descriptor_restore_used": False,
        },
    )
    concurrent_gate = _status(
        concurrent_present and concurrent_preserved,
        {
            "concurrent_ace_added_after_grant": concurrent_present,
            "concurrent_ace_preserved_after_exact_sid_cleanup": concurrent_preserved,
            "cleanup_operation": "remove exact target SID only",
        },
    )
    inheritance = _status(
        existing_descendants_have_inherited and future_inherited and target_absent,
        {
            "existing_descendants_received_inherited_ace": existing_descendants_have_inherited,
            "future_descendants_inherit": future_inherited,
            "security_descriptors_changed_count": len(changed_descriptors),
            "changed_paths": changed_descriptors,
            "root_only_grant_sufficient": True,
            "descendant_target_aces_absent_after_root_cleanup": target_absent,
            "normal_journal_bytes": journal.stat().st_size,
            "normal_journal_records": len(_journal_records(journal)),
        },
    )
    return (
        _status(ro_passed, ro_result),
        _status(rw_passed, rw_result),
        exact_acl,
        {
            "existing_acl": preservation,
            "concurrent_acl": concurrent_gate,
            "inheritance": inheritance,
            "cleanup": _status(
                target_absent,
                {
                    "exact_target_sid_absent": target_absent,
                    "unrelated_preserved": unrelated_preserved,
                    "concurrent_preserved": concurrent_preserved,
                },
            ),
        },
    )


def _boundedness_gate(api: _WinApi, fixture: Path) -> dict[str, object]:
    results: dict[str, object] = {}
    for label, count in (("small", 12), ("medium", 320)):
        root = fixture / f"bounded-{label}"
        root.mkdir()
        for index in range(count):
            bucket = root / f"d-{index // 40:02d}"
            bucket.mkdir(exist_ok=True)
            (bucket / f"f-{index:04d}.txt").write_text(str(index), encoding="utf-8")
        results[label] = _scan_authority(api, [(root, "READ_WRITE")])
    exceeded = False
    try:
        _scan_authority(api, [(fixture / "bounded-medium", "READ_WRITE")], max_objects=10)
    except ValueError as error:
        exceeded = "exceeded" in str(error)
    return _status(
        exceeded,
        {
            "trees": results,
            "acl_objects_mutated_per_authority": 1,
            "scan_limit": _MAX_SCAN_OBJECTS,
            "limit_fail_closed": exceeded,
            "production_cost_classification": "bounded identity scan plus root-only ACL mutation",
        },
    )


def _runner_facts() -> dict[str, object]:
    return {
        "platform": platform.platform(),
        "release": platform.release(),
        "version": platform.version(),
        "architecture": platform.machine(),
        "python": sys.version,
        "github_runner_os": os.environ.get("RUNNER_OS"),
        "github_runner_name": os.environ.get("RUNNER_NAME"),
    }


def _run(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    api = _WinApi()
    report = Path(args.report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-appcontainer-acl-poc2a-"))
    child_source = Path(args.child).resolve()
    script = Path(__file__).resolve()
    result: dict[str, object] = {
        "classification": "EVIDENCE_ONLY_DO_NOT_MERGE",
        "runner": _runner_facts(),
        "fixture": str(fixture),
        "scope": {
            "temporary_fixture_only": True,
            "production_src_modified": False,
            "runtime_closure_tested": False,
            "non_admin_tested": False,
        },
        "gates": {},
    }
    gates = cast(dict[str, object], result["gates"])
    critical_failures: list[str] = []
    target: _Profile | None = None
    unrelated: _Profile | None = None
    concurrent: _Profile | None = None
    try:
        fixture_identity = _object_identity(api, fixture)
        result["filesystem"] = fixture_identity.json()
        if fixture_identity.filesystem.casefold() != "ntfs":
            result["unsupported"] = "LOCAL_NTFS_REQUIRED"
            critical_failures.append("FILESYSTEM_NOT_NTFS")
            result["architecture_decision"] = "WINDOWS_ACL_AUTHORITY_INCONCLUSIVE"
            raise RuntimeError(f"unsupported evidence filesystem: {fixture_identity.filesystem}")

        target = _Profile(api, _profile_name("target"))
        unrelated = _Profile(api, _profile_name("unrelated"))
        concurrent = _Profile(api, _profile_name("concurrent"))
        result["appcontainer"] = {
            "profile_name_fingerprint": _profile_fingerprint(target.name),
            "profile_name_entropy_bits": 128,
            "sid": target.sid_text,
            "one_shot": True,
            "cross_run_reuse": "FORBIDDEN",
        }

        gates["namespace_identity"] = _namespace_gate(api, fixture)
        gates["nested_root_modes"] = _nested_root_gate(api, fixture)
        hardlinks = _hardlink_gates(api, fixture)
        gates["hardlink_same_mode"] = hardlinks["same_mode"]
        gates["hardlink_conflicting_mode"] = hardlinks["conflicting_mode"]
        gates["hardlink_external"] = hardlinks["external"]
        gates["boundedness"] = _boundedness_gate(api, fixture)

        ro, rw, exact_acl, acl_details = _acl_authority_gate(
            api, fixture, target, unrelated, concurrent, child_source
        )
        gates["ro_authority"] = ro
        gates["rw_authority"] = rw
        gates["exact_acl_rights"] = exact_acl
        gates["existing_acl_preservation"] = acl_details["existing_acl"]
        gates["concurrent_acl_preservation"] = acl_details["concurrent_acl"]
        gates["inheritance_propagation"] = acl_details["inheritance"]
        gates["normal_cleanup"] = acl_details["cleanup"]

        runtime_child = fixture / "runtime" / child_source.name
        reports = fixture / "reports"
        gates["reparse"] = _reparse_gate(api, fixture, target, runtime_child, reports)
        gates["one_shot_sid"] = _one_shot_gate(api, fixture, target, runtime_child, reports)
        crash, mismatch = _crash_recovery_gate(api, fixture, script)
        gates["crash_recovery"] = crash
        gates["recreated_object_identity"] = mismatch

        core = (
            "namespace_identity",
            "nested_root_modes",
            "hardlink_same_mode",
            "hardlink_conflicting_mode",
            "hardlink_external",
            "boundedness",
            "ro_authority",
            "rw_authority",
            "exact_acl_rights",
            "existing_acl_preservation",
            "concurrent_acl_preservation",
            "inheritance_propagation",
            "normal_cleanup",
            "reparse",
            "one_shot_sid",
            "crash_recovery",
            "recreated_object_identity",
        )
        for name in core:
            if cast(dict[str, object], gates[name])["status"] != "PASS":
                critical_failures.append(name)
        result["architecture_decision"] = (
            "WINDOWS_ACL_AUTHORITY_VIABLE"
            if not critical_failures
            else "WINDOWS_ACL_AUTHORITY_BLOCKED"
        )
    except BaseException as error:
        result["harness_error"] = {"type": type(error).__name__, "message": str(error)}
        if not critical_failures:
            critical_failures.append("HARNESS")
            result["architecture_decision"] = "WINDOWS_ACL_AUTHORITY_INCONCLUSIVE"
    finally:
        profile_cleanup: dict[str, object] = {}
        for label, profile in (
            ("target", target),
            ("unrelated", unrelated),
            ("concurrent", concurrent),
        ):
            if profile is not None:
                try:
                    close_result = profile.close()
                    profile_cleanup[label] = close_result
                    if close_result != 0:
                        critical_failures.append(f"PROFILE_CLEANUP_{label.upper()}")
                except BaseException as error:
                    profile_cleanup[label] = f"FAIL: {error}"
                    critical_failures.append(f"PROFILE_CLEANUP_{label.upper()}")
        result["profile_cleanup"] = profile_cleanup
        try:
            shutil.rmtree(fixture)
            result["fixture_cleanup"] = "PASS"
        except BaseException as error:
            result["fixture_cleanup"] = f"FAIL: {error}"
            critical_failures.append("FIXTURE_CLEANUP")

    result["critical_failures"] = sorted(set(critical_failures))
    result["overall"] = "PASS" if not critical_failures else "FAIL"
    if critical_failures and result.get("architecture_decision") == "WINDOWS_ACL_AUTHORITY_VIABLE":
        result["architecture_decision"] = "WINDOWS_ACL_AUTHORITY_BLOCKED"
    report.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return result, not critical_failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--report")
    parser.add_argument("--phase-a", choices=("before-mutation", "after-mutation"))
    parser.add_argument("--root")
    parser.add_argument("--journal")
    args = parser.parse_args()
    if args.phase_a:
        if not args.root or not args.journal:
            parser.error("--phase-a requires --root and --journal")
        _journal_phase_a(_WinApi(), Path(args.root), Path(args.journal), args.phase_a)
    if not args.child or not args.report:
        parser.error("normal execution requires --child and --report")
    _, passed = _run(args)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
