"""Windows sandbox filesystem authority planning and exact ACE reconciliation.

The planner is platform-neutral and deliberately narrow: it grants a dedicated
synthetic SID access only on explicitly named roots, adds explicit sensitive
read denies, and removes only ACE tuples that this installation previously
managed.  Unrelated controller-user ACL entries are never part of the managed
set.

Windows 沙箱文件系统 authority 的规划与精确 ACE reconciliation.

planner 平台无关且范围很窄:只在明确 roots 上授予 dedicated synthetic SID 权限,
为敏感路径加入显式 read deny,并且只删除本 installation 之前管理的 ACE tuple.
controller user 的其他 ACL entry 永远不在 managed set 中.
"""

from __future__ import annotations

import ctypes
import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from neuro_code.application.ports.windows_sandbox import WindowsSandboxSetupRequest
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.shared.errors import SandboxError

# These masks intentionally exclude WRITE_DAC/WRITE_OWNER.  A sandbox SID can
# edit files and create descendants but cannot rewrite the controller's ACL.
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_READ_EA = 0x00000008
FILE_WRITE_EA = 0x00000010
FILE_EXECUTE = 0x00000020
FILE_DELETE_CHILD = 0x00000040
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
DELETE = 0x00010000
READ_CONTROL = 0x00020000
SYNCHRONIZE = 0x00100000

READ_ACCESS_MASK = (
    FILE_READ_DATA | FILE_READ_EA | FILE_EXECUTE | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE
)
WRITE_ACCESS_MASK = (
    READ_ACCESS_MASK
    | FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_DELETE_CHILD
    | FILE_WRITE_ATTRIBUTES
    | DELETE
)
INHERIT_TO_CHILDREN = 0x00000003  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE


class WindowsAclError(SandboxError):
    """A filesystem ACL authority operation failed closed."""


class WindowsManagedAceKind(StrEnum):
    """Exact managed ACE roles owned by this installation."""

    READ_ALLOW = "read-allow"
    WRITE_ALLOW = "write-allow"
    SENSITIVE_READ_DENY = "sensitive-read-deny"


@dataclass(frozen=True, slots=True)
class WindowsManagedAce:
    """One exact, installation-owned ACE tuple."""

    path: Path
    sid: SyntheticWindowsSid
    kind: WindowsManagedAceKind
    access_mask: int
    inheritance: int = INHERIT_TO_CHILDREN

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("managed ACE path must be absolute")
        try:
            canonical = self.path.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ValueError("managed ACE path must be resolvable") from error
        if canonical == canonical.parent:
            raise ValueError("managed ACE path must not be a filesystem root")
        if not isinstance(self.sid, SyntheticWindowsSid):
            raise TypeError("managed ACE SID must be a canonical synthetic SID")
        if not isinstance(self.kind, WindowsManagedAceKind):
            raise TypeError("managed ACE kind must be canonical")
        if self.kind is WindowsManagedAceKind.WRITE_ALLOW:
            expected_mask = WRITE_ACCESS_MASK
        else:
            expected_mask = READ_ACCESS_MASK
        if self.access_mask != expected_mask:
            raise ValueError("managed ACE access mask does not match its role")
        if self.inheritance != INHERIT_TO_CHILDREN:
            raise ValueError("managed ACE inheritance must cover files and child directories")
        object.__setattr__(self, "path", canonical)

    @property
    def is_deny(self) -> bool:
        return self.kind is WindowsManagedAceKind.SENSITIVE_READ_DENY


@dataclass(frozen=True, slots=True)
class WindowsFilesystemSetupPlan:
    """Desired managed ACE set for one setup request."""

    entries: tuple[WindowsManagedAce, ...]

    def __post_init__(self) -> None:
        if len(set(self.entries)) != len(self.entries):
            raise ValueError("filesystem setup plan contains duplicate managed ACEs")

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(sorted({entry.path for entry in self.entries}, key=str))


