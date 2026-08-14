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
import shutil
import sys
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
    WindowsSandboxIdentityKind,
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    _WindowsNativeDesktopMode,
    current_user_sid,
)
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

            # Gate 1 runs only after the whoami transport baseline has passed.
            # The probe is copied into the disposable workspace and therefore
            # executes as the actual CreateProcessAsUserW final child, not as
            # the trusted runner that created its restricted token.
            probe_source = Path(__file__).with_name("windows_token_probe.py")
            token_probe_path = workspace / "windows_token_probe.py"
            shutil.copyfile(probe_source, token_probe_path)
            records = {record.kind: record for record in authority.identity_records(setup_request)}
            online_record = records[WindowsSandboxIdentityKind.ONLINE]
            offline_record = records[WindowsSandboxIdentityKind.OFFLINE]
            controller_sid = current_user_sid()
            expected_restricted_sid = online_record.write_sid.value
            self.assertEqual(expected_restricted_sid, offline_record.write_sid.value)
            self.assertNotEqual(online_record.user_sid.value, controller_sid)
            self.assertNotEqual(offline_record.user_sid.value, controller_sid)

            async def run_gate1_probe(
                *,
                label: str,
                network: LocalProcessNetworkPolicy,
                expected_user_sid: str,
            ) -> dict[str, object]:
                request = _request(
                    workspace=workspace,
                    profile=SandboxProfile.WORKSPACE,
                    network=network,
                    stdio=LocalProcessStdioMode.CAPTURE,
                    arguments=("-I", str(token_probe_path)),
                    executable=sys.executable,
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
                    "child_exit": "NOT_STARTED",
                    "Exit": "NOT_STARTED",
                    "expected_user_sid": expected_user_sid,
                }
                process: OwnedLocalProcess | None = None
                try:
                    print(f"W3_STAGE={label.lower()}_spawn_start", flush=True)
                    process = await adapter.spawn(request)
                    print(f"W3_STAGE={label.lower()}_spawn_ready", flush=True)
                    result["CreateProcessAsUser"] = "PASS"
                    result["SpawnReady"] = "PASS"
                    stream = process.stdout
                    if stream is None:
                        raise AssertionError("Gate 1 probe has no stdout stream")
                    output = await asyncio.wait_for(stream.read(65_536), timeout=15)
                    result["stdout"] = "NONEMPTY" if output else "EOF"
                    if output:
                        try:
                            decoded = json.loads(output.decode("utf-8", errors="strict"))
                        except (UnicodeError, json.JSONDecodeError):
                            result["token_probe"] = {"error": "INVALID_JSON"}
                        else:
                            result["token_probe"] = decoded
                    print(f"W3_STAGE={label.lower()}_stdout_read", flush=True)
                    await asyncio.wait_for(process.wait(), timeout=15)
                    result["child_exit"] = process.returncode
                    result["Exit"] = "PASS"
                    print(f"W3_STAGE={label.lower()}_child_waited", flush=True)
                except TimeoutError:
                    result["error"] = "TIMEOUT"
                except BaseException as error:
                    result["error"] = type(error).__name__
                finally:
                    if process is not None:
                        with contextlib.suppress(BaseException):
                            await process.terminate(grace_seconds=0.5)
                    diagnostic = (
                        cast(Any, process).diagnostic_snapshot() if process is not None else None
                    )
                    result["diagnostic_after_cleanup"] = diagnostic
                    if isinstance(diagnostic, dict):
                        result["runner_final"] = diagnostic.get("runner")
                        result["control_close"] = diagnostic.get("control_pipe")
                        result["event_close"] = diagnostic.get("event_pipe")
                    print(f"W3_STAGE={label.lower()}_probe_finished", flush=True)
                return result

            online_probe = await run_gate1_probe(
                label="GATE1_ONLINE",
                network=LocalProcessNetworkPolicy.INHERIT,
                expected_user_sid=online_record.user_sid.value,
            )
            offline_probe = await run_gate1_probe(
                label="GATE1_OFFLINE",
                network=LocalProcessNetworkPolicy.ISOLATED,
                expected_user_sid=offline_record.user_sid.value,
            )
            gate1_results = [online_probe, offline_probe]
            print(f"W3_GATE1_RESULTS={json.dumps(gate1_results, sort_keys=True)}", flush=True)

            def assert_gate1_result(
                result: dict[str, object],
                *,
                expected_user_sid: str,
            ) -> None:
                self.assertEqual(result.get("CreateProcessAsUser"), "PASS")
                self.assertEqual(result.get("SpawnReady"), "PASS")
                self.assertEqual(result.get("stdout"), "NONEMPTY")
                self.assertEqual(result.get("child_exit"), 0)
                self.assertEqual(result.get("Exit"), "PASS")
                runner_final = result.get("runner_final")
                self.assertIsInstance(runner_final, dict)
                self.assertEqual(runner_final.get("state"), "RUNNER_EXITED")
                self.assertEqual(runner_final.get("exit_code"), 0)
                token = result.get("token_probe")
                self.assertIsInstance(token, dict)
                assert isinstance(token, dict)
                self.assertEqual(token.get("user_sid"), expected_user_sid)
                self.assertNotEqual(token.get("user_sid"), controller_sid)
                self.assertTrue(token.get("is_restricted"))
                self.assertEqual(token.get("restricted_sids"), [expected_restricted_sid])
                self.assertTrue(token.get("change_notify"))
                self.assertEqual(token.get("unexpected_enabled_privileges"), [])
                logon_sid = token.get("logon_sid")
                self.assertIsInstance(logon_sid, str)
                self.assertNotIn(logon_sid, token.get("restricted_sids", []))
                self.assertNotIn(online_record.user_sid.value, token.get("restricted_sids", []))
                self.assertNotIn(offline_record.user_sid.value, token.get("restricted_sids", []))
                self.assertNotIn(controller_sid, token.get("restricted_sids", []))

            assert_gate1_result(online_probe, expected_user_sid=online_record.user_sid.value)
            assert_gate1_result(offline_probe, expected_user_sid=offline_record.user_sid.value)
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
