from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

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
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.terminal import TerminalSize
from neuro_code.infrastructure.sandbox.macos_local_process import (
    MacOSSeatbeltLocalProcessSandbox,
)
from neuro_code.shared.errors import SandboxError

pytestmark = [
    pytest.mark.sandbox_integration,
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt required"),
]

_REQUIRE_INTEGRATION_ENV = "NEURO_CODE_REQUIRE_MACOS_SANDBOX_INTEGRATION"


@pytest.fixture(autouse=True)
def _require_explicit_gate() -> None:
    if os.environ.get(_REQUIRE_INTEGRATION_ENV) != "1":
        pytest.skip("dedicated macOS Seatbelt integration gate is not enabled")


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    workspace = (tmp_path / 'workspace Ω "quoted" \\ path').resolve()
    state_dir = (tmp_path / "controller state").resolve()
    outside = (tmp_path / "outside").resolve()
    additional = (tmp_path / "additional root").resolve()
    for path in (workspace, state_dir, outside, additional):
        path.mkdir()
    (workspace / "readable.txt").write_text("workspace-readable", encoding="utf-8")
    (state_dir / "credentials.json").write_text("controller-secret", encoding="utf-8")
    (outside / "outside.txt").write_text("outside-secret", encoding="utf-8")
    (additional / "additional.txt").write_text("additional-readable", encoding="utf-8")
    return workspace, state_dir, outside, additional


def _request(
    profile: SandboxProfile,
    workspace: Path,
    *,
    code: str,
    purpose: LocalProcessPurpose = LocalProcessPurpose.BASH,
    stdio: LocalProcessStdioMode = LocalProcessStdioMode.CAPTURE,
    roots: tuple[LocalWorkspaceAccess, ...] | None = None,
    environment: LocalProcessEnvironmentPolicy | None = None,
) -> SandboxedProcessRequest:
    access = (
        LocalWorkspaceAccessMode.READ_ONLY
        if profile is SandboxProfile.READ_ONLY
        else LocalWorkspaceAccessMode.READ_WRITE
    )
    return SandboxedProcessRequest.exec(
        sys.executable,
        ("-u", "-c", code),
        purpose=purpose,
        cwd=workspace,
        sandbox_profile=profile,
        filesystem_policy=LocalProcessFilesystemPolicy(
            roots or (LocalWorkspaceAccess(workspace, access),)
        ),
        network_policy=(
            LocalProcessNetworkPolicy.ISOLATED
            if profile.restricts_child_network
            else LocalProcessNetworkPolicy.INHERIT
        ),
        environment_policy=environment or LocalProcessEnvironmentPolicy(),
        stdio_mode=stdio,
        lifecycle=LocalProcessLifecycle(termination_grace_seconds=0.05, force_wait_seconds=2),
    )


async def _run_json(
    adapter: MacOSSeatbeltLocalProcessSandbox,
    request: SandboxedProcessRequest,
) -> dict[str, object]:
    process = await adapter.spawn(request)
    assert process.stdout is not None
    assert process.stderr is not None
    stdout, stderr, returncode = await asyncio.gather(
        process.stdout.read(), process.stderr.read(), process.wait()
    )
    assert returncode == 0, stderr.decode("utf-8", errors="replace")
    return json.loads(stdout.decode("utf-8"))


