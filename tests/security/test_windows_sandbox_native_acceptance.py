"""Privileged Windows W2 acceptance.

This is intentionally separate from the portable model tests.  The CI job sets
``NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE=1`` on a Windows runner;
therefore an unavailable administrator boundary or a real ACL/firewall failure
is a test failure, not a skip or an in-memory PASS.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import socket
import unittest
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxIdentityKind,
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    SANDBOX_OFFLINE_USERNAME,
    SANDBOX_ONLINE_USERNAME,
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    WindowsManagedAce,
    _AceHeader,
    _NativeWindowsAclApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import _NativeWindowsFirewallApi
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _InstallationRecord,
    _NativeWindowsSetupPrivilegeApi,
)


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


@contextlib.contextmanager
def _impersonate(username: str, password: str) -> Iterator[None]:  # pragma: no cover - Windows CI
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise AssertionError("Win32 ctypes is unavailable")
    advapi = loader("advapi32.dll", use_last_error=True)
    kernel = loader("kernel32.dll", use_last_error=True)
    logon = advapi.LogonUserW
    logon.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    logon.restype = ctypes.c_int32
    impersonate = advapi.ImpersonateLoggedOnUser
    impersonate.argtypes = [ctypes.c_void_p]
    impersonate.restype = ctypes.c_int32
    revert = advapi.RevertToSelf
    revert.argtypes = []
    revert.restype = ctypes.c_int32
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int32
    token = ctypes.c_void_p()
    if not logon(username, None, password, 2, 0, ctypes.byref(token)):
        raise AssertionError(f"LogonUserW failed: {ctypes.get_last_error()}")
    try:
        if not impersonate(token):
            raise AssertionError(f"ImpersonateLoggedOnUser failed: {ctypes.get_last_error()}")
        try:
            yield
        finally:
            if not revert():
                raise AssertionError(f"RevertToSelf failed: {ctypes.get_last_error()}")
    finally:
        close(token)


def _assert_denied(action: object) -> None:
    try:
        action()  # type: ignore[operator]
    except (PermissionError, OSError):
        return
    raise AssertionError("sandbox user unexpectedly retained access")


_BENIGN_OUTBOUND_PROBE = ("1.1.1.1", 80)


def _benign_outbound_probe() -> None:
    """Make one fixed TCP connectivity probe; never scan or send a payload."""

    connection = socket.create_connection(_BENIGN_OUTBOUND_PROBE, timeout=5)
    connection.close()


def _reorder_native_acl(
    api: _NativeWindowsAclApi,
    path: Path,
    target: WindowsManagedAce,
) -> None:  # pragma: no cover - Windows CI
    """Model an external raw DACL reorder without replacing its descriptor."""

    raw_entries = api._raw_entries(path)
    target_indices = [
        index for index, raw in enumerate(raw_entries) if api._raw_matches(raw, target)
    ]
    if len(raw_entries) < 2 or len(target_indices) != 1:
        raise AssertionError("native fixture did not contain the managed ACE to reorder")
    target_raw = raw_entries[target_indices[0]]
    reordered = [raw for index, raw in enumerate(raw_entries) if index != target_indices[0]]
    reordered.append(target_raw)
    estimated_size = max(256, sum(len(raw) for raw in reordered) + 256)
    acl_buffer = ctypes.create_string_buffer(estimated_size)
    if not api._initialize_acl(acl_buffer, estimated_size, api._ACL_REVISION):
        raise AssertionError("InitializeAcl failed while injecting reorder drift")
    for raw in reordered:
        raw_buffer = ctypes.create_string_buffer(raw)
        if not api._add_ace(
            acl_buffer,
            api._ACL_REVISION,
            api._MAXDWORD,
            raw_buffer,
            len(raw),
        ):
            raise AssertionError("AddAce failed while injecting reorder drift")
    result = api._set_named_security_info(
        str(path),
        api._SE_FILE_OBJECT,
        api._DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.cast(acl_buffer, ctypes.c_void_p),
        None,
    )
    if result != 0:
        raise AssertionError(f"SetNamedSecurityInfoW reorder failed: {result}")


@unittest.skipUnless(_native_enabled(), "privileged Windows acceptance is CI-only")
class WindowsSandboxNativeAcceptanceTests(unittest.TestCase):
    def test_real_accounts_acl_dpapi_and_firewall(self) -> None:  # pragma: no cover - Windows CI
        account_api = _NativeWindowsSandboxAccountApi()
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(
            privilege_api.is_administrator(), "native acceptance requires an elevated runner"
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            readonly = root / "readonly"
            installation = root / "installation"
            workspace.mkdir()
            readonly.mkdir()
            installation.mkdir()
            workspace_file = workspace / "workspace.txt"
            readonly_file = readonly / "readonly.txt"
            sensitive_file = workspace / "controller-state.json"
            workspace_file.write_text("workspace", encoding="utf-8")
            readonly_file.write_text("readonly", encoding="utf-8")
            sensitive_file.write_text("controller secret", encoding="utf-8")
            request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace, readonly),
                writable_roots=(workspace,),
                sensitive_read_paths=(sensitive_file,),
            )
            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            acl_api = _NativeWindowsAclApi()
            firewall_api = _NativeWindowsFirewallApi()
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                acl_api=acl_api,
                firewall_api=firewall_api,
                account_api=account_api,
                privilege_api=privilege_api,
            )
            record: _InstallationRecord | None = None
            try:
                offline = authority.setup(request, identity=WindowsSandboxIdentityKind.OFFLINE)
                self.assertEqual(offline.state, WindowsSandboxSetupState.READY)
                self.assertIsNotNone(offline.offline_user_sid)
                self.assertIsNotNone(offline.online_user_sid)
                self.assertNotEqual(offline.offline_user_sid, offline.online_user_sid)
                self.assertNotEqual(offline.write_restricting_sid, offline.offline_user_sid)
                encoded = store.load()
                self.assertIsNotNone(encoded)
                record = _InstallationRecord.decode(encoded or b"")
                offline_record = record.offline
                online_record = record.online
                print(f"offline_user={offline_record.username} sid={offline_record.user_sid.value}")
                print(f"online_user={online_record.username} sid={online_record.user_sid.value}")
                print(f"write_restricting_sid={record.write_sid.value}")

                # Repeating the same profile must preserve all three identity
                # roles and leave the native policy in READY state.
                repeated = authority.setup(request, identity=WindowsSandboxIdentityKind.OFFLINE)
                self.assertEqual(repeated.state, WindowsSandboxSetupState.READY)
                self.assertEqual(repeated.offline_user_sid, offline.offline_user_sid)
                self.assertEqual(repeated.online_user_sid, offline.online_user_sid)
                self.assertEqual(repeated.write_restricting_sid, offline.write_restricting_sid)
                inspected = authority.inspect(request)
                self.assertEqual(inspected.state, WindowsSandboxSetupState.READY)

                # Native firewall drift must invalidate READY on every
                # managed semantic, not merely name/SID.  Mutate the exact
                # rule to an inbound disabled allow on a different profile,
                # then prove repair restores the complete contract.
                drift_script = (
                    f"Set-NetFirewallRule -Name {firewall_api._ps_quote(record.offline_firewall_rule.name)} "
                    "-Direction Inbound -Action Allow -Enabled False -Profile Domain"
                )
                drift_result = firewall_api._run(["-Command", drift_script], check=True)
                self.assertEqual(drift_result.returncode, 0)
                self.assertEqual(
                    authority.inspect(request).state, WindowsSandboxSetupState.NEEDS_REPAIR
                )
                self.assertEqual(
                    authority.repair(request, identity=WindowsSandboxIdentityKind.OFFLINE).state,
                    WindowsSandboxSetupState.READY,
                )
                replacement = workspace / "replacement.dpapi"
                replacement.write_bytes(b"replacement")

                # Existing broad Users access is not enough to bypass the
                # explicit Neuro deny on sensitive and read-only paths.
                with _impersonate(offline_record.username, offline_record.password.decode()):
                    self.assertEqual(workspace_file.read_text(encoding="utf-8"), "workspace")
                    workspace_file.write_text("offline write", encoding="utf-8")
                    self.assertEqual(readonly_file.read_text(encoding="utf-8"), "readonly")
                    _assert_denied(lambda: readonly_file.write_text("must fail", encoding="utf-8"))
                    _assert_denied(lambda: sensitive_file.read_text(encoding="utf-8"))
                    _assert_denied(lambda: os.stat(store.path))
                    _assert_denied(lambda: store.path.read_bytes())
                    _assert_denied(lambda: store.path.write_bytes(b"must fail"))
                    _assert_denied(lambda: os.replace(replacement, store.path))
                    _assert_denied(lambda: store.path.rename(workspace / "renamed.dpapi"))
                    _assert_denied(lambda: store.path.unlink())

                # SetEntriesInAclW must put the managed explicit read deny
                # before the pre-existing/inherited broad allow on the same
                # sensitive file.  The access probe above proves the semantic
                # effect; this checks the DACL ordering contract directly.
                sensitive_path = sensitive_file.resolve(strict=False)
                raw_sensitive = acl_api._raw_entries(sensitive_path)
                deny_indices = [
                    index
                    for index, raw in enumerate(raw_sensitive)
                    if _AceHeader.from_buffer_copy(raw).AceType == 1
                    and any(
                        acl_api._raw_matches(raw, entry)
                        for entry in record.managed_aces
                        if entry.path == sensitive_path and entry.is_deny
                    )
                ]
                allow_indices = [
                    index
                    for index, raw in enumerate(raw_sensitive)
                    if _AceHeader.from_buffer_copy(raw).AceType == 0
                ]
                self.assertTrue(deny_indices)
                self.assertTrue(allow_indices)
                self.assertLess(max(deny_indices), min(allow_indices))

                # Controller owner/ACE remains usable after ACL reconciliation.
                self.assertEqual(workspace_file.read_text(encoding="utf-8"), "offline write")
                sensitive_file.write_text("controller still owns it", encoding="utf-8")

                # The controller is not the firewall subject.  This is one
                # fixed, benign non-loopback TCP probe; no scan or payload is
                # performed.  Windows Firewall intentionally treats loopback
                # as a separate policy surface.
                _benign_outbound_probe()
                # A failed connect is checked explicitly because socket
                # errors are not filesystem PermissionErrors.
                with (
                    _impersonate(offline_record.username, offline_record.password.decode()),
                    self.assertRaises(OSError),
                ):
                    _benign_outbound_probe()
                with _impersonate(online_record.username, online_record.password.decode()):
                    _benign_outbound_probe()

                online = authority.setup(request, identity=WindowsSandboxIdentityKind.ONLINE)
                self.assertEqual(online.state, WindowsSandboxSetupState.READY)
                with _impersonate(online_record.username, online_record.password.decode()):
                    _benign_outbound_probe()
                replacement_online = workspace / "replacement-online.dpapi"
                replacement_online.write_bytes(b"replacement")
                with _impersonate(online_record.username, online_record.password.decode()):
                    self.assertEqual(workspace_file.read_text(encoding="utf-8"), "offline write")
                    _assert_denied(lambda: os.stat(store.path))
                    _assert_denied(lambda: store.path.read_bytes())
                    _assert_denied(lambda: store.path.write_bytes(b"must fail"))
                    _assert_denied(lambda: os.replace(replacement_online, store.path))
                    _assert_denied(lambda: store.path.rename(workspace / "renamed-online.dpapi"))
                    _assert_denied(lambda: store.path.unlink())

                # Controller/setup authority retains normal DPAPI state
                # operations after both sandbox identities are denied.
                controller_state = store.load()
                self.assertIsNotNone(controller_state)
                store.save(controller_state or b"controller-state")
                self.assertEqual(store.load(), controller_state or b"controller-state")

                # An externally reordered managed sensitive deny is not READY
                # even though the tuple is still present.  Repair must restore
                # canonical ordering and preserve its access semantics.
                sensitive_deny = next(
                    entry
                    for entry in record.managed_aces
                    if entry.path == sensitive_file.resolve(strict=False) and entry.is_deny
                )
                _reorder_native_acl(acl_api, sensitive_file, sensitive_deny)
                self.assertEqual(
                    authority.inspect(request).state, WindowsSandboxSetupState.NEEDS_REPAIR
                )
                self.assertEqual(
                    authority.repair(request, identity=WindowsSandboxIdentityKind.OFFLINE).state,
                    WindowsSandboxSetupState.READY,
                )
                with _impersonate(offline_record.username, offline_record.password.decode()):
                    _assert_denied(lambda: sensitive_file.read_text(encoding="utf-8"))

                # Remove one managed ACE and prove native repair restores it.
                workspace_path = workspace.resolve(strict=False)
                removable = next(
                    entry
                    for entry in record.managed_aces
                    if entry.path == workspace_path and entry.kind.value == "read-allow"
                )
                acl_api.reconcile(workspace_path, desired=(), remove=(removable,))
                self.assertEqual(
                    authority.inspect(request).state, WindowsSandboxSetupState.NEEDS_REPAIR
                )
                self.assertEqual(
                    authority.repair(request, identity=WindowsSandboxIdentityKind.ONLINE).state,
                    WindowsSandboxSetupState.READY,
                )
            finally:
                authority.cleanup(request)
                if record is not None:
                    self.assertFalse(firewall_api.rule_exists(record.offline_firewall_rule))
                if record is not None:
                    if record.offline.created_by_installation:
                        self.assertFalse(account_api.user_exists(SANDBOX_OFFLINE_USERNAME))
                    if record.online.created_by_installation:
                        self.assertFalse(account_api.user_exists(SANDBOX_ONLINE_USERNAME))
                self.assertFalse(store.path.exists())


if __name__ == "__main__":
    unittest.main()
