from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

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
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.terminal import TerminalSize
from neuro_code.infrastructure.background_tasks import LocalBackgroundTaskManager
from neuro_code.infrastructure.sandbox.linux_local_process import (
    LinuxBubblewrapLocalProcessSandbox,
)
from neuro_code.infrastructure.tools.bash import BashTool
from neuro_code.shared.errors import SandboxError, ToolError

pytestmark = [
    pytest.mark.sandbox_integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux Bubblewrap required"),
]

_REQUIRE_INTEGRATION_ENV = "NEURO_CODE_REQUIRE_SANDBOX_INTEGRATION"
# The controller itself runs under the matrix's uv-managed Python.  Sandbox
# fixtures intentionally use the runner's stable system interpreter so the
# test exercises Bubblewrap boundaries rather than uv's external venv symlink
# layout, which is not a child capability of the sandbox.
_SANDBOX_FIXTURE_PYTHON = "/usr/bin/python3"


def _capability_unavailable(reason: str) -> None:
    if os.environ.get(_REQUIRE_INTEGRATION_ENV) == "1":
        pytest.fail(f"required sandbox integration capability is unavailable: {reason}")
    pytest.skip(reason)


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = (tmp_path / "workspace").resolve()
    state_dir = (tmp_path / "controller-state").resolve()
    host_home = (tmp_path / "host-home").resolve()
    workspace.mkdir()
    state_dir.mkdir()
    host_home.mkdir()
    for name in ("credentials.json", "sessions.db", "providers.json", "config.toml"):
        (state_dir / name).write_text(f"private-{name}", encoding="utf-8")
    (host_home / "host-home-sentinel").write_text("host-private", encoding="utf-8")
    return workspace, state_dir, host_home


def _adapter(
    profile: SandboxProfile,
    workspace: Path,
    state_dir: Path,
) -> LinuxBubblewrapLocalProcessSandbox:
    try:
        return LinuxBubblewrapLocalProcessSandbox(profile, workspace, state_dir)
    except SandboxError as error:
        _capability_unavailable(str(error))


def _request(
    profile: SandboxProfile,
    workspace: Path,
    code: str,
    *,
    purpose: LocalProcessPurpose = LocalProcessPurpose.BASH,
    environment: dict[str, str] | None = None,
) -> SandboxedProcessRequest:
    access = (
        LocalWorkspaceAccessMode.READ_ONLY
        if profile is SandboxProfile.READ_ONLY
        else LocalWorkspaceAccessMode.READ_WRITE
    )
    return SandboxedProcessRequest.exec(
        _SANDBOX_FIXTURE_PYTHON,
        ("-c", code),
        purpose=purpose,
        cwd=workspace,
        sandbox_profile=profile,
        filesystem_policy=LocalProcessFilesystemPolicy((LocalWorkspaceAccess(workspace, access),)),
        network_policy=(
            LocalProcessNetworkPolicy.ISOLATED
            if profile.restricts_child_network
            else LocalProcessNetworkPolicy.INHERIT
        ),
        environment_policy=LocalProcessEnvironmentPolicy(environment or {}),
        stdio_mode=LocalProcessStdioMode.CAPTURE,
        lifecycle=LocalProcessLifecycle(
            termination_grace_seconds=0.05,
            force_wait_seconds=2,
        ),
    )


async def _run_json(
    adapter: LinuxBubblewrapLocalProcessSandbox,
    request: SandboxedProcessRequest,
) -> dict[str, object]:
    process = await adapter.spawn(request)
    assert process.stdout is not None
    assert process.stderr is not None
    stdout, stderr, returncode = await asyncio.gather(
        process.stdout.read(),
        process.stderr.read(),
        process.wait(),
    )
    assert returncode == 0, stderr.decode("utf-8", errors="replace")
    return json.loads(stdout.decode("utf-8"))