@pytest.mark.parametrize(
    ("profile", "workspace_writable", "network_allowed"),
    [
        (SandboxProfile.WORKSPACE, True, True),
        (SandboxProfile.READ_ONLY, False, False),
        (SandboxProfile.STRICT, True, False),
    ],
)
async def test_real_profile_filesystem_environment_and_network_boundary(
    tmp_path: Path,
    profile: SandboxProfile,
    workspace_writable: bool,
    network_allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, state_dir, outside, additional = _layout(tmp_path)
    host_sentinel = Path.home() / f".neuro-code-seatbelt-host-{os.getpid()}-{profile.value}"
    host_sentinel.write_text("host-home-secret", encoding="utf-8")
    monkeypatch.setenv("NEURO_CODE_SECRET_SHOULD_NOT_LEAK", "parent-process-secret")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = int(listener.getsockname()[1])
    accepted = threading.Event()

    def accept_once() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        connection.close()
        accepted.set()

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    code = f"""
import json, os, socket
from pathlib import Path

workspace = Path({str(workspace)!r})
def readable(path):
    try:
        return Path(path).read_text()
    except OSError:
        return None
def writable(path):
    try:
        Path(path).write_text("written")
        return True
    except OSError:
        return False
def mutation_modes(path):
    results = {{}}
    for mode in ("a", "w"):
        try:
            with Path(path).open(mode, encoding="utf-8") as stream:
                stream.write(mode)
            results[mode] = True
        except OSError:
            results[mode] = False
    return results
try:
    connection = socket.create_connection(("127.0.0.1", {port}), timeout=1)
except OSError:
    network = False
else:
    connection.close()
    network = True
home = Path(os.environ["HOME"])
temporary = Path(os.environ["TMPDIR"])
home.joinpath("owned").write_text("home")
temporary.joinpath("owned").write_text("tmp")
print(json.dumps({{
    "workspace_read": readable(workspace / "readable.txt"),
    "workspace_write": writable(workspace / "child-write"),
    "workspace_mutations": mutation_modes(workspace / "readable.txt"),
    "state_read": readable({str(state_dir / "credentials.json")!r}),
    "outside_read": readable({str(outside / "outside.txt")!r}),
    "outside_write": writable({str(outside / "child-write")!r}),
    "host_home_read": readable({str(host_sentinel)!r}),
    "additional_read": readable({str(additional / "additional.txt")!r}),
    "additional_write": writable({str(additional / "child-write")!r}),
    "network": network,
    "home": str(home),
    "tmp": str(temporary),
    "home_write": home.joinpath("owned").read_text(),
    "tmp_write": temporary.joinpath("owned").read_text(),
    "secret": os.environ.get("CONTROLLER_SECRET"),
    "parent_secret": os.environ.get("NEURO_CODE_SECRET_SHOULD_NOT_LEAK"),
    "explicit": os.environ.get("EXPLICIT_MCP_TOKEN"),
}}))
"""
    additional_mode = (
        LocalWorkspaceAccessMode.READ_ONLY
        if profile in {SandboxProfile.READ_ONLY, SandboxProfile.STRICT}
        else LocalWorkspaceAccessMode.READ_WRITE
    )
    environment = LocalProcessEnvironmentPolicy(
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "CONTROLLER_SECRET": "must-not-leak",
            "EXPLICIT_MCP_TOKEN": "authorized",
        },
        explicitly_authorized_names=frozenset({"EXPLICIT_MCP_TOKEN"}),
    )
    try:
        adapter = MacOSSeatbeltLocalProcessSandbox(profile, workspace, state_dir)
        result = await _run_json(
            adapter,
            _request(
                profile,
                workspace,
                code=code,
                roots=(
                    LocalWorkspaceAccess(
                        workspace,
                        LocalWorkspaceAccessMode.READ_ONLY
                        if profile is SandboxProfile.READ_ONLY
                        else LocalWorkspaceAccessMode.READ_WRITE,
                    ),
                    LocalWorkspaceAccess(additional, additional_mode),
                ),
                environment=environment,
            ),
        )
    finally:
        listener.close()
        thread.join(timeout=5)
        host_sentinel.unlink(missing_ok=True)

    assert result["workspace_read"] == "workspace-readable"
    assert result["workspace_write"] is workspace_writable
    assert result["workspace_mutations"] == {
        "a": workspace_writable,
        "w": workspace_writable,
    }
    assert result["state_read"] is None
    assert result["outside_read"] is None
    assert result["outside_write"] is False
    assert result["host_home_read"] is None
    assert result["additional_read"] == "additional-readable"
    assert result["additional_write"] is (profile is SandboxProfile.WORKSPACE)
    assert result["network"] is network_allowed
    assert accepted.is_set() is network_allowed
    assert result["home_write"] == "home"
    assert result["tmp_write"] == "tmp"
    assert result["secret"] is None
    assert result["parent_secret"] is None
    assert result["explicit"] == "authorized"
    assert not Path(str(result["home"])).exists()  # noqa: ASYNC240
    assert not Path(str(result["tmp"])).exists()  # noqa: ASYNC240


