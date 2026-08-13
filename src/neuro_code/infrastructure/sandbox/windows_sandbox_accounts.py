"""Windows local-account authority for the native sandbox setup.

W2 deliberately keeps two kinds of SID separate:

* :class:`WindowsAccountSid` is a SID resolved from a real, dedicated local
  account.  It is the subject used by filesystem and firewall policy.
* :class:`~windows_sandbox_identity.SyntheticWindowsSid` is the installation
  scoped restricted-token SID.  It is only passed to ``CreateRestrictedToken``
  and to the write-side ACL entry used by that token.

The native adapter is lazy and Windows-only.  The portable in-memory adapter
is a deterministic model used by contract tests; it never claims to provision
an operating-system account.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from neuro_code.shared.errors import SandboxError


class WindowsSandboxAccountError(SandboxError):
    """A local-account operation failed closed."""


def _windows_last_error() -> int:
    getter = cast(Callable[[], object], getattr(ctypes, "get_last_error", lambda: 0))
    return cast(int, getter())


@dataclass(frozen=True, slots=True)
class WindowsAccountSid:
    """A canonical SID resolved from a real Windows local account."""

    value: str

    def __post_init__(self) -> None:
        parts = self.value.split("-")
        if len(parts) < 4 or parts[0] != "S" or parts[1] != "1":
            raise ValueError("Windows account SID must use canonical S-1-... form")
        if any(not part.isdigit() for part in parts[2:]):
            raise ValueError("Windows account SID components must be decimal integers")
        canonical = "S-" + "-".join(str(int(part)) for part in parts[1:])
        object.__setattr__(self, "value", canonical)

    def __str__(self) -> str:
        return self.value


SANDBOX_OFFLINE_USERNAME = "NeuroSandboxOffline"
SANDBOX_ONLINE_USERNAME = "NeuroSandboxOnline"
SANDBOX_ACCOUNT_USER_GROUP = "Users"

_DISALLOWED_GROUPS = frozenset(
    {
        "administrators",
        "power users",
        "backup operators",
        "remote desktop users",
        "network configuration operators",
        "hyper-v administrators",
        "remote management users",
        "cryptographic operators",
        "event log readers",
        "performance log users",
        "distributed com users",
        "iis_iusrs",
    }
)


def _local_group_name(group: str) -> str:
    """Normalize local-group facts returned as ``BUILTIN\\Name`` or ``Name``."""

    return group.rsplit("\\", 1)[-1].casefold()


@dataclass(frozen=True, slots=True)
class WindowsLocalUserFacts:
    """Audited facts for one dedicated local account."""

    username: str
    sid: WindowsAccountSid
    groups: tuple[str, ...]
    enabled: bool
    user_privilege: int
    created_by_installation: bool

    def validate(self, *, expected_username: str) -> None:
        if self.username.casefold() != expected_username.casefold():
            raise WindowsSandboxAccountError("Windows sandbox account name mismatch")
        if not self.enabled:
            raise WindowsSandboxAccountError("Windows sandbox account is disabled")
        # USER_PRIV_USER is 1 in LM access constants.  Reject guest/admin
        # privilege levels and every known privileged local-group membership.
        if self.user_privilege != 1:
            raise WindowsSandboxAccountError("Windows sandbox account privilege is not USER")
        normalized_groups = {_local_group_name(group) for group in self.groups}
        if SANDBOX_ACCOUNT_USER_GROUP.casefold() not in normalized_groups:
            raise WindowsSandboxAccountError("Windows sandbox account is not a member of Users")
        if normalized_groups & _DISALLOWED_GROUPS:
            raise WindowsSandboxAccountError("Windows sandbox account has privileged group access")


class WindowsSandboxAccountApi(Protocol):
    """Provision, validate and remove dedicated local sandbox accounts."""

    def ensure_user(
        self,
        username: str,
        password: str,
        *,
        expected_sid: WindowsAccountSid | None = None,
    ) -> WindowsLocalUserFacts: ...

    def remove_user(self, facts: WindowsLocalUserFacts) -> None: ...

    def validate_user(
        self,
        username: str,
        password: str,
        *,
        expected_sid: WindowsAccountSid,
    ) -> WindowsLocalUserFacts: ...

    def lookup_user(
        self,
        username: str,
        *,
        expected_sid: WindowsAccountSid,
    ) -> WindowsLocalUserFacts: ...

    def user_exists(self, username: str) -> bool: ...


def generate_windows_account_password(length: int = 40) -> str:
    """Generate a non-loggable password suitable for a managed local user."""

    if length < 20:
        raise ValueError("Windows sandbox account password must be at least 20 characters")
    alphabet = string.ascii_letters + string.digits + "!#$%&()*+,-./:;<=>?@[]^_{|}~"
    required = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase)]
    required += [secrets.choice(string.digits), secrets.choice("!#$%&()*+,-./:;<=>?@[]^_{|}~")]
    required.extend(secrets.choice(alphabet) for _ in range(length - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


class InMemoryWindowsSandboxAccountApi:
    """Portable account model with real-account-shaped, stable SIDs."""

    def __init__(self) -> None:
        self.users: dict[str, tuple[str, WindowsLocalUserFacts]] = {}
        self._next_rid = 2000

    def ensure_user(
        self,
        username: str,
        password: str,
        *,
        expected_sid: WindowsAccountSid | None = None,
    ) -> WindowsLocalUserFacts:
        if not username or "\\" in username or "\x00" in username or not password:
            raise WindowsSandboxAccountError("invalid in-memory Windows account input")
        key = username.casefold()
        existing = self.users.get(key)
        if existing is None:
            sid = WindowsAccountSid(f"S-1-5-21-100-200-300-{self._next_rid}")
            self._next_rid += 1
            facts = WindowsLocalUserFacts(
                username, sid, (SANDBOX_ACCOUNT_USER_GROUP,), True, 1, True
            )
        else:
            _, facts = existing
            sid = facts.sid
            if expected_sid is not None and sid != expected_sid:
                raise WindowsSandboxAccountError("in-memory account SID mismatch")
            facts.validate(expected_username=username)
            facts = WindowsLocalUserFacts(
                facts.username,
                facts.sid,
                facts.groups,
                True,
                1,
                False,
            )
        if expected_sid is not None and sid != expected_sid:
            raise WindowsSandboxAccountError("in-memory account SID mismatch")
        self.users[key] = (password, facts)
        return facts

    def remove_user(self, facts: WindowsLocalUserFacts) -> None:
        existing = self.users.get(facts.username.casefold())
        if existing is not None and existing[1].sid == facts.sid and facts.created_by_installation:
            self.users.pop(facts.username.casefold(), None)

    def validate_user(
        self,
        username: str,
        password: str,
        *,
        expected_sid: WindowsAccountSid,
    ) -> WindowsLocalUserFacts:
        existing = self.users.get(username.casefold())
        if existing is None or existing[0] != password:
            raise WindowsSandboxAccountError("in-memory account credential is invalid")
        facts = existing[1]
        if facts.sid != expected_sid:
            raise WindowsSandboxAccountError("in-memory account SID mismatch")
        facts.validate(expected_username=username)
        return facts

    def user_exists(self, username: str) -> bool:
        return username.casefold() in self.users

    def lookup_user(
        self,
        username: str,
        *,
        expected_sid: WindowsAccountSid,
    ) -> WindowsLocalUserFacts:
        existing = self.users.get(username.casefold())
        if existing is None or existing[1].sid != expected_sid:
            raise WindowsSandboxAccountError("in-memory account is missing or has a different SID")
        existing[1].validate(expected_username=username)
        return existing[1]


class _NativeFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> object: ...


def _native_function(
    library: object, name: str, args: list[object], result: object
) -> _NativeFunction:
    function = cast(_NativeFunction, getattr(library, name))
    function.argtypes = args
    function.restype = result
    return function


class _USER_INFO_1(ctypes.Structure):
    _fields_ = [
        ("usri1_name", ctypes.c_wchar_p),
        ("usri1_password", ctypes.c_wchar_p),
        ("usri1_password_age", ctypes.c_uint32),
        ("usri1_priv", ctypes.c_uint32),
        ("usri1_home_dir", ctypes.c_wchar_p),
        ("usri1_comment", ctypes.c_wchar_p),
        ("usri1_flags", ctypes.c_uint32),
        ("usri1_script_path", ctypes.c_wchar_p),
    ]


class _USER_INFO_1_OUT(ctypes.Structure):
    _fields_ = [
        ("usri1_name", ctypes.c_wchar_p),
        ("usri1_password", ctypes.c_wchar_p),
        ("usri1_password_age", ctypes.c_uint32),
        ("usri1_priv", ctypes.c_uint32),
        ("usri1_home_dir", ctypes.c_wchar_p),
        ("usri1_comment", ctypes.c_wchar_p),
        ("usri1_flags", ctypes.c_uint32),
        ("usri1_script_path", ctypes.c_wchar_p),
    ]


class _USER_INFO_1003(ctypes.Structure):
    _fields_ = [("usri1003_password", ctypes.c_wchar_p)]


class _LOCALGROUP_USERS_INFO_0(ctypes.Structure):
    _fields_ = [("lgrui0_name", ctypes.c_wchar_p)]


class _LOCALGROUP_MEMBERS_INFO_3(ctypes.Structure):
    _fields_ = [("lgrmi3_domainandname", ctypes.c_wchar_p)]


class _NativeWindowsSandboxAccountApi:  # pragma: no cover - Windows native CI
    """NetAPI/LSA adapter for real, least-privileged local users."""

    _NERR_SUCCESS = 0
    _NERR_USER_EXISTS = 2224
    _USER_PRIV_USER = 1
    _UF_SCRIPT = 0x0001
    _UF_DONT_EXPIRE_PASSWD = 0x10000
    _UF_ACCOUNTDISABLE = 0x0002
    _UF_LOCKOUT = 0x0010
    _ERROR_MORE_DATA = 234
    _ERROR_INSUFFICIENT_BUFFER = 122
    _MAX_PREFERRED_LENGTH = 0xFFFFFFFF
    _LOGON32_LOGON_INTERACTIVE = 2
    _LOGON32_PROVIDER_DEFAULT = 0

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsSandboxAccountError("local account authority is available only on Windows")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise WindowsSandboxAccountError("this Python runtime has no Win32 ctypes API")
        self._netapi = cast(object, loader("netapi32.dll"))
        self._advapi = cast(object, loader("advapi32.dll", use_last_error=True))
        self._kernel = cast(object, loader("kernel32.dll", use_last_error=True))
        self._net_user_add = _native_function(
            self._netapi,
            "NetUserAdd",
            [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_uint32,
        )
        self._net_user_set_info = _native_function(
            self._netapi,
            "NetUserSetInfo",
            [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_uint32,
        )
        self._net_user_get_info = _native_function(
            self._netapi,
            "NetUserGetInfo",
            [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_uint32,
        )
        self._net_user_get_local_groups = _native_function(
            self._netapi,
            "NetUserGetLocalGroups",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_uint32,
        )
        self._net_user_del = _native_function(
            self._netapi,
            "NetUserDel",
            [ctypes.c_wchar_p, ctypes.c_wchar_p],
            ctypes.c_uint32,
        )
        self._net_local_group_add_members = _native_function(
            self._netapi,
            "NetLocalGroupAddMembers",
            [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_uint32,
        )
        self._net_api_buffer_free = _native_function(
            self._netapi, "NetApiBufferFree", [ctypes.c_void_p], ctypes.c_uint32
        )
        self._lookup_account_name = _native_function(
            self._advapi,
            "LookupAccountNameW",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
            ],
            ctypes.c_int32,
        )
        self._logon_user = _native_function(
            self._advapi,
            "LogonUserW",
            [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_int32,
        )
        self._close_handle = _native_function(
            self._kernel, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32
        )

    def _error(self, operation: str, code: int) -> WindowsSandboxAccountError:
        return WindowsSandboxAccountError(f"{operation} failed with Windows error {code}")

    def _sid_for_user(self, username: str) -> WindowsAccountSid:
        sid_size = ctypes.c_uint32(68)
        domain_size = ctypes.c_uint32(256)
        sid_use = ctypes.c_uint32(0)
        sid_buffer = ctypes.create_string_buffer(sid_size.value)
        domain_buffer = ctypes.create_unicode_buffer(domain_size.value)
        while not self._lookup_account_name(
            None,
            username,
            ctypes.cast(sid_buffer, ctypes.c_void_p),
            ctypes.byref(sid_size),
            ctypes.cast(domain_buffer, ctypes.c_wchar_p),
            ctypes.byref(domain_size),
            ctypes.byref(sid_use),
        ):
            error = _windows_last_error()
            if error != self._ERROR_INSUFFICIENT_BUFFER:
                raise self._error("LookupAccountNameW", error)
            sid_buffer = ctypes.create_string_buffer(max(1, sid_size.value))
            domain_buffer = ctypes.create_unicode_buffer(max(1, domain_size.value))
        convert = _native_function(
            self._advapi,
            "ConvertSidToStringSidW",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int32,
        )
        local_free = _native_function(self._kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
        output = ctypes.c_void_p()
        if (
            not convert(ctypes.cast(sid_buffer, ctypes.c_void_p), ctypes.byref(output))
            or not output.value
        ):
            raise self._error("ConvertSidToStringSidW", _windows_last_error())
        try:
            return WindowsAccountSid(ctypes.wstring_at(output.value))
        finally:
            local_free(output)

    def _facts(self, username: str, *, created: bool) -> WindowsLocalUserFacts:
        info_pointer = ctypes.c_void_p()
        result = cast(int, self._net_user_get_info(None, username, 1, ctypes.byref(info_pointer)))
        if result != self._NERR_SUCCESS or not info_pointer.value:
            raise self._error("NetUserGetInfo", result)
        try:
            info = ctypes.cast(info_pointer, ctypes.POINTER(_USER_INFO_1_OUT)).contents
            groups_pointer = ctypes.c_void_p()
            count = ctypes.c_uint32(0)
            total = ctypes.c_uint32(0)
            groups_result = cast(
                int,
                self._net_user_get_local_groups(
                    None,
                    username,
                    0,
                    # LG_INCLUDE_INDIRECT: inspect nested privileged groups too.
                    1,
                    ctypes.byref(groups_pointer),
                    self._MAX_PREFERRED_LENGTH,
                    ctypes.byref(count),
                    ctypes.byref(total),
                ),
            )
            groups: list[str] = []
            try:
                if (
                    groups_result in (self._NERR_SUCCESS, self._ERROR_MORE_DATA)
                    and groups_pointer.value
                ):
                    group_array = ctypes.cast(
                        groups_pointer, ctypes.POINTER(_LOCALGROUP_USERS_INFO_0 * count.value)
                    ).contents
                    groups = [str(group.lgrui0_name) for group in group_array]
                elif groups_result != self._NERR_SUCCESS:
                    raise self._error("NetUserGetLocalGroups", groups_result)
                return WindowsLocalUserFacts(
                    username,
                    self._sid_for_user(username),
                    tuple(groups),
                    not bool(info.usri1_flags & (self._UF_ACCOUNTDISABLE | self._UF_LOCKOUT)),
                    int(info.usri1_priv),
                    created,
                )
            finally:
                if groups_pointer.value:
                    self._net_api_buffer_free(groups_pointer)
        finally:
            self._net_api_buffer_free(info_pointer)

    def ensure_user(
        self,
        username: str,
        password: str,
        *,
        expected_sid: WindowsAccountSid | None = None,
    ) -> WindowsLocalUserFacts:
        if username not in (SANDBOX_OFFLINE_USERNAME, SANDBOX_ONLINE_USERNAME):
            raise WindowsSandboxAccountError("unexpected Windows sandbox account name")
        if not password or "\x00" in password:
            raise WindowsSandboxAccountError("invalid Windows sandbox account password")
        info = _USER_INFO_1(
            username,
            password,
            0,
            self._USER_PRIV_USER,
            None,
            "Neuro Code managed sandbox account",
            self._UF_SCRIPT | self._UF_DONT_EXPIRE_PASSWD,
            None,
        )
        parameter_error = ctypes.c_uint32(0)
        result = cast(
            int, self._net_user_add(None, 1, ctypes.byref(info), ctypes.byref(parameter_error))
        )
        created = result == self._NERR_SUCCESS
        if result == self._NERR_USER_EXISTS:
            # Validate a colliding account before changing its password.  A
            # username collision must never let setup mutate an unrelated or
            # privileged local user.
            existing = self._facts(username, created=False)
            existing.validate(expected_username=username)
            if expected_sid is not None and existing.sid != expected_sid:
                raise WindowsSandboxAccountError("Windows sandbox account SID mismatch")
            password_info = _USER_INFO_1003(password)
            result = cast(
                int,
                self._net_user_set_info(None, username, 1003, ctypes.byref(password_info), None),
            )
        if result != self._NERR_SUCCESS:
            raise self._error("NetUserAdd/NetUserSetInfo", result)
        membership = _LOCALGROUP_MEMBERS_INFO_3(username)
        membership_result = cast(
            int,
            self._net_local_group_add_members(
                None,
                SANDBOX_ACCOUNT_USER_GROUP,
                3,
                ctypes.byref(membership),
                1,
            ),
        )
        if membership_result not in (self._NERR_SUCCESS, 1378):  # already a member
            raise self._error("NetLocalGroupAddMembers", membership_result)
        facts = self._facts(username, created=created)
        facts.validate(expected_username=username)
        if expected_sid is not None and facts.sid != expected_sid:
            raise WindowsSandboxAccountError("Windows sandbox account SID mismatch")
        token = ctypes.c_void_p()
        if not self._logon_user(
            username,
            None,
            password,
            self._LOGON32_LOGON_INTERACTIVE,
            self._LOGON32_PROVIDER_DEFAULT,
            ctypes.byref(token),
        ):
            raise self._error("LogonUserW", _windows_last_error())
        if token.value:
            self._close_handle(token)
        return facts

    def remove_user(self, facts: WindowsLocalUserFacts) -> None:
        if not facts.created_by_installation:
            return
        result = cast(int, self._net_user_del(None, facts.username))
        if result not in (self._NERR_SUCCESS, 2221):  # NERR_UserNotFound
            raise self._error("NetUserDel", result)

    def validate_user(
        self,
        username: str,
        password: str,
        *,
        expected_sid: WindowsAccountSid,
    ) -> WindowsLocalUserFacts:
        if not self.user_exists(username):
            raise WindowsSandboxAccountError("Windows sandbox account is missing")
        facts = self._facts(username, created=False)
        facts.validate(expected_username=username)
        if facts.sid != expected_sid:
            raise WindowsSandboxAccountError("Windows sandbox account SID mismatch")
        token = ctypes.c_void_p()
        if not self._logon_user(
            username,
            None,
            password,
            self._LOGON32_LOGON_INTERACTIVE,
            self._LOGON32_PROVIDER_DEFAULT,
            ctypes.byref(token),
        ):
            raise self._error("LogonUserW", _windows_last_error())
        if token.value:
            self._close_handle(token)
        return facts

    def user_exists(self, username: str) -> bool:
        pointer = ctypes.c_void_p()
        result = cast(int, self._net_user_get_info(None, username, 1, ctypes.byref(pointer)))
        if pointer.value:
            self._net_api_buffer_free(pointer)
        return result == self._NERR_SUCCESS

    def lookup_user(
        self,
        username: str,
        *,
        expected_sid: WindowsAccountSid,
    ) -> WindowsLocalUserFacts:
        if not self.user_exists(username):
            raise WindowsSandboxAccountError("Windows sandbox account is missing")
        facts = self._facts(username, created=False)
        facts.validate(expected_username=username)
        if facts.sid != expected_sid:
            raise WindowsSandboxAccountError("Windows sandbox account SID mismatch")
        return facts


__all__ = [
    "SANDBOX_OFFLINE_USERNAME",
    "SANDBOX_ONLINE_USERNAME",
    "InMemoryWindowsSandboxAccountApi",
    "WindowsAccountSid",
    "WindowsLocalUserFacts",
    "WindowsSandboxAccountApi",
    "WindowsSandboxAccountError",
    "_NativeWindowsSandboxAccountApi",
    "generate_windows_account_password",
]
