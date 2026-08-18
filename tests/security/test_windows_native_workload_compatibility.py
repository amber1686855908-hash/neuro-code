"""W5 Gate 0 evidence for ordinary Windows developer workloads.

This module deliberately measures compatibility without changing the W1-W4
runtime.  Every workload first runs as a host control, then through the W3
non-PTY port and the W4 application PTY port.  A workload incompatibility is
reported as data; harness failures still fail the test so evidence cannot be
silently lost.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir, mkdtemp
from typing import Any, cast

from tests.security.windows_token_attestation import (
    token_attestation_is_exact,
    token_attestation_projection,
)

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
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
from neuro_code.application.sessions.terminal_sessions import LocalInteractiveTerminalManager
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.terminal.models import TerminalSize
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.sandbox.windows_native_runner import _WindowsNativeDesktopMode
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
from neuro_code.infrastructure.workspace.paths import FilesystemWorkspacePathResolver

_TIMEOUT_SECONDS = 15.0
_MAX_PREVIEW_BYTES = 512
_MAX_OUTPUT_BYTES = 1 << 20
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_Command = tuple[str, ...]


class _NativeProbeBuildError(RuntimeError):
    """The trusted Windows controller could not build the NUL probe."""


@dataclass(frozen=True, slots=True)
class _Workload:
    name: str
    variant: str
    executable: Path | None
    arguments: _Command
    expected_patterns: tuple[str, ...] = ()
    require_empty_output: bool = False
    exit_only: bool = False
    note: str = ""
    cwd: Path | None = None
    resolved_launcher: Path | None = None


def _native_enabled() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("NEURO_CODE_RUN_NATIVE_WINDOWS_SANDBOX_ACCEPTANCE") == "1"
    )


def _system_root() -> Path:
    value = os.environ.get("SYSTEMROOT")
    return Path(value or r"C:\Windows")


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _find_tool(*names: str) -> Path | None:
    for name in names:
        discovered = shutil.which(name)
        if discovered:
            return _canonical(Path(discovered))
    return None


def _find_vswhere() -> Path:
    discovered = shutil.which("vswhere.exe")
    if discovered:
        return _canonical(Path(discovered))
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    if program_files_x86:
        candidate = (
            Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        )
        if candidate.is_file():
            return _canonical(candidate)
    raise _NativeProbeBuildError("vswhere.exe is unavailable")


def _compile_nul_probe() -> Path:  # pragma: no cover - Windows CI
    source = Path(__file__).with_name("windows_nul_probe.c").resolve(strict=False)
    if not source.is_file():
        raise _NativeProbeBuildError("NUL probe source is unavailable")
    discovery = subprocess.run(
        [
            str(_find_vswhere()),
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
    build_directory = Path(
        mkdtemp(prefix="neuro-code-w5-nul-", dir=os.environ.get("RUNNER_TEMP", gettempdir()))
    )
    output = build_directory / "windows_nul_probe.exe"
    script = build_directory / "build_probe.cmd"
    script.write_text(
        "@echo off\r\n"
        f'call "{vcvars}"\r\n'
        "if errorlevel 1 exit /b 1\r\n"
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}"\r\n',
        encoding="ascii",
        newline="",
    )
    build = subprocess.run(
        ["cmd.exe", "/d", "/c", script.name],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        shell=False,
        cwd=str(build_directory),
    )
    if build.returncode != 0 or not output.is_file():
        detail = (build.stderr or build.stdout or "").strip().replace("\x00", "")[:512]
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _NativeProbeBuildError(
            f"MSVC NUL probe build failed (returncode={build.returncode}): {detail}"
        )
    return output


def _strip_terminal(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "")


def _preview(data: bytes) -> str:
    value = _strip_terminal(data.decode("utf-8", errors="replace"))
    value = value.replace("\x00", "<NUL>").strip()
    return value[:_MAX_PREVIEW_BYTES]


def _error_code(error: BaseException) -> int | None:
    value = getattr(error, "winerror", None)
    return value if isinstance(value, int) else None


def _classify_exception(error: BaseException) -> str:
    if isinstance(error, PermissionError) or _error_code(error) == 5:
        return "ACCESS_DENIED"
    text = str(error).casefold()
    if "dll" in text or "side-by-side" in text:
        return "DEPENDENCY_OR_DLL_FAILURE"
    if "timed out" in text or "timeout" in text:
        return "TIMEOUT"
    return "PROCESS_CREATE_FAILURE"


def _command_for(spec: _Workload) -> list[str]:
    if spec.executable is None:
        return []
    return [str(spec.executable), *spec.arguments]


def _output_matches(spec: _Workload, stdout: bytes, stderr: bytes) -> bool:
    combined = _strip_terminal((stdout + b"\n" + stderr).decode("utf-8", errors="replace")).replace(
        "\\", "/"
    )
    folded = combined.casefold()
    if spec.require_empty_output and combined.strip():
        return False
    if spec.name == "GIT_REPO_DISCOVERY":
        lines = [line.strip() for line in combined.splitlines() if line.strip()]
        if not lines or spec.cwd is None:
            return False
        try:
            return _canonical(Path(lines[0])) == _canonical(spec.cwd)
        except (OSError, RuntimeError, ValueError):
            return False
    return not spec.expected_patterns or all(
        re.search(pattern, folded, flags=re.IGNORECASE | re.DOTALL)
        for pattern in spec.expected_patterns
    )


def _reported_error_code(stdout: bytes, stderr: bytes) -> int | None:
    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace").replace("\x00", "")
    matches = re.findall(
        r"(create_error|write_error|hresult)[^0-9a-f]{0,12}((?:0x)?[0-9a-f]+)",
        text,
        flags=re.IGNORECASE,
    )
    for label, value in matches:
        try:
            parsed = int(value, 16 if label.casefold() == "hresult" else 10)
        except ValueError:
            continue
        if parsed:
            return parsed
    return None


def _nul_mode_results(stdout: bytes, stderr: bytes) -> dict[str, object] | None:
    text = _strip_terminal((stdout + b"\n" + stderr).decode("utf-8", errors="replace")).replace(
        "\x00", ""
    )
    marker = "W5_NUL_DIRECT="
    marker_start = text.find(marker)
    if marker_start < 0:
        return None
    payload = text[marker_start + len(marker) :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _completed_classification(spec: _Workload, exit_code: int, stdout: bytes, stderr: bytes) -> str:
    if exit_code == 0:
        return "PASS" if _output_matches(spec, stdout, stderr) else "OUTPUT_MISMATCH"
    if "NUL" in spec.name:
        evidence = (
            (stdout + b"\n" + stderr)
            .decode("utf-8", errors="replace")
            .replace("\x00", "")
            .casefold()
        )
        if (
            exit_code in {2, 3}
            or "access is denied" in evidence
            or '"create_file":"fail"' in evidence
            or '"create":"fail"' in evidence
            or '"write":"fail"' in evidence
        ):
            return "DEVICE_ACCESS_DENIED"
    evidence = (
        (stdout + b"\n" + stderr).decode("utf-8", errors="replace").replace("\x00", "").casefold()
    )
    if spec.name.startswith("GIT") and "could not open '/dev/null'" in evidence:
        return "DEVICE_ACCESS_DENIED"
    if spec.name == "GIT_REPO_DISCOVERY" and exit_code != 0:
        return "REPOSITORY_DISCOVERY_FAILURE"
    if spec.name.startswith("PYTHON") and spec.name != "PYTHON_VERSION":
        return "RUNTIME_INITIALIZATION_FAILURE"
    if "starting the clr failed" in evidence or "hresult 80070005" in evidence:
        return "RUNTIME_INITIALIZATION_FAILURE"
    return "NONZERO_EXIT"


def _cell_base(
    spec: _Workload, *, path: str, identity: str, profile: str, stdio: str
) -> dict[str, object]:
    return {
        "execution_path": path,
        "resolved_executable": str(spec.executable) if spec.executable else None,
        "resolved_launcher": str(spec.resolved_launcher) if spec.resolved_launcher else None,
        "argv": _command_for(spec),
        "identity": identity,
        "profile": profile,
        "stdio": stdio,
        "spawn_result": "NOT_STARTED",
        "spawn_ready": "NOT_APPLICABLE",
        "token_attestation": "NOT_APPLICABLE",
        "exit_code": None,
        "timeout": False,
        "stdout_preview": "",
        "stderr_preview": "",
        "win32_error": None,
        "runner_exit": "NOT_APPLICABLE",
        "forced_termination": False,
        "orphan_count": None,
        "timeout_cleanup": "NOT_APPLICABLE",
        "timeout_drain": "NOT_APPLICABLE",
        "classification": "INCONCLUSIVE",
        "note": spec.note,
    }


def _not_installed(
    spec: _Workload, *, path: str, identity: str, profile: str, stdio: str
) -> dict[str, object]:
    result = _cell_base(spec, path=path, identity=identity, profile=profile, stdio=stdio)
    result.update(
        {
            "spawn_result": "NOT_APPLICABLE",
            "classification": "NOT_INSTALLED",
            "note": "resolved executable is not installed",
        }
    )
    return result


def _workload_cwd(spec: _Workload, workspace: Path) -> Path:
    cwd = spec.cwd or workspace
    if not cwd.is_absolute() or not (cwd == workspace or cwd.is_relative_to(workspace)):
        raise RuntimeError("compatibility workload cwd escaped the disposable workspace")
    return cwd


def _host_run(spec: _Workload, workspace: Path) -> dict[str, object]:
    result = _cell_base(spec, path="HOST/OFF", identity="HOST", profile="OFF", stdio="CAPTURE")
    if spec.executable is None:
        return _not_installed(
            spec, path="HOST/OFF", identity="HOST", profile="OFF", stdio="CAPTURE"
        )
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            _command_for(spec),
            cwd=str(_workload_cwd(spec, workspace)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        result["spawn_result"] = "PASS"
        try:
            stdout, stderr = process.communicate(timeout=_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            result["timeout"] = True
            process.kill()
            stdout, stderr = process.communicate()
            result["classification"] = "TIMEOUT"
            result["note"] = "host control exceeded bounded timeout"
        result["exit_code"] = process.returncode
        result["stdout_preview"] = _preview(stdout)
        result["stderr_preview"] = _preview(stderr)
        result["nul_modes"] = _nul_mode_results(stdout, stderr)
        result["win32_error"] = _reported_error_code(stdout, stderr)
        if not result["timeout"]:
            result["classification"] = _completed_classification(
                spec, process.returncode, stdout, stderr
            )
    except (OSError, subprocess.SubprocessError) as error:
        result["classification"] = _classify_exception(error)
        result["note"] = str(error)[:512]
        result["win32_error"] = _error_code(error)
    return result


async def _read_output(stream: object | None) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        value = await cast(Any, stream).read(65_536)
        if not isinstance(value, bytes) or not value:
            return b"".join(chunks)
        total += len(value)
        if total > _MAX_OUTPUT_BYTES:
            raise RuntimeError("bounded compatibility output exceeded")
        chunks.append(value)


async def _remove_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _write_json_artifact(path: str, payload: dict[str, object]) -> None:
    Path(path).write_text(
        json.dumps(payload, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _security_attestation_status_for(
    diagnostic: object,
    *,
    expected_user_sid: str | None,
    expected_write_sid: str | None,
) -> str:
    if not isinstance(diagnostic, dict):
        return "UNKNOWN"
    if expected_user_sid is None or expected_write_sid is None:
        return "UNKNOWN"
    if token_attestation_is_exact(
        diagnostic,
        expected_user_sid=expected_user_sid,
        expected_write_sid=expected_write_sid,
    ):
        return "PASS"
    return "FAIL"


def _security_attestation_projection(diagnostic: object) -> dict[str, Any] | None:
    return token_attestation_projection(diagnostic)


def _record_security_attestation(
    result: dict[str, object],
    diagnostic: object,
    *,
    expected_user_sid: str,
    expected_write_sid: str,
) -> None:
    result["token_attestation"] = _security_attestation_status_for(
        diagnostic,
        expected_user_sid=expected_user_sid,
        expected_write_sid=expected_write_sid,
    )
    result["security_attestation"] = _security_attestation_projection(diagnostic)


def _runner_projection(diagnostic: object) -> object:
    if not isinstance(diagnostic, dict):
        return "UNKNOWN"
    runner = diagnostic.get("runner")
    if isinstance(runner, dict):
        return {key: runner[key] for key in ("state", "exit_code", "wait_error") if key in runner}
    return diagnostic.get("runner_state", "UNKNOWN")


def _request(spec: _Workload, workspace: Path) -> SandboxedProcessRequest:
    if spec.executable is None:
        raise ValueError("cannot create a request for an unavailable workload")
    system_root = str(_system_root())
    environment = {
        "SystemRoot": system_root,
        "SystemDrive": os.environ.get("SYSTEMDRIVE", "C:"),
        "PATH": os.environ.get("PATH", ""),
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
    }
    return SandboxedProcessRequest.exec(
        str(spec.executable),
        spec.arguments,
        purpose=LocalProcessPurpose.BASH,
        cwd=_workload_cwd(spec, workspace),
        sandbox_profile=SandboxProfile.WORKSPACE,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),)
        ),
        network_policy=LocalProcessNetworkPolicy.INHERIT,
        environment_policy=LocalProcessEnvironmentPolicy(environment),
        stdio_mode=LocalProcessStdioMode.CAPTURE,
        lifecycle=LocalProcessLifecycle(),
    )


async def _w3_run(
    spec: _Workload,
    *,
    workspace: Path,
    adapter: WindowsNativeLocalProcessSandbox,
    expected_user_sid: str,
    expected_write_sid: str,
) -> dict[str, object]:
    if spec.executable is None:
        return _not_installed(
            spec, path="W3_NON_PTY", identity="ONLINE", profile="WORKSPACE", stdio="CAPTURE"
        )
    result = _cell_base(
        spec, path="W3_NON_PTY", identity="ONLINE", profile="WORKSPACE", stdio="CAPTURE"
    )
    process: Any = None
    tasks: tuple[asyncio.Task[bytes], asyncio.Task[bytes], asyncio.Task[int]] | None = None
    stdout = b""
    stderr = b""
    try:
        process = await adapter.spawn(_request(spec, workspace))
        result["spawn_result"] = "PASS"
        result["spawn_ready"] = "PASS"
        diagnostic = process.diagnostic_snapshot()
        _record_security_attestation(
            result,
            diagnostic,
            expected_user_sid=expected_user_sid,
            expected_write_sid=expected_write_sid,
        )
        tasks = (
            asyncio.create_task(_read_output(process.stdout)),
            asyncio.create_task(_read_output(process.stderr)),
            asyncio.create_task(process.wait()),
        )
        done, _ = await asyncio.wait(tasks, timeout=_TIMEOUT_SECONDS)
        if len(done) == len(tasks):
            with contextlib.suppress(BaseException):
                stdout = tasks[0].result()
            with contextlib.suppress(BaseException):
                stderr = tasks[1].result()
            with contextlib.suppress(BaseException):
                result["exit_code"] = tasks[2].result()
        else:
            result["timeout"] = True
            result["classification"] = "TIMEOUT"
            result["timeout_cleanup"] = "canonical_process_terminate"
            await process.terminate(grace_seconds=0.5)
            result["timeout_drain"] = "bounded_2s_pipe_drain"
            drained, _ = await asyncio.wait(tasks, timeout=2.0)
            if tasks[0] in drained:
                with contextlib.suppress(BaseException):
                    stdout = tasks[0].result()
            if tasks[1] in drained:
                with contextlib.suppress(BaseException):
                    stderr = tasks[1].result()
            if tasks[2] in drained:
                with contextlib.suppress(BaseException):
                    result["exit_code"] = tasks[2].result()
        diagnostic = process.diagnostic_snapshot()
        result["runner_exit"] = _runner_projection(diagnostic)
        result["forced_termination"] = bool(
            isinstance(diagnostic, dict) and diagnostic.get("runner_forced_termination", False)
        )
        _record_security_attestation(
            result,
            diagnostic,
            expected_user_sid=expected_user_sid,
            expected_write_sid=expected_write_sid,
        )
    except BaseException as error:
        result["classification"] = _classify_exception(error)
        result["note"] = str(error)[:512]
        result["win32_error"] = _error_code(error)
        if process is not None:
            with contextlib.suppress(BaseException):
                await process.terminate(grace_seconds=0.5)
    finally:
        if tasks is not None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not stdout and tasks[0].done() and not tasks[0].cancelled():
                with contextlib.suppress(BaseException):
                    stdout = tasks[0].result()
            if not stderr and tasks[1].done() and not tasks[1].cancelled():
                with contextlib.suppress(BaseException):
                    stderr = tasks[1].result()
        result["stdout_preview"] = _preview(stdout)
        result["nul_modes"] = _nul_mode_results(stdout, b"")
        result["stderr_preview"] = _preview(stderr)
        if result["win32_error"] is None:
            result["win32_error"] = _reported_error_code(stdout, stderr)
        if result["classification"] == "INCONCLUSIVE" and not result["timeout"]:
            observed_exit = result.get("exit_code")
            if isinstance(observed_exit, int):
                result["classification"] = _completed_classification(
                    spec, observed_exit, stdout, stderr
                )
    return result


async def _w4_run(
    spec: _Workload,
    *,
    workspace: Path,
    manager: LocalInteractiveTerminalManager,
    expected_user_sid: str,
    expected_write_sid: str,
) -> dict[str, object]:
    if spec.executable is None:
        return _not_installed(
            spec, path="W4_PTY", identity="ONLINE", profile="WORKSPACE", stdio="PTY"
        )
    result = _cell_base(spec, path="W4_PTY", identity="ONLINE", profile="WORKSPACE", stdio="PTY")
    session: Any = None
    output = bytearray()
    offset = 0
    try:
        cwd = _workload_cwd(spec, workspace)
        relative_cwd = cwd.relative_to(workspace)
        session = await manager.create_exec(
            f"w5-{spec.name.lower()}-{spec.variant}",
            str(spec.executable),
            spec.arguments,
            cwd=str(relative_cwd) if str(relative_cwd) else ".",
            env={},
            size=TerminalSize(100, 30),
            output_capacity=_MAX_OUTPUT_BYTES,
        )
        result["spawn_result"] = "PASS"
        result["spawn_ready"] = "PASS"
        deadline = asyncio.get_running_loop().time() + _TIMEOUT_SECONDS
        while True:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                raise TimeoutError("W4 output drain timed out")
            chunk = await session.read(
                after_offset=offset, max_bytes=65_536, wait_seconds=min(0.25, remaining)
            )
            output.extend(chunk.data)
            offset = chunk.next_offset
            if len(output) > _MAX_OUTPUT_BYTES:
                raise RuntimeError("bounded PTY output exceeded")
            if chunk.eof:
                break
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        result["exit_code"] = await session.wait(timeout_seconds=remaining)
        platform = getattr(session, "_platform_session", None)
        diagnostic = platform.diagnostic_snapshot() if platform is not None else None
        result["runner_exit"] = _runner_projection(diagnostic)
        result["forced_termination"] = bool(
            isinstance(diagnostic, dict) and diagnostic.get("runner_forced_termination", False)
        )
        _record_security_attestation(
            result,
            diagnostic,
            expected_user_sid=expected_user_sid,
            expected_write_sid=expected_write_sid,
        )
    except TimeoutError as error:
        result["timeout"] = True
        result["classification"] = "TIMEOUT"
        result["timeout_cleanup"] = "canonical_terminal_session_close"
        result["timeout_drain"] = "bounded_session_close"
        result["note"] = str(error)[:512]
    except BaseException as error:
        result["classification"] = _classify_exception(error)
        result["note"] = str(error)[:512]
        result["win32_error"] = _error_code(error)
    finally:
        if session is not None:
            with contextlib.suppress(BaseException):
                await session.close()
            platform = getattr(session, "_platform_session", None)
            diagnostic = platform.diagnostic_snapshot() if platform is not None else None
            result["runner_exit"] = _runner_projection(diagnostic)
            result["forced_termination"] = bool(
                isinstance(diagnostic, dict) and diagnostic.get("runner_forced_termination", False)
            )
            _record_security_attestation(
                result,
                diagnostic,
                expected_user_sid=expected_user_sid,
                expected_write_sid=expected_write_sid,
            )
        stdout = bytes(output)
        result["stdout_preview"] = _preview(stdout)
        if result["win32_error"] is None:
            result["win32_error"] = _reported_error_code(stdout, b"")
        result["nul_modes"] = _nul_mode_results(stdout, b"")
        if result["classification"] == "INCONCLUSIVE" and not result["timeout"]:
            exit_code = result.get("exit_code")
            if isinstance(exit_code, int):
                result["classification"] = _completed_classification(spec, exit_code, stdout, b"")
    return result


def _version_command(path: Path, arguments: _Command, workspace: Path) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [str(path), *arguments],
            cwd=str(workspace),
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": _classify_exception(error), "error": str(error)[:256]}
    output = _preview(completed.stdout + b"\n" + completed.stderr)
    return {
        "status": "PASS" if completed.returncode == 0 else "NONZERO_EXIT",
        "version": output.splitlines()[0][:256] if output else "",
        "exit_code": completed.returncode,
    }


def _build_workloads(
    *,
    workspace: Path,
    repo: Path,
    nul_probe: Path,
    cmd: Path | None,
    powershell: Path | None,
    pwsh: Path | None,
    python: Path | None,
    python_base: Path | None,
    git: Path | None,
    node: Path | None,
    npm: Path | None,
    curl: Path | None,
) -> list[_Workload]:
    system_cmd = cmd
    workloads: list[_Workload] = [
        _Workload(
            "CMD_BASIC",
            "default",
            system_cmd,
            ("/d", "/s", "/c", "echo W5_CMD_OK"),
            ("w5_cmd_ok",),
        ),
        _Workload(
            "CMD_NUL_REDIRECT",
            "default",
            system_cmd,
            ("/d", "/s", "/c", "echo W5_NUL_OK>NUL"),
            exit_only=True,
            note="NUL_OUTPUT_REDIRECTION; exit code is the oracle and no stdout is expected",
        ),
        _Workload(
            "POWERSHELL_BASIC",
            "windows-powershell",
            powershell,
            (
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Write-Output 'W5_POWERSHELL_OK'",
            ),
            ("w5_powershell_ok",),
        ),
        _Workload(
            "PWSH_BASIC",
            "pwsh",
            pwsh,
            ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Write-Output 'W5_PWSH_OK'"),
            ("w5_pwsh_ok",),
        ),
        _Workload("PYTHON_VERSION", "default", python, ("--version",), ("python ",)),
        _Workload(
            "PYTHON_MINIMAL_NO_SITE",
            "-I-S",
            python,
            ("-I", "-S", "-c", "import sys; print('W5_PYTHON_MINIMAL_OK'); print(sys.executable)"),
            ("w5_python_minimal_ok",),
        ),
        _Workload(
            "PYTHON_ISOLATED",
            "-I",
            python,
            ("-I", "-c", "import sys; print('W5_PYTHON_ISOLATED_OK'); print(sys.executable)"),
            ("w5_python_isolated_ok",),
        ),
        _Workload(
            "PYTHON_NORMAL",
            "normal",
            python,
            ("-c", "import sys; print('W5_PYTHON_NORMAL_OK'); print(sys.executable)"),
            ("w5_python_normal_ok",),
        ),
        _Workload(
            "PYTHON_BASE_VERSION", "base-interpreter", python_base, ("--version",), ("python ",)
        ),
        _Workload(
            "PYTHON_BASE_MINIMAL_NO_SITE",
            "base-interpreter -I-S",
            python_base,
            (
                "-I",
                "-S",
                "-c",
                "import sys; print('W5_PYTHON_BASE_MINIMAL_OK'); print(sys.executable)",
            ),
            ("w5_python_base_minimal_ok",),
        ),
        _Workload("GIT_VERSION", "default", git, ("--version",), ("git version ",)),
        _Workload(
            "GIT_REPO_DISCOVERY",
            "workspace-repository",
            git,
            ("rev-parse", "--show-toplevel"),
            (),
            cwd=repo,
        ),
        _Workload(
            "GIT_STATUS",
            "porcelain-v1",
            git,
            ("status", "--porcelain=v1"),
            require_empty_output=True,
            cwd=repo,
        ),
        _Workload("NODE_VERSION", "default", node, ("--version",), (r"v\d+\.\d+",)),
        _Workload("NODE_EXEC", "-e", node, ("-e", "console.log('W5_NODE_OK')"), ("w5_node_ok",)),
        _Workload(
            "CURL_VERSION",
            "default",
            curl,
            ("--version",),
            ("curl ",),
        ),
        _Workload(
            "NUL_DIRECT_WIN32",
            "CreateFileW-read-write-modes",
            nul_probe,
            (),
            (
                r'"read":\{"create":"pass"',
                r'"write":\{"create":"pass"',
                r'"read_write":\{"create":"pass"',
            ),
        ),
    ]
    npm_arguments: _Command
    if npm is not None:
        if npm.suffix.casefold() in {".cmd", ".bat"} and cmd is not None:
            npm_executable = cmd
            npm_arguments = ("/d", "/c", "call", str(npm), "--version")
        else:
            npm_executable = npm
            npm_arguments = ("--version",)
    else:
        npm_executable = None
        npm_arguments = ()
    workloads.insert(
        next(index for index, workload in enumerate(workloads) if workload.name == "CURL_VERSION"),
        _Workload(
            "NPM_VERSION",
            "resolved-launcher",
            npm_executable,
            npm_arguments,
            (r"\d+\.\d+",),
            note=(f"resolved launcher: {npm}" if npm is not None else "npm launcher not installed"),
            resolved_launcher=npm,
        ),
    )
    return workloads


def _tool_paths() -> dict[str, Path | None]:
    system32 = _system_root() / "System32"
    return {
        "cmd": _canonical(system32 / "cmd.exe") if (system32 / "cmd.exe").is_file() else None,
        "powershell": (
            _canonical(system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            if (system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe").is_file()
            else None
        ),
        "pwsh": _find_tool("pwsh.exe", "pwsh"),
        "python": _canonical(Path(sys.executable)) if Path(sys.executable).is_file() else None,
        "git": _find_tool("git.exe", "git"),
        "node": _find_tool("node.exe", "node"),
        "npm": _find_tool("npm.cmd", "npm.exe", "npm"),
        "curl": _find_tool("curl.exe", "curl"),
    }


def _discover_base_python(python: Path | None) -> Path | None:
    """Resolve and verify the base interpreter behind the active venv."""

    if python is None:
        return None
    candidates: list[Path] = []
    current_base = getattr(sys, "_base_executable", None)
    if isinstance(current_base, str) and current_base:
        candidates.append(Path(current_base))
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        completed = subprocess.run(
            [str(python), "-I", "-S", "-c", "import sys; print(sys._base_executable)"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        candidates.extend(
            Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()
        )
    venv_path = _canonical(python)
    for candidate in candidates:
        if not candidate.is_absolute():
            continue
        resolved = _canonical(candidate)
        if resolved.is_file() and resolved != venv_path:
            return resolved
    return None


def _provenance(paths: dict[str, Path | None], workspace: Path) -> dict[str, object]:
    version_arguments: dict[str, _Command] = {
        "cmd": ("/d", "/c", "ver"),
        "powershell": (
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ),
        "pwsh": (
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ),
        "python": ("--version",),
        "python_base": ("--version",),
        "git": ("--version",),
        "node": ("--version",),
        "curl": ("--version",),
    }
    result: dict[str, object] = {}
    for name, path in paths.items():
        if path is None:
            result[name] = {"status": "NOT_INSTALLED"}
            continue
        args = version_arguments.get(name)
        if name == "npm":
            cmd = paths.get("cmd")
            if path.suffix.casefold() in {".cmd", ".bat"} and cmd is not None:
                version = _version_command(
                    cmd, ("/d", "/c", "call", str(path), "--version"), workspace
                )
            else:
                version = _version_command(path, ("--version",), workspace)
        else:
            version = _version_command(path, args or (), workspace)
        result[name] = {"path": str(path), **version}
    return result


class WindowsNativeWorkloadCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        _native_enabled(), "Windows W5 native evidence requires the enabled CI gate"
    )
    async def test_w5_compatibility_matrix(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        nul_probe = await asyncio.to_thread(_compile_nul_probe)
        self.addAsyncCleanup(_remove_directory, nul_probe.parent)
        paths = _tool_paths()
        paths["python_base"] = _discover_base_python(paths["python"])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            repo = workspace / "compat-repo"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            repo.mkdir()
            await asyncio.to_thread(
                subprocess.run,
                [str(paths["git"]), "init", "-q", str(repo)]
                if paths["git"] is not None
                else ["cmd.exe", "/d", "/c", "exit", "1"],
                check=False,
                capture_output=True,
                timeout=15,
                shell=False,
            )
            copied_probe = workspace / "windows-nul-probe.exe"
            shutil.copy2(nul_probe, copied_probe)
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
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            self.assertIsInstance(snapshot.online_user_sid, str)
            self.assertIsInstance(snapshot.write_restricting_sid, str)
            expected_online_sid = cast(str, snapshot.online_user_sid)
            expected_write_sid = cast(str, snapshot.write_restricting_sid)
            adapter = WindowsNativeLocalProcessSandbox(
                SandboxProfile.WORKSPACE,
                workspace,
                runtime_state,
                setup_authority=authority,
                setup_request_factory=lambda _request: setup_request,
                _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                # Match the production capture contract: no CREATE_NO_WINDOW.
                # The private desktop keeps the console surface isolated.
                _diagnostic_create_no_window=False,
            )
            manager = LocalInteractiveTerminalManager(
                workspace=workspace,
                workspace_path_resolver=FilesystemWorkspacePathResolver(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                sandbox_profile=SandboxProfile.WORKSPACE,
                local_process_sandbox=adapter,
                protected_environment_variables=frozenset(),
                max_sessions=1,
            )
            try:
                provenance = _provenance(paths, workspace)
                print("W5_TOOL_PROVENANCE=" + json.dumps(provenance, sort_keys=True), flush=True)
                workloads = _build_workloads(
                    workspace=workspace,
                    repo=repo,
                    nul_probe=copied_probe,
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
                matrix: list[dict[str, object]] = []
                for workload in workloads:
                    host = await asyncio.to_thread(_host_run, workload, workspace)
                    w3 = await _w3_run(
                        workload,
                        workspace=workspace,
                        adapter=adapter,
                        expected_user_sid=expected_online_sid,
                        expected_write_sid=expected_write_sid,
                    )
                    w4 = await _w4_run(
                        workload,
                        workspace=workspace,
                        manager=manager,
                        expected_user_sid=expected_online_sid,
                        expected_write_sid=expected_write_sid,
                    )
                    matrix.append(
                        {
                            "workload": workload.name,
                            "variant": workload.variant,
                            "HOST": host,
                            "W3": w3,
                            "W4": w4,
                        }
                    )
                diagnostics: dict[str, object] = {}
                powershell = paths.get("powershell")
                if powershell is not None:
                    powershell_spec = next(
                        workload for workload in workloads if workload.name == "POWERSHELL_BASIC"
                    )
                    inherited_desktop_adapter = WindowsNativeLocalProcessSandbox(
                        SandboxProfile.WORKSPACE,
                        workspace,
                        runtime_state,
                        setup_authority=authority,
                        setup_request_factory=lambda _request: setup_request,
                        _diagnostic_desktop_mode=_WindowsNativeDesktopMode.INHERIT_DESKTOP,
                        _diagnostic_create_no_window=False,
                    )
                    diagnostics["powershell_inherited_desktop"] = await _w3_run(
                        powershell_spec,
                        workspace=workspace,
                        adapter=inherited_desktop_adapter,
                        expected_user_sid=expected_online_sid,
                        expected_write_sid=expected_write_sid,
                    )
                    print(
                        "W5_POWERSHELL_INHERITED_DESKTOP="
                        + json.dumps(diagnostics["powershell_inherited_desktop"], sort_keys=True),
                        flush=True,
                    )
                    cmd = paths.get("cmd")
                    if cmd is not None:
                        encoded_command = base64.b64encode(
                            "Write-Output W5_POWERSHELL_OK".encode("utf-16le")
                        ).decode("ascii")
                        powershell_via_cmd = _Workload(
                            "POWERSHELL_VIA_CMD",
                            "cmd-wrapper",
                            cmd,
                            (
                                "/d",
                                "/s",
                                "/c",
                                subprocess.list2cmdline(
                                    [
                                        str(powershell),
                                        "-NoLogo",
                                        "-NoProfile",
                                        "-NonInteractive",
                                        "-EncodedCommand",
                                        encoded_command,
                                    ]
                                ),
                            ),
                            (r"(?m)^W5_POWERSHELL_OK$",),
                        )
                        diagnostics["powershell_via_cmd"] = await _w3_run(
                            powershell_via_cmd,
                            workspace=workspace,
                            adapter=adapter,
                            expected_user_sid=expected_online_sid,
                            expected_write_sid=expected_write_sid,
                        )
                        print(
                            "W5_POWERSHELL_VIA_CMD="
                            + json.dumps(diagnostics["powershell_via_cmd"], sort_keys=True),
                            flush=True,
                        )
                print("W5_MATRIX_RESULTS=" + json.dumps(matrix, sort_keys=True), flush=True)
                correlation = _correlate(matrix)
                print(
                    "W5_CORRELATION=" + json.dumps(correlation, sort_keys=True),
                    flush=True,
                )
                artifact_path = os.environ.get("NEURO_CODE_W5_EVIDENCE_JSON")
                if artifact_path:
                    await asyncio.to_thread(
                        _write_json_artifact,
                        artifact_path,
                        {
                            "tool_provenance": provenance,
                            "matrix": matrix,
                            "correlation": correlation,
                            "diagnostics": diagnostics,
                            "security_contract": {
                                "read": "LIMITED",
                                "write": "STRONG",
                                "network": "STRONG",
                                "lifecycle": "STRONG_DESCENDANT_OWNERSHIP",
                                "profile": "WORKSPACE",
                                "strict": "FAIL_CLOSED",
                                "expected_online_user_sid": expected_online_sid,
                                "expected_write_restricting_sid": expected_write_sid,
                            },
                        },
                    )
            finally:
                await manager.shutdown()
                await asyncio.to_thread(authority.cleanup, setup_request)


def _path_classifications(row: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for path in ("HOST", "W3", "W4"):
        cell = row.get(path)
        if isinstance(cell, dict):
            result[path] = cell.get("classification")
    return result


def _shared_transport_classification(row: dict[str, object]) -> bool:
    classes = _path_classifications(row)
    return classes.get("W3") == classes.get("W4")


def _correlate(matrix: list[dict[str, object]]) -> dict[str, object]:
    shared: list[str] = []
    w3_only: list[str] = []
    w4_only: list[str] = []
    host_failures: list[str] = []
    for row in matrix:
        name = str(row.get("workload"))
        host = row.get("HOST")
        w3 = row.get("W3")
        w4 = row.get("W4")
        host_class = host.get("classification") if isinstance(host, dict) else None
        w3_class = w3.get("classification") if isinstance(w3, dict) else None
        w4_class = w4.get("classification") if isinstance(w4, dict) else None
        host_failed = host_class not in {"PASS", "NOT_INSTALLED"}
        w3_failed = w3_class not in {"PASS", "NOT_INSTALLED"}
        w4_failed = w4_class not in {"PASS", "NOT_INSTALLED"}
        if host_failed:
            host_failures.append(name)
        if not host_failed and w3_failed and w4_failed:
            shared.append(name)
        elif not host_failed and w3_failed and not w4_failed:
            w3_only.append(name)
        elif not host_failed and not w3_failed and w4_failed:
            w4_only.append(name)
    return {
        "shared_w3_w4": shared,
        "w3_only": w3_only,
        "w4_only": w4_only,
        "host_failures": host_failures,
        "nul_rows": [row["workload"] for row in matrix if "NUL" in str(row.get("workload"))],
        "python_rows": [
            row["workload"] for row in matrix if str(row.get("workload", "")).startswith("PYTHON")
        ],
        "git_rows": [
            row["workload"] for row in matrix if str(row.get("workload", "")).startswith("GIT")
        ],
        "node_npm_rows": [
            row["workload"]
            for row in matrix
            if str(row.get("workload")) in {"NODE_VERSION", "NODE_EXEC", "NPM_VERSION"}
        ],
        "curl_rows": [
            row["workload"] for row in matrix if str(row.get("workload", "")).startswith("CURL")
        ],
        "nul_correlation": {
            "rows": [row["workload"] for row in matrix if "NUL" in str(row.get("workload"))],
            "shared_w3_w4": [
                row["workload"]
                for row in matrix
                if "NUL" in str(row.get("workload")) and _shared_transport_classification(row)
            ],
        },
        "python_layer_results": {
            row["workload"]: _path_classifications(row)
            for row in matrix
            if str(row.get("workload", "")).startswith("PYTHON")
        },
        "git_failure_stage": {
            row["workload"]: _path_classifications(row)
            for row in matrix
            if str(row.get("workload", "")).startswith("GIT")
        },
        "node_npm_failure_stage": {
            row["workload"]: _path_classifications(row)
            for row in matrix
            if str(row.get("workload")) in {"NODE_VERSION", "NODE_EXEC", "NPM_VERSION"}
        },
        "curl_failure_stage": {
            row["workload"]: _path_classifications(row)
            for row in matrix
            if str(row.get("workload", "")).startswith("CURL")
        },
        "prior_current_curl_reproduction": {
            "status": "NOT_FOUND",
            "note": "No exact prior restricted-curl command exists in current W3 evidence.",
        },
    }
