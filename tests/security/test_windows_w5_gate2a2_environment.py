"""W5 Gate 2A.2 evidence for the AppContainer launch environment seam.

This test is deliberately evidence-only.  It compares the existing Gate 2A
launch contract with ``lpEnvironment == NULL`` and with the documented
``CreateEnvironmentBlock`` result, then performs the smallest attribute
ablation only if the user environment does not make the child launch.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.security.test_windows_native_pty_acceptance import _compile_msvc_probe
from tests.security.test_windows_w5_gate1_6_loader_isolation import (
    _production_source_diff,
)
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
)
from tests.security.test_windows_w5_gate1_runtime_root_cause import (
    _LOGON_WITH_PROFILE,
    _Gate1DirectProcess,
    _native_enabled,
)
from tests.security.test_windows_w5_gate2a_appcontainer_feasibility import (
    _projection,
)

from neuro_code.application.ports.sandbox import LocalProcessEnvironmentPolicy
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import _NativeWindowsAclApi
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import (
    _NativeWindowsFirewallApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _NativeWindowsSetupPrivilegeApi,
)

_HEAD = "518caad2571482faf639f6dda865dcabaf6c1757"
_GATE1_HEAD = "902f82e014d0728445723630bc24d70bb1b52357"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"


def _child_succeeded(run: dict[str, object]) -> bool:
    return run.get("child_create") == "PASS"


def _child_error(run: dict[str, object]) -> int | None:
    value = run.get("createprocess_error")
    if isinstance(value, int):
        return value
    value = run.get("child_create_error")
    return value if isinstance(value, int) else None


def _environment_classification(
    env_null: dict[str, dict[str, object]],
    env_user: dict[str, dict[str, object]],
) -> str:
    null_203 = all(
        not _child_succeeded(run) and _child_error(run) == 203 for run in env_null.values()
    )
    user_success = all(_child_succeeded(run) for run in env_user.values())
    if null_203 and user_success:
        return "W5_GATE2A2_USER_ENVIRONMENT_PRECONDITION_ESTABLISHED"
    if all(
        _child_succeeded(env_null[mode]) == _child_succeeded(env_user[mode])
        and _child_error(env_null[mode]) == _child_error(env_user[mode])
        for mode in env_null
    ):
        return "W5_GATE2A2_ENVIRONMENT_NOT_CAUSAL"
    return "W5_GATE2A2_CHILD_LAUNCH_CAUSE_STILL_UNATTRIBUTED"


def _attribute_classification(
    variants: dict[str, dict[str, object]],
) -> str:
    for stage in ("a0", "a1", "a2"):
        pipe = variants.get(f"pipe-user-{stage}")
        pty = variants.get(f"pty-user-{stage}")
        if (
            pipe is not None
            and pty is not None
            and _child_succeeded(pipe)
            and _child_succeeded(pty)
        ):
            return f"EARLIEST_BOTH_MODE_LAUNCH_SUCCESS={stage.upper()}"
    return "NO_BOTH_MODE_LAUNCH_SUCCESS"


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate2A2EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    """Compare NULL and CreateEnvironmentBlock launch variants on Windows."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 2A.2 evidence requires the enabled CI gate"
    )
    async def test_gate2a2_environment_matrix(self) -> None:  # pragma: no cover - Windows CI
        self.assertEqual(
            _production_source_diff(), (), "Gate 2A.2 must not modify production source"
        )
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        probe = await asyncio.to_thread(
            _compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate2a_appcontainer_probe.c").resolve(),
            "windows_w5_gate2a_appcontainer_probe_gate2a2",
        )
        self.addAsyncCleanup(_cleanup_probe_directory, probe.parent)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            fixtures = root / "fixtures"
            workspace.mkdir()
            installation.mkdir()
            workspace_probe = workspace / "windows_w5_gate2a_appcontainer_probe.exe"
            shutil.copy2(probe, workspace_probe)
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                account_api=_NativeWindowsSandboxAccountApi(),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            self.addAsyncCleanup(asyncio.to_thread, authority.cleanup, setup_request)
            encoded = store.load()
            self.assertIsNotNone(encoded)
            from neuro_code.infrastructure.sandbox.windows_sandbox_setup import _InstallationRecord

            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            harness = _Gate1DirectProcess()
            controller_environment = WindowsNativeLocalProcessSandbox._child_environment(
                LocalProcessEnvironmentPolicy({})
            )

            async def run_mode(mode: str) -> dict[str, object]:
                raw = await asyncio.to_thread(
                    _run_harness_bounded,
                    harness,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=workspace_probe,
                    arguments=(mode, str(workspace), str(fixtures)),
                    cwd=workspace,
                    environment=controller_environment,
                    logon_flags=_LOGON_WITH_PROFILE,
                    timeout=120.0,
                )
                return _projection(raw, mode)

            env_null = {mode: await run_mode(f"{mode}-env-null") for mode in ("pipe", "pty")}
            env_user = {mode: await run_mode(f"{mode}-env-user") for mode in ("pipe", "pty")}
            environment_classification = _environment_classification(env_null, env_user)
            attribute_variants: dict[str, dict[str, object]] = {}
            if not all(_child_succeeded(run) for run in env_user.values()):
                for mode in ("pipe", "pty"):
                    for stage in ("a0", "a1", "a2"):
                        attribute_variants[f"{mode}-user-{stage}"] = await run_mode(
                            f"{mode}-user-{stage}"
                        )

            production_diff = _production_source_diff()
            artifact: dict[str, object] = {
                "gate": "W5_GATE2A.2",
                "old_head": _HEAD,
                "gate1_frozen_head": _GATE1_HEAD,
                "main": _MAIN,
                "status": "COMPLETED",
                "production_source_diff": production_diff,
                "controller_identity": "W2_ONLINE_WITH_PROFILE_via_CreateProcessWithLogonW",
                "launch_contract": {
                    "application": "copied probe in authorized workspace",
                    "command": "probe pipe/pty child mode",
                    "current_directory": "authorized workspace",
                    "security_capabilities": "AppContainer SID; NULL capabilities; count 0",
                    "pipe_attributes": "SECURITY_CAPABILITIES,JOB_LIST,HANDLE_LIST",
                    "pty_attributes": "SECURITY_CAPABILITIES,JOB_LIST,PSEUDOCONSOLE",
                    "inherit_handles": {"pipe": True, "pty": False},
                    "creation_flags": "CREATE_UNICODE_ENVIRONMENT,CREATE_NO_WINDOW,EXTENDED_STARTUPINFO_PRESENT",
                    "logon_flags": "LOGON_WITH_PROFILE",
                },
                "environment_null": env_null,
                "environment_user_block": env_user,
                "environment_classification": environment_classification,
                "attribute_isolation": attribute_variants,
                "attribute_classification": _attribute_classification(attribute_variants),
                "gate2b_started": False,
                "cleanup": {
                    "temporary_environment_block": True,
                    "temporary_fixture_root": True,
                    "system_acl_mutation": False,
                    "ksecdd_mutation": False,
                    "registry_mutation": False,
                    "firewall_mutation": False,
                    "device_io_control": False,
                    "persistent_profile": False,
                },
            }
            destination = os.environ.get("NEURO_CODE_W5_GATE2A2_EVIDENCE_JSON")
            if destination:
                await asyncio.to_thread(
                    Path(destination).write_text,
                    json.dumps(artifact, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            self.assertEqual(production_diff, ())


if __name__ == "__main__":  # pragma: no cover - unittest discovery entrypoint
    unittest.main()
