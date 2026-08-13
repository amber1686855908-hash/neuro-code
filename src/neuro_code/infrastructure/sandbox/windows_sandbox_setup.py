"""W2 Windows native sandbox setup authority.

This module owns installation-time authorities only.  It creates two logical
identities (Offline and Online), persists one installation-scoped synthetic
write SID and opaque per-identity credentials through DPAPI, reconciles exact
managed filesystem ACEs, and controls one SID-scoped outbound firewall rule.
It never launches a child and it never changes the W1 actual capability
declaration; runtime child enforcement remains a later W3 boundary.

W2 Windows native sandbox setup authority.

本模块只拥有 installation-time authority:创建 Offline/Online 两个逻辑 identity,
通过 DPAPI 持久化一个 installation-scoped synthetic write SID 和每个 identity 的
opaque credentials,reconcile exact managed filesystem ACE,并控制一个按 SID 限定的
outbound firewall rule.它不启动 child,也不改变 W1 actual capability declaration;
runtime child enforcement 仍属于后续 W3 boundary.
"""

from __future__ import annotations

import base64
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


class _NativeWindowsSetupPrivilegeApi:  # pragma: no cover - exercised by Windows native CI
    def is_administrator(self) -> bool:
        if os.name != "nt":
            return False
        import ctypes

        shell32 = getattr(ctypes, "WinDLL", lambda *_args, **_kwargs: None)(
            "shell32.dll",
            use_last_error=True,
        )
        if shell32 is None:
            raise WindowsSandboxSetupError("Windows administrator check is unavailable")
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
    write_sid: SyntheticWindowsSid
    credential_ref: str


