"""Windows sandbox filesystem authority planning and exact ACE reconciliation.

The planner is platform-neutral and deliberately narrow: it grants real
dedicated account SIDs read access, grants those accounts plus the synthetic
restricted-token SID write access only on explicitly named writable roots,
adds read-only and sensitive-read denies, and removes only ACE tuples that this
installation previously managed.  Unrelated controller-user ACL entries are
never part of the managed set.

Windows 沙箱文件系统 authority 的规划与精确 ACE reconciliation.

planner 平台无关且范围很窄:真实 dedicated account SID 获得 read 权限,真实 account
SID 和 synthetic restricted-token SID 仅在明确 writable roots 上获得 write 权限,为
read-only 和敏感路径加入显式 deny,并且只删除本 installation 之前管理的 ACE tuple.
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
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import WindowsAccountSid
from neuro_code.infrastructure.sandbox.windows_sandbox_diagnostics import (
    WindowsSandboxOperationDiagnostic,
)
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
WRITE_ONLY_ACCESS_MASK = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_DELETE_CHILD
    | FILE_WRITE_ATTRIBUTES
    | DELETE
)
INHERIT_TO_CHILDREN = 0x00000003  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE


class WindowsAclError(SandboxError):
    """A filesystem ACL authority operation failed closed."""

    def __init__(
        self,
        message: str,
        *,
        safe_diagnostic: WindowsSandboxOperationDiagnostic | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_diagnostic = safe_diagnostic or WindowsSandboxOperationDiagnostic(
            None,
            type(self).__name__,
        )


class WindowsManagedAceKind(StrEnum):
    """Exact managed ACE roles owned by this installation."""

    READ_ALLOW = "read-allow"
    WRITE_ALLOW = "write-allow"
    RESTRICTING_WRITE_ALLOW = "restricting-write-allow"
    READ_ONLY_WRITE_DENY = "read-only-write-deny"
    SENSITIVE_READ_DENY = "sensitive-read-deny"
    CREDENTIAL_PROTECTION_DENY = "credential-protection-deny"


@dataclass(frozen=True, slots=True)
class WindowsManagedAce:
    """One exact, installation-owned ACE tuple."""

    path: Path
    sid: WindowsAccountSid | SyntheticWindowsSid
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
        if not isinstance(self.sid, (WindowsAccountSid, SyntheticWindowsSid)):
            raise TypeError("managed ACE SID must be a canonical account or synthetic SID")
        if not isinstance(self.kind, WindowsManagedAceKind):
            raise TypeError("managed ACE kind must be canonical")
        if self.kind in (
            WindowsManagedAceKind.READ_ALLOW,
            WindowsManagedAceKind.READ_ONLY_WRITE_DENY,
            WindowsManagedAceKind.SENSITIVE_READ_DENY,
            WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY,
        ) and not isinstance(self.sid, WindowsAccountSid):
            raise TypeError("read and deny ACEs must target a real account SID")
        if self.kind is WindowsManagedAceKind.WRITE_ALLOW:
            if not isinstance(self.sid, WindowsAccountSid):
                raise TypeError("user write ACEs must target a real account SID")
            expected_mask = WRITE_ACCESS_MASK
        elif self.kind is WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW:
            if not isinstance(self.sid, SyntheticWindowsSid):
                raise TypeError("restricting write ACEs must target the synthetic SID")
            expected_mask = WRITE_ONLY_ACCESS_MASK
        elif self.kind is WindowsManagedAceKind.READ_ONLY_WRITE_DENY:
            expected_mask = WRITE_ONLY_ACCESS_MASK
        elif self.kind is WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY:
            # Machine-scope DPAPI is not a user boundary.  Deny the complete
            # file-state mutation surface (read/write/delete/replace), not
            # just reads, so an untrusted account cannot swap the envelope.
            expected_mask = WRITE_ACCESS_MASK
        else:
            expected_mask = READ_ACCESS_MASK
        if self.access_mask != expected_mask:
            raise ValueError("managed ACE access mask does not match its role")
        if self.kind in (
            WindowsManagedAceKind.SENSITIVE_READ_DENY,
            WindowsManagedAceKind.READ_ONLY_WRITE_DENY,
            WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY,
        ):
            if self.inheritance not in (0, INHERIT_TO_CHILDREN):
                raise ValueError("sensitive deny inheritance must be exact or cover descendants")
        elif self.inheritance != INHERIT_TO_CHILDREN:
            raise ValueError("managed ACE inheritance must cover files and child directories")
        object.__setattr__(self, "path", canonical)

    @property
    def is_deny(self) -> bool:
        return self.kind in (
            WindowsManagedAceKind.SENSITIVE_READ_DENY,
            WindowsManagedAceKind.READ_ONLY_WRITE_DENY,
            WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY,
        )


@dataclass(frozen=True, slots=True)
class WindowsAclEntryProjection:
    """Bounded, non-secret projection of one standard DACL ACE.

    The projection is intentionally read-only and contains only the SID,
    access mask, allow/deny type, and ACE flags needed by setup acceptance.
    It is not a second policy model and is never used to grant authority.
    """

    sid: str
    access_mask: int
    is_deny: bool
    inheritance: int

    def __post_init__(self) -> None:
        if not isinstance(self.sid, str) or not self.sid.startswith("S-1-"):
            raise ValueError("ACL projection SID must be canonical text")
        if type(self.access_mask) is not int or self.access_mask < 0:
            raise ValueError("ACL projection access mask must be non-negative")
        if type(self.is_deny) is not bool:
            raise TypeError("ACL projection is_deny must be bool")
        if type(self.inheritance) is not int or self.inheritance < 0 or self.inheritance > 0xFF:
            raise ValueError("ACL projection inheritance must be an ACE flag byte")


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
    *,
    read_user_sids: tuple[WindowsAccountSid, ...] = (),
    write_user_sids: tuple[WindowsAccountSid, ...] = (),
    credential_path: Path | None = None,
    private_root: Path | None = None,
) -> WindowsFilesystemSetupPlan:
    """Build explicit real-user read policy and synthetic write policy.

    Read and sensitive-deny ACEs always target real dedicated local users.
    ``write_sid`` is the restricted-token SID and is never used as a read or
    firewall principal.  The real users also receive write access on writable
    roots because a token's normal user SID remains part of its access check.
    """

    if not isinstance(request, WindowsSandboxSetupRequest):
        raise TypeError("filesystem setup request must be canonical")
    if not isinstance(write_sid, SyntheticWindowsSid):
        raise TypeError("filesystem setup write SID must be canonical")
    if not read_user_sids or set(read_user_sids) != set(write_user_sids):
        raise ValueError("read_user_sids and write_user_sids must contain the same real users")
    if any(not isinstance(sid, WindowsAccountSid) for sid in read_user_sids):
        raise TypeError("read_user_sids must contain real account SIDs")
    if credential_path is not None and (
        not isinstance(credential_path, Path) or not credential_path.is_absolute()
    ):
        raise ValueError("credential_path must be an absolute path")
    if private_root is not None and (
        not isinstance(private_root, Path) or not private_root.is_absolute()
    ):
        raise ValueError("private_root must be an absolute path")
    canonical_private_root = (
        private_root.expanduser().resolve(strict=False) if private_root is not None else None
    )
    canonical_credential_path = (
        credential_path.expanduser().resolve(strict=False) if credential_path is not None else None
    )
    if (
        canonical_private_root is not None
        and canonical_private_root == canonical_private_root.parent
    ):
        raise ValueError("private_root must not be a filesystem root")
    if (
        canonical_private_root is not None
        and canonical_credential_path is not None
        and not (canonical_credential_path.is_relative_to(canonical_private_root))
    ):
        raise ValueError("credential_path must be inside private_root")
    entries: list[WindowsManagedAce] = []
    for root in request.read_roots:
        entries.extend(
            WindowsManagedAce(root, sid, WindowsManagedAceKind.READ_ALLOW, READ_ACCESS_MASK)
            for sid in read_user_sids
        )
    for root in request.writable_roots:
        entries.extend(
            [
                WindowsManagedAce(root, sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK)
                for sid in write_user_sids
            ]
            + [
                WindowsManagedAce(
                    root,
                    write_sid,
                    WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                    WRITE_ONLY_ACCESS_MASK,
                )
            ]
        )
    read_only_roots = set(request.read_roots) - set(request.writable_roots)
    for root in read_only_roots:
        entries.extend(
            WindowsManagedAce(
                root,
                sid,
                WindowsManagedAceKind.READ_ONLY_WRITE_DENY,
                WRITE_ONLY_ACCESS_MASK,
            )
            for sid in read_user_sids
        )
    for sensitive in request.sensitive_read_paths:
        entries.extend(
            WindowsManagedAce(
                sensitive,
                sid,
                WindowsManagedAceKind.SENSITIVE_READ_DENY,
                READ_ACCESS_MASK,
                # A sensitive path is an exact file/path authority.  Do not
                # request child inheritance that Windows may normalize away;
                # recursive protection belongs to an explicitly named root.
                inheritance=0,
            )
            for sid in read_user_sids
        )
    if credential_path is not None:
        entries.extend(
            WindowsManagedAce(
                credential_path,
                sid,
                WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY,
                WRITE_ACCESS_MASK,
                inheritance=0,
            )
            for sid in read_user_sids
        )
    if private_root is not None:
        # The private installation root is a controller/setup boundary.  The
        # inherited deny protects atomic temporary files and future state
        # records as well as the current DPAPI envelope.
        assert canonical_private_root is not None
        entries.extend(
            WindowsManagedAce(
                canonical_private_root,
                sid,
                WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY,
                WRITE_ACCESS_MASK,
                inheritance=INHERIT_TO_CHILDREN,
            )
            for sid in read_user_sids
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
        # Keep a separate ordered projection so tests can model an external
        # ACE reorder without weakening the set-based compatibility surface.
        self.ordered_entries: dict[Path, list[WindowsManagedAce]] = defaultdict(list)
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
        ordered = list(self.entries[canonical])
        ordered.sort(key=lambda entry: (0 if entry.is_deny else 1, str(entry)))
        self.ordered_entries[canonical] = ordered
        if not self.entries[canonical]:
            self.entries.pop(canonical, None)
            self.ordered_entries.pop(canonical, None)

    def matches(self, path: Path, desired: tuple[WindowsManagedAce, ...]) -> bool:
        canonical = path.expanduser().resolve(strict=False)
        observed = self.entries.get(canonical, set())
        if not set(desired).issubset(observed):
            return False
        ordered = self.ordered_entries.get(canonical, [])
        if len(ordered) != len(observed):
            return False
        if ordered != sorted(ordered, key=lambda entry: (0 if entry.is_deny else 1, str(entry))):
            return False
        positions = {entry: index for index, entry in enumerate(ordered)}
        if any(entry not in positions for entry in desired):
            return False
        deny_positions = [positions[entry] for entry in desired if entry.is_deny]
        allow_positions = [positions[entry] for entry in desired if not entry.is_deny]
        return (
            not deny_positions or not allow_positions or max(deny_positions) < min(allow_positions)
        )


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
    _TRUSTEE_IS_UNKNOWN = 0
    _SET_ACCESS = 2
    _DENY_ACCESS = 3
    _MAXDWORD = 0xFFFFFFFF
    _INHERITED_ACE = 0x10
    _MAX_ACE_COUNT = 4096

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
        self._set_entries_in_acl = _load_acl_function(
            advapi32,
            "SetEntriesInAclW",
            [
                ctypes.c_uint32,
                ctypes.POINTER(_ExplicitAccessW),
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_uint32,
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
        return WindowsAclError(
            f"{operation} failed with Windows error {error}",
            safe_diagnostic=WindowsSandboxOperationDiagnostic(
                operation,
                "Win32Error",
                winerror=error,
            ),
        )

    def _sid_pointer(self, sid: WindowsAccountSid | SyntheticWindowsSid) -> int:
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
            if header.AceSize < ctypes.sizeof(_AceHeader) or offset + header.AceSize > len(raw):
                raise self._error("parse ACL ACE")
            entries.append(raw[offset : offset + header.AceSize])
            offset += header.AceSize
        return entries

    def inspect_entries(self, path: Path) -> tuple[WindowsAclEntryProjection, ...]:
        """Return a bounded projection of standard allow/deny DACL entries."""

        raw_entries = self._raw_entries(path)
        if len(raw_entries) > self._MAX_ACE_COUNT:
            raise self._error("inspect ACL ACE count")
        projections: list[WindowsAclEntryProjection] = []
        for raw in raw_entries:
            header = _AceHeader.from_buffer_copy(raw)
            if header.AceType not in (
                self._ACCESS_ALLOWED_ACE_TYPE,
                self._ACCESS_DENIED_ACE_TYPE,
            ):
                continue
            if len(raw) < 8:
                raise self._error("parse ACL ACE")
            sid_buffer = ctypes.create_string_buffer(raw[8:])
            projections.append(
                WindowsAclEntryProjection(
                    sid=self._sid_string(ctypes.addressof(sid_buffer)),
                    access_mask=int.from_bytes(raw[4:8], "little", signed=False),
                    is_deny=header.AceType == self._ACCESS_DENIED_ACE_TYPE,
                    inheritance=int(header.AceFlags),
                )
            )
        return tuple(projections)

    def matches(self, path: Path, desired: tuple[WindowsManagedAce, ...]) -> bool:
        raw_entries = self._raw_entries(path)
        positions: dict[WindowsManagedAce, list[int]] = {
            entry: [index for index, raw in enumerate(raw_entries) if self._raw_matches(raw, entry)]
            for entry in desired
        }
        if any(len(matches) != 1 for matches in positions.values()):
            return False
        managed_positions = {entry: matches[0] for entry, matches in positions.items()}
        explicit_allow_positions = [
            index
            for index, raw in enumerate(raw_entries)
            if _AceHeader.from_buffer_copy(raw).AceType == self._ACCESS_ALLOWED_ACE_TYPE
            and not (_AceHeader.from_buffer_copy(raw).AceFlags & self._INHERITED_ACE)
        ]
        inherited_positions = [
            index
            for index, raw in enumerate(raw_entries)
            if _AceHeader.from_buffer_copy(raw).AceFlags & self._INHERITED_ACE
        ]
        for entry, position in managed_positions.items():
            header = _AceHeader.from_buffer_copy(raw_entries[position])
            if entry.is_deny:
                # Explicit managed denies must precede every explicit allow
                # and every inherited ACE to remain an effective canonical
                # deny, including when a broad unrelated allow is present.
                if any(index < position for index in explicit_allow_positions):
                    return False
                if any(index < position for index in inherited_positions):
                    return False
            elif not (header.AceFlags & self._INHERITED_ACE):
                if any(index < position for index in inherited_positions):
                    return False
        return True

    def reconcile(
        self,
        path: Path,
        *,
        desired: tuple[WindowsManagedAce, ...],
        remove: tuple[WindowsManagedAce, ...],
    ) -> None:
        raw_entries = self._raw_entries(path)
        retained = [
            raw
            for raw in raw_entries
            if not any(self._raw_matches(raw, entry) for entry in (*remove, *desired))
        ]
        # Build a retained ACL in one buffer, preserving all unrelated raw ACE
        # bytes.  New entries are passed through SetEntriesInAclW below so
        # Windows performs canonical deny-before-allow ordering.
        estimated_size = max(256, sum(len(raw) for raw in retained) + 256)
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
        # Re-add every desired managed entry, even if it was already present,
        # so externally reordered managed ACEs cannot remain before a deny.
        missing = list(desired)
        sid_pointers: list[int] = []
        explicit_array = (_ExplicitAccessW * len(missing))() if missing else None
        if missing and explicit_array is not None:
            for index, entry in enumerate(missing):
                sid_pointer = self._sid_pointer(entry.sid)
                sid_pointers.append(sid_pointer)
                explicit_array[index] = _ExplicitAccessW(
                    entry.access_mask,
                    self._DENY_ACCESS if entry.is_deny else self._SET_ACCESS,
                    entry.inheritance,
                    _TrusteeW(
                        None,
                        0,
                        self._TRUSTEE_IS_SID,
                        self._TRUSTEE_IS_UNKNOWN,
                        sid_pointer,
                    ),
                )
        new_acl = ctypes.c_void_p()
        try:
            if missing and explicit_array is not None:
                result = self._set_entries_in_acl(
                    len(missing),
                    explicit_array,
                    ctypes.cast(acl_buffer, ctypes.c_void_p),
                    ctypes.byref(new_acl),
                )
                if result != 0 or not new_acl.value:
                    raise self._error("SetEntriesInAclW", cast(int, result))
            else:
                new_acl = ctypes.cast(acl_buffer, ctypes.c_void_p)
            result = self._set_named_security_info(
                str(path),
                self._SE_FILE_OBJECT,
                self._DACL_SECURITY_INFORMATION,
                None,
                None,
                new_acl,
                None,
            )
            if result != 0:
                raise self._error("SetNamedSecurityInfoW", cast(int, result))
        finally:
            for sid_pointer in sid_pointers:
                self._local_free(sid_pointer)
            if missing and new_acl.value:
                self._local_free(new_acl)


__all__ = [
    "INHERIT_TO_CHILDREN",
    "READ_ACCESS_MASK",
    "WRITE_ACCESS_MASK",
    "WRITE_ONLY_ACCESS_MASK",
    "InMemoryWindowsAclApi",
    "WindowsAclApi",
    "WindowsAclEntryProjection",
    "WindowsAclError",
    "WindowsFilesystemAclAuthority",
    "WindowsFilesystemSetupPlan",
    "WindowsManagedAce",
    "WindowsManagedAceKind",
    "_NativeWindowsAclApi",
    "plan_windows_filesystem_authority",
]
