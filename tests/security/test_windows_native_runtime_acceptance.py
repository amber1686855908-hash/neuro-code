"""Focused W3 acceptance for the directional Windows runtime transport.

This gate proves the PRIVATE_DESKTOP/CREATE_NO_WINDOW ``whoami.exe``
transport baseline and the minimal final-child Python startup contract.
Filesystem, network, token, protocol, and descendant gates remain separate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
    WindowsTrustedRunnerProvenance,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    _WindowsNativeDesktopMode,
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


async def _read_canary_stream(stream: object | None) -> bytes:
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

            try:
                canary_script = "import sys; print('PYTHON_CANARY_OK', flush=True)"

                async def run_python_canary(
                    *,
                    label: str,
                    executable_kind: str,
                    executable: str,
                    arguments: tuple[str, ...],
                ) -> dict[str, object]:
                    request = _request(
                        workspace=workspace,
                        profile=SandboxProfile.WORKSPACE,
                        network=LocalProcessNetworkPolicy.INHERIT,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=arguments,
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
                        "canary": label,
                        "executable_kind": executable_kind,
                        "CreateProcessAsUser": "UNKNOWN",
                        "SpawnReady": "FAIL",
                        "stdout": "NOT_STARTED",
                        "stdout_preview": "",
                        "stderr_preview": "",
                        "child_exit": "NOT_STARTED",
                        "child_state_at_timeout": "NOT_APPLICABLE",
                        "Exit": "NOT_STARTED",
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
                        stdout_task = asyncio.create_task(_read_canary_stream(process.stdout))
                        stderr_task = asyncio.create_task(_read_canary_stream(process.stderr))
                        wait_task = asyncio.create_task(process.wait())
                        combined = asyncio.gather(
                            stdout_task,
                            stderr_task,
                            wait_task,
                            return_exceptions=True,
                        )
                        try:
                            values = await asyncio.wait_for(asyncio.shield(combined), timeout=10)
                        except TimeoutError:
                            result["error"] = "TIMEOUT"
                            with contextlib.suppress(BaseException):
                                diagnostic = cast(Any, process).diagnostic_snapshot()
                                result["diagnostic_at_timeout"] = diagnostic
                                if isinstance(diagnostic, dict):
                                    child = diagnostic.get("child")
                                    if isinstance(child, dict):
                                        result["child_state_at_timeout"] = child.get(
                                            "state", "UNKNOWN"
                                        )
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
                        result["stdout_preview"] = captured_stdout.decode(
                            "utf-8", errors="replace"
                        ).strip()[:512]
                        result["stdout_contains_expected"] = (
                            result["stdout_preview"] == "PYTHON_CANARY_OK"
                        )
                        result["stderr_preview"] = captured_stderr.decode(
                            "utf-8", errors="replace"
                        )[:512]
                        diagnostic = (
                            cast(Any, process).diagnostic_snapshot()
                            if process is not None
                            else None
                        )
                        result["diagnostic_after_cleanup"] = diagnostic
                        if isinstance(diagnostic, dict):
                            result["runner_final"] = diagnostic.get("runner")
                            result["control_close"] = diagnostic.get("control_pipe")
                            result["event_close"] = diagnostic.get("event_pipe")
                        print(f"W3_STAGE={label.lower()}_canary_finished", flush=True)
                    return result

                def canary_passed(result: dict[str, object]) -> bool:
                    runner = result.get("runner_final")
                    return (
                        result.get("CreateProcessAsUser") == "PASS"
                        and result.get("SpawnReady") == "PASS"
                        and result.get("stdout_preview") == "PYTHON_CANARY_OK"
                        and result.get("stdout_contains_expected") is True
                        and result.get("child_exit") == 0
                        and result.get("Exit") == "PASS"
                        and isinstance(runner, dict)
                        and runner.get("state") == "RUNNER_EXITED"
                        and runner.get("exit_code") == 0
                    )

                def resolve_base_interpreter() -> dict[str, object]:
                    raw_base = getattr(sys, "_base_executable", "")
                    if not isinstance(raw_base, str) or not raw_base:
                        return {
                            "status": "BASE_INTERPRETER_UNAVAILABLE",
                            "reason": "sys._base_executable is empty",
                        }
                    try:
                        base_path = Path(raw_base).expanduser().resolve(strict=True)
                    except (OSError, RuntimeError, ValueError) as error:
                        return {
                            "status": "BASE_INTERPRETER_UNAVAILABLE",
                            "reason": type(error).__name__,
                        }
                    if not base_path.is_absolute() or not base_path.is_file():
                        return {
                            "status": "BASE_INTERPRETER_UNAVAILABLE",
                            "reason": "base interpreter is not an absolute file",
                        }
                    workspace_path = workspace.resolve()
                    try:
                        base_path.relative_to(workspace_path)
                    except ValueError:
                        pass
                    else:
                        return {
                            "status": "BASE_INTERPRETER_UNAVAILABLE",
                            "reason": "base interpreter overlaps model-writable workspace",
                            "path": str(base_path),
                        }
                    try:
                        WindowsTrustedRunnerProvenance.resolve().assert_disjoint((workspace,))
                    except BaseException as error:
                        return {
                            "status": "BASE_INTERPRETER_UNAVAILABLE",
                            "reason": f"provenance:{type(error).__name__}",
                            "path": str(base_path),
                        }
                    return {
                        "status": "PASS",
                        "path": str(base_path),
                        "provenance": "PASS",
                    }

                canary_results: list[dict[str, object]] = []
                canary_a = await run_python_canary(
                    label="PYTHON_CANARY_A",
                    executable_kind="current_sys_executable",
                    executable=sys.executable,
                    arguments=("-I", "-c", canary_script),
                )
                canary_results.append(canary_a)
                if canary_passed(canary_a):
                    print(f"W3_PYTHON_CANARY_RESULTS={json.dumps(canary_results, sort_keys=True)}")
                    return

                canary_b = await run_python_canary(
                    label="PYTHON_CANARY_B",
                    executable_kind="current_sys_executable",
                    executable=sys.executable,
                    arguments=("-I", "-S", "-B", "-c", "print('PYTHON_CANARY_OK', flush=True)"),
                )
                canary_results.append(canary_b)
                if canary_passed(canary_b):
                    print(f"W3_PYTHON_CANARY_RESULTS={json.dumps(canary_results, sort_keys=True)}")
                    return

                base_info = resolve_base_interpreter()
                if base_info.get("status") == "PASS":
                    canary_c = await run_python_canary(
                        label="PYTHON_CANARY_C",
                        executable_kind="base_interpreter",
                        executable=str(base_info["path"]),
                        arguments=(
                            "-I",
                            "-S",
                            "-B",
                            "-c",
                            "print('PYTHON_CANARY_OK', flush=True)",
                        ),
                    )
                else:
                    canary_c = {
                        "canary": "PYTHON_CANARY_C",
                        "executable_kind": "base_interpreter",
                        "result": "NOT_RUN",
                        "child_state_at_timeout": "NOT_APPLICABLE",
                        **base_info,
                    }
                canary_results.append(canary_c)
                print(
                    "W3_PYTHON_CANARY_RESULTS="
                    + json.dumps({"base": base_info, "canaries": canary_results}, sort_keys=True)
                )
                self.fail(
                    "RESTRICTED_CHILD_PYTHON_RUNTIME_COMPATIBILITY "
                    + json.dumps({"base": base_info, "canaries": canary_results}, sort_keys=True)
                )
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)


if __name__ == "__main__":
    unittest.main()