async def test_real_workspace_hardlink_is_rejected_before_child_launch(tmp_path: Path) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    private_file = state_dir / "credentials.json"
    workspace_alias = workspace / "credentials-hardlink"
    os.link(private_file, workspace_alias)
    assert private_file.stat().st_ino == workspace_alias.stat().st_ino
    assert private_file.stat().st_nlink > 1

    with pytest.raises(SandboxError, match="hardlink outside its trusted path"):
        LinuxBubblewrapLocalProcessSandbox(SandboxProfile.WORKSPACE, workspace, state_dir)


@pytest.mark.parametrize("outside_name", ["host-home", "denied-root"])
@pytest.mark.parametrize("profile", list(SandboxProfile)[1:])
async def test_real_external_workspace_aliases_fail_closed(
    tmp_path: Path,
    outside_name: str,
    profile: SandboxProfile,
) -> None:
    workspace, state_dir, host_home = _layout(tmp_path)
    outside_root = host_home if outside_name == "host-home" else (tmp_path / outside_name)
    if outside_name == "denied-root":
        outside_root.mkdir()
    outside_file = outside_root / "private.txt"
    outside_file.write_text("private", encoding="utf-8")
    os.link(outside_file, workspace / f"{outside_name}-alias.txt")

    with pytest.raises(SandboxError, match="outside the authorized roots"):
        LinuxBubblewrapLocalProcessSandbox(profile, workspace, state_dir)


def _request_with_roots(
    profile: SandboxProfile,
    workspace: Path,
    roots: tuple[LocalWorkspaceAccess, ...],
    code: str,
) -> SandboxedProcessRequest:
    return replace(
        _request(profile, workspace, code),
        filesystem_policy=LocalProcessFilesystemPolicy(roots),
    )


@pytest.mark.parametrize("profile", [SandboxProfile.WORKSPACE, SandboxProfile.STRICT])
async def test_real_mixed_read_only_read_write_alias_is_rejected(
    tmp_path: Path,
    profile: SandboxProfile,
) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    additional_ro = tmp_path / "additional-read-only"
    additional_ro.mkdir()
    source = additional_ro / "protected.txt"
    source.write_text("protected", encoding="utf-8")
    adapter = _adapter(profile, workspace, state_dir)
    alias = workspace / "protected-alias.txt"
    os.link(source, alias)

    request = _request_with_roots(
        profile,
        workspace,
        (
            LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),
            LocalWorkspaceAccess(additional_ro, LocalWorkspaceAccessMode.READ_ONLY),
        ),
        "raise SystemExit(99)",
    )
    with pytest.raises(SandboxError, match="both READ_ONLY and READ_WRITE"):
        await adapter.spawn(request)


@pytest.mark.parametrize("profile", [SandboxProfile.WORKSPACE, SandboxProfile.STRICT])
async def test_real_read_write_internal_alias_remains_allowed(
    tmp_path: Path,
    profile: SandboxProfile,
) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    additional_rw = tmp_path / "additional-read-write"
    additional_rw.mkdir()
    source = additional_rw / "shared.txt"
    source.write_text("before", encoding="utf-8")
    adapter = _adapter(profile, workspace, state_dir)
    alias = workspace / "shared-alias.txt"
    os.link(source, alias)
    code = (
        "import json;"
        f"from pathlib import Path; Path({str(alias)!r}).write_text('after');"
        f"print(json.dumps(Path({str(source)!r}).read_text()))"
    )

    result = await _run_json(
        adapter,
        _request_with_roots(
            profile,
            workspace,
            (
                LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),
                LocalWorkspaceAccess(additional_rw, LocalWorkspaceAccessMode.READ_WRITE),
            ),
            code,
        ),
    )
    assert result == "after"