def plan_windows_filesystem_authority(
    request: WindowsSandboxSetupRequest,
    write_sid: SyntheticWindowsSid,
) -> WindowsFilesystemSetupPlan:
    """Build read/write grants and sensitive-read denies without touching ACLs."""

    if not isinstance(request, WindowsSandboxSetupRequest):
        raise TypeError("filesystem setup request must be canonical")
    if not isinstance(write_sid, SyntheticWindowsSid):
        raise TypeError("filesystem setup write SID must be canonical")
    entries: list[WindowsManagedAce] = []
    for root in request.read_roots:
        entries.append(
            WindowsManagedAce(
                root,
                write_sid,
                WindowsManagedAceKind.READ_ALLOW,
                READ_ACCESS_MASK,
            )
        )
    for root in request.writable_roots:
        entries.append(
            WindowsManagedAce(
                root,
                write_sid,
                WindowsManagedAceKind.WRITE_ALLOW,
                WRITE_ACCESS_MASK,
            )
        )
    for sensitive in request.sensitive_read_paths:
        entries.append(
            WindowsManagedAce(
                sensitive,
                write_sid,
                WindowsManagedAceKind.SENSITIVE_READ_DENY,
                READ_ACCESS_MASK,
            )
        )
    return WindowsFilesystemSetupPlan(tuple(entries))


class WindowsAclApi(Protocol):
    """Minimal ACL mutation/query surface used by the setup authority."""

    def reconcile(
        self,
        path: Path,
        *,
        desired: tuple[WindowsManagedAce, ...],
        remove: tuple[WindowsManagedAce, ...],
    ) -> None: ...

    def matches(self, path: Path, desired: tuple[WindowsManagedAce, ...]) -> bool: ...


class InMemoryWindowsAclApi:
    """Deterministic fake/portable model for unit and repair tests."""

    def __init__(self) -> None:
        self.entries: dict[Path, set[WindowsManagedAce]] = defaultdict(set)
        self.unmanaged_entries: dict[Path, set[str]] = defaultdict(set)
        self.calls: list[
            tuple[Path, tuple[WindowsManagedAce, ...], tuple[WindowsManagedAce, ...]]
        ] = []

    def reconcile(
        self,
        path: Path,
        *,
        desired: tuple[WindowsManagedAce, ...],
        remove: tuple[WindowsManagedAce, ...],
    ) -> None:
        canonical = path.expanduser().resolve(strict=False)
        self.calls.append((canonical, desired, remove))
        self.entries[canonical].difference_update(remove)
        self.entries[canonical].update(desired)
        if not self.entries[canonical]:
            self.entries.pop(canonical, None)

    def matches(self, path: Path, desired: tuple[WindowsManagedAce, ...]) -> bool:
        canonical = path.expanduser().resolve(strict=False)
        return set(desired).issubset(self.entries.get(canonical, set()))


class WindowsFilesystemAclAuthority:
    """Apply only the exact managed ACE delta for one installation."""

    def __init__(self, api: WindowsAclApi) -> None:
        self._api = api

    @staticmethod
    def _group(entries: Iterable[WindowsManagedAce]) -> dict[Path, tuple[WindowsManagedAce, ...]]:
        grouped: dict[Path, list[WindowsManagedAce]] = defaultdict(list)
        for entry in entries:
            grouped[entry.path].append(entry)
        return {path: tuple(values) for path, values in grouped.items()}

    def reconcile(
        self,
        previous: tuple[WindowsManagedAce, ...],
        plan: WindowsFilesystemSetupPlan,
    ) -> None:
        old_by_path = self._group(previous)
        new_by_path = self._group(plan.entries)
        for path in sorted(set(old_by_path) | set(new_by_path), key=str):
            old_entries = set(old_by_path.get(path, ()))
            desired_entries = tuple(new_by_path.get(path, ()))
            remove_entries = tuple(sorted(old_entries - set(desired_entries), key=str))
            if remove_entries or desired_entries or old_entries:
                self._api.reconcile(
                    path,
                    desired=desired_entries,
                    remove=remove_entries,
                )

    def is_ready(self, plan: WindowsFilesystemSetupPlan) -> bool:
        grouped = self._group(plan.entries)
        return all(self._api.matches(path, entries) for path, entries in grouped.items())

    def cleanup(self, managed: tuple[WindowsManagedAce, ...]) -> None:
        grouped = self._group(managed)
        for path, entries in sorted(grouped.items(), key=lambda item: str(item[0])):
            self._api.reconcile(path, desired=(), remove=entries)


class _NativeAclFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> object: ...


