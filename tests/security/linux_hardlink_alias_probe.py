"""Evidence-only probe for Linux external-inode aliases.

This intentionally does not change the production Bubblewrap guard.  It asks a
real child to read hardlinks created before launch for protected files outside
the controller-state directory, documenting whether the current guard's scope
is broader than its name suggests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

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
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.infrastructure.sandbox.linux_local_process import (
    LinuxBubblewrapLocalProcessSandbox,
)
from neuro_code.shared.errors import SandboxError


def _request(profile: SandboxProfile, workspace: Path, code: str) -> SandboxedProcessRequest:
    access = (
        LocalWorkspaceAccessMode.READ_ONLY
        if profile is SandboxProfile.READ_ONLY
        else LocalWorkspaceAccessMode.READ_WRITE
    )
    return SandboxedProcessRequest.exec(
        "/usr/bin/python3",
        ("-c", code),
        purpose=LocalProcessPurpose.BASH,
        cwd=workspace,
        sandbox_profile=profile,
        filesystem_policy=LocalProcessFilesystemPolicy((LocalWorkspaceAccess(workspace, access),)),
        network_policy=(
            LocalProcessNetworkPolicy.INHERIT
            if profile is SandboxProfile.WORKSPACE
            else LocalProcessNetworkPolicy.ISOLATED
        ),
        environment_policy=LocalProcessEnvironmentPolicy({}),
        stdio_mode=LocalProcessStdioMode.CAPTURE,
        lifecycle=LocalProcessLifecycle(
            termination_grace_seconds=0.05,
            force_wait_seconds=2,
        ),
    )


async def _probe_profile(
    profile: SandboxProfile,
    workspace: Path,
    aliases: dict[str, Path],
    state_dir: Path,
) -> dict[str, object]:
    aliases_literal = json.dumps({name: str(path) for name, path in aliases.items()})
    code = f"""
import json
from pathlib import Path

aliases = json.loads({aliases_literal!r})
result = {{}}
for name, raw in aliases.items():
    path = Path(raw)
    value = {{"read": False, "content": None, "error": None}}
    try:
        value["content"] = path.read_text()
        value["read"] = True
    except OSError as error:
        value["error"] = {{"errno": error.errno, "error": str(error)}}
    result[name] = value
print(json.dumps(result))
"""
    try:
        adapter = LinuxBubblewrapLocalProcessSandbox(profile, workspace, state_dir)
    except SandboxError as error:
        return {"capability": "unavailable", "error": str(error)}
    process = await adapter.spawn(_request(profile, workspace, code))
    assert process.stdout is not None
    assert process.stderr is not None
    stdout, stderr, returncode = await asyncio.gather(
        process.stdout.read(), process.stderr.read(), process.wait()
    )
    decoded = stdout.decode("utf-8", errors="replace")
    try:
        child_result: object = json.loads(decoded)
    except json.JSONDecodeError:
        child_result = {"raw_stdout": decoded}
    return {
        "capability": "available",
        "returncode": returncode,
        "stderr": stderr.decode("utf-8", errors="replace")[-2000:],
        "child": child_result,
    }


async def _run() -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="neuro-code-linux-hardlink-probe-"))
    try:
        workspace = root / "workspace"
        state_dir = root / "controller-state"
        host_home = root / "host-home"
        denied_root = root / "denied-root"
        for directory in (workspace, state_dir, host_home, denied_root):
            directory.mkdir()
        (state_dir / "credentials.json").write_text("controller-secret", encoding="utf-8")
        (host_home / "sentinel").write_text("host-home-secret", encoding="utf-8")
        (denied_root / "sentinel").write_text("denied-secret", encoding="utf-8")
        aliases: dict[str, Path] = {}
        for name, target in {
            "host_home": host_home / "sentinel",
            "denied_root": denied_root / "sentinel",
        }.items():
            alias = workspace / f"alias-{name}"
            os.link(target, alias)
            aliases[name] = alias
        profiles: dict[str, object] = {}
        for profile in (SandboxProfile.WORKSPACE, SandboxProfile.READ_ONLY, SandboxProfile.STRICT):
            profiles[profile.value] = await _probe_profile(profile, workspace, aliases, state_dir)
        findings: list[str] = []
        for profile, result in profiles.items():
            if not isinstance(result, dict) or result.get("capability") != "available":
                continue
            child = result.get("child")
            if not isinstance(child, dict):
                continue
            for name, value in child.items():
                if isinstance(value, dict) and value.get("read") is True:
                    findings.append(f"{profile}/{name}: external inode readable through workspace")
        return {
            "probe": "linux-external-inode-alias-evidence-v1",
            "status": "BLOCKED_CAPABILITY" if findings else "NO_ALIAS_OBSERVED",
            "production_guard_scope": "controller-state hardlink validation only",
            "workspace": str(workspace),
            "state_dir": str(state_dir),
            "host_home": str(host_home),
            "denied_root": str(denied_root),
            "profiles": profiles,
            "findings": findings,
            "interpretation": (
                "A BLOCKED_CAPABILITY result is an evidence finding, not a skipped test: "
                "the production guard was intentionally not modified by this probe."
            ),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Linux external-inode aliases")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_run())
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