async def test_real_read_only_internal_alias_remains_read_only(tmp_path: Path) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    additional_ro = tmp_path / "additional-read-only"
    additional_ro.mkdir()
    source = additional_ro / "shared.txt"
    source.write_text("before", encoding="utf-8")
    adapter = _adapter(SandboxProfile.READ_ONLY, workspace, state_dir)
    alias = workspace / "shared-alias.txt"
    os.link(source, alias)
    code = (
        "import json;"
        f"from pathlib import Path; path=Path({str(alias)!r});"
        "read=path.read_text();"
        "\ntry: path.write_text('after')\n"
        "except OSError: write=False\n"
        "else: write=True\n"
        "print(json.dumps({'read': read, 'write': write}))"
    )

    result = await _run_json(
        adapter,
        _request_with_roots(
            SandboxProfile.READ_ONLY,
            workspace,
            (
                LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_ONLY),
                LocalWorkspaceAccess(additional_ro, LocalWorkspaceAccessMode.READ_ONLY),
            ),
            code,
        ),
    )
    assert result == {"read": "before", "write": False}


@pytest.mark.parametrize(
    ("profile", "workspace_writable"),
    [
        (SandboxProfile.WORKSPACE, True),
        (SandboxProfile.READ_ONLY, False),
        (SandboxProfile.STRICT, True),
    ],
)
async def test_real_filesystem_and_environment_boundary(
    tmp_path: Path,
    profile: SandboxProfile,
    workspace_writable: bool,
) -> None:
    workspace, state_dir, host_home = _layout(tmp_path)
    outside = state_dir / "credentials.json"
    (workspace / "credential-link").symlink_to(outside)
    private_tmp_name = f"neuro-code-private-{tmp_path.name}"
    host_tmp = Path("/tmp") / private_tmp_name
    host_var_tmp = Path("/var/tmp") / private_tmp_name
    assert not host_tmp.exists()
    assert not host_var_tmp.exists()
    code = f"""
import json, os
from pathlib import Path

workspace = Path({str(workspace)!r})
try:
    (workspace / "child-write").write_text("written")
    write_ok = True
except OSError:
    write_ok = False
try:
    symlink_value = (workspace / "credential-link").read_text()
except OSError:
    symlink_value = None
Path("/tmp/{private_tmp_name}").write_text("private-tmp")
Path("/var/tmp/{private_tmp_name}").write_text("private-var-tmp")
Path.home().joinpath("private-home-probe").write_text("private-home")
print(json.dumps({{
    "write_ok": write_ok,
    "state_visible": Path({str(outside)!r}).exists(),
    "symlink_value": symlink_value,
    "host_home_visible": Path({str(host_home)!r}).exists(),
    "home": os.environ.get("HOME"),
    "tmpdir": os.environ.get("TMPDIR"),
    "secrets": {{name: os.environ.get(name) for name in (
        "DEEPSEEK_API_KEY", "HTTPS_PROXY", "USERPROFILE", "APPDATA",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"
    )}},
}}))
"""
    adapter = _adapter(profile, workspace, state_dir)
    result = await _run_json(
        adapter,
        _request(
            profile,
            workspace,
            code,
            environment={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "DEEPSEEK_API_KEY": "provider-secret",
                "HTTPS_PROXY": "http://proxy-secret.invalid",
                "USERPROFILE": str(host_home),
                "APPDATA": str(host_home / "AppData"),
                "XDG_CONFIG_HOME": str(host_home / ".config"),
                "XDG_DATA_HOME": str(host_home / ".local/share"),
                "XDG_STATE_HOME": str(host_home / ".local/state"),
            },
        ),
    )

    assert result["write_ok"] is workspace_writable
    assert result["state_visible"] is False
    assert result["symlink_value"] is None
    assert result["host_home_visible"] is False
    assert result["home"] == "/home/neuro-code"
    assert result["tmpdir"] == "/tmp"
    assert result["secrets"] == {
        "DEEPSEEK_API_KEY": None,
        "HTTPS_PROXY": None,
        "USERPROFILE": None,
        "APPDATA": None,
        "XDG_CONFIG_HOME": None,
        "XDG_DATA_HOME": None,
        "XDG_STATE_HOME": None,
    }
    assert not host_tmp.exists()
    assert not host_var_tmp.exists()


