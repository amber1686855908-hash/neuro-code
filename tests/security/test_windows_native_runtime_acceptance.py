"""Focused W3 acceptance for the directional Windows runtime transport.

This gate proves the PRIVATE_DESKTOP/CREATE_NO_WINDOW ``whoami.exe``
transport baseline and the final-child identity-token contract.  Filesystem,
network, protocol, and descendant gates remain separate until the transport
has proved their respective lifecycle and framing contracts.
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

_GATE1_MARKERS = {
    "PYTHON_STARTED",
    "CTYPES_IMPORTED",
    "TOKEN_OPENED",
    "TOKEN_USER_OK",
    "RESTRICTED_SIDS_OK",
    "IS_RESTRICTED_OK",
    "PRIVILEGES_OK",
    "JSON_WRITTEN",
}
_GATE1_STAGE_AFTER_MARKER = {
    "PYTHON_STARTED": "CTYPES_IMPORT",
    "CTYPES_IMPORTED": "OPEN_PROCESS_TOKEN",
    "TOKEN_OPENED": "TOKEN_USER",
    "TOKEN_USER_OK": "TOKEN_RESTRICTED_SIDS",
    "RESTRICTED_SIDS_OK": "IS_TOKEN_RESTRICTED",
    "IS_RESTRICTED_OK": "TOKEN_PRIVILEGES",
    "PRIVILEGES_OK": "JSON_WRITE",
    "JSON_WRITTEN": "COMPLETE",
}


def _gate1_stderr_markers(data: bytes) -> list[str]:
    """Extract only the probe's fixed, bounded diagnostic markers."""

    markers: list[str] = []
    text = data.decode("ascii", errors="ignore")
    for fragment in text.split("G1_PROBE=")[1:]:
        marker = fragment.splitlines()[0].strip()
        if marker in _GATE1_MARKERS and len(markers) < len(_GATE1_MARKERS):
            markers.append(marker)
    return markers


def _gate1_probe_stage(markers: list[str]) -> str:
    if not markers:
        return "PYTHON_STARTUP"
    return _GATE1_STAGE_AFTER_MARKER[markers[-1]]


async def _read_gate1_stream(stream: object | None) -> bytes:
    if stream is None:
        return b""
    return await cast(Any, stream).read(65_536)


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
                combined: asyncio.Future[Any] | None = None
                captured_stdout = b""
                captured_stderr = b""

                def record_stream_results(values: object) -> None:
                    nonlocal captured_stdout, captured_stderr
                    if not isinstance(values, list) or len(values) != 3:
                        return
                    if isinstance(values[0], bytes):
                        captured_stdout = values[0]
                    if isinstance(values[1], bytes):
                        captured_stderr = values[1]
                    wait_result = values[2]
                    if isinstance(wait_result, int):
                        result["child_exit"] = wait_result
                        result["Exit"] = "PASS" if wait_result == 0 else "FAIL"
                    elif isinstance(wait_result, BaseException):
                        result["wait_error"] = type(wait_result).__name__

                try:
                    print(f"W3_STAGE={label.lower()}_spawn_start", flush=True)
                    process = await adapter.spawn(request)
                    print(f"W3_STAGE={label.lower()}_spawn_ready", flush=True)
                    result["CreateProcessAsUser"] = "PASS"
                    result["SpawnReady"] = "PASS"
                    stdout_task = asyncio.create_task(_read_gate1_stream(process.stdout))
                    stderr_task = asyncio.create_task(_read_gate1_stream(process.stderr))
                    wait_task = asyncio.create_task(process.wait())
                    combined = asyncio.gather(
                        stdout_task,
                        stderr_task,
                        wait_task,
                        return_exceptions=True,
                    )
                    try:
                        values = await asyncio.wait_for(asyncio.shield(combined), timeout=15)
                    except TimeoutError:
                        result["error"] = "TIMEOUT"
                    else:
                        record_stream_results(values)
                        print(f"W3_STAGE={label.lower()}_streams_drained", flush=True)
                except TimeoutError:
                    result["error"] = "TIMEOUT"
                except BaseException as error:
                    result["error"] = type(error).__name__
                finally:
                    if process is not None:
                        with contextlib.suppress(BaseException):
                            await process.terminate(grace_seconds=0.5)
                    if combined is not None and not combined.done():
                        with contextlib.suppress(BaseException):
                            values = await asyncio.wait_for(combined, timeout=2)
                            record_stream_results(values)
                    elif combined is not None:
                        with contextlib.suppress(BaseException):
                            record_stream_results(combined.result())
                    if process is not None and process.returncode is not None:
                        result["child_exit"] = process.returncode
                        if result.get("Exit") == "NOT_STARTED":
                            result["Exit"] = "PASS" if process.returncode == 0 else "FAIL"
                    result["stdout"] = "NONEMPTY" if captured_stdout else "EOF"
                    if captured_stdout:
                        try:
                            decoded = json.loads(captured_stdout.decode("utf-8", errors="strict"))
                        except (UnicodeError, json.JSONDecodeError):
                            result["token_probe"] = {"error": "INVALID_JSON"}
                        else:
                            result["token_probe"] = decoded
                    markers = _gate1_stderr_markers(captured_stderr)
                    result["stderr_markers"] = markers
                    result["probe_stage"] = _gate1_probe_stage(markers)
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
                self.assertEqual(token.get("unexpected_enabled_privilege_count"), 0)
                restricted_sids = token.get("restricted_sids")
                self.assertIsInstance(restricted_sids, list)
                assert isinstance(restricted_sids, list)
                self.assertNotIn("S-1-1-0", restricted_sids)
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
