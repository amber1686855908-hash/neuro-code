"""Focused W3 Gate 1 acceptance for the native final-child token boundary.

The gate deliberately uses ``whoami.exe`` rather than Python.  The trusted
runner attests the actual ``CreateProcessAsUserW`` child handle before sending
``SpawnReady``; this test validates that controller-only metadata together
with stdout, child exit, and runner exit for both W2 identities.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    OwnedLocalProcess,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    _WindowsNativeDesktopMode,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    SANDBOX_OFFLINE_USERNAME,
    SANDBOX_ONLINE_USERNAME,
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import _NativeWindowsAclApi
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import _NativeWindowsFirewallApi
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _NativeWindowsSetupPrivilegeApi,
)


async def _read_stream(stream: object | None) -> bytes:
    if stream is None:
        return b""
    value = await cast(Any, stream).read(65_536)
    return value if isinstance(value, bytes) else b""


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


def _request(
    *,
    workspace: Path,
    network: LocalProcessNetworkPolicy,
    executable: str,
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        executable,
        (),
        purpose=LocalProcessPurpose.BASH,
        cwd=workspace,
        sandbox_profile=SandboxProfile.WORKSPACE,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (
                LocalWorkspaceAccess(
                    workspace,
                    LocalWorkspaceAccessMode.READ_WRITE,
                ),
            )
        ),
        network_policy=network,
        environment_policy=LocalProcessEnvironmentPolicy({}),
        stdio_mode=LocalProcessStdioMode.CAPTURE,
        lifecycle=LocalProcessLifecycle(),
    )


@unittest.skipUnless(_native_enabled(), "privileged Windows W3 acceptance is CI-only")
class WindowsNativeRuntimeAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate1_actual_child_token_attestation(
        self,
    ) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W2 setup acceptance requires elevation")
        account_api = _NativeWindowsSandboxAccountApi()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            state.mkdir()
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
            snapshot = authority.setup(setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            self.assertIsNotNone(snapshot.online_user_sid)
            self.assertIsNotNone(snapshot.offline_user_sid)
            self.assertIsNotNone(snapshot.write_restricting_sid)
            print("W3_STAGE=setup_ready", flush=True)
            executable = str(
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "whoami.exe"
            )

            async def run_probe(
                *,
                label: str,
                network: LocalProcessNetworkPolicy,
                expected_username: str,
                expected_sid: str,
            ) -> dict[str, object]:
                request = _request(
                    workspace=workspace,
                    network=network,
                    executable=executable,
                )
                adapter = WindowsNativeLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    state,
                    setup_authority=authority,
                    setup_request_factory=lambda _request: setup_request,
                    _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                    _diagnostic_create_no_window=True,
                )
                result: dict[str, object] = {
                    "probe": label,
                    "CreateProcessAsUser": "UNKNOWN",
                    "SpawnReady": "FAIL",
                    "stdout": "NOT_STARTED",
                    "stderr": "NOT_STARTED",
                    "child_exit": "NOT_STARTED",
                    "runner_exit": "NOT_STARTED",
                }
                process: OwnedLocalProcess | None = None
                combined: asyncio.Future[Any] | None = None
                captured_stdout = b""
                captured_stderr = b""
                try:
                    print(f"W3_STAGE={label.lower()}_spawn_start", flush=True)
                    process = await adapter.spawn(request)
                    print(f"W3_STAGE={label.lower()}_spawn_ready", flush=True)
                    result["CreateProcessAsUser"] = "PASS"
                    result["SpawnReady"] = "PASS"
                    combined = asyncio.gather(
                        asyncio.create_task(_read_stream(process.stdout)),
                        asyncio.create_task(_read_stream(process.stderr)),
                        asyncio.create_task(process.wait()),
                        return_exceptions=True,
                    )
                    try:
                        values = cast(
                            object,
                            await asyncio.wait_for(asyncio.shield(combined), timeout=10),
                        )
                    except TimeoutError:
                        result["error"] = "TIMEOUT"
                    else:
                        if isinstance(values, list) and len(values) == 3:
                            if isinstance(values[0], bytes):
                                captured_stdout = values[0]
                            if isinstance(values[1], bytes):
                                captured_stderr = values[1]
                            if isinstance(values[2], int):
                                result["child_exit"] = values[2]
                            elif isinstance(values[2], BaseException):
                                result["wait_error"] = type(values[2]).__name__
                        print(f"W3_STAGE={label.lower()}_streams_drained", flush=True)
                except TimeoutError:
                    result["error"] = "TIMEOUT"
                except BaseException as error:
                    result["error"] = type(error).__name__
                finally:
                    if process is not None and process.returncode is None:
                        with contextlib.suppress(BaseException):
                            await process.terminate(grace_seconds=0.5)
                    if combined is not None and not combined.done():
                        with contextlib.suppress(BaseException):
                            values = cast(object, await asyncio.wait_for(combined, timeout=2))
                            if isinstance(values, list) and len(values) == 3:
                                if isinstance(values[0], bytes):
                                    captured_stdout = values[0]
                                if isinstance(values[1], bytes):
                                    captured_stderr = values[1]
                                if isinstance(values[2], int):
                                    result["child_exit"] = values[2]
                    if process is not None and process.returncode is not None:
                        result["child_exit"] = process.returncode
                    result["stdout"] = "NONEMPTY" if captured_stdout else "EOF"
                    result["stderr"] = "NONEMPTY" if captured_stderr else "EOF"
                    result["stdout_preview"] = captured_stdout.decode(
                        "utf-8", errors="replace"
                    ).strip()[:512]
                    result["stderr_preview"] = captured_stderr.decode("utf-8", errors="replace")[
                        :512
                    ]
                    result["stdout_contains_expected"] = (
                        expected_username.casefold() in str(result["stdout_preview"]).casefold()
                    )
                    diagnostic = (
                        cast(Any, process).diagnostic_snapshot() if process is not None else None
                    )
                    result["diagnostic"] = diagnostic
                    if isinstance(diagnostic, dict):
                        result["security_attestation"] = diagnostic.get("security_attestation")
                        runner = diagnostic.get("runner")
                        if isinstance(runner, dict):
                            result["runner_state"] = runner.get("state")
                            result["runner_exit"] = runner.get("exit_code", "UNKNOWN")
                    attestation = result.get("security_attestation")
                    if isinstance(attestation, dict):
                        result["TokenUser"] = attestation.get("user_sid")
                        result["IsTokenRestricted"] = attestation.get("is_restricted")
                        result["TokenRestrictedSids"] = attestation.get("restricted_sids")
                        result["SeChangeNotifyPrivilege"] = attestation.get(
                            "change_notify_privilege_enabled"
                        )
                        result["unexpected_enabled_privilege_count"] = attestation.get(
                            "unexpected_enabled_privilege_count"
                        )
                    result["expected_sid"] = expected_sid
                    print(f"W3_STAGE={label.lower()}_probe_finished", flush=True)
                return result

            try:
                online = await run_probe(
                    label="ONLINE",
                    network=LocalProcessNetworkPolicy.INHERIT,
                    expected_username=SANDBOX_ONLINE_USERNAME,
                    expected_sid=cast(str, snapshot.online_user_sid),
                )
                offline = await run_probe(
                    label="OFFLINE",
                    network=LocalProcessNetworkPolicy.ISOLATED,
                    expected_username=SANDBOX_OFFLINE_USERNAME,
                    expected_sid=cast(str, snapshot.offline_user_sid),
                )
                probes = [online, offline]
                print(f"W3_GATE1_NATIVE_RESULTS={json.dumps(probes, sort_keys=True)}")

                for probe in probes:
                    attestation = probe.get("security_attestation")
                    runner_ok = (
                        probe.get("runner_state") == "RUNNER_EXITED"
                        and probe.get("runner_exit") == 0
                    )
                    if not (
                        probe.get("CreateProcessAsUser") == "PASS"
                        and probe.get("SpawnReady") == "PASS"
                        and probe.get("stdout_contains_expected") is True
                        and probe.get("child_exit") == 0
                        and runner_ok
                        and isinstance(attestation, dict)
                        and attestation.get("user_sid") == probe.get("expected_sid")
                        and attestation.get("is_restricted") is True
                        and tuple(attestation.get("restricted_sids", ()))
                        == (cast(str, snapshot.write_restricting_sid),)
                        and attestation.get("change_notify_privilege_enabled") is True
                        and attestation.get("unexpected_enabled_privilege_count") == 0
                    ):
                        self.fail(
                            "W3_GATE1_IDENTITY_TOKEN_BLOCKED " + json.dumps(probes, sort_keys=True)
                        )
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)


if __name__ == "__main__":
    unittest.main()