@pytest.mark.parametrize(
    "profile",
    [SandboxProfile.WORKSPACE, SandboxProfile.READ_ONLY, SandboxProfile.STRICT],
)
async def test_real_metadata_authority_and_symlink_isolation(
    tmp_path: Path,
    profile: SandboxProfile,
) -> None:
    workspace, state_dir, outside, additional = _layout(tmp_path)
    state_file = state_dir / "credentials.json"
    outside_file = outside / "outside.txt"
    additional_file = additional / "additional.txt"
    host_sentinel = Path.home() / f".neuro-code-seatbelt-metadata-{os.getpid()}-{profile.value}"
    host_sentinel.write_text("host-home-metadata-secret", encoding="utf-8")
    state_link = workspace / "state-file-link"
    outside_directory_link = workspace / "outside-directory-link"
    state_link.symlink_to(state_file)
    outside_directory_link.symlink_to(outside, target_is_directory=True)
    code = f"""
import json, os, sys
from pathlib import Path

def metadata(path):
    result = {{}}
    for name, operation in (("stat", os.stat), ("lstat", os.lstat)):
        try:
            operation(path)
        except OSError:
            result[name] = False
        else:
            result[name] = True
    try:
        result["exists"] = Path(path).exists()
    except OSError:
        result["exists"] = False
    return result

print(json.dumps({{
    "workspace": metadata({str(workspace / "readable.txt")!r}),
    "additional": metadata({str(additional_file)!r}),
    "private_home": metadata(os.environ["HOME"]),
    "private_tmp": metadata(os.environ["TMPDIR"]),
    "runtime": metadata(sys.executable),
    "state": metadata({str(state_file)!r}),
    "outside": metadata({str(outside_file)!r}),
    "host_home": metadata({str(host_sentinel)!r}),
    "state_link": metadata({str(state_link)!r}),
    "outside_via_symlink": metadata({str(outside_directory_link / "outside.txt")!r}),
}}))
"""
    roots = (
        LocalWorkspaceAccess(
            workspace,
            LocalWorkspaceAccessMode.READ_ONLY
            if profile is SandboxProfile.READ_ONLY
            else LocalWorkspaceAccessMode.READ_WRITE,
        ),
        LocalWorkspaceAccess(additional, LocalWorkspaceAccessMode.READ_ONLY),
    )
    try:
        adapter = MacOSSeatbeltLocalProcessSandbox(profile, workspace, state_dir)
        result = await _run_json(
            adapter,
            _request(profile, workspace, code=code, roots=roots),
        )
    finally:
        host_sentinel.unlink(missing_ok=True)

    visible = {"stat": True, "lstat": True, "exists": True}
    hidden = {"stat": False, "lstat": False, "exists": False}
    for name in ("workspace", "additional", "private_home", "private_tmp", "runtime"):
        assert result[name] == visible
    for name in ("state", "outside", "host_home", "outside_via_symlink"):
        assert result[name] == hidden
    assert result["state_link"] == {"stat": False, "lstat": True, "exists": False}


