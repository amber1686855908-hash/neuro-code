from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from neuro_code.application.ports.sandbox import (
    LocalProcessSecurityCapability,
    LocalProcessSecurityStrength,
)
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxIdentityKind,
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    SANDBOX_OFFLINE_USERNAME,
    SANDBOX_ONLINE_USERNAME,
    InMemoryWindowsSandboxAccountApi,
    WindowsAccountSid,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    READ_ACCESS_MASK,
    InMemoryWindowsAclApi,
    WindowsFilesystemSetupPlan,
    WindowsManagedAce,
    WindowsManagedAceKind,
    plan_windows_filesystem_authority,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import (
    InMemoryWindowsFirewallApi,
    WindowsFirewallRule,
    firewall_rule_for_installation,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import (
    WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES,
    WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES,
    SyntheticWindowsSid,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
    WindowsDpapiError,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    WindowsSandboxSetupPrivilegeError,
)


class _FakeDpapi:
    def __init__(self) -> None:
        self.protect_calls: list[bytes] = []
        self.unprotect_calls: list[bytes] = []

    def protect(self, plaintext: bytes) -> bytes:
        self.protect_calls.append(plaintext)
        return b"protected:" + plaintext

    def unprotect(self, protected: bytes) -> bytes:
        self.unprotect_calls.append(protected)
        if not protected.startswith(b"protected:"):
            raise WindowsDpapiError("fake DPAPI rejected blob")
        return protected[len(b"protected:") :]


class _FakePrivilege:
    def __init__(self, administrator: bool) -> None:
        self.administrator = administrator
        self.calls = 0

    def is_administrator(self) -> bool:
        self.calls += 1
        return self.administrator


class WindowsDpapiCredentialStoreTests(unittest.TestCase):
    def test_envelope_contains_no_plaintext_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _FakeDpapi()
            store = WindowsDpapiCredentialStore(Path(directory) / "credentials.dpapi", api=api)
            store.save(b"installation secret")
            encoded = store.path.read_bytes()
            self.assertNotIn(b"installation secret", encoded)
            self.assertEqual(store.load(), b"installation secret")
            self.assertEqual(api.protect_calls, [b"installation secret"])
            self.assertEqual(api.unprotect_calls, [b"protected:installation secret"])

    def test_corrupt_envelope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.dpapi"
            path.write_text("{}", encoding="utf-8")
            store = WindowsDpapiCredentialStore(path, api=_FakeDpapi())
            with self.assertRaises(WindowsDpapiError):
                store.load()

    def test_clear_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowsDpapiCredentialStore(
                Path(directory) / "credentials.dpapi",
                api=_FakeDpapi(),
            )
            store.save(b"secret")
            store.clear()
            store.clear()
            self.assertIsNone(store.load())

    @unittest.skipUnless(os.name == "nt", "native DPAPI requires Windows")
    def test_native_dpapi_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowsDpapiCredentialStore(Path(directory) / "credentials.dpapi")
            store.save(b"native installation credential")
            self.assertEqual(store.load(), b"native installation credential")


class WindowsSandboxSetupAuthorityTests(unittest.TestCase):
    def _request(self, directory: str) -> WindowsSandboxSetupRequest:
        root = Path(directory)
        workspace = root / "workspace"
        install = root / "installation"
        return WindowsSandboxSetupRequest(
            installation_root=install,
            read_roots=(workspace,),
            writable_roots=(workspace,),
            sensitive_read_paths=(workspace / "controller-state.json",),
        )

    def _authority(
        self,
        directory: str,
        *,
        administrator: bool = True,
    ) -> tuple[
        WindowsNativeSandboxSetupAuthority,
        InMemoryWindowsAclApi,
        InMemoryWindowsFirewallApi,
        WindowsDpapiCredentialStore,
    ]:
        acl = InMemoryWindowsAclApi()
        firewall = InMemoryWindowsFirewallApi()
        store = WindowsDpapiCredentialStore(
            Path(directory) / "installation" / "credentials.dpapi",
            api=_FakeDpapi(),
        )
        authority = WindowsNativeSandboxSetupAuthority(
            credential_store=store,
            acl_api=acl,
            firewall_api=firewall,
            account_api=InMemoryWindowsSandboxAccountApi(),
            privilege_api=_FakePrivilege(administrator),
        )
        return authority, acl, firewall, store

    def test_request_rejects_sensitive_ancestor_of_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                WindowsSandboxSetupRequest(
                    installation_root=root / "install",
                    read_roots=(root / "workspace",),
                    writable_roots=(root / "workspace",),
                    sensitive_read_paths=(root,),
                )

    def test_request_validation_rejects_ambiguous_or_unsafe_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = {
                "installation_root": root / "install",
                "read_roots": (root / "workspace",),
                "writable_roots": (root / "workspace",),
                "sensitive_read_paths": (),
            }
            with self.assertRaises(ValueError):
                WindowsSandboxSetupRequest(**{**valid, "installation_root": Path("relative")})
            with self.assertRaises(ValueError):
                WindowsSandboxSetupRequest(**{**valid, "installation_root": Path("/")})
            with self.assertRaises(ValueError):
                WindowsSandboxSetupRequest(**{**valid, "read_roots": ()})
            with self.assertRaises(ValueError):
                WindowsSandboxSetupRequest(
                    **{**valid, "read_roots": (root / "workspace", root / "workspace")}
                )
            with self.assertRaises(ValueError):
                WindowsSandboxSetupRequest(**{**valid, "writable_roots": (root / "outside",)})

    def test_managed_ace_contract_rejects_unsafe_tuples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace"
            sid = SyntheticWindowsSid.from_components((1, 2, 3, 4))
            with self.assertRaises(ValueError):
                WindowsManagedAce(
                    Path("relative"), sid, WindowsManagedAceKind.READ_ALLOW, READ_ACCESS_MASK
                )
            with self.assertRaises(ValueError):
                WindowsManagedAce(
                    Path("/"), sid, WindowsManagedAceKind.READ_ALLOW, READ_ACCESS_MASK
                )
            with self.assertRaises(TypeError):
                WindowsManagedAce(
                    path, object(), WindowsManagedAceKind.READ_ALLOW, READ_ACCESS_MASK
                )  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                WindowsManagedAce(path, sid, object(), READ_ACCESS_MASK)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                WindowsManagedAce(
                    path,
                    WindowsAccountSid("S-1-5-21-100-200-300-2000"),
                    WindowsManagedAceKind.READ_ALLOW,
                    0,
                )
            with self.assertRaises(ValueError):
                WindowsManagedAce(
                    path,
                    WindowsAccountSid("S-1-5-21-100-200-300-2000"),
                    WindowsManagedAceKind.READ_ALLOW,
                    READ_ACCESS_MASK,
                    inheritance=0,
                )
            deny = WindowsManagedAce(
                path,
                WindowsAccountSid("S-1-5-21-100-200-300-2000"),
                WindowsManagedAceKind.SENSITIVE_READ_DENY,
                READ_ACCESS_MASK,
            )
            self.assertTrue(deny.is_deny)
            with self.assertRaises(ValueError):
                WindowsFilesystemSetupPlan((deny, deny))
            self.assertEqual(
                WindowsFilesystemSetupPlan((deny,)).paths,
                (path.resolve(),),
            )

    def test_planner_requires_canonical_request_and_sid(self) -> None:
        with self.assertRaises(TypeError):
            plan_windows_filesystem_authority(
                object(),
                SyntheticWindowsSid.from_components((1, 2, 3, 4)),
                read_user_sids=(WindowsAccountSid("S-1-5-21-100-200-300-2000"),),
                write_user_sids=(WindowsAccountSid("S-1-5-21-100-200-300-2000"),),
            )  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(TypeError):
            plan_windows_filesystem_authority(
                self._request(directory),
                object(),
                read_user_sids=(WindowsAccountSid("S-1-5-21-100-200-300-2000"),),
                write_user_sids=(WindowsAccountSid("S-1-5-21-100-200-300-2000"),),
            )  # type: ignore[arg-type]

    def test_firewall_rule_contract_is_scoped_and_validated(self) -> None:
        sid = WindowsAccountSid("S-1-5-21-100-200-300-2000")
        offline = firewall_rule_for_installation(
            "install-1", WindowsSandboxIdentityKind.OFFLINE, sid
        )
        self.assertTrue(offline.outbound_block)
        self.assertEqual(offline.sid, sid)
        with self.assertRaises(ValueError):
            firewall_rule_for_installation("", WindowsSandboxIdentityKind.OFFLINE, sid)
        with self.assertRaises(TypeError):
            firewall_rule_for_installation(
                "install-1",
                WindowsSandboxIdentityKind.OFFLINE,
                SyntheticWindowsSid.from_components((1, 2, 3, 4)),
            )  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            WindowsFirewallRule("\x00", WindowsSandboxIdentityKind.OFFLINE, sid, True)
        with self.assertRaises(TypeError):
            WindowsFirewallRule("rule", object(), sid, True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WindowsFirewallRule("rule", WindowsSandboxIdentityKind.OFFLINE, object(), True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            WindowsFirewallRule("rule", WindowsSandboxIdentityKind.OFFLINE, sid, 1)  # type: ignore[arg-type]

    def test_plan_has_write_read_and_sensitive_deny_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            request = WindowsSandboxSetupRequest(
                installation_root=request.installation_root,
                read_roots=(*request.read_roots, request.installation_root / "readonly"),
                writable_roots=request.writable_roots,
                sensitive_read_paths=request.sensitive_read_paths,
            )
            account_sids = (
                WindowsAccountSid("S-1-5-21-100-200-300-2000"),
                WindowsAccountSid("S-1-5-21-100-200-300-2001"),
            )
            plan = plan_windows_filesystem_authority(
                request,
                SyntheticWindowsSid.from_components((1, 2, 3, 4)),
                read_user_sids=account_sids,
                write_user_sids=account_sids,
            )
            self.assertEqual(
                {entry.kind for entry in plan.entries},
                {
                    WindowsManagedAceKind.READ_ALLOW,
                    WindowsManagedAceKind.WRITE_ALLOW,
                    WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                    WindowsManagedAceKind.READ_ONLY_WRITE_DENY,
                    WindowsManagedAceKind.SENSITIVE_READ_DENY,
                },
            )
            read_entries = [
                entry
                for entry in plan.entries
                if entry.kind
                in (WindowsManagedAceKind.READ_ALLOW, WindowsManagedAceKind.SENSITIVE_READ_DENY)
            ]
            self.assertTrue(all(isinstance(entry.sid, WindowsAccountSid) for entry in read_entries))
            write_entries = [
                entry for entry in plan.entries if entry.kind is WindowsManagedAceKind.WRITE_ALLOW
            ]
            self.assertTrue(
                all(isinstance(entry.sid, WindowsAccountSid) for entry in write_entries)
            )
            restricting_entries = [
                entry
                for entry in plan.entries
                if entry.kind is WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW
            ]
            self.assertTrue(restricting_entries)
            self.assertTrue(
                all(isinstance(entry.sid, SyntheticWindowsSid) for entry in restricting_entries)
            )
            credential_plan = plan_windows_filesystem_authority(
                request,
                SyntheticWindowsSid.from_components((1, 2, 3, 4)),
                read_user_sids=account_sids,
                write_user_sids=account_sids,
                credential_path=request.installation_root / "credentials.dpapi",
            )
            self.assertTrue(
                any(
                    entry.kind is WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY
                    for entry in credential_plan.entries
                )
            )
            credential_plan = plan_windows_filesystem_authority(
                request,
                SyntheticWindowsSid.from_components((1, 2, 3, 4)),
                read_user_sids=account_sids,
                write_user_sids=account_sids,
                credential_path=request.installation_root / "credentials.dpapi",
            )
            self.assertTrue(
                any(
                    entry.kind is WindowsManagedAceKind.CREDENTIAL_PROTECTION_DENY
                    for entry in credential_plan.entries
                )
            )

    def test_setup_creates_dedicated_identities_with_one_persistent_write_sid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            authority, acl, firewall, store = self._authority(directory)
            offline = authority.setup(request, identity=WindowsSandboxIdentityKind.OFFLINE)
            self.assertEqual(offline.state, WindowsSandboxSetupState.READY)
            self.assertTrue(offline.offline_firewall_enabled)
            self.assertEqual(len(offline.identities), 2)
            records = authority.identity_records(request)
            self.assertEqual(
                {record.kind for record in records},
                set(WindowsSandboxIdentityKind),
            )
            self.assertEqual({record.write_sid.value for record in records}, {offline.write_sid})
            self.assertEqual(
                {record.username for record in records},
                {SANDBOX_OFFLINE_USERNAME, SANDBOX_ONLINE_USERNAME},
            )
            self.assertEqual(
                {record.user_sid.value for record in records},
                {offline.offline_user_sid, offline.online_user_sid},
            )
            self.assertNotIn(offline.write_sid, {offline.offline_user_sid, offline.online_user_sid})
            self.assertGreaterEqual(len(acl.entries), 2)
            self.assertEqual(len(firewall.rules), 1)
            rule = next(iter(firewall.rules.values()))
            self.assertEqual(rule.sid.value, offline.offline_user_sid)
            self.assertTrue(rule.outbound_block)
            self.assertNotEqual(rule.sid.value, offline.write_restricting_sid)
            self.assertIsNotNone(store.load())

            online = authority.setup(request, identity=WindowsSandboxIdentityKind.ONLINE)
            self.assertEqual(online.state, WindowsSandboxSetupState.READY)
            self.assertFalse(online.offline_firewall_enabled)
            self.assertEqual(online.write_sid, offline.write_sid)
            self.assertEqual(firewall.rules, {})

    def test_setup_is_idempotent_and_inspect_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            authority, acl, firewall, _ = self._authority(directory)
            authority.setup(request, identity=WindowsSandboxIdentityKind.ONLINE)
            first_entry_count = sum(len(entries) for entries in acl.entries.values())
            authority.setup(request, identity=WindowsSandboxIdentityKind.ONLINE)
            self.assertEqual(
                sum(len(entries) for entries in acl.entries.values()),
                first_entry_count,
            )
            self.assertEqual(authority.inspect(request).state, WindowsSandboxSetupState.READY)
            self.assertEqual(firewall.rules, {})

    def test_acl_drift_is_repaired_without_removing_unmanaged_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            authority, acl, _, _ = self._authority(directory)
            authority.setup(request, identity=WindowsSandboxIdentityKind.ONLINE)
            workspace = request.read_roots[0]
            acl.unmanaged_entries[workspace].add("controller-user-ace")
            removed = next(iter(acl.entries[workspace]))
            acl.entries[workspace].remove(removed)
            self.assertEqual(
                authority.inspect(request).state, WindowsSandboxSetupState.NEEDS_REPAIR
            )
            repaired = authority.repair(request, identity=WindowsSandboxIdentityKind.ONLINE)
            self.assertEqual(repaired.state, WindowsSandboxSetupState.READY)
            self.assertIn("controller-user-ace", acl.unmanaged_entries[workspace])

    def test_corrupt_persisted_record_reports_needs_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            authority, _, _, store = self._authority(directory)
            authority.setup(request, identity=WindowsSandboxIdentityKind.ONLINE)
            store.save(b"not-a-valid-installation-record")
            self.assertEqual(
                authority.inspect(request).state, WindowsSandboxSetupState.NEEDS_REPAIR
            )

    def test_setup_requires_admin_and_does_not_mutate_when_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            authority, acl, firewall, _ = self._authority(directory, administrator=False)
            with self.assertRaises(WindowsSandboxSetupPrivilegeError):
                authority.setup(request, identity=WindowsSandboxIdentityKind.OFFLINE)
            self.assertEqual(acl.calls, [])
            self.assertEqual(firewall.calls, [])

    def test_cleanup_removes_only_managed_entries_and_credential_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(directory)
            authority, acl, firewall, store = self._authority(directory)
            authority.setup(request, identity=WindowsSandboxIdentityKind.OFFLINE)
            acl.unmanaged_entries[request.read_roots[0]].add("real-controller-user")
            cleaned = authority.cleanup(request)
            self.assertEqual(cleaned.state, WindowsSandboxSetupState.NEEDS_SETUP)
            self.assertEqual(acl.entries, {})
            self.assertEqual(firewall.rules, {})
            self.assertIsNone(store.load())
            self.assertIn("real-controller-user", acl.unmanaged_entries[request.read_roots[0]])
            account_api = authority._account_api
            assert account_api is not None
            self.assertFalse(account_api.user_exists("NeuroSandboxOffline"))
            self.assertFalse(account_api.user_exists("NeuroSandboxOnline"))

    def test_setup_privilege_boundary_is_admin_only_for_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authority, _, _, _ = self._authority(directory)
            self.assertTrue(authority.privilege_boundary.setup_requires_administrator)
            self.assertFalse(authority.privilege_boundary.runtime_requires_administrator)

    def test_default_non_windows_authority_is_unsupported(self) -> None:
        if os.name == "nt":
            return
        with tempfile.TemporaryDirectory() as directory:
            authority = WindowsNativeSandboxSetupAuthority()
            self.assertEqual(
                authority.inspect(self._request(directory)).state,
                WindowsSandboxSetupState.UNSUPPORTED,
            )


class WindowsSandboxCapabilityRegressionTests(unittest.TestCase):
    def test_w2_does_not_advertise_runtime_security_capabilities(self) -> None:
        for capability in LocalProcessSecurityCapability:
            self.assertIs(
                WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES.strength_for(capability),
                LocalProcessSecurityStrength.UNSUPPORTED,
            )
        self.assertIs(
            WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES.strength_for(
                LocalProcessSecurityCapability.READ_ISOLATION
            ),
            LocalProcessSecurityStrength.LIMITED,
        )


if __name__ == "__main__":
    unittest.main()