@pytest.mark.parametrize(
    ("profile", "connection_expected"),
    [
        (SandboxProfile.WORKSPACE, True),
        (SandboxProfile.READ_ONLY, False),
        (SandboxProfile.STRICT, False),
    ],
)
@pytest.mark.parametrize("grandchild", [False, True])
async def test_real_network_policy_is_inherited_by_descendants(
    tmp_path: Path,
    profile: SandboxProfile,
    connection_expected: bool,
    grandchild: bool,
) -> None:
    workspace, state_dir, _ = _layout(tmp_path)

    async def accept(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        del reader
        writer.close()
        await writer.wait_closed()

    try:
        server = await asyncio.start_server(accept, "127.0.0.1", 0)
    except OSError as error:
        _capability_unavailable(f"host loopback listener cannot be created: {error}")
    assert server.sockets
    port = int(server.sockets[0].getsockname()[1])
    child = (
        "import socket,sys;"
        "sock=socket.socket();sock.settimeout(1);"
        f"\ntry: sock.connect(('127.0.0.1',{port}))\n"
        "except OSError: sys.exit(7)\n"
        "else: sock.close();sys.exit(0)"
    )
    code = child
    if grandchild:
        code = (
            "import subprocess,sys;"
            f"result=subprocess.run([sys.executable,'-c',{child!r}],check=False);"
            "sys.exit(result.returncode)"
        )
    try:
        adapter = _adapter(profile, workspace, state_dir)
        process = await adapter.spawn(_request(profile, workspace, code))
        returncode = await process.wait()
    finally:
        server.close()
        await server.wait_closed()

    assert (returncode == 0) is connection_expected


def _lifecycle_code(workspace: Path, kind: str) -> tuple[str, Path, Path, Path]:
    ready = workspace / f"{kind}-ready"
    release = workspace / f"{kind}-release-after-termination"
    leaked = workspace / f"{kind}-leaked"
    descendant = (
        "import pathlib,time;"
        f"release=pathlib.Path({str(release)!r});"
        "deadline=time.monotonic()+60;"
        "\nwhile not release.exists() and time.monotonic()<deadline: time.sleep(0.01);"
        f"\nif release.exists(): pathlib.Path({str(leaked)!r}).write_text('leaked')"
    )
    if kind == "ordinary":
        code = (
            "import pathlib,time;"
            f"pathlib.Path({str(ready)!r}).write_text('ready');"
            f"release=pathlib.Path({str(release)!r});"
            "deadline=time.monotonic()+60;"
            "\nwhile not release.exists() and time.monotonic()<deadline: time.sleep(0.01);"
            f"\nif release.exists(): pathlib.Path({str(leaked)!r}).write_text('leaked')"
        )
    else:
        detached = ",start_new_session=True" if kind.startswith("setsid") else ""
        code = (
            "import pathlib,subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-c',{descendant!r}]{detached});"
            f"pathlib.Path({str(ready)!r}).write_text('ready');"
            "time.sleep(60)"
        )
    return code, ready, release, leaked


async def _wait_for_path(path: Path) -> None:
    for _ in range(500):
        if path.exists():  # noqa: ASYNC240 - bounded test-only metadata probe
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"sandbox child did not create readiness marker: {path}")


@pytest.mark.parametrize("kind", ["ordinary", "grandchild", "setsid"])
async def test_real_terminate_owns_all_pid_namespace_descendants(
    tmp_path: Path,
    kind: str,
) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    adapter = _adapter(SandboxProfile.WORKSPACE, workspace, state_dir)
    code, ready, release, leaked = _lifecycle_code(workspace, kind)
    process = await adapter.spawn(_request(SandboxProfile.WORKSPACE, workspace, code))
    try:
        await asyncio.wait_for(_wait_for_path(ready), timeout=6)
    except TimeoutError:
        stderr = b""
        if process.stderr is not None:
            stderr = await asyncio.wait_for(process.stderr.read(), timeout=1)
        pytest.fail(
            "sandbox lifecycle fixture did not start: "
            f"returncode={process.returncode}, "
            f"stderr={stderr.decode('utf-8', errors='replace')!r}"
        )

    try:
        await asyncio.wait_for(process.terminate(grace_seconds=0.05), timeout=6)
    except BaseException:
        # Failure cleanup only: passing assertions must rely exclusively on
        # the production lifecycle boundary, never this release marker.
        release.write_text("release", encoding="utf-8")
        await asyncio.sleep(0.1)
        raise
    release.write_text("release", encoding="utf-8")
    await asyncio.sleep(0.25)

    assert not leaked.exists()


