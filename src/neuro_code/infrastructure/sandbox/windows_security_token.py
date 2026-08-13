"""Restricted-token primitives for the Windows native sandbox foundation.

This module owns only the token boundary.  W2 setup authority provisions the
installation record, ACL plan, and firewall rule separately; this token layer
does not launch a command-runner broker or wire runtime child enforcement.

Windows native sandbox W1 的 restricted-token 原语.

本模块只负责 token 边界.W2 setup authority 独立负责 installation record、ACL plan
和 firewall rule;本 token layer 不启动 command-runner broker,也不接通 runtime child enforcement.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
from dataclasses import dataclass
from typing import Protocol, Self, cast

from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.shared.errors import SandboxError

_TOKEN_DUPLICATE = 0x0002
_TOKEN_QUERY = 0x0008
_TOKEN_ASSIGN_PRIMARY = 0x0001
_TOKEN_ADJUST_PRIVILEGES = 0x0020
_DISABLE_MAX_PRIVILEGE = 0x00000001
_LUA_TOKEN = 0x00000004
_WRITE_RESTRICTED = 0x00000008
_TOKEN_PRIVILEGES = 3
_TOKEN_RESTRICTED_SIDS = 11
_TOKEN_IS_RESTRICTED = 40
_ERROR_INSUFFICIENT_BUFFER = 122
_SE_PRIVILEGE_ENABLED = 0x00000002
_ERROR_NOT_ALL_ASSIGNED = 1300
_SE_CHANGE_NOTIFY_PRIVILEGE = "SeChangeNotifyPrivilege"


class WindowsTokenError(SandboxError):
    """A fail-closed Win32 security-token operation failure.

    The operation label and numeric Win32 code are safe diagnostics; no token
    contents, SID secrets, command arguments, or credentials are included.

    Win32 security-token 操作失败时失败关闭的错误.错误只包含操作名和数值错误码,
    不包含 token 内容、SID secret、命令参数或凭据.
    """

    def __init__(self, operation: str, error_code: int | None = None) -> None:
        self.operation = operation
        self.error_code = error_code
        suffix = "" if error_code is None else f" (Win32 error {error_code})"
        super().__init__(f"Windows security token operation failed: {operation}{suffix}")


@dataclass(frozen=True, slots=True)
class WindowsRestrictedTokenRequest:
    """Validated inputs for one write-restricted token creation.

    The default flags disable maximum privilege, request a LUA-style token, and
    apply ``WRITE_RESTRICTED``.  W1 supplies no disabled privileges or disabled
    SIDs; the restricted SID list is the only new write authority.

    一个受验证的 write-restricted token 创建请求.默认 flags 会禁用最大权限、请求 LUA
    token 并启用 ``WRITE_RESTRICTED``.W1 不传入 disabled privileges 或 disabled SIDs;
    restricted SID list 是唯一新增的写入 authority.
    """

    restricted_sids: tuple[SyntheticWindowsSid, ...]
    disable_max_privilege: bool = True
    lua_token: bool = True
    write_restricted: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.restricted_sids, tuple) or any(
            not isinstance(sid, SyntheticWindowsSid) for sid in self.restricted_sids
        ):
            raise TypeError("restricted_sids must be a tuple of canonical synthetic SIDs")
        if self.write_restricted and not self.restricted_sids:
            raise ValueError("WRITE_RESTRICTED requires at least one restricted SID")
        for value, name in (
            (self.disable_max_privilege, "disable_max_privilege"),
            (self.lua_token, "lua_token"),
            (self.write_restricted, "write_restricted"),
        ):
            if type(value) is not bool:
                raise TypeError(f"{name} must be bool")

    @property
    def flags(self) -> int:
        """Return the exact ``CreateRestrictedToken`` flag set."""

        flags = 0
        if self.disable_max_privilege:
            flags |= _DISABLE_MAX_PRIVILEGE
        if self.lua_token:
            flags |= _LUA_TOKEN
        if self.write_restricted:
            flags |= _WRITE_RESTRICTED
        return flags


@dataclass(frozen=True, slots=True)
class WindowsTokenInspection:
    """Safe token metadata used to attest creation without exposing contents."""

    restricted_sid_count: int
    is_restricted: bool
    privilege_count: int = 0
    enabled_privilege_count: int = 0

    def __post_init__(self) -> None:
        if type(self.restricted_sid_count) is not int or self.restricted_sid_count < 0:
            raise ValueError("restricted_sid_count must be a non-negative integer")
        if type(self.is_restricted) is not bool:
            raise TypeError("is_restricted must be bool")
        if type(self.privilege_count) is not int or self.privilege_count < 0:
            raise ValueError("privilege_count must be a non-negative integer")
        if type(self.enabled_privilege_count) is not int or self.enabled_privilege_count < 0:
            raise ValueError("enabled_privilege_count must be a non-negative integer")
        if self.enabled_privilege_count > self.privilege_count:
            raise ValueError("enabled privileges cannot exceed the privilege count")

    @property
    def has_restricted_sids(self) -> bool:
        return self.restricted_sid_count > 0


class _WindowsSecurityTokenApi(Protocol):
    """Small injectable surface around Win32 token operations."""

    def open_current_process_token(self) -> int: ...

    def create_restricted_token(
        self,
        existing_handle: int,
        flags: int,
        restricted_sids: tuple[SyntheticWindowsSid, ...],
    ) -> int: ...

    def inspect_token(self, token_handle: int) -> WindowsTokenInspection: ...

    def enable_change_notify_privilege(self, token_handle: int) -> None: ...

    def close_handle(self, handle: int) -> None: ...


class _CFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> object: ...


def _load_function(
    library: object,
    name: str,
    argtypes: list[object],
    restype: object,
) -> _CFunction:
    function = cast(_CFunction, getattr(library, name))
    function.argtypes = argtypes
    function.restype = restype
    return function


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.c_uint32),
    ]


class _Luid(ctypes.Structure):
    _fields_ = [
        ("LowPart", ctypes.c_uint32),
        ("HighPart", ctypes.c_int32),
    ]


class _LuidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("Luid", _Luid),
        ("Attributes", ctypes.c_uint32),
    ]


class _TokenPrivilegesOne(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", ctypes.c_uint32),
        ("Privileges", _LuidAndAttributes),
    ]


class _NativeWindowsSecurityTokenApi:
    """Lazy, narrow ctypes facade for the W1 Win32 token calls."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsTokenError("load Win32 security-token API")

        loader = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if loader is None or get_last_error is None:  # pragma: no cover - defensive on Windows
            raise WindowsTokenError("load Win32 security-token API")

        advapi32 = cast(object, loader("advapi32.dll", use_last_error=True))
        kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
        self._get_last_error = cast(_CFunction, get_last_error)
        self._get_current_process = _load_function(
            kernel32,
            "GetCurrentProcess",
            [],
            ctypes.c_void_p,
        )
        self._close_handle = _load_function(
            kernel32,
            "CloseHandle",
            [ctypes.c_void_p],
            ctypes.c_int32,
        )
        self._local_free = _load_function(
            kernel32,
            "LocalFree",
            [ctypes.c_void_p],
            ctypes.c_void_p,
        )
        self._open_process_token = _load_function(
            advapi32,
            "OpenProcessToken",
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int32,
        )
        self._create_restricted_token = _load_function(
            advapi32,
            "CreateRestrictedToken",
            [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(_SidAndAttributes),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_SidAndAttributes),
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_int32,
        )
        self._convert_string_sid = _load_function(
            advapi32,
            "ConvertStringSidToSidW",
            [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int32,
        )
        self._get_token_information = _load_function(
            advapi32,
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
        self._lookup_privilege_value = _load_function(
            advapi32,
            "LookupPrivilegeValueW",
            [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(_Luid)],
            ctypes.c_int32,
        )
        self._adjust_token_privileges = _load_function(
            advapi32,
            "AdjustTokenPrivileges",
            [
                ctypes.c_void_p,
                ctypes.c_int32,
                ctypes.POINTER(_TokenPrivilegesOne),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_int32,
        )

    def _error(self, operation: str) -> WindowsTokenError:
        return WindowsTokenError(operation, cast(int, self._get_last_error()))

    def open_current_process_token(self) -> int:
        token_handle = ctypes.c_void_p()
        opened = self._open_process_token(
            self._get_current_process(),
            _TOKEN_DUPLICATE | _TOKEN_QUERY | _TOKEN_ASSIGN_PRIMARY | _TOKEN_ADJUST_PRIVILEGES,
            ctypes.byref(token_handle),
        )
        if not opened or not token_handle.value:
            raise self._error("OpenProcessToken")
        return int(token_handle.value)

    def _convert_sid(self, sid: SyntheticWindowsSid) -> int:
        sid_pointer = ctypes.c_void_p()
        converted = self._convert_string_sid(sid.value, ctypes.byref(sid_pointer))
        if not converted or not sid_pointer.value:
            raise self._error("ConvertStringSidToSidW")
        return int(sid_pointer.value)

    def _free_sid(self, sid_pointer: int) -> None:
        if self._local_free(sid_pointer):
            raise self._error("LocalFree")

    def create_restricted_token(
        self,
        existing_handle: int,
        flags: int,
        restricted_sids: tuple[SyntheticWindowsSid, ...],
    ) -> int:
        sid_pointers: list[int] = []
        created_handle: int | None = None
        operation_failure: BaseException | None = None
        try:
            sid_pointers.extend(self._convert_sid(sid) for sid in restricted_sids)
            sid_array_type = _SidAndAttributes * len(sid_pointers)
            sid_array = sid_array_type(*(_SidAndAttributes(pointer, 0) for pointer in sid_pointers))
            new_token = ctypes.c_void_p()
            created = self._create_restricted_token(
                existing_handle,
                flags,
                0,
                None,
                0,
                None,
                len(sid_pointers),
                ctypes.cast(sid_array, ctypes.POINTER(_SidAndAttributes)) if sid_pointers else None,
                ctypes.byref(new_token),
            )
            if not created or not new_token.value:
                raise self._error("CreateRestrictedToken")
            created_handle = int(new_token.value)
        except BaseException as error:
            operation_failure = error

        cleanup_failure: BaseException | None = None
        for sid_pointer in sid_pointers:
            try:
                self._free_sid(sid_pointer)
            except BaseException as error:
                if cleanup_failure is None:
                    cleanup_failure = error

        if operation_failure is not None:
            raise operation_failure
        if cleanup_failure is not None:
            raise cleanup_failure
        if created_handle is None:  # pragma: no cover - defensive
            raise WindowsTokenError("CreateRestrictedToken produced no handle")
        return created_handle

    def _query_token_information(self, token_handle: int, information_class: int) -> bytes:
        required_size = ctypes.c_uint32()
        self._get_token_information(
            token_handle,
            information_class,
            None,
            0,
            ctypes.byref(required_size),
        )
        error_code = cast(int, self._get_last_error())
        if error_code != _ERROR_INSUFFICIENT_BUFFER or required_size.value == 0:
            raise WindowsTokenError("GetTokenInformation", error_code)
        buffer = ctypes.create_string_buffer(required_size.value)
        returned_size = ctypes.c_uint32()
        queried = self._get_token_information(
            token_handle,
            information_class,
            ctypes.byref(buffer),
            required_size.value,
            ctypes.byref(returned_size),
        )
        if not queried:
            raise self._error("GetTokenInformation")
        return buffer.raw[: returned_size.value]

    def inspect_token(self, token_handle: int) -> WindowsTokenInspection:
        restricted_sids = self._query_token_information(token_handle, _TOKEN_RESTRICTED_SIDS)
        if len(restricted_sids) < ctypes.sizeof(ctypes.c_uint32):
            raise WindowsTokenError("parse TokenRestrictedSids")
        restricted_sid_count = int.from_bytes(restricted_sids[:4], "little", signed=False)

        is_restricted = self._query_token_information(token_handle, _TOKEN_IS_RESTRICTED)
        if len(is_restricted) < ctypes.sizeof(ctypes.c_int32):
            raise WindowsTokenError("parse TokenIsRestricted")
        privileges = self._query_token_information(token_handle, _TOKEN_PRIVILEGES)
        if len(privileges) < ctypes.sizeof(ctypes.c_uint32):
            raise WindowsTokenError("parse TokenPrivileges")
        privilege_count = int.from_bytes(privileges[:4], "little", signed=False)
        entry_size = 12  # LUID (8 bytes) + DWORD attributes (4 bytes).
        if len(privileges) < 4 + privilege_count * entry_size:
            raise WindowsTokenError("parse TokenPrivileges")
        enabled_privilege_count = sum(
            bool(
                int.from_bytes(
                    privileges[offset + 8 : offset + entry_size],
                    "little",
                    signed=False,
                )
                & _SE_PRIVILEGE_ENABLED
            )
            for offset in range(4, 4 + privilege_count * entry_size, entry_size)
        )
        return WindowsTokenInspection(
            restricted_sid_count=restricted_sid_count,
            is_restricted=bool(int.from_bytes(is_restricted[:4], "little", signed=False)),
            privilege_count=privilege_count,
            enabled_privilege_count=enabled_privilege_count,
        )

    def enable_change_notify_privilege(self, token_handle: int) -> None:
        """Restore only directory traversal for a restricted child token.

        ``DISABLE_MAX_PRIVILEGE`` intentionally removes the source token's
        privileges.  A process still needs ``SeChangeNotifyPrivilege`` to
        traverse ordinary parent directories while resolving an executable,
        cwd, or imported module.  This method does not grant file, network, or
        administrative authority; it restores the single Windows traversal
        privilege and fails closed when the token cannot receive it.
        """

        luid = _Luid()
        if not self._lookup_privilege_value(None, _SE_CHANGE_NOTIFY_PRIVILEGE, ctypes.byref(luid)):
            raise self._error("LookupPrivilegeValueW(SeChangeNotifyPrivilege)")
        privileges = _TokenPrivilegesOne(
            PrivilegeCount=1,
            Privileges=_LuidAndAttributes(luid, _SE_PRIVILEGE_ENABLED),
        )
        if not self._adjust_token_privileges(
            token_handle,
            0,
            ctypes.byref(privileges),
            0,
            None,
            None,
        ):
            raise self._error("AdjustTokenPrivileges(SeChangeNotifyPrivilege)")
        if cast(int, self._get_last_error()) == _ERROR_NOT_ALL_ASSIGNED:
            raise WindowsTokenError(
                "AdjustTokenPrivileges(SeChangeNotifyPrivilege)",
                _ERROR_NOT_ALL_ASSIGNED,
            )

    def close_handle(self, handle: int) -> None:
        if not self._close_handle(handle):
            raise self._error("CloseHandle")


class WindowsRestrictedToken:
    """Own one restricted token handle with deterministic cleanup."""

    def __init__(
        self,
        handle: int,
        request: WindowsRestrictedTokenRequest,
        inspection: WindowsTokenInspection,
        api: _WindowsSecurityTokenApi,
    ) -> None:
        if handle <= 0:
            raise ValueError("token handle must be positive")
        self._handle: int | None = handle
        self._request = request
        self._inspection = inspection
        self._api = api

    @classmethod
    def create_from_current_process(
        cls,
        request: WindowsRestrictedTokenRequest,
        *,
        api: _WindowsSecurityTokenApi | None = None,
    ) -> Self:
        """Create and attest a restricted token, closing the source token."""

        token_api = _NativeWindowsSecurityTokenApi() if api is None else api
        source_handle: int | None = None
        created_handle: int | None = None
        inspection: WindowsTokenInspection | None = None
        failure: BaseException | None = None
        try:
            source_handle = token_api.open_current_process_token()
            if source_handle <= 0:
                raise WindowsTokenError("OpenProcessToken returned an invalid handle")
            created_handle = token_api.create_restricted_token(
                source_handle,
                request.flags,
                request.restricted_sids,
            )
            if created_handle <= 0:
                raise WindowsTokenError("CreateRestrictedToken returned an invalid handle")
            inspection = token_api.inspect_token(created_handle)
        except BaseException as error:
            failure = error

        if source_handle is not None:
            try:
                token_api.close_handle(source_handle)
            except BaseException as error:
                if failure is None:
                    failure = error

        if failure is not None:
            if created_handle is not None:
                with contextlib.suppress(BaseException):
                    token_api.close_handle(created_handle)
            raise failure
        if created_handle is None or inspection is None:  # pragma: no cover - defensive
            raise WindowsTokenError("CreateRestrictedToken produced no attestation")
        return cls(created_handle, request, inspection, token_api)

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise WindowsTokenError("use closed restricted token")
        return self._handle

    @property
    def request(self) -> WindowsRestrictedTokenRequest:
        return self._request

    @property
    def inspection(self) -> WindowsTokenInspection:
        return self._inspection

    def enable_change_notify_privilege(self) -> None:
        """Enable only the traversal privilege required by native child startup."""

        if self._handle is None:
            raise WindowsTokenError("enable traversal privilege on closed token")
        self._api.enable_change_notify_privilege(self._handle)
        self._inspection = self._api.inspect_token(self._handle)

    def close(self) -> None:
        """Close the owned handle exactly once."""

        handle, self._handle = self._handle, None
        if handle is not None:
            self._api.close_handle(handle)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup
        with contextlib.suppress(BaseException):
            self.close()


__all__ = [
    "WindowsRestrictedToken",
    "WindowsRestrictedTokenRequest",
    "WindowsTokenError",
    "WindowsTokenInspection",
]
