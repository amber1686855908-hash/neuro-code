"""Privileged W3 acceptance through the final restricted child process.

Unlike the W2 tests, every assertion below is made from a child created by the
trusted runner.  The test is enabled only in the dedicated Windows CI job; on
Windows that job must execute the test (the environment guard is intentional
for ordinary cross-platform pytest runs).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import socket
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
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
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
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


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


def _wait_for_native_process_exit(pid: int, timeout_ms: int = 5_000) -> bool:
    """Wait for a descendant PID without using a shell or tasklist parsing."""

    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    wait = kernel.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int32
    handle = open_process(
        0x00100000 | 0x1000, False, pid
    )  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error in {2, 87}:  # ERROR_FILE_NOT_FOUND / ERROR_INVALID_PARAMETER
            return True
        raise OSError(error, "OpenProcess(descendant) failed")
    try:
        return wait(handle, timeout_ms) == 0
    finally:
        close(handle)


async def _read_with_native_timeout(
    process: object,
    *,
    stream_name: str = "stdout",
    limit_seconds: float = 30.0,
) -> bytes:
    """Read native output in bounded chunks and terminate a stuck child.

    A single ``StreamReader.read()`` waits for EOF.  Keeping that wait in one
    coroutine makes a child that emitted data but failed to close its stream
    indistinguishable from a child that never produced output.  Chunked reads
    preserve binary output while applying the timeout to every native read.
    """

    print(f"W3_PHASE read-start:{stream_name}", flush=True)
    try:
        stream = getattr(process, stream_name)
        chunks: list[bytes] = []
        while True:
            chunk = await asyncio.wait_for(stream.read(65_536), timeout=limit_seconds)
            if not chunk:
                break
            chunks.append(chunk)
            print(f"W3_PHASE read-chunk:{stream_name}:{len(chunk)}", flush=True)
        result = b"".join(chunks)
        print(f"W3_PHASE read-done:{stream_name}:{len(result)}", flush=True)
        return result
    except TimeoutError as error:
        with contextlib.suppress(BaseException):
            await process.terminate(grace_seconds=0.5)  # type: ignore[attr-defined]
        raise AssertionError(
            "Windows W3 child produced no completed output before timeout"
        ) from error


_TOKEN_PROBE = r"""
import sys
print("TOKEN_PROBE_EARLY", flush=True)
import ctypes
import json
import sys
from pathlib import Path

print("TOKEN_PROBE_START", flush=True)

advapi = ctypes.WinDLL("advapi32.dll", use_last_error=True)
kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)

class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]

class TokenUser(ctypes.Structure):
    _fields_ = [("User", SidAndAttributes)]

get_current_process = kernel.GetCurrentProcess
get_current_process.restype = ctypes.c_void_p
open_token = advapi.OpenProcessToken
open_token.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
open_token.restype = ctypes.c_int32
get_info = advapi.GetTokenInformation
get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
get_info.restype = ctypes.c_int32
sid_to_string = advapi.ConvertSidToStringSidW
sid_to_string.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
sid_to_string.restype = ctypes.c_int32
local_free = kernel.LocalFree
local_free.argtypes = [ctypes.c_void_p]
local_free.restype = ctypes.c_void_p
close = kernel.CloseHandle
close.argtypes = [ctypes.c_void_p]
close.restype = ctypes.c_int32

token = ctypes.c_void_p()
if not open_token(get_current_process(), 0x0008, ctypes.byref(token)):
    raise OSError(ctypes.get_last_error(), "OpenProcessToken")

def info(kind):
    required = ctypes.c_uint32()
    get_info(token, kind, None, 0, ctypes.byref(required))
    data = ctypes.create_string_buffer(required.value)
    if not get_info(token, kind, data, required.value, ctypes.byref(required)):
        raise OSError(ctypes.get_last_error(), "GetTokenInformation")
    return data

user = TokenUser.from_buffer_copy(info(1))
sid_buffer = ctypes.c_void_p()
if not sid_to_string(user.User.Sid, ctypes.byref(sid_buffer)):
    raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
sid = ctypes.wstring_at(sid_buffer.value)
local_free(sid_buffer)
restricted = bool(int.from_bytes(info(40)[:4], "little"))
restricted_info = info(11)
restricted_count = int.from_bytes(restricted_info[:4], "little")
restricted_sids = []
for index in range(restricted_count):
    entry = SidAndAttributes.from_buffer_copy(restricted_info, 4 + index * ctypes.sizeof(SidAndAttributes))
    text = ctypes.c_void_p()
    if not sid_to_string(entry.Sid, ctypes.byref(text)):
        raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
    restricted_sids.append(ctypes.wstring_at(text.value))
    local_free(text)