@pytest.mark.parametrize("source_name", ["state", "outside", "host-home"])
async def test_real_external_hardlink_is_rejected_before_child_launch(
    tmp_path: Path,
    source_name: str,
) -> None:
    workspace, state_dir, outside, _ = _layout(tmp_path)
    host_sentinel = Path.home() / f".neuro-code-seatbelt-hardlink-{os.getpid()}-{source_name}"
    host_sentinel.write_text("host-home-secret", encoding="utf-8")
    sources = {
        "state": state_dir / "credentials.json",
        "outside": outside / "outside.txt",
        "host-home": host_sentinel,
    }
    os.link(sources[source_name], workspace / f"{source_name}-alias")
    marker = workspace / "child-started"
    try:
        adapter = MacOSSeatbeltLocalProcessSandbox(SandboxProfile.WORKSPACE, workspace, state_dir)
        with pytest.raises(SandboxError, match="hardlink outside"):
            await adapter.spawn(
                _request(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    code=f"from pathlib import Path; Path({str(marker)!r}).write_text('started')",
                )
            )
    finally:
        host_sentinel.unlink(missing_ok=True)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("profile", "mode"),
    [
        (SandboxProfile.WORKSPACE, LocalWorkspaceAccessMode.READ_WRITE),
        (SandboxProfile.READ_ONLY, LocalWorkspaceAccessMode.READ_ONLY),
        (SandboxProfile.STRICT, LocalWorkspaceAccessMode.READ_WRITE),
    ],
)
async def test_real_same_mode_internal_hardlinks_are_preserved(
    tmp_path: Path,
    profile: SandboxProfile,
    mode: LocalWorkspaceAccessMode,
) -> None:
    workspace, state_dir, _, additional = _layout(tmp_path)
    source = additional / "additional.txt"
    alias = workspace / "same-mode-alias"
    os.link(source, alias)
    adapter = MacOSSeatbeltLocalProcessSandbox(profile, workspace, state_dir)
    code = (
        "import json;from pathlib import Path;"
        f"source=Path({str(source)!r});alias=Path({str(alias)!r});"
        "value=alias.read_text();"
        "\ntry: alias.write_text('updated'); write=True"
        "\nexcept OSError: write=False"
        "\nprint(json.dumps({'value':value,'write':write,'source':source.read_text()}))"
    )
    result = await _run_json(
        adapter,
        _request(
            profile,
            workspace,
            code=code,
            roots=(
                LocalWorkspaceAccess(workspace, mode),
                LocalWorkspaceAccess(additional, mode),
            ),
        ),
    )
    assert result["value"] == "additional-readable"
    assert result["write"] is (mode is LocalWorkspaceAccessMode.READ_WRITE)
    assert result["source"] == (
        "updated" if mode is LocalWorkspaceAccessMode.READ_WRITE else "additional-readable"
    )


async def test_real_mixed_mode_hardlink_is_rejected_before_child_launch(
    tmp_path: Path,
) -> None:
    workspace, state_dir, _, additional = _layout(tmp_path)
    os.link(additional / "additional.txt", workspace / "mixed-mode-alias")
    marker = workspace / "mixed-child-started"
    adapter = MacOSSeatbeltLocalProcessSandbox(SandboxProfile.STRICT, workspace, state_dir)
    request = _request(
        SandboxProfile.STRICT,
        workspace,
        code=f"from pathlib import Path;Path({str(marker)!r}).write_text('started')",
        roots=(
            LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),
            LocalWorkspaceAccess(additional, LocalWorkspaceAccessMode.READ_ONLY),
        ),
    )
    with pytest.raises(SandboxError, match="both READ_ONLY and READ_WRITE"):
        await adapter.spawn(request)
    assert not marker.exists()


