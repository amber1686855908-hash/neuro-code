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
                if inspected.state is not WindowsSandboxSetupState.READY:
                    # Keep the privileged artifact actionable without ever
                    # printing passwords or DPAPI plaintext.  Native ACL
                    # providers can normalize inheritance flags, so expose
                    # the exact missing managed tuple and the independent
                    # account/firewall checks before failing the acceptance.
                    print(f"native_inspect_state={inspected.state.value}")
                    loaded = _InstallationRecord.decode(store.load() or b"")
                    plan = authority._plan(request, loaded, store.path)
                    grouped: dict[Path, list[WindowsManagedAce]] = {}
                    for entry in plan.entries:
                        grouped.setdefault(entry.path, []).append(entry)
                    for path, entries in grouped.items():
                        try:
                            raw_entries = acl_api._raw_entries(path)
                            for entry in entries:
                                if not any(acl_api._raw_matches(raw, entry) for raw in raw_entries):
                                    print(
                                        "native_missing_ace "
                                        f"path={path} kind={entry.kind.value} sid={entry.sid.value} "
                                        f"mask={entry.access_mask} inheritance={entry.inheritance}"
                                    )
                        except BaseException as error:
                            print(
                                f"native_acl_diagnostic_error path={path} error={type(error).__name__}"
                            )
                    print(
                        "native_firewall_rule_exists="
                        f"{firewall_api.rule_exists(loaded.offline_firewall_rule)}"
                    )
                    for identity in loaded.identities:
                        try:
                            account_api.validate_user(
                                identity.username,
                                identity.password.decode(),
                                expected_sid=identity.user_sid,
                            )
                        except BaseException as error:
                            print(
                                f"native_account_diagnostic username={identity.username} "
                                f"error={type(error).__name__}"
                            )
                self.assertEqual(inspected.state, WindowsSandboxSetupState.READY)

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

                # SetEntriesInAclW must put the managed explicit read deny
                # before the pre-existing/inherited broad allow on the same
                # sensitive file.  The access probe above proves the semantic
                # effect; this checks the DACL ordering contract directly.
                raw_sensitive = acl_api._raw_entries(sensitive_file)
                deny_indices = [
                    index
                    for index, raw in enumerate(raw_sensitive)
                    if _AceHeader.from_buffer_copy(raw).AceType == 1
                    and any(
                        acl_api._raw_matches(raw, entry)
                        for entry in record.managed_aces
                        if entry.path == sensitive_file and entry.is_deny
                    )
                ]
                if not deny_indices:
                    expected_denies = [
                        entry
                        for entry in record.managed_aces
                        if entry.path == sensitive_file and entry.is_deny
                    ]
                    print(
                        "native_sensitive_expected "
                        f"count={len(expected_denies)} "
                        + ";".join(
                            f"path={entry.path} sid={entry.sid.value} mask={entry.access_mask} inheritance={entry.inheritance}"
                            for entry in expected_denies
                        )
                    )
                    for index, raw in enumerate(raw_sensitive):
                        header = _AceHeader.from_buffer_copy(raw)
                        sid_buffer = ctypes.create_string_buffer(raw[8:])
                        print(
                            "native_sensitive_ace "
                            f"index={index} type={header.AceType} flags={header.AceFlags} "
                            f"mask={int.from_bytes(raw[4:8], 'little')} "
                            f"sid={acl_api._sid_string(ctypes.addressof(sid_buffer))}"
                        )
                    for entry in expected_denies:
                        print(
                            "native_sensitive_match "
                            f"sid={entry.sid.value} "
                            f"matches={any(acl_api._raw_matches(raw, entry) for raw in raw_sensitive)}"
                        )
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

                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.bind(("127.0.0.1", 0))
                listener.listen(2)
                port = listener.getsockname()[1]
                try:
                    # The controller is not the firewall subject.
                    controller = socket.create_connection(("127.0.0.1", port), timeout=3)
                    controller.close()
                    # A failed connect is checked explicitly because socket
                    # errors are not filesystem PermissionErrors.
                    with (
                        _impersonate(offline_record.username, offline_record.password.decode()),
                        self.assertRaises(OSError),
                    ):
                        socket.create_connection(("127.0.0.1", port), timeout=3)
                    with _impersonate(online_record.username, online_record.password.decode()):
                        online_client = socket.create_connection(("127.0.0.1", port), timeout=3)
                        online_client.close()
                finally:
                    listener.close()

                online = authority.setup(request, identity=WindowsSandboxIdentityKind.ONLINE)
                self.assertEqual(online.state, WindowsSandboxSetupState.READY)
                online_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                online_listener.bind(("127.0.0.1", 0))
                online_listener.listen(1)
                try:
                    online_port = online_listener.getsockname()[1]
                    with _impersonate(online_record.username, online_record.password.decode()):
                        online_client = socket.create_connection(
                            ("127.0.0.1", online_port), timeout=3
                        )
                        online_client.close()
                finally:
                    online_listener.close()
                with _impersonate(online_record.username, online_record.password.decode()):
                    self.assertEqual(workspace_file.read_text(encoding="utf-8"), "offline write")
                    _assert_denied(lambda: os.stat(store.path))
                    _assert_denied(lambda: store.path.read_bytes())

                # Remove one managed ACE and prove native repair restores it.
                removable = next(
                    entry
                    for entry in record.managed_aces
                    if entry.path == workspace and entry.kind.value == "read-allow"
                )
                acl_api.reconcile(workspace, desired=(), remove=(removable,))
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