print(json.dumps({"sid": sid, "restricted": restricted, "restricted_count": restricted_count, "restricted_sids": restricted_sids}))
close(token)
"""


def _request(
    *,
    workspace: Path,
    read_roots: tuple[Path, ...],
    writable_roots: tuple[Path, ...],
    profile: SandboxProfile,
    network: LocalProcessNetworkPolicy,
    stdio: LocalProcessStdioMode,
    arguments: tuple[str, ...],
    executable: str | None = None,
) -> SandboxedProcessRequest:
    mode = (
        LocalWorkspaceAccessMode.READ_WRITE
        if writable_roots
        else LocalWorkspaceAccessMode.READ_ONLY
    )
    roots = tuple(LocalWorkspaceAccess(path, mode) for path in read_roots)
    return SandboxedProcessRequest.exec(
        executable or sys.executable,
        arguments,
        purpose=(
            LocalProcessPurpose.MCP_STDIO
            if stdio is LocalProcessStdioMode.PROTOCOL
            else LocalProcessPurpose.BASH
        ),
        cwd=workspace,
        sandbox_profile=profile,
        filesystem_policy=LocalProcessFilesystemPolicy(roots),
        network_policy=network,
        environment_policy=LocalProcessEnvironmentPolicy({}),
        stdio_mode=stdio,
        lifecycle=LocalProcessLifecycle(),
    )


@unittest.skipUnless(_native_enabled(), "privileged Windows W3 acceptance is CI-only")
class WindowsNativeRuntimeAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_restricted_child_enforces_identity_acl_network_and_protocol(
        self,
    ) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W2 setup acceptance requires elevation")
        account_api = _NativeWindowsSandboxAccountApi()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            readonly = root / "readonly"
            installation = root / "installation"
            state = root / "runtime-state"
            outside = root / "outside.txt"
            workspace.mkdir()
            readonly.mkdir()
            installation.mkdir()
            state.mkdir()
            outside.write_text("controller-owned", encoding="utf-8")
            sensitive = workspace / "controller-state.json"
            sensitive.write_text("controller secret", encoding="utf-8")
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace, readonly),
                writable_roots=(workspace,),
                sensitive_read_paths=(sensitive,),
            )
            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                account_api=account_api,
                privilege_api=privilege_api,
            )
            self.assertEqual(authority.setup(setup_request).state, WindowsSandboxSetupState.READY)
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
            )
            try:
                print("W3_PHASE stdio-probe-start", flush=True)
                stdio_probe = await adapter.spawn(
                    _request(
                        workspace=workspace,
                        read_roots=(workspace,),
                        writable_roots=(workspace,),
                        profile=SandboxProfile.WORKSPACE,
                        network=LocalProcessNetworkPolicy.INHERIT,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=("/d", "/c", "echo W3_STDIO"),
                        executable=os.environ.get("COMSPEC", r"C:\\Windows\\System32\\cmd.exe"),
                    )
                )
                print("W3_PHASE stdio-probe-spawned", flush=True)
                stdio_output = await _read_with_native_timeout(stdio_probe)
                self.assertIn(b"W3_STDIO", stdio_output)
                self.assertEqual(await stdio_probe.wait(), 0)
                print("W3_PHASE stdio-probe-done", flush=True)

                print("W3_PHASE identity-spawn-start", flush=True)
                identity_request = _request(
                    workspace=workspace,
                    read_roots=(workspace,),
                    writable_roots=(workspace,),
                    profile=SandboxProfile.WORKSPACE,
                    network=LocalProcessNetworkPolicy.INHERIT,
                    stdio=LocalProcessStdioMode.CAPTURE,
                    arguments=("-c", _TOKEN_PROBE),
                )
                process = await adapter.spawn(identity_request)
                print("W3_PHASE identity-spawn-done", flush=True)
                stdout = await _read_with_native_timeout(process)
                self.assertEqual(await process.wait(), 0)
                print("W3_PHASE identity-wait-done", flush=True)
                facts = json.loads(stdout.decode("utf-8"))
                record = authority.identity_records(setup_request)
                online = next(item for item in record if item.kind.value == "online")
                self.assertEqual(facts["sid"], online.user_sid.value)
                self.assertTrue(facts["restricted"])
                self.assertGreaterEqual(facts["restricted_count"], 1)
                self.assertIn(online.write_sid.value, facts["restricted_sids"])

                workspace_probe = (
                    "from pathlib import Path\n"
                    "import os\n"
                    f"p=Path({str(workspace / 'runtime.txt')!r}); p.write_bytes(b'workspace-write')\n"
                    f"s=Path({str(sensitive)!r}); i=Path({str(installation)!r}); c=Path({str(installation / 'credentials.dpapi')!r})\n"
                    f"o=Path({str(outside)!r})\n"
                    "def denied(action):\n"
                    "    try: action(); return False\n"
                    "    except (PermissionError, OSError): return True\n"
                    f"print(p.read_bytes().decode(), 'sensitive=' + str(denied(lambda: s.read_bytes())), "
                    "'installation_read=' + str(denied(lambda: list(i.iterdir()))), "
                    "'credential_read=' + str(denied(lambda: c.read_bytes())), "
                    "'credential_write=' + str(denied(lambda: c.write_bytes(b'x'))), "
                    "'credential_delete=' + str(denied(lambda: c.unlink())), "
                    "'outside_write=' + str(denied(lambda: o.write_bytes(b'x'))))"
                )
                compile(workspace_probe, "<workspace-probe>", "exec")
                print("W3_PHASE workspace-spawn-start", flush=True)
                process = await adapter.spawn(
                    _request(
                        workspace=workspace,
                        read_roots=(workspace,),
                        writable_roots=(workspace,),
                        profile=SandboxProfile.WORKSPACE,
                        network=LocalProcessNetworkPolicy.INHERIT,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=("-c", workspace_probe),
                    )
                )
                print("W3_PHASE workspace-spawn-done", flush=True)
                workspace_output = await _read_with_native_timeout(process)
                self.assertIn(b"workspace-write", workspace_output)
                self.assertIn(b"sensitive=True", workspace_output)
                self.assertIn(b"installation_read=True", workspace_output)
                self.assertIn(b"credential_read=True", workspace_output)
                self.assertIn(b"credential_write=True", workspace_output)
                self.assertIn(b"credential_delete=True", workspace_output)
                self.assertIn(b"outside_write=True", workspace_output)
                self.assertEqual(await process.wait(), 0)
                print("W3_PHASE workspace-wait-done", flush=True)

                read_only_probe = (
                    "from pathlib import Path; "
                    f"p=Path({str(readonly / 'readonly.txt')!r}); "
                    "print('READ=' + p.read_text()); "
                    "\ntry: p.write_text('x'); print('WRITABLE')\n"
                    "except (PermissionError, OSError): print('DENIED')"
                )
                compile(read_only_probe, "<read-only-probe>", "exec")
                (readonly / "readonly.txt").write_text("readonly", encoding="utf-8")
                read_only_adapter = WindowsNativeLocalProcessSandbox(
                    SandboxProfile.READ_ONLY,
                    readonly,
                    state,
                    setup_authority=authority,
                    setup_request_factory=lambda _request: setup_request,
                )
                print("W3_PHASE readonly-spawn-start", flush=True)
                process = await read_only_adapter.spawn(
                    _request(
                        workspace=readonly,
                        read_roots=(readonly,),
                        writable_roots=(),
                        profile=SandboxProfile.READ_ONLY,
                        network=LocalProcessNetworkPolicy.ISOLATED,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=("-c", read_only_probe),
                    )
                )
                print("W3_PHASE readonly-spawn-done", flush=True)
                output = await _read_with_native_timeout(process)
                self.assertEqual(await process.wait(), 0)
                self.assertIn(b"READ=readonly", output)
                self.assertNotIn(b"WRITABLE", output)
                print("W3_PHASE readonly-wait-done", flush=True)

                print("W3_PHASE offline-identity-spawn-start", flush=True)
                offline_identity_process = await read_only_adapter.spawn(
                    _request(
                        workspace=readonly,
                        read_roots=(readonly,),
                        writable_roots=(),
                        profile=SandboxProfile.READ_ONLY,
                        network=LocalProcessNetworkPolicy.ISOLATED,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=("-c", _TOKEN_PROBE),
                    )
                )
                print("W3_PHASE offline-identity-spawn-done", flush=True)
                offline_facts = json.loads(
                    (await _read_with_native_timeout(offline_identity_process)).decode("utf-8")
                )
                self.assertEqual(await offline_identity_process.wait(), 0)
                offline = next(item for item in record if item.kind.value == "offline")
                self.assertEqual(offline_facts["sid"], offline.user_sid.value)
                self.assertIn(offline.write_sid.value, offline_facts["restricted_sids"])
                print("W3_PHASE offline-identity-wait-done", flush=True)

                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(2)
                listener.settimeout(3)
                port = listener.getsockname()[1]
                network_probe = (
                    "import socket; "
                    f"s=socket.socket(); s.settimeout(2); "
                    f"\ntry: s.connect(('127.0.0.1', {port})); print('CONNECTED')\n"
                    "except (OSError, TimeoutError): print('DENIED')"
                )
                compile(network_probe, "<network-probe>", "exec")
                try:
                    print("W3_PHASE offline-network-spawn-start", flush=True)
                    offline_process = await read_only_adapter.spawn(
                        _request(
                            workspace=readonly,
                            read_roots=(readonly,),
                            writable_roots=(),
                            profile=SandboxProfile.READ_ONLY,
                            network=LocalProcessNetworkPolicy.ISOLATED,
                            stdio=LocalProcessStdioMode.CAPTURE,
                            arguments=("-c", network_probe),
                        )
                    )
                    print("W3_PHASE offline-network-spawn-done", flush=True)
                    print("W3_PHASE online-network-spawn-start", flush=True)
                    online_process = await adapter.spawn(
                        _request(
                            workspace=workspace,
                            read_roots=(workspace,),
                            writable_roots=(workspace,),
                            profile=SandboxProfile.WORKSPACE,
                            network=LocalProcessNetworkPolicy.INHERIT,
                            stdio=LocalProcessStdioMode.CAPTURE,
                            arguments=("-c", network_probe),
                        )
                    )
                    print("W3_PHASE online-network-spawn-done", flush=True)
                    offline_output, online_output = await asyncio.gather(
                        _read_with_native_timeout(offline_process),
                        _read_with_native_timeout(online_process),
                    )
                    self.assertEqual(await offline_process.wait(), 0)
                    self.assertEqual(await online_process.wait(), 0)
                    self.assertIn(b"DENIED", offline_output)
                    self.assertIn(b"CONNECTED", online_output)
                    connection, _ = listener.accept()
                    connection.close()
                    self.assertEqual(
                        authority.inspect(setup_request).state, WindowsSandboxSetupState.READY
                    )
                    print("W3_PHASE network-wait-done", flush=True)
                finally:
                    listener.close()

                protocol = (
                    "import sys; data=sys.stdin.buffer.read(); "
                    "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data[::-1])"
                )
                compile(protocol, "<protocol-probe>", "exec")
                print("W3_PHASE protocol-spawn-start", flush=True)
                process = await adapter.spawn(
                    _request(
                        workspace=workspace,
                        read_roots=(workspace,),
                        writable_roots=(workspace,),
                        profile=SandboxProfile.WORKSPACE,
                        network=LocalProcessNetworkPolicy.INHERIT,
                        stdio=LocalProcessStdioMode.PROTOCOL,
                        arguments=("-c", protocol),
                    )
                )
                print("W3_PHASE protocol-spawn-done", flush=True)
                payload = (b"\x00\r\n" + bytes(range(256))) * 512
                await process.write_stdin(payload)
                await process.close_stdin()
                stdout = await _read_with_native_timeout(process)
                stderr = await _read_with_native_timeout(process, stream_name="stderr")
                self.assertEqual(await process.wait(), 0)
                self.assertEqual(stdout, payload)
                self.assertEqual(stderr, payload[::-1])
                print("W3_PHASE protocol-wait-done", flush=True)

                grandchild_marker = workspace / "grandchild.pid"
                grandchild_code = (
                    "import os,time; "
                    f"from pathlib import Path; Path({str(grandchild_marker)!r}).write_text(str(os.getpid()), encoding='ascii'); "
                    "time.sleep(120)"
                )
                compile(grandchild_code, "<grandchild-probe>", "exec")
                descendant_probe = (
                    "import subprocess,sys,time; "
                    f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
                    "print('READY', flush=True); time.sleep(120)"
                )
                compile(descendant_probe, "<descendant-probe>", "exec")
                print("W3_PHASE descendant-spawn-start", flush=True)
                process = await adapter.spawn(
                    _request(
                        workspace=workspace,
                        read_roots=(workspace,),
                        writable_roots=(workspace,),
                        profile=SandboxProfile.WORKSPACE,
                        network=LocalProcessNetworkPolicy.INHERIT,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=("-c", descendant_probe),
                    )
                )
                print("W3_PHASE descendant-spawn-done", flush=True)
                line = await asyncio.wait_for(
                    process.stdout.read(6),  # type: ignore[union-attr]
                    timeout=5,
                )
                self.assertEqual(line.strip(), b"READY")
                for _ in range(50):
                    if grandchild_marker.exists():
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(grandchild_marker.exists())
                grandchild_pid = int(grandchild_marker.read_text(encoding="ascii"))
                await process.terminate(grace_seconds=0.5)
                print("W3_PHASE descendant-terminated", flush=True)
                self.assertTrue(
                    await asyncio.to_thread(_wait_for_native_process_exit, grandchild_pid)
                )
                print("W3_PHASE descendant-wait-done", flush=True)
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)


if __name__ == "__main__":
    unittest.main()