@dataclass(frozen=True, slots=True)
class _StoredIdentity:
    kind: WindowsSandboxIdentityKind
    credential: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _InstallationRecord:
    schema_version: int
    installation_id: str
    write_sid: SyntheticWindowsSid
    identities: tuple[_StoredIdentity, ...]
    managed_aces: tuple[WindowsManagedAce, ...]
    offline_firewall_rule: WindowsFirewallRule
    active_identity: WindowsSandboxIdentityKind

    @classmethod
    def fresh(cls, write_sid: SyntheticWindowsSid) -> _InstallationRecord:
        installation_id = secrets.token_hex(16)
        return cls(
            schema_version=WINDOWS_SANDBOX_SETUP_SCHEMA_VERSION,
            installation_id=installation_id,
            write_sid=write_sid,
            identities=tuple(
                _StoredIdentity(kind, secrets.token_bytes(32))
                for kind in WindowsSandboxIdentityKind
            ),
            managed_aces=(),
            offline_firewall_rule=firewall_rule_for_installation(
                installation_id,
                WindowsSandboxIdentityKind.OFFLINE,
                write_sid,
            ),
            active_identity=WindowsSandboxIdentityKind.ONLINE,
        )

    def identity_records(self) -> tuple[WindowsSandboxIdentityRecord, ...]:
        return tuple(
            WindowsSandboxIdentityRecord(identity.kind, self.write_sid, identity.kind.value)
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
                    "credential": base64.b64encode(identity.credential).decode("ascii"),
                }
                for identity in self.identities
            ],
            "managed_aces": [
                {
                    "path": str(entry.path),
                    "sid": entry.sid.value,
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
            identities = tuple(
                _StoredIdentity(
                    WindowsSandboxIdentityKind(item["kind"]),
                    base64.b64decode(item["credential"].encode("ascii"), validate=True),
                )
                for item in identities_payload
            )
            if {identity.kind for identity in identities} != set(WindowsSandboxIdentityKind):
                raise ValueError("record must contain exactly Offline and Online identities")
            if any(len(identity.credential) != 32 for identity in identities):
                raise ValueError("identity credential length is invalid")
            managed_aces = tuple(
                WindowsManagedAce(
                    Path(item["path"]),
                    SyntheticWindowsSid(item["sid"]),
                    WindowsManagedAceKind(item["kind"]),
                    int(item["access_mask"]),
                    int(item["inheritance"]),
                )
                for item in payload["managed_aces"]
            )
            firewall_payload = payload["offline_firewall_rule"]
            write_sid = SyntheticWindowsSid(payload["write_sid"])
            firewall_rule = WindowsFirewallRule(
                firewall_payload["name"],
                WindowsSandboxIdentityKind(firewall_payload["identity"]),
                SyntheticWindowsSid(firewall_payload["sid"]),
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
        if record.offline_firewall_rule.sid != record.write_sid:
            raise WindowsSandboxSetupError("Windows sandbox firewall SID does not match write SID")
        if (
            record.offline_firewall_rule.identity is not WindowsSandboxIdentityKind.OFFLINE
            or not record.offline_firewall_rule.outbound_block
        ):
            raise WindowsSandboxSetupError("Windows sandbox offline firewall rule is invalid")
        if any(entry.sid != record.write_sid for entry in record.managed_aces):
            raise WindowsSandboxSetupError("managed ACE SID does not match installation write SID")
        if not record.installation_id or "\x00" in record.installation_id:
            raise WindowsSandboxSetupError("Windows sandbox installation ID is invalid")
        return record


class WindowsNativeSandboxSetupAuthority:
    """W2 setup authority with injectable filesystem, DPAPI and firewall APIs."""

    def __init__(
        self,
        *,
        credential_store: WindowsCredentialStore | None = None,
        acl_api: object | None = None,
        firewall_api: WindowsFirewallApi | None = None,
        privilege_api: WindowsSetupPrivilegeApi | None = None,
    ) -> None:
        self._credential_store = credential_store
        self._acl_api = acl_api
        self._firewall_api = firewall_api
        self._privilege_api = privilege_api
        self._unsupported = False
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
    ) -> tuple[WindowsFilesystemAclAuthority, WindowsFirewallApi]:
        if self._unsupported:
            raise WindowsSandboxSetupError("Windows sandbox setup authority is unsupported")
        if self._acl_authority is None:
            api = self._acl_api
            if api is None:
                api = _NativeWindowsAclApi()
            self._acl_authority = WindowsFilesystemAclAuthority(api)  # type: ignore[arg-type]
        firewall = self._firewall_api
        if firewall is None:
            firewall = _NativeWindowsFirewallApi()
        del request
        return self._acl_authority, firewall

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
                state=state,
                privilege_boundary=self.privilege_boundary,
            )
        return WindowsSandboxSetupSnapshot(
            state=state,
            schema_version=record.schema_version,
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
    ) -> WindowsFilesystemSetupPlan:
        return plan_windows_filesystem_authority(request, record.write_sid)

    def inspect(self, request: WindowsSandboxSetupRequest) -> WindowsSandboxSetupSnapshot:
        if not isinstance(request, WindowsSandboxSetupRequest):
            raise TypeError("Windows sandbox setup request must be canonical")
        if self._unsupported:
            return self._snapshot(WindowsSandboxSetupState.UNSUPPORTED, None)
        try:
            acl_authority, firewall = self._apis(request)
        except (SandboxError, OSError):
            return self._snapshot(WindowsSandboxSetupState.UNSUPPORTED, None)
        try:
            record = self._load(request)
        except WindowsSandboxSetupError:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_REPAIR, None)
        if record is None:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_SETUP, None)
        try:
            plan = self._plan(request, record)
            acl_ready = acl_authority.is_ready(plan)
            firewall_ready = (
                firewall.rule_exists(record.offline_firewall_rule)
                if record.active_identity is WindowsSandboxIdentityKind.OFFLINE
                else not firewall.rule_exists(record.offline_firewall_rule)
            )
        except (WindowsSandboxSetupError, OSError):
            return self._snapshot(WindowsSandboxSetupState.NEEDS_REPAIR, record)
        if not acl_ready or not firewall_ready:
            return self._snapshot(WindowsSandboxSetupState.NEEDS_REPAIR, record)
        return self._snapshot(
            WindowsSandboxSetupState.READY,
            record,
            offline_firewall_enabled=record.active_identity is WindowsSandboxIdentityKind.OFFLINE,
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
        acl_authority, firewall = self._apis(request)
        record = self._load(request)
        fresh = record is None
        if record is None:
            record = _InstallationRecord.fresh(SyntheticWindowsSid.generate())
        plan = self._plan(request, record)
        try:
            acl_authority.reconcile(record.managed_aces, plan)
            if identity is WindowsSandboxIdentityKind.OFFLINE:
                firewall.ensure_outbound_block(record.offline_firewall_rule)
            else:
                firewall.remove_rule(record.offline_firewall_rule)
            updated = _InstallationRecord(
                record.schema_version,
                record.installation_id,
                record.write_sid,
                record.identities,
                plan.entries,
                record.offline_firewall_rule,
                identity,
            )
            self._store(request).save(updated.encode())
        except BaseException:
            if fresh:
                try:
                    acl_authority.cleanup(plan.entries)
                    firewall.remove_rule(record.offline_firewall_rule)
                except BaseException:
                    pass
            raise
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
        acl_authority, firewall = self._apis(request)
        try:
            acl_authority.cleanup(record.managed_aces)
            firewall.remove_rule(record.offline_firewall_rule)
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
