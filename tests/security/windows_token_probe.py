"""Bounded final-child token probe used by the privileged W3 Gate 1.

The script is copied into a disposable workspace and executed by the actual
``CreateProcessAsUserW`` child.  It reports only token identity, restriction,
and bounded privilege facts; it never emits token bytes, environment data, or
credential material.
"""

from __future__ import annotations

import sys

print("G1_PROBE=PYTHON_STARTED", file=sys.stderr, flush=True)

import ctypes  # noqa: E402
import json  # noqa: E402

print("G1_PROBE=CTYPES_IMPORTED", file=sys.stderr, flush=True)

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_PRIVILEGES = 3
_TOKEN_RESTRICTED_SIDS = 11
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
    is_token_restricted = _load(
        advapi,
        "IsTokenRestricted",
        [ctypes.c_void_p],
        ctypes.c_int32,
    )
    lookup_value = _load(
        advapi,
        "LookupPrivilegeValueW",
        [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(_Luid)],
        ctypes.c_int32,
    )
    token = ctypes.c_void_p()
    if (
        not open_process_token(get_current_process(), _TOKEN_QUERY, ctypes.byref(token))
        or not token.value
    ):
        raise RuntimeError("OpenProcessToken failed")
    print("G1_PROBE=TOKEN_OPENED", file=sys.stderr, flush=True)
    try:
        user_buffer = _query(get_information, token, _TOKEN_USER)
        user = _SidAndAttributes.from_buffer(user_buffer)
        user_sid = _sid_text(convert_sid, local_free, int(user.Sid or 0))
        print("G1_PROBE=TOKEN_USER_OK", file=sys.stderr, flush=True)

        restricted_buffer = _query(get_information, token, _TOKEN_RESTRICTED_SIDS)
        restricted_count = int.from_bytes(restricted_buffer.raw[:4], "little", signed=False)
        if restricted_count > _MAX_SID_COUNT:
            raise RuntimeError("TokenRestrictedSids is unbounded")
        restricted_offset = _TokenGroupsOne.Groups.offset
        entry_size = ctypes.sizeof(_SidAndAttributes)
        if restricted_offset + restricted_count * entry_size > len(restricted_buffer):
            raise RuntimeError("TokenRestrictedSids size is invalid")
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
        print("G1_PROBE=RESTRICTED_SIDS_OK", file=sys.stderr, flush=True)

        is_restricted = bool(is_token_restricted(token))
        print("G1_PROBE=IS_RESTRICTED_OK", file=sys.stderr, flush=True)

        privilege_buffer = _query(get_information, token, _TOKEN_PRIVILEGES)
        privilege_count = int.from_bytes(privilege_buffer.raw[:4], "little", signed=False)
        if privilege_count > _MAX_PRIVILEGES:
            raise RuntimeError("TokenPrivileges is unbounded")
        privilege_offset = _TokenPrivilegesOne.Privileges.offset
        privilege_size = ctypes.sizeof(_LuidAndAttributes)
        if privilege_offset + privilege_count * privilege_size > len(privilege_buffer):
            raise RuntimeError("TokenPrivileges size is invalid")
        expected_luid = _Luid()
        if not lookup_value(None, _SE_CHANGE_NOTIFY_PRIVILEGE, ctypes.byref(expected_luid)):
            raise RuntimeError("LookupPrivilegeValueW failed")
        expected_luid_tuple = (int(expected_luid.LowPart), int(expected_luid.HighPart))
        change_notify_enabled = False
        unexpected_enabled_privilege_count = 0
        for index in range(privilege_count):
            entry = _LuidAndAttributes.from_buffer(
                privilege_buffer, privilege_offset + index * privilege_size
            )
            if not entry.Attributes & _SE_PRIVILEGE_ENABLED:
                continue
            luid_tuple = (int(entry.Luid.LowPart), int(entry.Luid.HighPart))
            if luid_tuple == expected_luid_tuple:
                change_notify_enabled = True
            else:
                unexpected_enabled_privilege_count += 1
        print("G1_PROBE=PRIVILEGES_OK", file=sys.stderr, flush=True)

        result = {
            "user_sid": user_sid,
            "is_restricted": is_restricted,
            "restricted_sids": list(restricted_sids),
            "change_notify": change_notify_enabled,
            "unexpected_enabled_privilege_count": unexpected_enabled_privilege_count,
        }
        print(json.dumps(result, sort_keys=True), flush=True)
        print("G1_PROBE=JSON_WRITTEN", file=sys.stderr, flush=True)
    finally:
        close_handle(token)


if __name__ == "__main__":
    _run()