async def test_real_inheritable_fd_is_closed_and_mcp_protocol_is_argv_safe(
    tmp_path: Path,
) -> None:
    workspace, state_dir, _, _ = _layout(tmp_path)
    sensitive = state_dir / "sensitive-fd"
    sensitive.write_text("fd-secret", encoding="utf-8")
    descriptor = os.open(sensitive, os.O_RDONLY)
    try:
        os.set_inheritable(descriptor, True)
        details = os.fstat(descriptor)
        fd_code = (
            "import json,os,sys; fd=int(sys.argv[1]); expected=(int(sys.argv[2]),int(sys.argv[3]));"
            "\ntry: actual=(os.fstat(fd).st_dev,os.fstat(fd).st_ino)"
            "\nexcept OSError: actual=None"
            "\nleaked=os.read(fd,100).decode() if actual == expected else None"
            "\nprint(json.dumps({'inherited': actual == expected, 'leaked': leaked}))"
        )
        adapter = MacOSSeatbeltLocalProcessSandbox(SandboxProfile.WORKSPACE, workspace, state_dir)
        request = replace(
            _request(SandboxProfile.WORKSPACE, workspace, code="pass"),
            arguments=(
                "-u",
                "-c",
                fd_code,
                str(descriptor),
                str(details.st_dev),
                str(details.st_ino),
            ),
        )
        assert await _run_json(adapter, request) == {"inherited": False, "leaked": None}
    finally:
        os.close(descriptor)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    port = int(listener.getsockname()[1])
    protocol_code = f"""
import json, socket, sys
from pathlib import Path
line = sys.stdin.buffer.readline().decode().strip()
try:
    outside = Path({str(state_dir / "credentials.json")!r}).read_text()
except OSError:
    outside = None
try:
    connection = socket.create_connection(("127.0.0.1", {port}), timeout=1)
except OSError:
    network = False
else:
    connection.close()
    network = True
print(json.dumps({{"line": line, "outside": outside, "network": network}}), flush=True)
"""
    protocol = _request(
        SandboxProfile.STRICT,
        workspace,
        code=protocol_code,
        purpose=LocalProcessPurpose.MCP_STDIO,
        stdio=LocalProcessStdioMode.PROTOCOL,
    )
    strict_adapter = MacOSSeatbeltLocalProcessSandbox(SandboxProfile.STRICT, workspace, state_dir)
    process = await strict_adapter.spawn(protocol)
    assert process.stdout is not None
    await process.write_stdin(b"frame\n")
    await process.close_stdin()
    response = json.loads((await process.stdout.readline()).decode("utf-8"))
    assert response == {"line": "frame", "outside": None, "network": False}
    assert await process.wait() == 0
    listener.close()


async def test_real_pty_runs_inside_seatbelt_and_reports_best_effort_lifecycle(
    tmp_path: Path,
) -> None:
    workspace, state_dir, outside, _ = _layout(tmp_path)
    adapter = MacOSSeatbeltLocalProcessSandbox(SandboxProfile.STRICT, workspace, state_dir)
    code = (
        "line=input();from pathlib import Path;"
        f"\ntry: value=Path({str(outside / 'outside.txt')!r}).read_text()"
        "\nexcept OSError: value='denied'"
        "\nprint('pty:' + line + ':' + value, flush=True)"
    )
    request = _request(
        SandboxProfile.STRICT,
        workspace,
        code=code,
        purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
        stdio=LocalProcessStdioMode.PTY,
        environment=LocalProcessEnvironmentPolicy({"TERM": "xterm-256color"}),
    )
    output = bytearray()
    eof = threading.Event()
    errors: list[BaseException] = []
    session = adapter.spawn_terminal(
        request,
        size=TerminalSize(80, 24),
        on_output=output.extend,
        on_eof=eof.set,
        on_error=errors.append,
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5
        session.write(b"round-trip\n")
        while (  # noqa: ASYNC110 - bounded polling of a synchronous PTY callback
            b"pty:round-trip:denied" not in output and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        assert b"pty:round-trip:denied" in output
        assert session.lifecycle_capability is (
            LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT
        )
        assert eof.wait(timeout=5)
        assert not errors
    finally:
        session.close()


async def test_strong_lifecycle_requirement_fails_before_os_child_creation(
    tmp_path: Path,
) -> None:
    workspace, state_dir, _, _ = _layout(tmp_path)
    marker = workspace / "strong-child-started"
    adapter = MacOSSeatbeltLocalProcessSandbox(SandboxProfile.WORKSPACE, workspace, state_dir)
    request = replace(
        _request(
            SandboxProfile.WORKSPACE,
            workspace,
            code=f"from pathlib import Path; Path({str(marker)!r}).write_text('started')",
        ),
        lifecycle=LocalProcessLifecycle(
            required_capability=LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
        ),
    )
    with pytest.raises(SandboxError, match="does not satisfy required"):
        await adapter.spawn(request)
    assert not marker.exists()
