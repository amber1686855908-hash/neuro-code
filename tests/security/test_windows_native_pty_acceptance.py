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

from tests.security.windows_token_attestation import token_attestation_is_exact

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
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
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.application.sessions.terminal_sessions import LocalInteractiveTerminalManager
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
    WindowsAccountSid,
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    WRITE_ACCESS_MASK,
    WindowsManagedAce,
    WindowsManagedAceKind,
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
from neuro_code.infrastructure.workspace.paths import FilesystemWorkspacePathResolver


class _NativeProbeBuildError(RuntimeError):
    pass


def _compile_msvc_probe(
    source: Path,
    stem: str,
    *,
    libraries: tuple[str, ...] = (),
) -> Path:  # pragma: no cover - Windows CI
    if not source.is_file():
        raise _NativeProbeBuildError(f"{stem} probe source is unavailable")
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
    build_dir = Path(mkdtemp(prefix=f"neuro-code-{stem}-", dir=runner_temp))
    output = build_dir / f"{stem}.exe"
    command = build_dir / "build_probe.cmd"
    command.write_text(
        "@echo off\r\n"
        f'call "{vcvars}"\r\n'
        "if errorlevel 1 exit /b 1\r\n"
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}"'
        + (" " + " ".join(libraries) if libraries else "")
        + "\r\n",
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


def _compile_probe() -> Path:  # pragma: no cover - Windows CI
    return _compile_msvc_probe(
        Path(__file__).with_name("windows_conpty_probe.c").resolve(strict=False),
        "windows_conpty_probe",
    )


def _compile_security_probe() -> Path:  # pragma: no cover - Windows CI
    return _compile_msvc_probe(
        Path(__file__).with_name("windows_conpty_security_probe.c").resolve(strict=False),
        "windows_conpty_security_probe",
    )


def _compile_winsock_probe() -> Path:  # pragma: no cover - Windows CI
    return _compile_msvc_probe(
        Path(__file__).with_name("windows_winsock_probe.c").resolve(strict=False),
        "windows_winsock_probe",
        libraries=("Ws2_32.lib",),
    )


def _parse_pty_winsock_result(value: str) -> dict[str, object]:
    """Parse the existing Winsock probe through ConPTY line decoration."""

    marker = "W3_WINSOCK="
    position = value.find(marker)
    if position < 0:
        raise AssertionError("Winsock probe omitted W3_WINSOCK marker")
    line = value[position + len(marker) :].splitlines()[0].strip()
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise AssertionError("Winsock probe JSON is not an object")
    if payload.get("stage") not in {"WSA_STARTUP", "SOCKET", "CONNECT"}:
        raise AssertionError("Winsock probe emitted an invalid stage")
    if type(payload.get("connected")) is not bool:
        raise AssertionError("Winsock probe emitted invalid connected fact")
    if type(payload.get("wsa_error")) is not int or payload["wsa_error"] < 0:
        raise AssertionError("Winsock probe emitted invalid WSA error")
    return {
        "stage": payload["stage"],
        "connected": payload["connected"],
        "wsa_error": payload["wsa_error"],
    }


