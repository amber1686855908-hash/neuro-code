"""Focused W3 acceptance for the Windows setup authority only.

This gate intentionally stops before provenance, named pipes, runner launch,
and child creation.  It exists to attribute the first native setup failure
without exposing credentials, DPAPI plaintext, paths, or command text.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import _NativeWindowsAclApi
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import _NativeWindowsFirewallApi
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    WindowsSandboxSetupError,
    _NativeWindowsSetupPrivilegeApi,
)


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


@unittest.skipUnless(_native_enabled(), "privileged Windows W3 acceptance is CI-only")
class WindowsNativeRuntimeAcceptanceTests(unittest.TestCase):
    def test_setup_only_probe(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W2 setup acceptance requires elevation")
        account_api = _NativeWindowsSandboxAccountApi()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            workspace.mkdir()
            installation.mkdir()
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=WindowsDpapiCredentialStore(installation / "credentials.dpapi"),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                account_api=account_api,
                privilege_api=privilege_api,
            )
            print("W3_STAGE=setup_start", flush=True)
            try:
                snapshot = authority.setup(setup_request)
            except WindowsSandboxSetupError as error:
                print(
                    f"W3_SETUP_FAILURE={json.dumps(error.diagnostic_payload(), sort_keys=True)}",
                    flush=True,
                )
                raise AssertionError("W3_SETUP_BLOCKED") from None
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            print("W3_STAGE=setup_ready", flush=True)
            cleaned = authority.cleanup(setup_request)
            self.assertEqual(cleaned.state, WindowsSandboxSetupState.NEEDS_SETUP)
            print("W3_STAGE=setup_done", flush=True)


if __name__ == "__main__":
    unittest.main()