async def test_real_bash_timeout_and_cancellation_kill_detached_descendants(
    tmp_path: Path,
) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    adapter = _adapter(SandboxProfile.WORKSPACE, workspace, state_dir)
    tool = BashTool(local_process_sandbox=adapter)

    timeout_code, _, timeout_release, timeout_leak = _lifecycle_code(workspace, "setsid-timeout")
    with pytest.raises(ToolError, match="timed out"):
        await tool.execute(
            {
                "command": shlex.join((_SANDBOX_FIXTURE_PYTHON, "-c", timeout_code)),
                "timeout_seconds": 0.2,
            },
            ToolContext(
                workspace,
                sandbox_profile=SandboxProfile.WORKSPACE,
                local_process_sandbox=adapter,
                termination_grace_seconds=0.05,
            ),
        )

    cancel_code, cancel_ready, cancel_release, cancel_leak = _lifecycle_code(
        workspace, "setsid-cancel"
    )
    operation = asyncio.create_task(
        tool.execute(
            {"command": shlex.join((_SANDBOX_FIXTURE_PYTHON, "-c", cancel_code))},
            ToolContext(
                workspace,
                sandbox_profile=SandboxProfile.WORKSPACE,
                local_process_sandbox=adapter,
                termination_grace_seconds=0.05,
            ),
        )
    )
    await _wait_for_path(cancel_ready)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    timeout_release.write_text("release", encoding="utf-8")
    cancel_release.write_text("release", encoding="utf-8")
    await asyncio.sleep(0.25)

    assert not timeout_leak.exists()
    assert not cancel_leak.exists()


async def test_real_background_shutdown_kills_detached_descendant(tmp_path: Path) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    adapter = _adapter(SandboxProfile.WORKSPACE, workspace, state_dir)
    manager = LocalBackgroundTaskManager(local_process_sandbox=adapter)
    code, ready, release, leaked = _lifecycle_code(workspace, "setsid-shutdown")
    request = _request(
        SandboxProfile.WORKSPACE,
        workspace,
        code,
        purpose=LocalProcessPurpose.BACKGROUND_BASH,
    )
    try:
        await manager.start_process(
            request,
            display_command="sandbox lifecycle fixture",
            output_byte_limit=2_000,
        )
        await _wait_for_path(ready)
        await manager.shutdown()
        release.write_text("release", encoding="utf-8")
        await asyncio.sleep(0.25)
        assert not leaked.exists()
    finally:
        await manager.shutdown()


async def test_real_pty_close_kills_detached_descendant(tmp_path: Path) -> None:
    workspace, state_dir, _ = _layout(tmp_path)
    adapter = _adapter(SandboxProfile.WORKSPACE, workspace, state_dir)
    code, ready, release, leaked = _lifecycle_code(workspace, "setsid-pty-close")
    request = replace(
        _request(
            SandboxProfile.WORKSPACE,
            workspace,
            code,
            purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
        ),
        stdio_mode=LocalProcessStdioMode.PTY,
    )
    errors: list[BaseException] = []
    session = adapter.spawn_terminal(
        request,
        size=TerminalSize(columns=80, rows=24),
        on_output=lambda _data: None,
        on_eof=lambda: None,
        on_error=errors.append,
    )
    try:
        await _wait_for_path(ready)
        session.close()
        release.write_text("release", encoding="utf-8")
        await asyncio.sleep(0.25)
        assert not leaked.exists()
        assert not errors
    finally:
        if session.poll_exit() is None:
            session.close()