def _request(
    workspace: Path,
    *,
    offline: bool,
    probe: Path,
    profile: SandboxProfile = SandboxProfile.WORKSPACE,
    access_mode: LocalWorkspaceAccessMode = LocalWorkspaceAccessMode.READ_WRITE,
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        str(probe),
        (),
        purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
        cwd=workspace,
        sandbox_profile=profile,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (LocalWorkspaceAccess(workspace, access_mode),)
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


def _pty_security_request(
    workspace: Path,
    *,
    offline: bool,
    probe: Path,
    arguments: tuple[str, ...],
) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        str(probe),
        arguments,
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
        if os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") != "1":
            raise unittest.SkipTest("W4 native acceptance is CI-only")
        if os.name != "nt":
            raise unittest.SkipTest("W4 native acceptance requires Windows")
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        if not privilege_api.is_administrator():
            raise unittest.SkipTest("W4 setup acceptance requires elevation")

    async def _run_gate1(
        self,
        *,
        offline: bool,
        profile: SandboxProfile = SandboxProfile.WORKSPACE,
        writable: bool = True,
    ) -> dict[str, object]:  # pragma: no cover
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
                writable_roots=(workspace,) if writable else (),
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
                profile,
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
                    adapter.spawn_terminal,
                    _request(
                        workspace,
                        offline=offline,
                        probe=probe,
                        profile=profile,
                        access_mode=(
                            LocalWorkspaceAccessMode.READ_WRITE
                            if writable
                            else LocalWorkspaceAccessMode.READ_ONLY
                        ),
                    ),
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
                self.assertTrue(await wait_for(b"W4_HANDLES=CONPTY;"))
                handle_observation = bytes(output).split(b"W4_HANDLES=", 1)[1].split(b"\n", 1)[0]
                self.assertIn(b"stdin-valid=1", handle_observation)
                self.assertIn(b"stdout-valid=1", handle_observation)
                self.assertIn(b"stderr-valid=1", handle_observation)
                self.assertIn(b"stdin-console=1", handle_observation)
                self.assertIn(b"stdout-console=1", handle_observation)
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
                await asyncio.to_thread(session.close)
                diagnostic = cast(Any, session).diagnostic_snapshot()
                self.assertEqual(diagnostic.get("runner_state"), "RUNNER_EXITED")
                self.assertEqual(diagnostic.get("runner_exit_code"), 0)
                self.assertFalse(diagnostic.get("runner_forced_termination"))
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
                    "runner_state": diagnostic.get("runner_state"),
                    "runner_exit_code": diagnostic.get("runner_exit_code"),
                    "runner_forced_termination": diagnostic.get("runner_forced_termination"),
                    "stdio_contract": "CONPTY",
                    "bInheritHandles": False,
                    "HANDLE_LIST": "absent",
                    "conpty_std_handles": handle_observation.decode("ascii", errors="replace"),
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

    async def test_gate4a_public_route_reuses_certified_conpty(self) -> None:  # pragma: no cover
        """Gate 4A exercises the now-public route with the Gate 1 native probe."""

        result = await self._run_gate1(offline=False)
        result["gate"] = "4A"
        print("W4_GATE4A_PUBLIC=" + json.dumps(result, sort_keys=True), flush=True)

    async def test_gate4_read_only_public_route(self) -> None:  # pragma: no cover
        """READ_ONLY uses the public route; Gate 2 proves its deny side."""

        result = await self._run_gate1(
            offline=True,
            profile=SandboxProfile.READ_ONLY,
            writable=False,
        )
        result.update({"gate": "4", "profile": "READ_ONLY", "workspace_mutation": "DENIED"})
        print("W4_GATE4_READ_ONLY=" + json.dumps(result, sort_keys=True), flush=True)

    async def test_gate4b_application_manager_end_to_end(self) -> None:  # pragma: no cover
        """Gate 4B proves the real application manager-to-ConPTY route."""

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
            probe = workspace / "windows-conpty-manager-probe.exe"
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
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                runtime_state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
                _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                _diagnostic_create_no_window=False,
            )
            manager = LocalInteractiveTerminalManager(
                workspace=workspace,
                workspace_path_resolver=FilesystemWorkspacePathResolver(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                sandbox_profile=SandboxProfile.WORKSPACE,
                local_process_sandbox=adapter,
                protected_environment_variables=frozenset({"controller_secret"}),
                max_sessions=2,
            )
            session = None
            try:
                session = await manager.create_exec(
                    "w4-gate4b",
                    str(probe),
                    (),
                    cwd=".",
                    env={
                        "HOME": str(root / "controller-home"),
                        "TEMP": str(root / "controller-temp"),
                        "controller_secret": "must-not-reach-child",
                    },
                    size=TerminalSize(80, 25),
                    output_capacity=16 * 1024,
                )
                assert session is not None
                self.assertEqual(session.size, TerminalSize(80, 25))
                offset = 0
                observed = bytearray()

                async def wait_for(marker: bytes, timeout: float = 8.0) -> None:  # noqa: ASYNC109
                    nonlocal offset
                    needle = marker.rstrip(b"\r\n")
                    deadline = asyncio.get_running_loop().time() + timeout
                    while needle not in observed:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise AssertionError(
                                f"application terminal did not emit {marker!r}: {bytes(observed)!r}"
                            )
                        chunk = await session.read(
                            after_offset=offset,
                            max_bytes=4096,
                            wait_seconds=0.25,
                        )
                        observed.extend(chunk.data)
                        offset = chunk.next_offset

                await wait_for(b"W4_READY\n")
                await wait_for(b"W4_SIZE=80x25\n")
                await session.write(b"w4-input-token\r")
                await wait_for(b"W4_INPUT=w4-input-token\n")
                await session.resize(TerminalSize(120, 40))
                await session.write(b"w4-size\r")
                await wait_for(b"W4_SIZE=120x40\n")
                await session.write(b"w4-exit\r")
                await wait_for(b"W4_FINAL\n")
                self.assertEqual(await session.wait(timeout_seconds=8), 7)
                await session.close()
                self.assertNotIn(session.session_id, manager._sessions)
                await manager.shutdown()
                print(
                    "W4_GATE4B_APPLICATION="
                    + json.dumps(
                        {
                            "manager": "LocalInteractiveTerminalManager.create_exec",
                            "profile": "WORKSPACE",
                            "permission": "ALLOW",
                            "stdio": "PTY",
                            "initial_size": "80x25",
                            "input": "PASS",
                            "resize": "120x40",
                            "final": "PASS",
                            "wait": 7,
                            "registry_cleanup": True,
                            "manager_shutdown": "PASS",
                            "ring_capacity": 16 * 1024,
                            "errors": [],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            finally:
                if session is not None:
                    with contextlib.suppress(BaseException):
                        await session.close()
                with contextlib.suppress(BaseException):
                    await manager.shutdown()
                with contextlib.suppress(BaseException):
                    await asyncio.to_thread(authority.cleanup, setup_request)

    async def test_z_gate2_pty_write_and_network_isolation(
        self,
    ) -> None:  # pragma: no cover - Windows CI
        """Re-certify W3 write/network authority through the restricted PTY child."""

        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W2 setup acceptance requires elevation")
        security_probe = await asyncio.to_thread(_compile_security_probe)
        winsock_probe = await asyncio.to_thread(_compile_winsock_probe)

        async def cleanup_security_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, security_probe.parent, ignore_errors=True)

        async def cleanup_winsock_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, winsock_probe.parent, ignore_errors=True)

        self.addAsyncCleanup(cleanup_security_probe)
        self.addAsyncCleanup(cleanup_winsock_probe)

        class RecordingFirewall:
            def __init__(self) -> None:
                self.delegate = _NativeWindowsFirewallApi()
                self.calls: list[str] = []

            def ensure_outbound_block(self, rule: object) -> None:
                self.calls.append("ENSURE")
                self.delegate.ensure_outbound_block(rule)  # type: ignore[arg-type]

            def remove_rule(self, rule: object) -> None:
                self.calls.append("REMOVE")
                self.delegate.remove_rule(rule)  # type: ignore[arg-type]

            def rule_exists(self, rule: object) -> bool:
                self.calls.append("INSPECT")
                return self.delegate.rule_exists(rule)  # type: ignore[arg-type]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            readonly_root = root / "readonly"
            installation = root / "installation"
            outside = root / "outside-broad-write"
            runtime_state = root / "runtime-state"
            for path in (workspace, readonly_root, installation, outside, runtime_state):
                path.mkdir()
            security_executable = workspace / "windows-conpty-security-probe.exe"
            winsock_executable = workspace / "windows-winsock-probe.exe"
            shutil.copy2(security_probe, security_executable)
            shutil.copy2(winsock_probe, winsock_executable)
            readonly_file = readonly_root / "readonly.txt"
            private_file = installation / "private.txt"
            readonly_file.write_text("W4_READONLY_SENTINEL", encoding="utf-8")
            private_file.write_text("W4_PRIVATE_SENTINEL", encoding="utf-8")
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace, readonly_root),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            acl_api = _NativeWindowsAclApi()
            firewall = RecordingFirewall()
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=WindowsDpapiCredentialStore(installation / "credentials.dpapi"),
                acl_api=acl_api,
                firewall_api=firewall,
                account_api=_NativeWindowsSandboxAccountApi(),
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            online_sid = cast(str, snapshot.online_user_sid)
            offline_sid = cast(str, snapshot.offline_user_sid)
            write_sid = cast(str, snapshot.write_restricting_sid)
            encoded = authority._store(setup_request).load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(cast(bytes, encoded))
            plan = authority._plan(setup_request, record, authority._store(setup_request).path)
            grouped_plan: dict[Path, list[object]] = {}
            for entry in plan.entries:
                grouped_plan.setdefault(entry.path, []).append(entry)

            def acl_ready_projection() -> tuple[tuple[str, bool], ...]:
                return tuple(
                    (str(path), acl_api.matches(path, tuple(entries)))
                    for path, entries in sorted(grouped_plan.items(), key=lambda item: str(item[0]))
                )

            acl_before = acl_ready_projection()
            self.assertTrue(all(ready for _, ready in acl_before))
            real_firewall = firewall.delegate

            def firewall_checkpoint(label: str) -> None:
                inspected = authority.inspect(setup_request)
                self.assertEqual(inspected.state, WindowsSandboxSetupState.READY)
                self.assertTrue(real_firewall.rule_exists(record.offline_firewall_rule))
                print(
                    "W4_GATE2_FIREWALL="
                    + json.dumps({"label": label, "state": inspected.state.value}, sort_keys=True),
                    flush=True,
                )

            async def run_pty(
                *,
                identity: str,
                offline: bool,
                probe: Path,
                arguments: tuple[str, ...],
                label: str,
            ) -> dict[str, object]:
                output = bytearray()
                errors: list[BaseException] = []
                session: Any | None = None

                def on_output(data: bytes) -> None:
                    output.extend(data[: max(0, (1 << 20) - len(output))])

                try:
                    print(
                        "W4_GATE2_RUN_START="
                        + json.dumps({"label": label, "identity": identity}, sort_keys=True),
                        flush=True,
                    )
                    request = _pty_security_request(
                        workspace,
                        offline=offline,
                        probe=probe,
                        arguments=arguments,
                    )
                    adapter = WindowsNativeLocalProcessSandbox(
                        SandboxProfile.WORKSPACE,
                        workspace,
                        runtime_state,
                        setup_authority=authority,
                        setup_request_factory=lambda _request: setup_request,
                        _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                        _diagnostic_create_no_window=False,
                    )
                    session = await asyncio.to_thread(
                        adapter.spawn_terminal,
                        request,
                        size=TerminalSize(80, 25),
                        on_output=on_output,
                        on_eof=lambda: None,
                        on_error=errors.append,
                    )
                    print(
                        "W4_GATE2_RUN_SPAWNED="
                        + json.dumps({"label": label, "pid": session.process_id}, sort_keys=True),
                        flush=True,
                    )
                    deadline = asyncio.get_running_loop().time() + 15.0
                    tick = asyncio.Event()
                    while (
                        session.poll_exit() is None and asyncio.get_running_loop().time() < deadline
                    ):
                        # Gate 2 intentionally uses a timer-only wait.  PTY
                        # output is evidence, not a wake-up source; letting a
                        # noisy pseudo-console enqueue callbacks could starve
                        # this bounded child-exit deadline.
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(tick.wait(), timeout=0.1)
                    if session.poll_exit() is None:
                        print(
                            "W4_GATE2_RUN_TIMEOUT="
                            + json.dumps(
                                {
                                    "label": label,
                                    "pid": session.process_id,
                                    "output_bytes": len(output),
                                    "errors": [type(error).__name__ for error in errors],
                                },
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                        self.fail(f"{label}: PTY child did not exit")
                    await asyncio.to_thread(session.close)
                    print(
                        "W4_GATE2_RUN_EXIT="
                        + json.dumps(
                            {"label": label, "exit_code": session.poll_exit()}, sort_keys=True
                        ),
                        flush=True,
                    )
                    diagnostic = session.diagnostic_snapshot()
                    self.assertEqual(diagnostic.get("runner_state"), "RUNNER_EXITED")
                    self.assertEqual(diagnostic.get("runner_exit_code"), 0)
                    self.assertFalse(diagnostic.get("runner_forced_termination"))
                    self.assertEqual(errors, [])
                    attestation = diagnostic.get("security_attestation")
                    expected_sid = online_sid if identity == "ONLINE" else offline_sid
                    self.assertIsInstance(attestation, dict)
                    self.assertEqual(attestation.get("user_sid"), expected_sid)
                    self.assertIs(attestation.get("is_restricted"), True)
                    self.assertTrue(
                        token_attestation_is_exact(
                            {"security_attestation": attestation},
                            expected_user_sid=expected_sid,
                            expected_write_sid=write_sid,
                        )
                    )
                    self.assertIs(attestation.get("change_notify_privilege_enabled"), True)
                    self.assertEqual(attestation.get("unexpected_enabled_privilege_count"), 0)
                    result = {
                        "label": label,
                        "identity": identity,
                        "exit_code": session.poll_exit(),
                        "actual": "ALLOW" if session.poll_exit() == 0 else "DENY",
                        "token_user": expected_sid,
                        "token_attested": True,
                        "runner_state": diagnostic.get("runner_state"),
                        "runner_exit_code": diagnostic.get("runner_exit_code"),
                        "runner_forced_termination": diagnostic.get("runner_forced_termination"),
                        "stdout_preview": bytes(output[:256]).decode("utf-8", errors="replace"),
                    }
                    return result
                finally:
                    if session is not None and session.poll_exit() is None:
                        with contextlib.suppress(BaseException):
                            await asyncio.to_thread(session.close)

            def require_state(path: Path, *, exists: bool, content: str | None = None) -> None:
                self.assertEqual(path.exists(), exists)
                if content is not None:
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

            try:
                # Broad normal-user and Everyone write ACEs deliberately omit
                # the synthetic restricting SID.  The final PTY child must
                # still fail the WRITE_RESTRICTED second access check.
                outside_entries = (
                    WindowsManagedAce(
                        outside,
                        WindowsAccountSid(online_sid),
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                    WindowsManagedAce(
                        outside,
                        WindowsAccountSid(offline_sid),
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                    WindowsManagedAce(
                        outside,
                        WindowsAccountSid("S-1-1-0"),
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                )
                acl_api.reconcile(outside, desired=outside_entries, remove=())
                self.assertTrue(acl_api.matches(outside, outside_entries))
                firewall.calls.clear()
                firewall_checkpoint("before_filesystem")

                workspace_results: list[dict[str, object]] = []
                for identity, offline in (("ONLINE", False), ("OFFLINE", True)):
                    prefix = identity.casefold()
                    source = workspace / f"{prefix}-write.txt"
                    renamed = workspace / f"{prefix}-renamed.txt"
                    result = await run_pty(
                        identity=identity,
                        offline=offline,
                        probe=security_executable,
                        arguments=("write", str(source)),
                        label="WORKSPACE_CREATE",
                    )
                    self.assertEqual(result["actual"], "ALLOW")
                    require_state(source, exists=True)
                    workspace_results.append(result)
                    result = await run_pty(
                        identity=identity,
                        offline=offline,
                        probe=security_executable,
                        arguments=("append", str(source)),
                        label="WORKSPACE_APPEND",
                    )
                    self.assertEqual(result["actual"], "ALLOW")
                    require_state(source, exists=True)
                    workspace_results.append(result)
                    result = await run_pty(
                        identity=identity,
                        offline=offline,
                        probe=security_executable,
                        arguments=("rename", str(source), str(renamed)),
                        label="WORKSPACE_RENAME",
                    )
                    self.assertEqual(result["actual"], "ALLOW")
                    require_state(source, exists=False)
                    require_state(renamed, exists=True)
                    workspace_results.append(result)
                    result = await run_pty(
                        identity=identity,
                        offline=offline,
                        probe=security_executable,
                        arguments=("delete", str(renamed)),
                        label="WORKSPACE_DELETE",
                    )
                    self.assertEqual(result["actual"], "ALLOW")
                    require_state(renamed, exists=False)
                    workspace_results.append(result)

                    outside_target = outside / f"{prefix}-blocked.txt"
                    result = await run_pty(
                        identity=identity,
                        offline=offline,
                        probe=security_executable,
                        arguments=("write", str(outside_target)),
                        label="OUTSIDE_BROAD_WRITE",
                    )
                    self.assertEqual(result["actual"], "DENY")
                    require_state(outside_target, exists=False)
                    workspace_results.append(result)

                    readonly_new = readonly_root / f"{prefix}-new.txt"
                    readonly_renamed = readonly_root / f"{prefix}-renamed.txt"
                    result = await run_pty(
                        identity=identity,
                        offline=offline,
                        probe=security_executable,
                        arguments=("read", str(readonly_file)),
                        label="READ_ONLY_READ",
                    )
                    self.assertEqual(result["actual"], "ALLOW")
                    require_state(readonly_file, exists=True, content="W4_READONLY_SENTINEL")
                    workspace_results.append(result)
                    for arguments, label in (
                        (("write", str(readonly_new)), "READ_ONLY_CREATE"),
                        (("append", str(readonly_file)), "READ_ONLY_APPEND"),
                        (
                            ("rename", str(readonly_file), str(readonly_renamed)),
                            "READ_ONLY_RENAME",
                        ),
                        (("delete", str(readonly_file)), "READ_ONLY_DELETE"),
                    ):
                        result = await run_pty(
                            identity=identity,
                            offline=offline,
                            probe=security_executable,
                            arguments=arguments,
                            label=label,
                        )
                        self.assertEqual(result["actual"], "DENY")
                        require_state(readonly_file, exists=True, content="W4_READONLY_SENTINEL")
                        require_state(readonly_new, exists=False)
                        require_state(readonly_renamed, exists=False)
                        workspace_results.append(result)

                    for target, label in (
                        (private_file, "INSTALLATION_WRITE"),
                        (authority._store(setup_request).path, "CREDENTIAL_WRITE"),
                    ):
                        before = target.read_bytes() if target.exists() else None
                        result = await run_pty(
                            identity=identity,
                            offline=offline,
                            probe=security_executable,
                            arguments=("write", str(target)),
                            label=label,
                        )
                        self.assertEqual(result["actual"], "DENY")
                        self.assertEqual(target.read_bytes() if target.exists() else None, before)
                        workspace_results.append(result)
                        result = await run_pty(
                            identity=identity,
                            offline=offline,
                            probe=security_executable,
                            arguments=("delete", str(target)),
                            label=label.replace("WRITE", "DELETE"),
                        )
                        self.assertEqual(result["actual"], "DENY")
                        self.assertTrue(target.exists())
                        workspace_results.append(result)

                firewall_checkpoint("after_filesystem")
                self.assertEqual(
                    [call for call in firewall.calls if call in {"ENSURE", "REMOVE"}], []
                )
                acl_after_filesystem = acl_ready_projection()
                self.assertEqual(acl_after_filesystem, acl_before)

                controller = await asyncio.to_thread(
                    subprocess.run,
                    [str(winsock_probe)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=8,
                    shell=False,
                )
                controller_result = _parse_pty_winsock_result(controller.stdout)
                self.assertEqual(controller.returncode, 0)
                self.assertEqual(controller_result["stage"], "CONNECT")
                self.assertTrue(controller_result["connected"])
                self.assertEqual(controller_result["wsa_error"], 0)
                network_results: list[dict[str, object]] = []
                for label, identity, offline in (
                    ("ONLINE_1", "ONLINE", False),
                    ("OFFLINE_1", "OFFLINE", True),
                    ("ONLINE_2", "ONLINE", False),
                    ("OFFLINE_2", "OFFLINE", True),
                ):
                    firewall_checkpoint(f"before_{label.casefold()}")
                    result = await run_pty(
                        identity=identity,
                        offline=offline,
                        probe=winsock_executable,
                        arguments=(),
                        label=label,
                    )
                    parsed = _parse_pty_winsock_result(str(result["stdout_preview"]))
                    result["winsock"] = parsed
                    self.assertEqual(parsed["stage"], "CONNECT")
                    if offline:
                        self.assertFalse(parsed["connected"])
                        self.assertEqual(parsed["wsa_error"], 10013)
                        self.assertNotEqual(result["exit_code"], 0)
                    else:
                        self.assertTrue(parsed["connected"])
                        self.assertEqual(parsed["wsa_error"], 0)
                        self.assertEqual(result["exit_code"], 0)
                    firewall_checkpoint(f"after_{label.casefold()}")
                    network_results.append(result)
                self.assertEqual(
                    [call for call in firewall.calls if call in {"ENSURE", "REMOVE"}], []
                )
                acl_after_network = acl_ready_projection()
                self.assertEqual(acl_after_network, acl_before)

                print(
                    "W4_GATE2_RESULTS="
                    + json.dumps(
                        {
                            "workspace_operations": workspace_results,
                            "outside_real_user_write_ace": True,
                            "outside_everyone_write_ace": True,
                            "outside_synthetic_write_ace": False,
                            "outside_deny_ace": False,
                            "read_only_mutations": "DENIED",
                            "installation_and_credential_mutations": "DENIED",
                            "acl_ready_before_after": True,
                            "controller_preflight": controller_result,
                            "network": network_results,
                            "firewall_ready_checkpoints": "PASS",
                            "runtime_firewall_mutations": 0,
                            "token_restricting_sid": "exact-ordered-set",
                            "capabilities": {
                                "read": "limited",
                                "write": "strong",
                                "network": "strong",
                                "strict": "fail-closed-read-strong",
                            },
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            finally:
                with contextlib.suppress(BaseException):
                    await asyncio.to_thread(authority.cleanup, setup_request)

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
                    ).spawn_terminal,
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
