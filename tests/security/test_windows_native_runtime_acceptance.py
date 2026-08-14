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
from neuro_code.infrastructure.sandbox.windows_native_runner import current_user_sid
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    READ_ACCESS_MASK,
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

    try:
        stream = getattr(process, stream_name)
        chunks: list[bytes] = []
        while True:
            chunk = await asyncio.wait_for(stream.read(65_536), timeout=limit_seconds)
            if not chunk:
                break
            chunks.append(chunk)
        result = b"".join(chunks)
        return result
    except TimeoutError as error:
        with contextlib.suppress(BaseException):
            await process.terminate(grace_seconds=0.5)  # type: ignore[attr-defined]
        raise AssertionError(
            "Windows W3 child produced no completed output before timeout"
        ) from error


_TOKEN_PROBE = r"""
import ctypes
import json

advapi = ctypes.WinDLL("advapi32.dll", use_last_error=True)
kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)

class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]

class TokenUser(ctypes.Structure):
    _fields_ = [("User", SidAndAttributes)]

class TokenGroupsOne(ctypes.Structure):
    _fields_ = [("GroupCount", ctypes.c_uint32), ("Groups", SidAndAttributes * 1)]

class Luid(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

class LuidAndAttributes(ctypes.Structure):
    _fields_ = [("Luid", Luid), ("Attributes", ctypes.c_uint32)]

class TokenPrivilegesOne(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_uint32), ("Privileges", LuidAndAttributes * 1)]

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
lookup_privilege = advapi.LookupPrivilegeValueW
lookup_privilege.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(Luid)]
lookup_privilege.restype = ctypes.c_int32
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

def sid_text(pointer):
    text = ctypes.c_void_p()
    if not sid_to_string(pointer, ctypes.byref(text)):
        raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(text.value)
    finally:
        local_free(text)

def sid_entries(data):
    count = int.from_bytes(data[:4], "little")
    offset = TokenGroupsOne.Groups.offset
    size = ctypes.sizeof(SidAndAttributes)
    return [SidAndAttributes.from_buffer(data, offset + index * size) for index in range(count)]

user_info = info(1)
user = TokenUser.from_buffer(user_info)
sid = sid_text(user.User.Sid)
restricted = bool(int.from_bytes(info(40)[:4], "little"))
restricted_info = info(11)
restricted_count = int.from_bytes(restricted_info[:4], "little")
restricted_sids = [sid_text(entry.Sid) for entry in sid_entries(restricted_info)]
groups_info = info(2)
logon_sids = [
    sid_text(entry.Sid)
    for entry in sid_entries(groups_info)
    if entry.Attributes & 0xC0000000 == 0xC0000000
]
change_notify_luid = Luid()
if not lookup_privilege(None, "SeChangeNotifyPrivilege", ctypes.byref(change_notify_luid)):
    raise OSError(ctypes.get_last_error(), "LookupPrivilegeValueW")
privileges_info = info(3)
privilege_count = int.from_bytes(privileges_info[:4], "little")
privilege_offset = TokenPrivilegesOne.Privileges.offset
privilege_size = ctypes.sizeof(LuidAndAttributes)
privileges = [
    LuidAndAttributes.from_buffer(privileges_info, privilege_offset + index * privilege_size)
    for index in range(privilege_count)
]
change_notify_enabled = any(
    item.Luid.LowPart == change_notify_luid.LowPart
    and item.Luid.HighPart == change_notify_luid.HighPart
    and bool(item.Attributes & 0x2)
    for item in privileges
)
print(json.dumps({
    "sid": sid,
    "restricted": restricted,
    "restricted_count": restricted_count,
    "restricted_sids": restricted_sids,
    "logon_sids": logon_sids,
    "change_notify_enabled": change_notify_enabled,
}))
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
            outside_broad_write = root / "outside-broad-write"
            workspace.mkdir()
            readonly.mkdir()
            installation.mkdir()
            state.mkdir()
            outside_broad_write.mkdir()
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
            acl_api = _NativeWindowsAclApi()
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                acl_api=acl_api,
                firewall_api=_NativeWindowsFirewallApi(),
                account_api=account_api,
                privilege_api=privilege_api,
            )
            self.assertEqual(authority.setup(setup_request).state, WindowsSandboxSetupState.READY)
            record = authority.identity_records(setup_request)
            online = next(item for item in record if item.kind.value == "online")
            broad_write_aces = (
                WindowsManagedAce(
                    outside_broad_write,
                    online.user_sid,
                    WindowsManagedAceKind.READ_ALLOW,
                    READ_ACCESS_MASK,
                ),
                WindowsManagedAce(
                    outside_broad_write,
                    online.user_sid,
                    WindowsManagedAceKind.WRITE_ALLOW,
                    WRITE_ACCESS_MASK,
                ),
            )
            acl_api.reconcile(outside_broad_write, desired=broad_write_aces, remove=())
            self.assertTrue(acl_api.matches(outside_broad_write, broad_write_aces))
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
            )
            try:
                whoami_probe = await adapter.spawn(
                    _request(
                        workspace=workspace,
                        read_roots=(workspace,),
                        writable_roots=(workspace,),
                        profile=SandboxProfile.WORKSPACE,
                        network=LocalProcessNetworkPolicy.INHERIT,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=(),
                        executable=str(
                            Path(os.environ.get("SYSTEMROOT", r"C:\\Windows"))
                            / "System32"
                            / "whoami.exe"
                        ),
                    )
                )
                whoami_output = await _read_with_native_timeout(whoami_probe, limit_seconds=10)
                self.assertTrue(whoami_output)
                self.assertEqual(await whoami_probe.wait(), 0)

                stdio_marker = workspace / "stdio-marker.txt"
                stdio_probe_code = (
                    "import os; "
                    f"open({str(stdio_marker)!r}, 'w', encoding='ascii').write('marker'); "
                    "os.write(1, b'W3_STDIO\\n'); os._exit(0)"
                )
                stdio_probe = await adapter.spawn(
                    _request(
                        workspace=workspace,
                        read_roots=(workspace,),
                        writable_roots=(workspace,),
                        profile=SandboxProfile.WORKSPACE,
                        network=LocalProcessNetworkPolicy.INHERIT,
                        stdio=LocalProcessStdioMode.CAPTURE,
                        arguments=("-c", stdio_probe_code),
                    )
                )
                self.assertEqual(await asyncio.wait_for(stdio_probe.wait(), timeout=5), 0)
                stdio_output = await _read_with_native_timeout(stdio_probe)
                self.assertIn(b"W3_STDIO", stdio_output)
                self.assertEqual(await stdio_probe.wait(), 0)

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
                stdout = await _read_with_native_timeout(process)
                self.assertEqual(await process.wait(), 0)
                facts = json.loads(stdout.decode("utf-8"))
                self.assertEqual(facts["sid"], online.user_sid.value)
                self.assertTrue(facts["restricted"])
                self.assertEqual(facts["restricted_count"], 1)
                self.assertEqual(set(facts["restricted_sids"]), {online.write_sid.value})
                self.assertNotIn("S-1-1-0", facts["restricted_sids"])
                self.assertNotIn(online.user_sid.value, facts["restricted_sids"])
                self.assertNotIn(current_user_sid(), facts["restricted_sids"])
                self.assertTrue(facts["logon_sids"])
                self.assertTrue(set(facts["logon_sids"]).isdisjoint(facts["restricted_sids"]))
                self.assertTrue(facts["change_notify_enabled"])

                workspace_probe = (
                    "from pathlib import Path\n"
                    "import os\n"
                    f"p=Path({str(workspace / 'runtime.txt')!r}); p.write_bytes(b'workspace-write')\n"
                    f"s=Path({str(sensitive)!r}); i=Path({str(installation)!r}); c=Path({str(installation / 'credentials.dpapi')!r})\n"
                    f"o=Path({str(outside)!r})\n"
                    f"b=Path({str(outside_broad_write / 'created.txt')!r})\n"
                    "def denied(action):\n"
                    "    try: action(); return False\n"
                    "    except (PermissionError, OSError): return True\n"
                    f"print(p.read_bytes().decode(), 'sensitive=' + str(denied(lambda: s.read_bytes())), "
                    "'installation_read=' + str(denied(lambda: list(i.iterdir()))), "
                    "'credential_read=' + str(denied(lambda: c.read_bytes())), "
                    "'credential_write=' + str(denied(lambda: c.write_bytes(b'x'))), "
                    "'credential_delete=' + str(denied(lambda: c.unlink())), "
                    "'outside_write=' + str(denied(lambda: o.write_bytes(b'x'))), "
                    "'broad_write_outside=' + str(denied(lambda: b.write_bytes(b'x'))))"
                )
                compile(workspace_probe, "<workspace-probe>", "exec")
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
                workspace_output = await _read_with_native_timeout(process)
                self.assertIn(b"workspace-write", workspace_output)
                self.assertIn(b"sensitive=True", workspace_output)
                self.assertIn(b"installation_read=True", workspace_output)
                self.assertIn(b"credential_read=True", workspace_output)
                self.assertIn(b"credential_write=True", workspace_output)
                self.assertIn(b"credential_delete=True", workspace_output)
                self.assertIn(b"outside_write=True", workspace_output)
                self.assertIn(b"broad_write_outside=True", workspace_output)
                self.assertEqual(await process.wait(), 0)

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
                output = await _read_with_native_timeout(process)
                self.assertEqual(await process.wait(), 0)
                self.assertIn(b"READ=readonly", output)
                self.assertNotIn(b"WRITABLE", output)

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
                offline_facts = json.loads(
                    (await _read_with_native_timeout(offline_identity_process)).decode("utf-8")
                )
                self.assertEqual(await offline_identity_process.wait(), 0)
                offline = next(item for item in record if item.kind.value == "offline")
                self.assertEqual(offline_facts["sid"], offline.user_sid.value)
                self.assertEqual(set(offline_facts["restricted_sids"]), {offline.write_sid.value})
                self.assertTrue(offline_facts["change_notify_enabled"])

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
                finally:
                    listener.close()

                protocol = (
                    "import sys; data=sys.stdin.buffer.read(); "
                    "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data[::-1])"
                )
                compile(protocol, "<protocol-probe>", "exec")
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
                payload = (b"\x00\r\n" + bytes(range(256))) * 512
                await process.write_stdin(payload)
                await process.close_stdin()
                stdout = await _read_with_native_timeout(process)
                stderr = await _read_with_native_timeout(process, stream_name="stderr")
                self.assertEqual(await process.wait(), 0)
                self.assertEqual(stdout, payload)
                self.assertEqual(stderr, payload[::-1])

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
                self.assertTrue(
                    await asyncio.to_thread(_wait_for_native_process_exit, grandchild_pid)
                )
            finally:
                with contextlib.suppress(BaseException):
                    authority.cleanup(setup_request)


if __name__ == "__main__":
    unittest.main()
