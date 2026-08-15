"""Focused W4 Gate 1 acceptance for the restricted ConPTY vertical slice."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir, mkdtemp
from typing import Any, cast

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.windows_sandbox import WindowsSandboxSetupRequest
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.terminal.models import TerminalSize
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import _WindowsNativeDesktopMode
from neuro_code.infrastructure.sandbox.windows_native_runtime_protocol import (
    RuntimeFrameType,
    encode_frame,
    encode_json,
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
    _NativeWindowsSetupPrivilegeApi,
)


class _NativeProbeBuildError(RuntimeError):
    pass


def _compile_probe() -> Path:  # pragma: no cover - Windows CI
    source = Path(__file__).with_name("windows_conpty_probe.c").resolve(strict=False)
    if not source.is_file():
        raise _NativeProbeBuildError("ConPTY probe source is unavailable")
    vswhere = shutil.which("vswhere.exe")
    if vswhere is None:
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
        if program_files_x86:
            candidate = (
                Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
            )
            if candidate.is_file():
                vswhere = str(candidate)
    if vswhere is None:
        raise _NativeProbeBuildError("vswhere.exe is unavailable")
    discovery = subprocess.run(
        [
            vswhere,
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    if discovery.returncode != 0:
        raise _NativeProbeBuildError("vswhere did not find an MSVC installation")
    installation = next(
        (Path(line.strip()) for line in discovery.stdout.splitlines() if line.strip()),
        None,
    )
    if installation is None:
        raise _NativeProbeBuildError("vswhere returned no installation path")
    vcvars = installation / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise _NativeProbeBuildError("vcvars64.bat is unavailable")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", gettempdir()))
    build_dir = Path(mkdtemp(prefix="neuro-code-w4-", dir=runner_temp))
    output = build_dir / "windows_conpty_probe.exe"
    command = build_dir / "build_probe.cmd"
    command.write_text(
        "@echo off\r\n"
        f'call "{vcvars}"\r\n'
        "if errorlevel 1 exit /b 1\r\n"
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}"\r\n',
        encoding="ascii",
    )
    build = subprocess.run(
        ["cmd.exe", "/d", "/c", command.name],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        shell=False,
        cwd=str(build_dir),
    )
    if build.returncode != 0 or not output.is_file():
        diagnostic = (build.stderr or build.stdout or "").strip()[:512]
        shutil.rmtree(build_dir, ignore_errors=True)
        raise _NativeProbeBuildError(f"MSVC probe build failed: {diagnostic}")
    return output


def _request(workspace: Path, *, offline: bool, probe: Path) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        str(probe),
        (),
        purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
        cwd=workspace,
        sandbox_profile=SandboxProfile.WORKSPACE,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),)
        ),
        network_policy=(
            LocalProcessNetworkPolicy.ISOLATED if offline else LocalProcessNetworkPolicy.INHERIT
        ),
        environment_policy=LocalProcessEnvironmentPolicy({}),
        stdio_mode=LocalProcessStdioMode.PTY,
        lifecycle=LocalProcessLifecycle(
            required_capability=LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
        ),
    )


class WindowsNativePtyAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """W4 acceptance runs only on an elevated Windows runner."""

    @classmethod
    def setUpClass(cls) -> None:  # pragma: no cover - Windows CI
        if os.name != "nt":
            raise unittest.SkipTest("W4 native acceptance requires Windows")
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        if not privilege_api.is_administrator():
            raise unittest.SkipTest("W4 setup acceptance requires elevation")

    async def _run_gate1(self, *, offline: bool) -> dict[str, object]:  # pragma: no cover
        compiled = await asyncio.to_thread(_compile_probe)

        async def cleanup_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, compiled.parent, ignore_errors=True)

        self.addAsyncCleanup(cleanup_probe)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            probe = workspace / "windows-conpty-probe.exe"
            shutil.copy2(compiled, probe)
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
                account_api=_NativeWindowsSandboxAccountApi(),
                privilege_api=_NativeWindowsSetupPrivilegeApi(),
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state.value, "ready")
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                runtime_state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
                _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                _diagnostic_create_no_window=False,
            )
            output = bytearray()
            output_changed = asyncio.Event()
            errors: list[BaseException] = []
            loop = asyncio.get_running_loop()

            def on_output(data: bytes) -> None:
                output.extend(data)
                loop.call_soon_threadsafe(output_changed.set)

            def on_eof() -> None:
                loop.call_soon_threadsafe(output_changed.set)

            def on_error(error: BaseException) -> None:
                errors.append(error)
                loop.call_soon_threadsafe(output_changed.set)

            session = None
            try:
                session = await asyncio.to_thread(
                    adapter._spawn_terminal_candidate,
                    _request(workspace, offline=offline, probe=probe),
                    size=TerminalSize(80, 25),
                    on_output=on_output,
                    on_eof=on_eof,
                    on_error=on_error,
                )

                async def wait_for(marker: bytes, timeout: float = 8.0) -> bool:  # noqa: ASYNC109
                    needle = marker.rstrip(b"\n")
                    deadline = asyncio.get_running_loop().time() + timeout
                    while needle not in output and asyncio.get_running_loop().time() < deadline:
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(output_changed.wait(), timeout=0.25)
                        output_changed.clear()
                    return needle in output

                ready = await wait_for(b"W4_READY\n")
                if not ready:
                    print(
                        "W4_GATE1_BLOCKER="
                        + json.dumps(
                            {
                                "classification": "PTY_CHILD_OUTPUT_STARTUP_BLOCKER",
                                "mode": "offline" if offline else "online",
                                "child_exit": session.poll_exit(),
                                "output_bytes": len(output),
                                "output_preview": bytes(output[:128]).hex(),
                                "error_types": [type(error).__name__ for error in errors],
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                self.assertTrue(ready)
                self.assertTrue(await wait_for(b"W4_SIZE=80x25\n"))
                await asyncio.to_thread(session.write, b"w4-input-token\r")
                self.assertTrue(await wait_for(b"W4_INPUT=w4-input-token\n"))
                await asyncio.to_thread(session.resize, TerminalSize(120, 40))
                await asyncio.to_thread(session.write, b"w4-size\r")
                self.assertTrue(await wait_for(b"W4_SIZE=120x40\n"))
                await asyncio.to_thread(session.write, b"w4-exit\r")
                self.assertTrue(await wait_for(b"W4_FINAL\n"))
                deadline = asyncio.get_running_loop().time() + 8.0
                while (  # noqa: ASYNC110
                    session.poll_exit() is None and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.02)
                self.assertEqual(session.poll_exit(), 7)
                self.assertEqual(errors, [])
                return {
                    "mode": "offline" if offline else "online",
                    "create_process_with_logon": "PASS",
                    "restricted_token": "PASS",
                    "create_pseudo_console": "PASS",
                    "create_process_as_user": "PASS",
                    "token_attestation": "PASS",
                    "spawn_ready": "PASS",
                    "initial_size": "80x25",
                    "input": "PASS",
                    "resize": "120x40",
                    "final_tail": "PASS",
                    "exit_code": session.poll_exit(),
                    "lifecycle": session.lifecycle_capability.value,
                    "runner": "PASS",
                }
            finally:
                if session is not None:
                    await asyncio.to_thread(session.close)
                with contextlib.suppress(BaseException):
                    await asyncio.to_thread(authority.cleanup, setup_request)

    async def test_online_restricted_conpty_gate1(self) -> None:  # pragma: no cover - Windows CI
        result = await self._run_gate1(offline=False)
        print("W4_GATE1_ONLINE=" + json.dumps(result, sort_keys=True), flush=True)

    async def test_offline_restricted_conpty_gate1(self) -> None:  # pragma: no cover - Windows CI
        result = await self._run_gate1(offline=True)
        print("W4_GATE1_OFFLINE=" + json.dumps(result, sort_keys=True), flush=True)

    async def test_malformed_resize_fails_closed(self) -> None:  # pragma: no cover - Windows CI
        compiled = await asyncio.to_thread(_compile_probe)

        async def cleanup_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, compiled.parent, ignore_errors=True)

        self.addAsyncCleanup(cleanup_probe)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, installation, runtime_state = (
                root / "workspace",
                root / "installation",
                root / "runtime-state",
            )
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            probe = workspace / "windows-conpty-probe.exe"
            shutil.copy2(compiled, probe)
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
                account_api=_NativeWindowsSandboxAccountApi(),
                privilege_api=_NativeWindowsSetupPrivilegeApi(),
            )
            await asyncio.to_thread(authority.setup, setup_request)
            errors: list[BaseException] = []
            session = None
            try:
                session = await asyncio.to_thread(
                    WindowsNativeLocalProcessSandbox(
                        SandboxProfile.WORKSPACE,
                        workspace,
                        runtime_state,
                        setup_authority=authority,
                        setup_request_factory=lambda _request: setup_request,
                    )._spawn_terminal_candidate,
                    _request(workspace, offline=False, probe=probe),
                    size=TerminalSize(80, 25),
                    on_output=lambda _data: None,
                    on_eof=lambda: None,
                    on_error=errors.append,
                )
                cast(Any, session)._control.write(
                    encode_frame(
                        RuntimeFrameType.RESIZE,
                        encode_json({"version": 1, "columns": 0, "rows": 40}),
                    )
                )
                deadline = asyncio.get_running_loop().time() + 8.0
                while not errors and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
                self.assertTrue(errors)
                print(
                    "W4_GATE1_FAILURE="
                    + json.dumps({"classification": "MALFORMED_RESIZE_FAIL_CLOSED"}),
                    flush=True,
                )
            finally:
                if session is not None:
                    await asyncio.to_thread(session.close)
                with contextlib.suppress(BaseException):
                    await asyncio.to_thread(authority.cleanup, setup_request)


if __name__ == "__main__":
    unittest.main()