def _load_acl_function(
    library: object,
    name: str,
    argtypes: list[object],
    restype: object,
) -> _NativeAclFunction:
    function = cast(_NativeAclFunction, getattr(library, name))
    function.argtypes = argtypes
    function.restype = restype
    return function


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


class _TrusteeW(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_uint32),
        ("TrusteeForm", ctypes.c_uint32),
        ("TrusteeType", ctypes.c_uint32),
        ("ptstrName", ctypes.c_void_p),
    ]


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", ctypes.c_uint32),
        ("grfAccessMode", ctypes.c_uint32),
        ("grfInheritance", ctypes.c_uint32),
        ("Trustee", _TrusteeW),
    ]


class _NativeWindowsAclApi:  # pragma: no cover - exercised by Windows native CI
    """Native ACL facade preserving unrelated ACEs while changing exact tuples."""

    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _ACL_SIZE_INFORMATION = 2
    _ACL_REVISION = 2
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _ACCESS_DENIED_ACE_TYPE = 1
    _TRUSTEE_IS_SID = 0
    _TRUSTEE_IS_USER = 1
    _MAXDWORD = 0xFFFFFFFF

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsAclError("native Windows ACL authority is available only on Windows")
        loader = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if loader is None or get_last_error is None:  # pragma: no cover - defensive on Windows
            raise WindowsAclError("this Python runtime does not expose the Win32 ctypes API")
        advapi32 = cast(object, loader("advapi32.dll", use_last_error=True))
        kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
        self._get_last_error = cast(_NativeAclFunction, get_last_error)
        self._local_free = _load_acl_function(
            kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p
        )
        self._convert_sid = _load_acl_function(
            advapi32,
            "ConvertStringSidToSidW",
            [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int32,
        )
        self._sid_to_string = _load_acl_function(
            advapi32,
            "ConvertSidToStringSidW",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int32,
        )
        self._get_named_security_info = _load_acl_function(
            advapi32,
            "GetNamedSecurityInfoW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_uint32,
        )
        self._get_acl_information = _load_acl_function(
            advapi32,
            "GetAclInformation",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self._get_ace = _load_acl_function(
            advapi32,
            "GetAce",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int32,
        )
        self._initialize_acl = _load_acl_function(
            advapi32,
            "InitializeAcl",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self._add_ace = _load_acl_function(
            advapi32,
            "AddAce",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int32,
        )
        self._add_allowed_ace = _load_acl_function(
            advapi32,
            "AddAccessAllowedAceEx",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self._add_denied_ace = _load_acl_function(
            advapi32,
            "AddAccessDeniedAceEx",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p],
            ctypes.c_int32,
        )
        self._set_named_security_info = _load_acl_function(
            advapi32,
            "SetNamedSecurityInfoW",
            [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_uint32,
        )

    def _error(self, operation: str, code: int | None = None) -> WindowsAclError:
        error = cast(int, self._get_last_error()) if code is None else code
        return WindowsAclError(f"{operation} failed with Windows error {error}")

    def _sid_pointer(self, sid: SyntheticWindowsSid) -> int:
        pointer = ctypes.c_void_p()
        if not self._convert_sid(sid.value, ctypes.byref(pointer)) or not pointer.value:
            raise self._error("ConvertStringSidToSidW")
        return int(pointer.value)

    def _sid_string(self, pointer: int) -> str:
        output = ctypes.c_void_p()
        if not self._sid_to_string(pointer, ctypes.byref(output)) or not output.value:
            raise self._error("ConvertSidToStringSidW")
        try:
            return ctypes.wstring_at(output.value)
        finally:
            self._local_free(output)

    def _read_acl(self, path: Path) -> tuple[bytes, int]:
        owner = ctypes.c_void_p()
        group = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        sacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self._get_named_security_info(
            str(path),
            self._SE_FILE_OBJECT,
            self._DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(descriptor),
        )
        if result != 0:
            raise self._error("GetNamedSecurityInfoW", cast(int, result))
        try:
            if not dacl.value:
                raise WindowsAclError("refusing to mutate a NULL DACL")
            information = _AclSizeInformation()
            if not self._get_acl_information(
                dacl,
                ctypes.byref(information),
                ctypes.sizeof(information),
                self._ACL_SIZE_INFORMATION,
            ):
                raise self._error("GetAclInformation")
            raw_entries: list[bytes] = []
            for index in range(information.AceCount):
                ace = ctypes.c_void_p()
                if not self._get_ace(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise self._error("GetAce")
                header = _AceHeader.from_address(ace.value)
                raw_entries.append(ctypes.string_at(ace.value, header.AceSize))
            return b"".join(raw_entries), int(information.AceCount)
        finally:
            self._local_free(descriptor)

    def _raw_matches(self, raw: bytes, entry: WindowsManagedAce) -> bool:
        header = _AceHeader.from_buffer_copy(raw)
        expected_type = (
            self._ACCESS_DENIED_ACE_TYPE if entry.is_deny else self._ACCESS_ALLOWED_ACE_TYPE
        )
        if header.AceType != expected_type or header.AceFlags != entry.inheritance:
            return False
        mask = int.from_bytes(raw[4:8], "little")
        sid_pointer = ctypes.addressof(ctypes.create_string_buffer(raw[8:]))
        del sid_pointer  # The SID bytes are converted through a temporary buffer below.
        sid_buffer = ctypes.create_string_buffer(raw[8:])
        return (
            mask == entry.access_mask
            and self._sid_string(ctypes.addressof(sid_buffer)) == entry.sid.value
        )

    def _raw_entries(self, path: Path) -> list[bytes]:
        raw, _ = self._read_acl(path)
        entries: list[bytes] = []
        offset = 0
        while offset < len(raw):
            header = _AceHeader.from_buffer_copy(raw[offset:])
            entries.append(raw[offset : offset + header.AceSize])
            offset += header.AceSize
        return entries

    def matches(self, path: Path, desired: tuple[WindowsManagedAce, ...]) -> bool:
        raw_entries = self._raw_entries(path)
        return all(any(self._raw_matches(raw, entry) for raw in raw_entries) for entry in desired)

    def reconcile(
        self,
        path: Path,
        *,
        desired: tuple[WindowsManagedAce, ...],
        remove: tuple[WindowsManagedAce, ...],
    ) -> None:
        raw_entries = self._raw_entries(path)
        retained = [
            raw for raw in raw_entries if not any(self._raw_matches(raw, entry) for entry in remove)
        ]
        # Build the ACL in one buffer, preserving all unrelated raw ACE bytes.
        estimated_size = max(256, sum(len(raw) for raw in retained) + 1024 * max(1, len(desired)))
        acl_buffer = ctypes.create_string_buffer(estimated_size)
        if not self._initialize_acl(acl_buffer, estimated_size, self._ACL_REVISION):
            raise self._error("InitializeAcl")
        for raw in retained:
            raw_buffer = ctypes.create_string_buffer(raw)
            if not self._add_ace(
                acl_buffer,
                self._ACL_REVISION,
                self._MAXDWORD,
                raw_buffer,
                len(raw),
            ):
                raise self._error("AddAce")
        retained_entries = list(retained)
        for entry in desired:
            if any(self._raw_matches(raw, entry) for raw in retained_entries):
                continue
            sid_pointer = self._sid_pointer(entry.sid)
            try:
                function = self._add_denied_ace if entry.is_deny else self._add_allowed_ace
                if not function(
                    acl_buffer,
                    self._ACL_REVISION,
                    entry.inheritance,
                    entry.access_mask,
                    sid_pointer,
                ):
                    raise self._error(
                        "AddAccessDeniedAceEx" if entry.is_deny else "AddAccessAllowedAceEx"
                    )
            finally:
                self._local_free(sid_pointer)
        result = self._set_named_security_info(
            str(path),
            self._SE_FILE_OBJECT,
            self._DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.cast(acl_buffer, ctypes.c_void_p),
            None,
        )
        if result != 0:
            raise self._error("SetNamedSecurityInfoW", cast(int, result))


__all__ = [
    "INHERIT_TO_CHILDREN",
    "READ_ACCESS_MASK",
    "WRITE_ACCESS_MASK",
    "InMemoryWindowsAclApi",
    "WindowsAclApi",
    "WindowsAclError",
    "WindowsFilesystemAclAuthority",
    "WindowsFilesystemSetupPlan",
    "WindowsManagedAce",
    "WindowsManagedAceKind",
    "_NativeWindowsAclApi",
    "plan_windows_filesystem_authority",
]
