"""Focused W3 acceptance for the directional Windows runtime transport.

This gate intentionally stops after the PRIVATE_DESKTOP/CREATE_NO_WINDOW
``whoami.exe`` baseline.  Filesystem, network, protocol, and descendant gates
remain separate until the transport has proved its lifecycle and framing
contract.
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
from neuro_code.infrastructure.sandbox.windows_native_runner import _WindowsNativeDesktopMode
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
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


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


def _request(
    *,
    workspace: Path,
    profile: SandboxProfile,
    network: LocalProcessNetworkPolicy,
    stdio: LocalProcessStdioMode,
    arguments: tuple[str, ...],
    executable: str,
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        executable,
        arguments,
        purpose=LocalProcessPurpose.BASH,
        cwd=workspace,
        sandbox_profile=profile,
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
        stdio_mode=stdio,
        lifecycle=LocalProcessLifecycle(),
    )


@unittest.skipUnless(_native_enabled(), "privileged Windows W3 acceptance is CI-only")
class WindowsNativeRuntimeAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_directional_transport_probe_a(self) -> None:  # pragma: no cover - Windows CI
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
            self.assertEqual(authority.setup(setup_request).state, WindowsSandboxSetupState.READY)
            print("W3_STAGE=setup_ready", flush=True)
            executable = str(
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "whoami.exe"
            )
            whoami_request = _request(
                workspace=workspace,
                profile=SandboxProfile.WORKSPACE,
                network=LocalProcessNetworkPolicy.INHERIT,
                stdio=LocalProcessStdioMode.CAPTURE,
                arguments=(),
                executable=executable,
            )

            async def run_probe() -> dict[str, object]:
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
                    "probe": "A_PRIVATE_DESKTOP_CREATE_NO_WINDOW",
                    "CreateProcessAsUser": "UNKNOWN",
                    "SpawnReady": "FAIL",
                    "stdout": "NOT_STARTED",
                    "child_exit": "NOT_STARTED",
                    "Exit": "NOT_STARTED",
                }
                process: OwnedLocalProcess | None = None
                try:
                    print("W3_STAGE=spawn_start", flush=True)
                    process = await adapter.spawn(whoami_request)
                    print("W3_STAGE=spawn_ready", flush=True)
                    result["CreateProcessAsUser"] = "PASS"
                    result["SpawnReady"] = "PASS"
                    stream = process.stdout
                    if stream is None:
                        raise AssertionError("W3 probe has no stdout stream")
                    try:
                        output = await asyncio.wait_for(stream.read(65_536), timeout=10)
                        output_text = output.decode("utf-8", errors="replace").strip()
                        result["stdout"] = "NONEMPTY" if output else "EOF"
                        result["stdout_contains_expected"] = (
                            SANDBOX_ONLINE_USERNAME.casefold() in output_text.casefold()
                        )
                        result["stdout_preview"] = output_text[:256]
                        print("W3_STAGE=stdout_read", flush=True)
                    except TimeoutError:
                        result["stdout"] = "TIMEOUT"
                    except BaseException as error:
                        result["stdout"] = f"ERROR:{type(error).__name__}"
                    if result["stdout"] != "TIMEOUT":
                        try:
                            await asyncio.wait_for(process.wait(), timeout=10)
                            result["child_exit"] = process.returncode
                            result["Exit"] = "PASS"
                            print("W3_STAGE=child_waited", flush=True)
                        except TimeoutError:
                            result["child_exit"] = "TIMEOUT"
                            result["Exit"] = "TIMEOUT"
                        except BaseException as error:
                            result["child_exit"] = f"ERROR:{type(error).__name__}"
                            result["Exit"] = f"ERROR:{type(error).__name__}"
                    else:
                        result["child_exit"] = "TIMEOUT"
                        result["Exit"] = "TIMEOUT"
                except BaseException as error:
                    result["error"] = str(error)[:512]
                finally:
                    if process is not None:
                        with contextlib.suppress(BaseException):
                            await process.terminate(grace_seconds=0.5)
                    diagnostic = (
                        cast(Any, process).diagnostic_snapshot() if process is not None else None
                    )
                    result["diagnostic_after_cleanup"] = diagnostic
                    if isinstance(diagnostic, dict):
                        runner = diagnostic.get("runner")
                        result["runner_final"] = runner
                        result["control_close"] = diagnostic.get("control_pipe")
                        result["event_close"] = diagnostic.get("event_pipe")
                    print("W3_STAGE=probe_finished", flush=True)
                return result

            probe = await run_probe()
            print(f"W3_PROBE_RESULTS={json.dumps([probe], sort_keys=True)}")
            runner_final = probe.get("runner_final")
            if (
                probe.get("stdout") != "NONEMPTY"
                or probe.get("stdout_contains_expected") is not True
                or probe.get("child_exit") != 0
                or probe.get("Exit") != "PASS"
                or not (
                    isinstance(runner_final, dict)
                    and runner_final.get("state") == "RUNNER_EXITED"
                    and runner_final.get("exit_code") == 0
                )
            ):
                self.fail(
                    "W3_IPC_DIRECTIONAL_TRANSPORT_BLOCKED " + json.dumps([probe], sort_keys=True)
                )
            try:
                self.assertEqual(
                    authority.inspect(setup_request).state,
                    WindowsSandboxSetupState.READY,
                )
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)


if __name__ == "__main__":
    unittest.main()
