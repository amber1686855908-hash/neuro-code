"""Evidence-only Linux mixed-mode authorized-root hardlink probe.

This deliberately exercises the real Bubblewrap adapter without changing its
production hardlink guard.  It records whether an inode exposed read-only can
be linked through a read-write root and then modified through that alias.

仅用于证据的 Linux 混合模式授权根硬链接探针.

该探针使用真实 Bubblewrap adapter,但不会修改生产硬链接 guard;它记录只读根 inode
是否能通过读写根建立链接并被别名修改.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from hardlink_mode_probe_common import (
    APIS,
    DIRECTIONS,
    build_child_code,
    build_concurrent_actor_code,
    build_mixed_mode_access_code,
    classify_concurrent_findings,
    classify_findings,
    classify_mixed_mode_access,
)

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


def _request(
    profile: SandboxProfile,
    root_rw: Path,
    root_ro: Path,
    code: str,
) -> SandboxedProcessRequest:
    root_rw_mode = (
        LocalWorkspaceAccessMode.READ_ONLY
        if profile is SandboxProfile.READ_ONLY
        else LocalWorkspaceAccessMode.READ_WRITE
    )
    return SandboxedProcessRequest.exec(
        "/usr/bin/python3",
        ("-c", code),
        purpose=LocalProcessPurpose.BASH,
        cwd=root_rw,
        sandbox_profile=profile,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (
                LocalWorkspaceAccess(root_rw, root_rw_mode),
                LocalWorkspaceAccess(root_ro, LocalWorkspaceAccessMode.READ_ONLY),
            )
        ),
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
    root_rw: Path,
    root_ro: Path,
    state_dir: Path,
    operations: dict[str, dict[str, str]],
) -> dict[str, object]:
    code = build_child_code(operations)
    try:
        adapter = LinuxBubblewrapLocalProcessSandbox(profile, root_rw, state_dir)
    except SandboxError as error:
        return {"capability": "unavailable", "error": str(error)}
    process = await adapter.spawn(_request(profile, root_rw, root_ro, code))
    if process.stdout is None or process.stderr is None:
        return {"capability": "unavailable", "error": "capture streams unavailable"}
    stdout, stderr, returncode = await asyncio.gather(
        process.stdout.read(), process.stderr.read(), process.wait()
    )
    decoded = stdout.decode("utf-8", errors="replace")
    try:
        child = json.loads(decoded)
    except json.JSONDecodeError:
        child = {"result_available": False, "raw_stdout": decoded}
    return {
        "capability": "available",
        "returncode": returncode,
        "stderr": stderr.decode("utf-8", errors="replace")[-2_000:],
        "child": child,
    }


async def _probe_mixed_mode_profile(
    profile: SandboxProfile,
    root_rw: Path,
    root_ro: Path,
    state_dir: Path,
    source: Path,
    alias: Path,
) -> dict[str, object]:
    code = build_mixed_mode_access_code(source=str(source), alias=str(alias))
    try:
        adapter = LinuxBubblewrapLocalProcessSandbox(profile, root_rw, state_dir)
    except SandboxError as error:
        return {"capability": "unavailable", "error": str(error)}
    process = await adapter.spawn(_request(profile, root_rw, root_ro, code))
    if process.stdout is None or process.stderr is None:
        return {"capability": "unavailable", "error": "capture streams unavailable"}
    stdout, stderr, returncode = await asyncio.gather(
        process.stdout.read(), process.stderr.read(), process.wait()
    )
    decoded = stdout.decode("utf-8", errors="replace")
    try:
        child = json.loads(decoded)
    except json.JSONDecodeError:
        child = {"result_available": False, "raw_stdout": decoded}
    return {
        "capability": "available",
        "returncode": returncode,
        "stderr": stderr.decode("utf-8", errors="replace")[-2_000:],
        "child": child,
    }


async def _probe_concurrent_profile(
    profile: SandboxProfile,
    root_rw: Path,
    root_ro: Path,
    state_dir: Path,
    operations: dict[str, dict[str, str]],
    ready_path: Path,
) -> dict[str, object]:
    writer_code = build_concurrent_actor_code(
        "writer", operations=operations, ready_path=str(ready_path)
    )
    observer_code = build_concurrent_actor_code(
        "observer", operations=operations, ready_path=str(ready_path)
    )
    try:
        adapter = LinuxBubblewrapLocalProcessSandbox(profile, root_rw, state_dir)
    except SandboxError as error:
        return {"capability": "unavailable", "error": str(error)}
    writer_process, observer_process = await asyncio.gather(
        adapter.spawn(_request(profile, root_rw, root_ro, writer_code)),
        adapter.spawn(_request(profile, root_rw, root_ro, observer_code)),
    )

    async def collect(process: Any) -> dict[str, object]:
        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            return {"capability": "unavailable", "error": "capture streams unavailable"}
        output, error_output, returncode = await asyncio.gather(
            stdout.read(), stderr.read(), process.wait()
        )
        decoded = output.decode("utf-8", errors="replace")
        try:
            child = json.loads(decoded)
        except json.JSONDecodeError:
            child = {"raw_stdout": decoded}
        return {
            "capability": "available",
            "returncode": returncode,
            "stderr": error_output.decode("utf-8", errors="replace")[-2_000:],
            **(child if isinstance(child, dict) else {"child": child}),
        }

    writer, observer = await asyncio.gather(collect(writer_process), collect(observer_process))
    return {"writer": writer, "observer": observer}


def _restore_operations(
    operations: dict[str, dict[str, str]],
    source_contents: dict[str, str],
) -> None:
    """Restore fixtures between profile runs outside the async hot path."""

    for operation in operations.values():
        Path(operation["destination"]).unlink(missing_ok=True)
        Path(operation["source"]).write_text(source_contents[operation["source"]], encoding="utf-8")


async def _run() -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="neuro-code-linux-hardlink-mode-"))
    try:
        root_rw = root / "authorized-rw"
        root_ro = root / "authorized-ro"
        state_dir = root / "controller-state"
        for path in (root_rw, root_ro, state_dir):
            path.mkdir()
        (state_dir / "credentials.json").write_text("controller-secret", encoding="utf-8")

        root_pairs = {
            "ro_to_rw": (root_ro, root_rw),
            "rw_to_ro": (root_rw, root_ro),
            "rw_to_rw": (root_rw, root_rw),
            "ro_to_ro": (root_ro, root_ro),
        }
        operations: dict[str, dict[str, str]] = {}
        source_contents: dict[str, str] = {}
        for direction in DIRECTIONS:
            source_root, destination_root = root_pairs[direction]
            for api in APIS:
                name = f"{direction}.{api}"
                source = source_root / f"mode-source-{direction}-{api}"
                destination = destination_root / f"mode-alias-{direction}-{api}"
                content = f"source:{direction}:{api}"
                source.write_text(content, encoding="utf-8")
                destination.unlink(missing_ok=True)
                operations[name] = {
                    "direction": direction,
                    "api": api,
                    "source": str(source),
                    "destination": str(destination),
                }
                source_contents[str(source)] = content

        profiles: dict[str, object] = {}
        for profile in (
            SandboxProfile.WORKSPACE,
            SandboxProfile.STRICT,
            SandboxProfile.READ_ONLY,
        ):
            result = await _probe_profile(profile, root_rw, root_ro, state_dir, operations)
            profiles[profile.value] = result
            await asyncio.to_thread(_restore_operations, operations, source_contents)

        mixed_source = root_ro / "preexisting-mixed-ro-source"
        mixed_alias = root_rw / "preexisting-mixed-rw-alias"
        mixed_profiles: dict[str, object] = {}
        mixed_findings: list[str] = []
        for profile in (
            SandboxProfile.WORKSPACE,
            SandboxProfile.STRICT,
            SandboxProfile.READ_ONLY,
        ):
            mixed_alias.unlink(missing_ok=True)
            mixed_source.write_text("mixed-mode-source", encoding="utf-8")
            try:
                os.link(mixed_source, mixed_alias)
            except OSError as error:
                mixed_profiles[profile.value] = {
                    "fixture": "unavailable",
                    "errno": error.errno,
                    "error": str(error),
                }
                continue
            result = await _probe_mixed_mode_profile(
                profile,
                root_rw,
                root_ro,
                state_dir,
                mixed_source,
                mixed_alias,
            )
            mixed_profiles[profile.value] = result
            mixed_alias.unlink(missing_ok=True)
            mixed_source.write_text("mixed-mode-source", encoding="utf-8")
        mixed_findings.extend(
            classify_mixed_mode_access(
                {
                    profile: value["child"]
                    for profile, value in mixed_profiles.items()
                    if isinstance(value, dict)
                    and value.get("capability") == "available"
                    and isinstance(value.get("child"), dict)
                }
            )
        )
        mixed_findings.extend(
            f"{profile}: mixed-mode fixture unavailable"
            for profile, value in mixed_profiles.items()
            if isinstance(value, dict) and value.get("fixture") == "unavailable"
        )

        concurrent_root_pairs = {
            "external_to_rw": (
                state_dir / "credentials.json",
                root_rw / "concurrent-external-alias",
            ),
            "ro_to_rw": (root_ro / "concurrent-ro-source", root_rw / "concurrent-ro-alias"),
            "rw_to_rw": (root_rw / "concurrent-rw-source", root_rw / "concurrent-rw-alias"),
        }
        concurrent_operations = {
            f"{name}.os_link": {
                "source": str(source),
                "destination": str(destination),
                "api": "os_link",
            }
            for name, (source, destination) in concurrent_root_pairs.items()
        }
        for source, _destination in concurrent_root_pairs.values():
            source.write_text("concurrent-source", encoding="utf-8")
        ready_path = root_rw / "concurrent-writer-ready"
        concurrent_profiles: dict[str, object] = {}
        for profile in (
            SandboxProfile.WORKSPACE,
            SandboxProfile.STRICT,
            SandboxProfile.READ_ONLY,
        ):
            for path in (
                ready_path,
                *(destination for _, destination in concurrent_root_pairs.values()),
            ):
                path.unlink(missing_ok=True)
            concurrent_profiles[profile.value] = await _probe_concurrent_profile(
                profile,
                root_rw,
                root_ro,
                state_dir,
                concurrent_operations,
                ready_path,
            )

        concurrent_findings: list[str] = []
        for profile, raw in concurrent_profiles.items():
            if not isinstance(raw, dict):
                concurrent_findings.append(f"{profile}: malformed concurrent report")
                continue
            writer = raw.get("writer")
            observer = raw.get("observer")
            if isinstance(writer, dict) and isinstance(observer, dict):
                concurrent_findings.extend(
                    f"{profile}: {finding}"
                    for finding in classify_concurrent_findings(
                        writer,
                        observer,
                        require_ready=profile != SandboxProfile.READ_ONLY.value,
                    )
                )
            else:
                concurrent_findings.append(f"{profile}: writer/observer report missing")

        findings = classify_findings(
            {
                profile: value["child"]
                for profile, value in profiles.items()
                if isinstance(value, dict)
                and value.get("capability") == "available"
                and isinstance(value.get("child"), dict)
            }
        )
        all_findings = [*findings, *mixed_findings, *concurrent_findings]
        return {
            "probe": "linux-cross-root-hardlink-mode-v1",
            "status": "BLOCKED_CAPABILITY" if all_findings else "NO_MODE_BYPASS_OBSERVED",
            "roots": {"read_write": str(root_rw), "read_only": str(root_ro)},
            "directions": DIRECTIONS,
            "apis": APIS,
            "profiles": profiles,
            "preexisting_mixed_mode": {
                "source": str(mixed_source),
                "alias": str(mixed_alias),
                "profiles": mixed_profiles,
                "findings": mixed_findings,
                "status": (
                    "BLOCKED_CAPABILITY" if mixed_findings else "NO_MIXED_MODE_BYPASS_OBSERVED"
                ),
            },
            "findings": all_findings,
            "concurrent_children": {
                "profiles": concurrent_profiles,
                "findings": concurrent_findings,
                "status": (
                    "BLOCKED_CAPABILITY" if concurrent_findings else "NO_CONCURRENT_ALIAS_BYPASS"
                ),
            },
            "production_guard_scope": "controller-state hardlink validation only",
            "interpretation": (
                "A BLOCKED_CAPABILITY result is an evidence finding; this probe does not "
                "change the production Bubblewrap hardlink guard."
            ),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Linux mixed-mode hardlinks")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_run())
    serialized = json.dumps(report, indent=2, sort_keys=True)
    print(serialized)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
