"""W5 Gate 1 evidence for the shared Windows restricted-runtime seam.

This module deliberately contains no compatibility workaround and never edits
the production sandbox.  It compares the same fixed native probe and a small
set of Gate 0 workload canaries across the host, the two W2 profile-loading
modes, and the real W3 restricted-child route.  The profile-enabled W3 row is
an in-memory evidence experiment: the production ``_LOGON_FLAGS`` value is
restored before the test exits.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from tests.security.test_windows_native_workload_compatibility import (
    _MAX_OUTPUT_BYTES,
    _TIMEOUT_SECONDS,
    _build_workloads,
    _classify_exception,
    _completed_classification,
    _error_code,
    _host_run,
    _preview,
    _provenance,
    _reported_error_code,
    _request,
    _strip_terminal,
    _tool_paths,
    _w3_run,
    _Workload,
)

from neuro_code.application.ports.sandbox import SandboxedProcessRequest
from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.sandbox import windows_native_runner
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import (
    _environment_block,
    _ProcessInformation,
    _StartupInfoW,
    _WindowsNativeDesktopMode,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _InstallationRecord,
    _NativeWindowsSetupPrivilegeApi,
)

_LOGON_WITH_PROFILE = 0x00000001
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_HANDLE_FLAG_PROTECT_FROM_CLOSE = 0x00000002
_STD_INPUT_HANDLE = -10
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_MAX_PREVIEW_BYTES = 512
_PROBE_WORKLOADS = frozenset(
    {
        "CMD_BASIC",
        "NODE_VERSION",
        "POWERSHELL_BASIC",
        "PWSH_BASIC",
        "PYTHON_BASE_MINIMAL_NO_SITE",
        "GIT_VERSION",
        "CURL_VERSION",
        "NUL_DIRECT_WIN32",
    }
)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int32),
    ]


class _Gate1DirectProcess:
    """Minimal test-only CreateProcessWithLogonW evidence harness."""

    def __init__(self) -> None:  # pragma: no cover - Windows CI
        if os.name != "nt":
            raise OSError("Gate 1 direct process harness is Windows-only")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("Win32 ctypes is unavailable")
        self._kernel32 = cast(Any, loader("kernel32.dll", use_last_error=True))
        self._advapi32 = cast(Any, loader("advapi32.dll", use_last_error=True))
        self._create_pipe = self._kernel32.CreatePipe
        self._create_pipe.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(_SecurityAttributes),
            ctypes.c_uint32,
        ]
        self._create_pipe.restype = ctypes.c_int32
        self._set_handle_information = self._kernel32.SetHandleInformation
        self._set_handle_information.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        self._set_handle_information.restype = ctypes.c_int32
        self._create_file = self._kernel32.CreateFileW
        self._create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_SecurityAttributes),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._create_file.restype = ctypes.c_void_p
        self._read_file = self._kernel32.ReadFile
        self._read_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._read_file.restype = ctypes.c_int32
        self._wait = self._kernel32.WaitForSingleObject
        self._wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._wait.restype = ctypes.c_uint32
        self._terminate = self._kernel32.TerminateProcess
        self._terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._terminate.restype = ctypes.c_int32
        self._get_exit_code = self._kernel32.GetExitCodeProcess
        self._get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self._get_exit_code.restype = ctypes.c_int32
        self._close = self._kernel32.CloseHandle
        self._close.argtypes = [ctypes.c_void_p]
        self._close.restype = ctypes.c_int32
        self._create = self._advapi32.CreateProcessWithLogonW
        self._create.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(_StartupInfoW),
            ctypes.POINTER(_ProcessInformation),
        ]
        self._create.restype = ctypes.c_int32

    @staticmethod
    def _last_error() -> int:
        getter = getattr(ctypes, "get_last_error", None)
        return int(getter()) if getter is not None else 0

    def _close_handle(self, handle: int | None) -> None:
        if handle:
            self._close(ctypes.c_void_p(handle))

    def _new_pipe(self) -> tuple[int, int]:
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            None,
            1,
        )
        read_handle = ctypes.c_void_p()
        write_handle = ctypes.c_void_p()
        if not self._create_pipe(
            ctypes.byref(read_handle),
            ctypes.byref(write_handle),
            ctypes.byref(attributes),
            0,
        ):
            raise OSError(self._last_error(), "CreatePipe failed")
        if not self._set_handle_information(
            read_handle,
            _HANDLE_FLAG_INHERIT,
            0,
        ):
            self._close_handle(int(read_handle.value or 0))
            self._close_handle(int(write_handle.value or 0))
            raise OSError(self._last_error(), "SetHandleInformation failed")
        return int(read_handle.value or 0), int(write_handle.value or 0)

    def _null_input(self) -> int:
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            None,
            1,
        )
        handle = self._create_file(
            "NUL",
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            ctypes.byref(attributes),
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or int(cast(int, handle)) == -1:
            raise OSError(self._last_error(), "CreateFileW(NUL) failed")
        return int(cast(int, handle))

    def run(
        self,
        *,
        username: str,
        password: str,
        executable: Path,
        arguments: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        logon_flags: int,
    ) -> dict[str, object]:  # pragma: no cover - Windows CI
        stdout_read, stdout_write = self._new_pipe()
        stderr_read, stderr_write = self._new_pipe()
        stdin_handle = self._null_input()
        process_info = _ProcessInformation()
        command = subprocess.list2cmdline([str(executable), *arguments])
        mutable_command = ctypes.create_unicode_buffer(command)
        environment_block = _environment_block(environment)
        startup = _StartupInfoW()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = _STARTF_USESTDHANDLES
        startup.hStdInput = ctypes.c_void_p(stdin_handle)
        startup.hStdOutput = ctypes.c_void_p(stdout_write)
        startup.hStdError = ctypes.c_void_p(stderr_write)
        result: dict[str, object] = {
            "spawn_result": "NOT_STARTED",
            "logon_flags": "LOGON_WITH_PROFILE" if logon_flags else "NONE",
            "exit_code": None,
            "timeout": False,
            "stdout_preview": "",
            "stderr_preview": "",
            "win32_error": None,
            "classification": "INCONCLUSIVE",
            "token_attestation": "NOT_APPLICABLE",
        }
        stdout = bytearray()
        stderr = bytearray()
        readers: list[threading.Thread] = []

        def drain(handle: int, target: bytearray) -> None:
            buffer = ctypes.create_string_buffer(65_536)
            while True:
                returned = ctypes.c_uint32()
                ok = self._read_file(
                    ctypes.c_void_p(handle),
                    buffer,
                    ctypes.sizeof(buffer),
                    ctypes.byref(returned),
                    None,
                )
                if not ok or returned.value == 0:
                    return
                if len(target) < _MAX_OUTPUT_BYTES:
                    target.extend(
                        bytes(buffer.raw[: returned.value])[: _MAX_OUTPUT_BYTES - len(target)]
                    )

        try:
            created = self._create(
                username,
                ".",
                password,
                logon_flags,
                str(executable),
                mutable_command,
                _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
                ctypes.cast(environment_block, ctypes.c_void_p),
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            )
            if not created or not process_info.hProcess:
                raise OSError(self._last_error(), "CreateProcessWithLogonW failed")
            result["spawn_result"] = "PASS"
            process_handle = int(cast(int, process_info.hProcess))
            self._close_handle(int(cast(int, process_info.hThread)))
            self._close_handle(stdin_handle)
            self._close_handle(stdout_write)
            self._close_handle(stderr_write)
            readers = [
                threading.Thread(target=drain, args=(stdout_read, stdout), daemon=True),
                threading.Thread(target=drain, args=(stderr_read, stderr), daemon=True),
            ]
            for reader in readers:
                reader.start()
            wait_result = cast(
                int,
                self._wait(ctypes.c_void_p(process_handle), int(_TIMEOUT_SECONDS * 1000)),
            )
            if wait_result == _WAIT_TIMEOUT:
                result["timeout"] = True
                result["classification"] = "TIMEOUT"
                self._terminate(ctypes.c_void_p(process_handle), 0xC000013A)
                self._wait(ctypes.c_void_p(process_handle), 2_000)
            elif wait_result != _WAIT_OBJECT_0:
                result["classification"] = "WAIT_FAILED"
                result["win32_error"] = wait_result
            else:
                exit_code = ctypes.c_uint32()
                if not self._get_exit_code(
                    ctypes.c_void_p(process_handle), ctypes.byref(exit_code)
                ):
                    raise OSError(self._last_error(), "GetExitCodeProcess failed")
                result["exit_code"] = int(exit_code.value)
            self._close_handle(process_handle)
        except (OSError, subprocess.SubprocessError) as error:
            result["classification"] = _classify_exception(error)
            result["win32_error"] = _error_code(error)
        finally:
            self._close_handle(stdin_handle)
            self._close_handle(stdout_write)
            self._close_handle(stderr_write)
            for reader in readers:
                reader.join(timeout=2.0)
            self._close_handle(stdout_read)
            self._close_handle(stderr_read)
            result["stdout_preview"] = _preview(bytes(stdout))
            result["stderr_preview"] = _preview(bytes(stderr))
            if result["win32_error"] is None:
                result["win32_error"] = _reported_error_code(bytes(stdout), bytes(stderr))
            if result["classification"] == "INCONCLUSIVE" and not result["timeout"]:
                observed_exit_code = result.get("exit_code")
                if isinstance(observed_exit_code, int):
                    result["classification"] = _completed_classification(
                        _Workload(
                            "GATE1_DIRECT",
                            "direct",
                            executable,
                            arguments,
                        ),
                        observed_exit_code,
                        bytes(stdout),
                        bytes(stderr),
                    )
        return result


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


def _probe_result(output: str) -> dict[str, object]:
    """Parse only fixed probe markers; never retain arbitrary output."""

    text = _strip_terminal(output)
    return {
        "started": "W5_GATE1_PROBE_STARTED" in text,
        "finished": "W5_GATE1_PROBE_FINISHED" in text,
        "profile": (
            "AVAILABLE"
            if "W5_GATE1_PROFILE=AVAILABLE" in text
            else "UNAVAILABLE"
            if "W5_GATE1_PROFILE=UNAVAILABLE" in text
            else "UNKNOWN"
        ),
        "token": "ERROR" if "W5_GATE1_TOKEN=ERROR" in text else "PASS",
    }


def _environment_for(request: SandboxedProcessRequest) -> dict[str, str]:
    """Use the same explicit allowlist that W3 gives to the final child."""

    return WindowsNativeLocalProcessSandbox._child_environment(request.environment_policy)


def _cell_from_direct(
    spec: _Workload,
    *,
    workspace: Path,
    harness: _Gate1DirectProcess,
    username: str,
    password: str,
    logon_flags: int,
) -> dict[str, object]:
    if spec.executable is None:
        return {
            "classification": "NOT_INSTALLED",
            "spawn_result": "NOT_APPLICABLE",
            "token_attestation": "NOT_APPLICABLE",
        }
    result = harness.run(
        username=username,
        password=password,
        executable=spec.executable,
        arguments=spec.arguments,
        cwd=spec.cwd or workspace,
        environment=_environment_for(_request(spec, workspace)),
        logon_flags=logon_flags,
    )
    result["identity"] = "ONLINE"
    result["profile"] = "WITH_PROFILE" if logon_flags else "NO_PROFILE"
    return result


def _matrix_projection(matrix: list[dict[str, object]]) -> dict[str, object]:
    projection: dict[str, object] = {}
    for row in matrix:
        name = str(row.get("workload"))
        projection[name] = {
            key: (value.get("classification") if isinstance(value, dict) else value)
            for key, value in row.items()
            if key != "workload"
        }
    return projection


async def _cleanup_probe_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate1RuntimeRootCauseTests(unittest.IsolatedAsyncioTestCase):
    """Run Gate 1 exactly once on the elevated Windows evidence runner."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 1 evidence requires the enabled CI gate"
    )
    async def test_gate1_runtime_root_cause_matrix(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        compile_probe = cast(
            Callable[[Path, str], Path],
            (
                __import__(
                    "tests.security.test_windows_native_runtime_acceptance",
                    fromlist=["_compile_msvc_probe"],
                )
            )._compile_msvc_probe,
        )
        probe = await asyncio.to_thread(
            compile_probe,
            Path(__file__).with_name("windows_w5_gate1_probe.c"),
            "windows_w5_gate1_probe",
        )
        self.addAsyncCleanup(_cleanup_probe_directory, probe.parent)
        nul_probe = await asyncio.to_thread(
            __import__(
                "tests.security.test_windows_native_workload_compatibility",
                fromlist=["_compile_nul_probe"],
            )._compile_nul_probe
        )
        self.addAsyncCleanup(_cleanup_probe_directory, nul_probe.parent)
        paths = _tool_paths()
        paths["python_base"] = __import__(
            "tests.security.test_windows_native_workload_compatibility",
            fromlist=["_discover_base_python"],
        )._discover_base_python(paths["python"])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            repo = workspace / "compat-repo"
            repo.mkdir()
            if paths["git"] is not None:
                await asyncio.to_thread(
                    subprocess.run,
                    [str(paths["git"]), "init", "-q", str(repo)],
                    check=False,
                    capture_output=True,
                    timeout=15,
                    shell=False,
                )
            copied_probe = workspace / "windows-w5-gate1-probe.exe"
            shutil.copy2(probe, copied_probe)
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                account_api=__import__(
                    "neuro_code.infrastructure.sandbox.windows_sandbox_accounts",
                    fromlist=["_NativeWindowsSandboxAccountApi"],
                )._NativeWindowsSandboxAccountApi(),
                acl_api=__import__(
                    "neuro_code.infrastructure.sandbox.windows_sandbox_acl",
                    fromlist=["_NativeWindowsAclApi"],
                )._NativeWindowsAclApi(),
                firewall_api=__import__(
                    "neuro_code.infrastructure.sandbox.windows_sandbox_firewall",
                    fromlist=["_NativeWindowsFirewallApi"],
                )._NativeWindowsFirewallApi(),
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            expected_online_sid = online.user_sid.value
            expected_write_sid = record.write_sid.value
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                runtime_state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
                _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                _diagnostic_create_no_window=True,
            )
            harness = _Gate1DirectProcess()
            original_logon_flags = windows_native_runner._LOGON_FLAGS
            try:
                probe_spec = _Workload("GATE1_NATIVE_PROBE", "fixed", copied_probe, ())
                probe_request = _request(probe_spec, workspace)
                environment = _environment_for(probe_request)
                probe_matrix: dict[str, object] = {
                    "HOST": _host_run(probe_spec, workspace),
                    "W2_UNRESTRICTED_NO_PROFILE": harness.run(
                        username=online.username,
                        password=online.password.decode("utf-8"),
                        executable=copied_probe,
                        arguments=(),
                        cwd=workspace,
                        environment=environment,
                        logon_flags=0,
                    ),
                    "W2_UNRESTRICTED_WITH_PROFILE": harness.run(
                        username=online.username,
                        password=online.password.decode("utf-8"),
                        executable=copied_probe,
                        arguments=(),
                        cwd=workspace,
                        environment=environment,
                        logon_flags=_LOGON_WITH_PROFILE,
                    ),
                }
                probe_matrix["W2_UNRESTRICTED_NO_PROFILE"] = {
                    **cast(dict[str, object], probe_matrix["W2_UNRESTRICTED_NO_PROFILE"]),
                    "probe": _probe_result(
                        str(
                            cast(dict[str, object], probe_matrix["W2_UNRESTRICTED_NO_PROFILE"]).get(
                                "stdout_preview", ""
                            )
                        )
                    ),
                }
                probe_matrix["W2_UNRESTRICTED_WITH_PROFILE"] = {
                    **cast(dict[str, object], probe_matrix["W2_UNRESTRICTED_WITH_PROFILE"]),
                    "probe": _probe_result(
                        str(
                            cast(
                                dict[str, object], probe_matrix["W2_UNRESTRICTED_WITH_PROFILE"]
                            ).get("stdout_preview", "")
                        )
                    ),
                }
                probe_matrix["W2_RESTRICTED_NO_PROFILE"] = await _w3_run(
                    probe_spec,
                    workspace=workspace,
                    adapter=adapter,
                    expected_user_sid=expected_online_sid,
                    expected_write_sid=expected_write_sid,
                )
                original_flags = windows_native_runner._LOGON_FLAGS
                try:
                    windows_native_runner._LOGON_FLAGS = _LOGON_WITH_PROFILE
                    probe_matrix["W2_RESTRICTED_WITH_PROFILE"] = await _w3_run(
                        probe_spec,
                        workspace=workspace,
                        adapter=adapter,
                        expected_user_sid=expected_online_sid,
                        expected_write_sid=expected_write_sid,
                    )
                finally:
                    windows_native_runner._LOGON_FLAGS = original_flags
                for authority in (
                    "HOST",
                    "W2_RESTRICTED_NO_PROFILE",
                    "W2_RESTRICTED_WITH_PROFILE",
                ):
                    cell = probe_matrix.get(authority)
                    if isinstance(cell, dict):
                        probe_matrix[authority] = {
                            **cell,
                            "probe": _probe_result(str(cell.get("stdout_preview", ""))),
                        }

                workloads = _build_workloads(
                    workspace=workspace,
                    repo=repo,
                    nul_probe=nul_probe,
                    cmd=paths["cmd"],
                    powershell=paths["powershell"],
                    pwsh=paths["pwsh"],
                    python=paths["python"],
                    python_base=paths["python_base"],
                    git=paths["git"],
                    node=paths["node"],
                    npm=paths["npm"],
                    curl=paths["curl"],
                )
                workloads = [
                    workload for workload in workloads if workload.name in _PROBE_WORKLOADS
                ]
                matrix: list[dict[str, object]] = []
                for workload in workloads:
                    row: dict[str, object] = {
                        "workload": workload.name,
                        "HOST": await asyncio.to_thread(_host_run, workload, workspace),
                        "W2_UNRESTRICTED_NO_PROFILE": await asyncio.to_thread(
                            _cell_from_direct,
                            workload,
                            workspace=workspace,
                            harness=harness,
                            username=online.username,
                            password=online.password.decode("utf-8"),
                            logon_flags=0,
                        ),
                        "W2_UNRESTRICTED_WITH_PROFILE": await asyncio.to_thread(
                            _cell_from_direct,
                            workload,
                            workspace=workspace,
                            harness=harness,
                            username=online.username,
                            password=online.password.decode("utf-8"),
                            logon_flags=_LOGON_WITH_PROFILE,
                        ),
                    }
                    row["W2_RESTRICTED_NO_PROFILE"] = await _w3_run(
                        workload,
                        workspace=workspace,
                        adapter=adapter,
                        expected_user_sid=expected_online_sid,
                        expected_write_sid=expected_write_sid,
                    )
                    original_flags = windows_native_runner._LOGON_FLAGS
                    try:
                        windows_native_runner._LOGON_FLAGS = _LOGON_WITH_PROFILE
                        row["W2_RESTRICTED_WITH_PROFILE"] = await _w3_run(
                            workload,
                            workspace=workspace,
                            adapter=adapter,
                            expected_user_sid=expected_online_sid,
                            expected_write_sid=expected_write_sid,
                        )
                    finally:
                        windows_native_runner._LOGON_FLAGS = original_flags
                    matrix.append(row)

                artifact = {
                    "gate": "W5_GATE1",
                    "base": "00879b9b71f637804ff6e40c82451d86f2bd6165",
                    "authorities": [
                        "HOST",
                        "W2_UNRESTRICTED_NO_PROFILE",
                        "W2_UNRESTRICTED_WITH_PROFILE",
                        "W2_RESTRICTED_NO_PROFILE",
                        "W2_RESTRICTED_WITH_PROFILE",
                    ],
                    "probe": probe_matrix,
                    "matrix": matrix,
                    "projection": _matrix_projection(matrix),
                    "security_contract": {
                        "restricted_no_profile": {
                            "expected_user_sid": expected_online_sid,
                            "expected_restricted_sids": [expected_write_sid],
                            "attestation": "W3 exact singleton contract",
                        },
                        "profile_experiment": "in-memory _LOGON_FLAGS only",
                        "production_source_diff": 0,
                    },
                    "tool_provenance": _provenance(paths, workspace),
                }
                artifact_path = os.environ.get("NEURO_CODE_W5_GATE1_EVIDENCE_JSON")
                if artifact_path:
                    await asyncio.to_thread(
                        Path(artifact_path).write_text,
                        json.dumps(artifact, sort_keys=True, indent=2),
                        encoding="utf-8",
                    )
                print("W5_GATE1_PROBE=" + json.dumps(probe_matrix, sort_keys=True), flush=True)
                print("W5_GATE1_MATRIX=" + json.dumps(matrix, sort_keys=True), flush=True)
            finally:
                await asyncio.to_thread(authority.cleanup, setup_request)
                # Prevent an evidence failure from leaking the in-memory
                # experiment into a subsequent test process.
                windows_native_runner._LOGON_FLAGS = original_logon_flags


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
