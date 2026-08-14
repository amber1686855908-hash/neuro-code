"""Bounded final-child token probe used by the privileged W3 Gate 1.

The script is copied into a disposable workspace and executed by the actual
``CreateProcessAsUserW`` child.  It reports only token identity, restriction,
and bounded privilege facts; it never emits token bytes, environment data, or
credential material.
"""

from __future__ import annotations

import ctypes
import json
import sys

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_PRIVILEGES = 3
_TOKEN_RESTRICTED_SIDS = 11
_TOKEN_LOGON_SID = 28
_TOKEN_IS_RESTRICTED = 40
_ERROR_INSUFFICIENT_BUFFER = 122
_SE_PRIVILEGE_ENABLED = 0x00000002
_SE_CHANGE_NOTIFY_PRIVILEGE = "SeChangeNotifyPrivilege"
_MAX_PRIVILEGES = 64
_MAX_SID_COUNT = 64


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _TokenGroupsOne(ctypes.Structure):
    _fields_ = [("GroupCount", ctypes.c_uint32), ("Groups", _SidAndAttributes * 1)]


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _LuidAndAttributes(ctypes.Structure):
    _fields_ = [("Luid", _Luid), ("Attributes", ctypes.c_uint32)]


class _TokenPrivilegesOne(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_uint32), ("Privileges", _LuidAndAttributes * 1)]


def _load(library: object, name: str, argtypes: list[object], restype: object) -> object:
    function = getattr(library, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def _query(
    get_information: object, token: int, information_class: int
) -> ctypes.Array[ctypes.c_char]:
    required = ctypes.c_uint32()
    get_information(token, information_class, None, 0, ctypes.byref(required))
    if required.value == 0 or ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
        raise RuntimeError("GetTokenInformation sizing failed")
    buffer = ctypes.create_string_buffer(required.value)
    returned = ctypes.c_uint32()
    if not get_information(
        token,
        information_class,
        ctypes.byref(buffer),
        required.value,
        ctypes.byref(returned),
    ):
        raise RuntimeError("GetTokenInformation failed")
    if returned.value > required.value:
        raise RuntimeError("GetTokenInformation returned invalid size")
    return buffer


def _sid_text(convert_sid: object, local_free: object, pointer: int) -> str:
    if not pointer:
        raise RuntimeError("token SID pointer is empty")
    output = ctypes.c_void_p()
    if not convert_sid(pointer, ctypes.byref(output)) or not output.value:
        raise RuntimeError("ConvertSidToStringSidW failed")
    try:
        value = ctypes.wstring_at(output.value)
    finally:
        local_free(output)
    if not value.startswith("S-1-") or len(value) > 128:
        raise RuntimeError("token SID text is invalid")
    return value


def _privilege_name(lookup_name: object, luid: _Luid) -> str:
    size = ctypes.c_uint32()
    lookup_name(None, ctypes.byref(luid), None, ctypes.byref(size))
    if size.value == 0 or size.value > 128:
        raise RuntimeError("LookupPrivilegeNameW sizing failed")
    name = ctypes.create_unicode_buffer(size.value + 1)
    if not lookup_name(None, ctypes.byref(luid), name, ctypes.byref(size)):
        raise RuntimeError("LookupPrivilegeNameW failed")
    return name.value


def _run() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows token probe requires Windows")
    advapi = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    get_current_process = _load(kernel, "GetCurrentProcess", [], ctypes.c_void_p)
    close_handle = _load(kernel, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32)
    open_process_token = _load(
        advapi,
        "OpenProcessToken",
        [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.c_int32,
    )
    get_information = _load(
        advapi,
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
    convert_sid = _load(
        advapi,
        "ConvertSidToStringSidW",
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        ctypes.c_int32,
    )
    local_free = _load(kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    lookup_name = _load(
        advapi,
        "LookupPrivilegeNameW",
        [
            ctypes.c_wchar_p,
            ctypes.POINTER(_Luid),
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ],
        ctypes.c_int32,
    )
    token = ctypes.c_void_p()
    if (
        not open_process_token(get_current_process(), _TOKEN_QUERY, ctypes.byref(token))
        or not token.value
    ):
        raise RuntimeError("OpenProcessToken failed")
    try:
        user_buffer = _query(get_information, token, _TOKEN_USER)
        user = _SidAndAttributes.from_buffer(user_buffer)
        user_sid = _sid_text(convert_sid, local_free, int(user.Sid or 0))

        logon_buffer = _query(get_information, token, _TOKEN_LOGON_SID)
        logon = _TokenGroupsOne.from_buffer(logon_buffer)
        if logon.GroupCount != 1:
            raise RuntimeError("TokenLogonSid is not a singleton")
        logon_sid = _sid_text(convert_sid, local_free, int(logon.Groups[0].Sid or 0))

        restricted_buffer = _query(get_information, token, _TOKEN_RESTRICTED_SIDS)
        restricted_count = int.from_bytes(restricted_buffer.raw[:4], "little", signed=False)
        if restricted_count > _MAX_SID_COUNT:
            raise RuntimeError("TokenRestrictedSids is unbounded")
        restricted_offset = _TokenGroupsOne.Groups.offset
        entry_size = ctypes.sizeof(_SidAndAttributes)
        restricted_sids = tuple(
            _sid_text(
                convert_sid,
                local_free,
                int(
                    _SidAndAttributes.from_buffer(
                        restricted_buffer, restricted_offset + index * entry_size
                    ).Sid
                    or 0
                ),
            )
            for index in range(restricted_count)
        )

        restricted_flag = _query(get_information, token, _TOKEN_IS_RESTRICTED)
        is_restricted = bool(int.from_bytes(restricted_flag.raw[:4], "little", signed=False))

        privilege_buffer = _query(get_information, token, _TOKEN_PRIVILEGES)
        privilege_count = int.from_bytes(privilege_buffer.raw[:4], "little", signed=False)
        if privilege_count > _MAX_PRIVILEGES:
            raise RuntimeError("TokenPrivileges is unbounded")
        privilege_offset = _TokenPrivilegesOne.Privileges.offset
        privilege_size = ctypes.sizeof(_LuidAndAttributes)
        enabled_privileges = tuple(
            _privilege_name(
                lookup_name,
                _LuidAndAttributes.from_buffer(
                    privilege_buffer, privilege_offset + index * privilege_size
                ).Luid,
            )
            for index in range(privilege_count)
            if _LuidAndAttributes.from_buffer(
                privilege_buffer, privilege_offset + index * privilege_size
            ).Attributes
            & _SE_PRIVILEGE_ENABLED
        )
        unexpected = tuple(
            name for name in enabled_privileges if name != _SE_CHANGE_NOTIFY_PRIVILEGE
        )
        result = {
            "user_sid": user_sid,
            "logon_sid": logon_sid,
            "is_restricted": is_restricted,
            "restricted_sids": list(restricted_sids),
            "change_notify": _SE_CHANGE_NOTIFY_PRIVILEGE in enabled_privileges,
            "enabled_privileges": list(enabled_privileges),
            "unexpected_enabled_privileges": list(unexpected),
        }
        print(json.dumps(result, sort_keys=True), flush=True)
    finally:
        close_handle(token)


if __name__ == "__main__":
    _run()
