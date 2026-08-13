"""W2 Windows native sandbox setup authority.

W2 provisions two real, least-privileged local users and keeps their account
SIDs separate from the installation-scoped synthetic SID used by
``CreateRestrictedToken(WRITE_RESTRICTED)``.  It owns setup-time ACL,
credential-file and firewall state only; runtime child enforcement remains a
W3 concern and the actual capability declaration remains fail closed.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from neuro_code.application.ports.windows_sandbox import (
    WINDOWS_SANDBOX_SETUP_SCHEMA_VERSION,
    WindowsSandboxIdentityKind,
    WindowsSandboxPrivilegeBoundary,
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupSnapshot,
    WindowsSandboxSetupState,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    SANDBOX_OFFLINE_USERNAME,
    SANDBOX_ONLINE_USERNAME,
    WindowsAccountSid,
    WindowsLocalUserFacts,
    WindowsSandboxAccountApi,
    WindowsSandboxAccountError,
    _NativeWindowsSandboxAccountApi,
    generate_windows_account_password,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    WindowsFilesystemAclAuthority,
    WindowsFilesystemSetupPlan,
    WindowsManagedAce,
    WindowsManagedAceKind,
    _NativeWindowsAclApi,
    plan_windows_filesystem_authority,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import (
    WindowsFirewallApi,
    WindowsFirewallRule,
    _NativeWindowsFirewallApi,
    firewall_rule_for_installation,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.shared.errors import SandboxError


class WindowsSandboxSetupError(SandboxError):
    """A W2 setup authority operation failed closed."""


class WindowsSandboxSetupPrivilegeError(WindowsSandboxSetupError):
    """Setup/repair/cleanup was attempted without administrator authority."""


class WindowsSetupPrivilegeApi(Protocol):
    def is_administrator(self) -> bool: ...


class WindowsCredentialStore(Protocol):
    @property
    def path(self) -> Path: ...

    def save(self, plaintext: bytes) -> None: ...

    def load(self) -> bytes | None: ...

    def clear(self) -> None: ...


class _NativeWindowsSetupPrivilegeApi:  # pragma: no cover - Windows native CI
    def is_administrator(self) -> bool:
        if os.name != "nt":
            return False
        import ctypes

        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise WindowsSandboxSetupError("Windows administrator check is unavailable")
        shell32 = loader("shell32.dll", use_last_error=True)
        function = getattr(shell32, "IsUserAnAdmin", None)
        if function is None:
            raise WindowsSandboxSetupError("Windows administrator check is unavailable")
        function.argtypes = []
        function.restype = ctypes.c_int32
        return bool(function())


@dataclass(frozen=True, slots=True)
class WindowsSandboxIdentityRecord:
    """Non-secret identity projection returned after successful setup."""

    kind: WindowsSandboxIdentityKind
    username: str
    user_sid: WindowsAccountSid
    write_sid: SyntheticWindowsSid
    credential_ref: str
    created_by_installation: bool


@dataclass(frozen=True, slots=True)
class _StoredIdentity:
    kind: WindowsSandboxIdentityKind
    username: str
    user_sid: WindowsAccountSid
    password: bytes = field(repr=False)
    created_by_installation: bool


@dataclass(frozen=True, slots=True)
class _InstallationRecord:
    schema_version: int
    installation_id: str
    write_sid: SyntheticWindowsSid
    identities: tuple[_StoredIdentity, ...]
    managed_aces: tuple[WindowsManagedAce, ...]
    offline_firewall_rule: WindowsFirewallRule
    active_identity: WindowsSandboxIdentityKind

    @property
    def offline(self) -> _StoredIdentity:
        return next(
            identity
            for identity in self.identities
            if identity.kind is WindowsSandboxIdentityKind.OFFLINE
        )

    @property
    def online(self) -> _StoredIdentity:
        return next(
            identity
            for identity in self.identities
            if identity.kind is WindowsSandboxIdentityKind.ONLINE
        )

    @classmethod
    def from_facts(
        cls,
        *,
        write_sid: SyntheticWindowsSid,
        facts: tuple[tuple[WindowsSandboxIdentityKind, WindowsLocalUserFacts, str], ...],
    ) -> _InstallationRecord:
        if {item[0] for item in facts} != set(WindowsSandboxIdentityKind):
            raise WindowsSandboxSetupError("setup must provision exactly Offline and Online users")
        installation_id = secrets.token_hex(16)
        identities = tuple(
            _StoredIdentity(
                kind,
                user.username,
                user.sid,
                password.encode("utf-8"),
                user.created_by_installation,
            )
            for kind, user, password in facts
        )
        offline = next(
            user for kind, user, _ in facts if kind is WindowsSandboxIdentityKind.OFFLINE
        )
        return cls(
            WINDOWS_SANDBOX_SETUP_SCHEMA_VERSION,
            installation_id,
            write_sid,
            identities,
            (),
            firewall_rule_for_installation(
                installation_id, WindowsSandboxIdentityKind.OFFLINE, offline.sid
            ),
            WindowsSandboxIdentityKind.ONLINE,
        )

    def identity_records(self) -> tuple[WindowsSandboxIdentityRecord, ...]:
        return tuple(
            WindowsSandboxIdentityRecord(
                identity.kind,
                identity.username,
                identity.user_sid,
                self.write_sid,
                identity.kind.value,
                identity.created_by_installation,
            )
            for identity in self.identities
        )

    def encode(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "installation_id": self.installation_id,
            "write_sid": self.write_sid.value,
            "identities": [
                {
                    "kind": identity.kind.value,
                    "username": identity.username,
                    "user_sid": identity.user_sid.value,
                    "password": base64.b64encode(identity.password).decode("ascii"),
                    "created_by_installation": identity.created_by_installation,
                }
                for identity in self.identities
            ],
            "managed_aces": [
                {
                    "path": str(entry.path),
                    "sid": entry.sid.value,
                    "sid_type": "synthetic"
                    if isinstance(entry.sid, SyntheticWindowsSid)
                    else "account",
                    "kind": entry.kind.value,
                    "access_mask": entry.access_mask,
                    "inheritance": entry.inheritance,
                }
                for entry in self.managed_aces
            ],
            "offline_firewall_rule": {
                "name": self.offline_firewall_rule.name,
                "identity": self.offline_firewall_rule.identity.value,
                "sid": self.offline_firewall_rule.sid.value,
                "outbound_block": self.offline_firewall_rule.outbound_block,
            },
            "active_identity": self.active_identity.value,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode(cls, encoded: bytes) -> _InstallationRecord:
        try:
            payload = json.loads(encoded.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("record must be an object")
            identities_payload = payload["identities"]
            if not isinstance(identities_payload, list):
                raise ValueError("identities must be a list")
            if len(identities_payload) != len(WindowsSandboxIdentityKind):
                raise ValueError("record must contain exactly two identities")
            identities = tuple(
                _StoredIdentity(
                    WindowsSandboxIdentityKind(item["kind"]),
                    str(item["username"]),
                    WindowsAccountSid(item["user_sid"]),
                    base64.b64decode(item["password"].encode("ascii"), validate=True),
                    bool(item["created_by_installation"]),
                )
                for item in identities_payload
            )
            if {identity.kind for identity in identities} != set(WindowsSandboxIdentityKind):
                raise ValueError("record must contain exactly Offline and Online identities")
            if {identity.username for identity in identities} != {
                SANDBOX_OFFLINE_USERNAME,
                SANDBOX_ONLINE_USERNAME,
            }:
                raise ValueError("record contains unexpected sandbox account names")
            if any(not identity.password for identity in identities):
                raise ValueError("record contains an empty account credential")
            if identities[0].user_sid == identities[1].user_sid:
                raise ValueError("Offline and Online accounts must have distinct SIDs")
            managed_entries: list[WindowsManagedAce] = []
            for item in payload["managed_aces"]:
                sid_type = item["sid_type"]
                if sid_type == "synthetic":
                    sid: WindowsAccountSid | SyntheticWindowsSid = SyntheticWindowsSid(item["sid"])
                elif sid_type == "account":
                    sid = WindowsAccountSid(item["sid"])
                else:
                    raise ValueError("unknown managed ACE SID type")
                managed_entries.append(
                    WindowsManagedAce(
                        Path(item["path"]),
                        sid,
                        WindowsManagedAceKind(item["kind"]),
                        int(item["access_mask"]),
                        int(item["inheritance"]),
                    )
                )
            managed_aces = tuple(managed_entries)
            firewall_payload = payload["offline_firewall_rule"]
            write_sid = SyntheticWindowsSid(payload["write_sid"])
            firewall_rule = WindowsFirewallRule(
                firewall_payload["name"],
                WindowsSandboxIdentityKind(firewall_payload["identity"]),
                WindowsAccountSid(firewall_payload["sid"]),
                bool(firewall_payload["outbound_block"]),
            )
            record = cls(
                int(payload["schema_version"]),
                str(payload["installation_id"]),
                write_sid,
                identities,
                managed_aces,
                firewall_rule,
                WindowsSandboxIdentityKind(payload["active_identity"]),
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise WindowsSandboxSetupError("Windows sandbox setup record is invalid") from error
        if record.schema_version != WINDOWS_SANDBOX_SETUP_SCHEMA_VERSION:
            raise WindowsSandboxSetupError("Windows sandbox setup record version is unsupported")
        if record.offline_firewall_rule.sid != record.offline.user_sid:
            raise WindowsSandboxSetupError("Offline firewall must target the Offline user SID")
        if (
            record.offline_firewall_rule.identity is not WindowsSandboxIdentityKind.OFFLINE
            or not record.offline_firewall_rule.outbound_block
        ):
            raise WindowsSandboxSetupError("Windows sandbox offline firewall rule is invalid")
        allowed_sids = {record.offline.user_sid, record.online.user_sid, record.write_sid}
        if any(entry.sid not in allowed_sids for entry in record.managed_aces):
            raise WindowsSandboxSetupError("managed ACE contains an unknown principal")
        if not record.installation_id or "\x00" in record.installation_id:
            raise WindowsSandboxSetupError("Windows sandbox installation ID is invalid")
        return record


class WindowsNativeSandboxSetupAuthority:
    """W2 setup authority with injectable account, ACL, DPAPI and firewall APIs."""

    def __init__(
        self,
        *,
        credential_store: WindowsCredentialStore | None = None,
        acl_api: object | None = None,
        firewall_api: WindowsFirewallApi | None = None,
        account_api: WindowsSandboxAccountApi | None = None,
        privilege_api: WindowsSetupPrivilegeApi | None = None,
    ) -> None:
        self._credential_store = credential_store
        self._acl_api = acl_api
        self._firewall_api = firewall_api
        self._account_api = account_api
        self._privilege_api = privilege_api
        self._acl_authority: WindowsFilesystemAclAuthority | None = None

    @property
    def privilege_boundary(self) -> WindowsSandboxPrivilegeBoundary:
        return WindowsSandboxPrivilegeBoundary()

    def _store(self, request: WindowsSandboxSetupRequest) -> WindowsCredentialStore:
        if self._credential_store is None:
            self._credential_store = WindowsDpapiCredentialStore(
                request.installation_root / "credentials.dpapi"
            )
        return self._credential_store

    def _apis(
        self, request: WindowsSandboxSetupRequest
    ) -> tuple[WindowsFilesystemAclAuthority, WindowsFirewallApi, WindowsSandboxAccountApi]:
        del request
        if self._acl_authority is None:
            api = self._acl_api if self._acl_api is not None else _NativeWindowsAclApi()
            self._acl_authority = WindowsFilesystemAclAuthority(api)  # type: ignore[arg-type]
        firewall = (
            self._firewall_api if self._firewall_api is not None else _NativeWindowsFirewallApi()
        )
        accounts = (
            self._account_api
            if self._account_api is not None
            else _NativeWindowsSandboxAccountApi()
        )
        return self._acl_authority, firewall, accounts

    def _load(self, request: WindowsSandboxSetupRequest) -> _InstallationRecord | None:
        try:
            encoded = self._store(request).load()
        except SandboxError:
            raise WindowsSandboxSetupError("Windows sandbox setup record needs repair") from None
        if encoded is None:
            return None
        return _InstallationRecord.decode(encoded)

    def _snapshot(
        self,
        state: WindowsSandboxSetupState,
        record: _InstallationRecord | None,
        *,
        offline_firewall_enabled: bool = False,
    ) -> WindowsSandboxSetupSnapshot:
        if record is None:
            return WindowsSandboxSetupSnapshot(
                state=state, privilege_boundary=self.privilege_boundary
            )
        return WindowsSandboxSetupSnapshot(
            state=state,
            schema_version=record.schema_version,
            offline_user_sid=record.offline.user_sid.value,
            online_user_sid=record.online.user_sid.value,
            write_restricting_sid=record.write_sid.value,
            write_sid=record.write_sid.value,
            identities=tuple(identity.kind for identity in record.identities),
            managed_ace_count=len(record.managed_aces),
            offline_firewall_enabled=offline_firewall_enabled,
            privilege_boundary=self.privilege_boundary,
        )

    def _is_admin(self) -> bool:
        api = self._privilege_api
        if api is None:
            api = _NativeWindowsSetupPrivilegeApi()
            self._privilege_api = api
        try:
            return api.is_administrator()
        except WindowsSandboxSetupError:
            raise
        except BaseException as error:
            raise WindowsSandboxSetupError("Windows administrator check failed") from error

    def _require_admin(self) -> None:
        if not self._is_admin():
            raise WindowsSandboxSetupPrivilegeError(
                "Windows sandbox setup requires administrator authority"
            )

    @staticmethod
    def _plan(
        request: WindowsSandboxSetupRequest,
        record: _InstallationRecord,
        credential_path: Path,
    ) -> WindowsFilesystemSetupPlan:
        user_sids = tuple(identity.user_sid for identity in record.identities)
        return plan_windows_filesystem_authority(
            request,
            record.write_sid,
            read_user_sids=user_sids,
            write_user_sids=user_sids,
            credential_path=credential_path,
        )

    @staticmethod
    def _account_password(identity: _StoredIdentity) -> str:
        try:
            return identity.password.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WindowsSandboxSetupError(
                "stored Windows account credential is invalid"
            ) from error

    def _validate_accounts(
        self,
        account_api: WindowsSandboxAccountApi,
        record: _InstallationRecord,
    ) -> tuple[WindowsLocalUserFacts, ...]:
        facts: list[WindowsLocalUserFacts] = []
        for identity in record.identities:
            facts.append(
                account_api.validate_user(
                    identity.username,
                    self._account_password(identity),
                    expected_sid=identity.user_sid,
                )
            )
        return tuple(facts)

    def inspect(self, request: WindowsSandboxSetupRequest) -> WindowsSandboxSetupSnapshot:
        if not isinstance(request, WindowsSandboxSetupRequest):
            raise TypeError("Windows sandbox setup request must be canonical")
        try:
            acl_authority, firewall, accounts = self._apis(request)
        except (SandboxError, OSError):
            return self._snapshot(WindowsSandboxSetupState.UNSUPPORTED, None)
        try:
            record = self._load(request)
        except WindowsSandboxSetupError:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_REPAIR, None)
        if record is None:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_SETUP, None)
        try:
            self._validate_accounts(accounts, record)
            plan = self._plan(request, record, self._store(request).path)
            acl_ready = acl_authority.is_ready(plan)
            firewall_ready = (
                firewall.rule_exists(record.offline_firewall_rule)
                if record.active_identity is WindowsSandboxIdentityKind.OFFLINE
                else not firewall.rule_exists(record.offline_firewall_rule)
            )
        except (WindowsSandboxSetupError, WindowsSandboxAccountError, OSError):
            return self._snapshot(WindowsSandboxSetupState.NEEDS_REPAIR, record)
        if not acl_ready or not firewall_ready:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_REPAIR, record)
        return self._snapshot(
            WindowsSandboxSetupState.READY,
            record,
            offline_firewall_enabled=record.active_identity is WindowsSandboxIdentityKind.OFFLINE,
        )

    def _new_record(
        self,
        account_api: WindowsSandboxAccountApi,
    ) -> tuple[_InstallationRecord, tuple[WindowsLocalUserFacts, ...]]:
        created: list[tuple[WindowsSandboxIdentityKind, WindowsLocalUserFacts, str]] = []
        try:
            for kind, username in (
                (WindowsSandboxIdentityKind.OFFLINE, SANDBOX_OFFLINE_USERNAME),
                (WindowsSandboxIdentityKind.ONLINE, SANDBOX_ONLINE_USERNAME),
            ):
                if account_api.user_exists(username):
                    raise WindowsSandboxSetupError(
                        "a pre-existing account name requires an existing managed setup record"
                    )
                password = generate_windows_account_password()
                facts = account_api.ensure_user(username, password)
                facts.validate(expected_username=username)
                if not facts.created_by_installation:
                    raise WindowsSandboxSetupError(
                        "new sandbox account was not installation-created"
                    )
                created.append((kind, facts, password))
        except BaseException as error:
            # If provisioning the second account fails, do not leave the first
            # newly-created account behind.  Adopted pre-existing accounts are
            # explicitly preserved by their created_by_installation fact.
            for _, facts, _ in reversed(created):
                with contextlib.suppress(BaseException):
                    account_api.remove_user(facts)
            if isinstance(error, WindowsSandboxSetupError):
                raise
            raise WindowsSandboxSetupError(
                f"Windows sandbox account provisioning failed: {type(error).__name__}: {error}"
            ) from error
        record = _InstallationRecord.from_facts(
            write_sid=SyntheticWindowsSid.generate(),
            facts=tuple(created),
        )
        return record, tuple(item[1] for item in created)

    def _repair_missing_accounts(
        self,
        account_api: WindowsSandboxAccountApi,
        record: _InstallationRecord,
    ) -> tuple[_InstallationRecord, tuple[WindowsLocalUserFacts, ...]]:
        identities: list[_StoredIdentity] = []
        facts: list[WindowsLocalUserFacts] = []
        for identity in record.identities:
            password = self._account_password(identity)
            if not account_api.user_exists(identity.username):
                new_facts = account_api.ensure_user(identity.username, password)
                if not new_facts.created_by_installation:
                    raise WindowsSandboxSetupError(
                        "recreated Windows sandbox account was not installation-created"
                    )
                new_identity = _StoredIdentity(
                    identity.kind,
                    new_facts.username,
                    new_facts.sid,
                    identity.password,
                    True,
                )
            else:
                try:
                    new_facts = account_api.validate_user(
                        identity.username,
                        password,
                        expected_sid=identity.user_sid,
                    )
                except WindowsSandboxAccountError:
                    # Existing account with a known SID but a rotated/changed
                    # password is repaired in place with a fresh credential; a
                    # different SID fails closed in ensure_user.
                    password = generate_windows_account_password()
                    new_facts = account_api.ensure_user(
                        identity.username,
                        password,
                        expected_sid=identity.user_sid,
                    )
                new_identity = _StoredIdentity(
                    identity.kind,
                    identity.username,
                    identity.user_sid,
                    password.encode("utf-8"),
                    # The adapter reports current OS facts, not installation
                    # provenance.  Preserve the persisted ownership decision
                    # so a repair/repeat setup cannot make cleanup retain a
                    # user that this installation created.
                    identity.created_by_installation,
                )
            identities.append(new_identity)
            facts.append(new_facts)
        offline = next(
            item for item in identities if item.kind is WindowsSandboxIdentityKind.OFFLINE
        )
        updated_rule = firewall_rule_for_installation(
            record.installation_id,
            WindowsSandboxIdentityKind.OFFLINE,
            offline.user_sid,
        )
        return (
            _InstallationRecord(
                record.schema_version,
                record.installation_id,
                record.write_sid,
                tuple(identities),
                record.managed_aces,
                updated_rule,
                record.active_identity,
            ),
            tuple(facts),
        )

    def setup(
        self,
        request: WindowsSandboxSetupRequest,
        *,
        identity: WindowsSandboxIdentityKind,
    ) -> WindowsSandboxSetupSnapshot:
        if not isinstance(identity, WindowsSandboxIdentityKind):
            raise TypeError("Windows sandbox identity must be canonical")
        self._require_admin()
        acl_authority, firewall, account_api = self._apis(request)
        record = self._load(request)
        fresh = record is None
        created_facts: tuple[WindowsLocalUserFacts, ...] = ()
        if record is None:
            record, created_facts = self._new_record(account_api)
        else:
            record, _ = self._repair_missing_accounts(account_api, record)
        store = self._store(request)
        plan = self._plan(request, record, store.path)
        try:
            updated = _InstallationRecord(
                record.schema_version,
                record.installation_id,
                record.write_sid,
                record.identities,
                plan.entries,
                record.offline_firewall_rule,
                identity,
            )
            # The file must exist before native GetNamedSecurityInfoW can apply
            # its exact deny ACE.  It is DPAPI encrypted before ACL mutation;
            # the controller's account remains the owner and can decrypt it.
            store.save(updated.encode())
            acl_authority.reconcile(record.managed_aces, plan)
            if identity is WindowsSandboxIdentityKind.OFFLINE:
                firewall.ensure_outbound_block(record.offline_firewall_rule)
            else:
                firewall.remove_rule(record.offline_firewall_rule)
        except BaseException:
            if fresh:
                try:
                    acl_authority.cleanup(plan.entries)
                    firewall.remove_rule(record.offline_firewall_rule)
                    store.clear()
                    for facts in created_facts:
                        account_api.remove_user(facts)
                except BaseException:
                    pass
            raise WindowsSandboxSetupError(
                "Windows sandbox setup failed and was rolled back"
            ) from None
        return self._snapshot(
            WindowsSandboxSetupState.READY,
            updated,
            offline_firewall_enabled=identity is WindowsSandboxIdentityKind.OFFLINE,
        )

    def repair(
        self,
        request: WindowsSandboxSetupRequest,
        *,
        identity: WindowsSandboxIdentityKind,
    ) -> WindowsSandboxSetupSnapshot:
        record = self._load(request)
        if record is None:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_SETUP, None)
        return self.setup(request, identity=identity)

    def cleanup(self, request: WindowsSandboxSetupRequest) -> WindowsSandboxSetupSnapshot:
        self._require_admin()
        record = self._load(request)
        if record is None:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_SETUP, None)
        acl_authority, firewall, account_api = self._apis(request)
        try:
            acl_authority.cleanup(record.managed_aces)
            firewall.remove_rule(record.offline_firewall_rule)
            for identity in record.identities:
                if account_api.user_exists(identity.username):
                    facts = account_api.lookup_user(
                        identity.username,
                        expected_sid=identity.user_sid,
                    )
                    facts = WindowsLocalUserFacts(
                        facts.username,
                        facts.sid,
                        facts.groups,
                        facts.enabled,
                        facts.user_privilege,
                        identity.created_by_installation,
                    )
                    account_api.remove_user(facts)
            self._store(request).clear()
        except BaseException:
            raise WindowsSandboxSetupError("Windows sandbox cleanup needs repair") from None
        return self._snapshot(WindowsSandboxSetupState.NEEDS_SETUP, None)

    def identity_records(
        self,
        request: WindowsSandboxSetupRequest,
    ) -> tuple[WindowsSandboxIdentityRecord, ...]:
        record = self._load(request)
        if record is None:
            raise WindowsSandboxSetupError("Windows sandbox identities need setup")
        return record.identity_records()


__all__ = [
    "WindowsCredentialStore",
    "WindowsNativeSandboxSetupAuthority",
    "WindowsSandboxIdentityRecord",
    "WindowsSandboxSetupError",
    "WindowsSandboxSetupPrivilegeError",
    "WindowsSetupPrivilegeApi",
]
