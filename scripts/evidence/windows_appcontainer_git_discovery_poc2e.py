"""Evidence-only Git for Windows AppContainer repository-discovery probe."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, cast

POC2B_HEAD = "7ac1b77e6cc0dc1d51ab7bd47fe4f0ac76d71336"
POC2D_HEAD = "ff32981db8f53d82e7692bd2998952df2838af50"
CLASSIFICATION = "EVIDENCE_ONLY_DO_NOT_MERGE"
DECISIONS = {
    "GIT_WINDOWS_DISCOVERY_BOUNDARY_VIABLE",
    "GIT_WINDOWS_PROTECTED_ANCESTOR_BLOCKED",
    "GIT_WINDOWS_PARENT_METADATA_LEAK_REQUIRED",
    "WINDOWS_GIT_DISCOVERY_ARCHITECTURE_BLOCKED",
}
MINIMAL_ANCESTOR_RIGHTS = {
    "FILE_TRAVERSE": 0x20,
    "FILE_READ_ATTRIBUTES": 0x80,
    "READ_CONTROL": 0x20000,
}


def _load_poc2b(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("neuro_poc2b_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load pinned POC2B harness: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _status(passed: bool, detail: object) -> dict[str, object]:
    return {"status": "PASS" if passed else "FAIL", "detail": detail}


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 90) -> dict[str, object]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    return {
        "command": command,
        "cwd": str(cwd) if cwd is not None else None,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _relay(
    pb: ModuleType,
    api: Any,
    launcher: Any,
    bootstrap: Path,
    command: list[str],
    *,
    exit_code: int | None = 0,
    stdout_contains: tuple[str, ...] = (),
) -> dict[str, object]:
    gate = cast(
        dict[str, object],
        pb._command_relay_gate(api, launcher, bootstrap, command, timeout_ms=90_000),
    )
    detail = cast(dict[str, object], gate["detail"])
    passed = gate.get("status") == "PASS"
    if exit_code is not None:
        passed = passed and detail.get("target_exit_code") == exit_code
    stdout = str(detail.get("stdout_text", ""))
    passed = passed and all(value in stdout for value in stdout_contains)
    gate["status"] = "PASS" if passed else "FAIL"
    return gate


def _parse_relay_json(gate: dict[str, object]) -> dict[str, object]:
    detail = cast(dict[str, object], gate.get("detail", {}))
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(str(detail.get("stdout_text", "")))
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
    return {}


def _command(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    git: Path,
    cwd: Path,
    environment: dict[str, str],
    arguments: list[str],
    *,
    exit_code: int | None = 0,
    stdout_contains: tuple[str, ...] = (),
) -> dict[str, object]:
    launcher = pb._Launcher(api, profile, cwd, environment.copy())
    return _relay(
        pb,
        api,
        launcher,
        bootstrap,
        [str(git), *arguments],
        exit_code=exit_code,
        stdout_contains=stdout_contains,
    )


def _path_identity(text: str) -> str:
    return os.path.normcase(text.strip().replace("/", "\\").removeprefix("\\\\?\\"))


def _short_path(path: Path) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    function = kernel32.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(function(str(path), buffer, len(buffer)))
    if not 0 < length < len(buffer):
        return None
    return buffer.value if _path_identity(buffer.value) != _path_identity(str(path)) else None


def _rev_parse_gate(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    git: Path,
    cwd: Path,
    environment: dict[str, str],
    expected: Path,
) -> dict[str, object]:
    gate = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        cwd,
        environment,
        ["rev-parse", "--show-toplevel"],
    )
    detail = cast(dict[str, object], gate["detail"])
    returned = str(detail.get("stdout_text", "")).strip()
    detail["expected_identity"] = str(expected.resolve(strict=True))
    detail["returned_path"] = returned
    detail["identity_matches"] = _path_identity(returned) == _path_identity(str(expected))
    if detail["identity_matches"] is not True:
        gate["status"] = "FAIL"
    return gate


def _no_repo_gate(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    git: Path,
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    gate = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        cwd,
        environment,
        ["rev-parse", "--show-toplevel"],
        exit_code=None,
    )
    detail = cast(dict[str, object], gate["detail"])
    stderr = str(detail.get("stderr_text", ""))
    gate["status"] = (
        "PASS"
        if detail.get("target_exit_code") == 128
        and "not a git repository" in stderr.lower()
        and "permission denied" not in stderr.lower()
        else "FAIL"
    )
    return gate


def _private_environment(
    pb: ModuleType,
    private_home: Path,
    runtime: Path,
    *,
    workspace_parent: Path | None = None,
    path_sep: str,
    across: bool = False,
    ceiling: bool = False,
) -> dict[str, str]:
    environment = cast(dict[str, str], pb._private_environment(private_home, [runtime]))
    if across:
        environment["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "1"
    if ceiling:
        if workspace_parent is None:
            raise ValueError("ceiling environment requires a workspace parent")
        environment["GIT_CEILING_DIRECTORIES"] = f"{path_sep}{workspace_parent}"
    return environment


def _init_normal(git: Path, repo: Path) -> dict[str, object]:
    repo.mkdir(parents=True, exist_ok=True)
    return _run([str(git), "init", "."], cwd=repo)


def _discovery_scenarios(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    git: Path,
    runtime: Path,
    private_home: Path,
    workspace_parent: Path,
    root_repo: Path,
    nested_workspace: Path,
    no_repo: Path,
    path_sep: str,
) -> dict[str, object]:
    root_subdir = root_repo / "sub" / "dir"
    nested_repo = nested_workspace / "a" / "b"
    nested_subdir = nested_repo / "sub"
    no_repo_subdir = no_repo / "sub" / "dir"
    for directory in (root_subdir, nested_subdir, no_repo_subdir):
        directory.mkdir(parents=True, exist_ok=True)
    precreate = {
        "root_repo": _init_normal(git, root_repo),
        "nested_repo": _init_normal(git, nested_repo),
    }
    default = _private_environment(pb, private_home, runtime, path_sep=path_sep)
    across_only = _private_environment(pb, private_home, runtime, path_sep=path_sep, across=True)
    ceiling_only = _private_environment(
        pb,
        private_home,
        runtime,
        workspace_parent=workspace_parent,
        path_sep=path_sep,
        ceiling=True,
    )
    candidate = _private_environment(
        pb,
        private_home,
        runtime,
        workspace_parent=workspace_parent,
        path_sep=path_sep,
        across=True,
        ceiling=True,
    )
    controls = {
        "default": _rev_parse_gate(
            pb, api, profile, bootstrap, git, root_subdir, default, root_repo
        ),
        "across_filesystem_only": _rev_parse_gate(
            pb, api, profile, bootstrap, git, root_subdir, across_only, root_repo
        ),
        "ceiling_only": _rev_parse_gate(
            pb, api, profile, bootstrap, git, root_subdir, ceiling_only, root_repo
        ),
    }
    scenarios = {
        "workspace_root_repo": _rev_parse_gate(
            pb, api, profile, bootstrap, git, root_repo, candidate, root_repo
        ),
        "subdir_to_root_repo": _rev_parse_gate(
            pb, api, profile, bootstrap, git, root_subdir, candidate, root_repo
        ),
        "nested_nearest_repo": _rev_parse_gate(
            pb, api, profile, bootstrap, git, nested_subdir, candidate, nested_repo
        ),
        "no_repo_ceiling_stop": _no_repo_gate(
            pb, api, profile, bootstrap, git, no_repo_subdir, candidate
        ),
    }
    aliases: dict[str, object] = {
        "extended": _rev_parse_gate(
            pb,
            api,
            profile,
            bootstrap,
            git,
            Path("\\\\?\\" + str(root_subdir)),
            candidate,
            root_repo,
        )
    }
    short = _short_path(root_subdir)
    aliases["short_8dot3"] = (
        _rev_parse_gate(pb, api, profile, bootstrap, git, Path(short), candidate, root_repo)
        if short is not None
        else {"status": "UNAVAILABLE", "reason": "8.3 alias not available"}
    )
    return {
        "precreated_by_trusted_controller": precreate,
        "trusted_environment": {
            "PATH_SEP": path_sep,
            "GIT_CEILING_DIRECTORIES": candidate["GIT_CEILING_DIRECTORIES"],
            "GIT_DISCOVERY_ACROSS_FILESYSTEM": candidate["GIT_DISCOVERY_ACROSS_FILESYSTEM"],
            "ceiling_boundary": str(workspace_parent),
        },
        "controls": controls,
        "scenarios": scenarios,
        "alias_scenarios": aliases,
        "overall": "PASS"
        if all(gate["status"] == "PASS" for gate in scenarios.values())
        else "FAIL",
    }


def _direct_authority_probe(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    cwd: Path,
    parent: Path,
    known_child: Path,
    sibling: Path,
    protected_ancestor: Path,
    host_sentinel: Path,
    outside_secret: Path,
    controller_secret: Path,
    junction_alias: Path,
) -> dict[str, object]:
    launcher = pb._Launcher(api, profile, cwd, None)
    gate = _relay(
        pb,
        api,
        launcher,
        bootstrap,
        [
            str(probe),
            "authority-check",
            str(parent),
            str(known_child),
            sibling.parent.name,
            str(sibling),
            str(protected_ancestor),
            str(host_sentinel),
            str(outside_secret),
            str(controller_secret),
            str(junction_alias),
        ],
    )
    detail = cast(dict[str, object], gate["detail"])
    detail["parsed_result"] = _parse_relay_json(gate)
    return gate


def _direct_boundary_pass(gate: dict[str, object]) -> bool:
    detail = cast(dict[str, object], gate.get("detail", {}))
    parsed = cast(dict[str, object], detail.get("parsed_result", {}))
    return bool(
        cast(dict[str, object], parsed.get("stat_parent", {})).get("success") is False
        and cast(dict[str, object], parsed.get("traverse_known_child", {})).get("success") is True
        and parsed.get("enumerate_parent") is False
        and parsed.get("sibling_name_visible") is False
        and cast(dict[str, object], parsed.get("sibling_content_read", {})).get("success") is False
        and cast(dict[str, object], parsed.get("protected_ancestor_stat", {})).get("success")
        is False
        and parsed.get("protected_ancestor_enumerate") is False
        and cast(dict[str, object], parsed.get("host_sentinel_read", {})).get("success") is False
        and cast(dict[str, object], parsed.get("outside_secret_read", {})).get("success") is False
        and cast(dict[str, object], parsed.get("controller_secret_read", {})).get("success")
        is False
        and cast(dict[str, object], parsed.get("junction_target_read", {})).get("success") is False
    )


def _functional_gate(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    git: Path,
    runtime: Path,
    repo: Path,
    private_home: Path,
    workspace_parent: Path,
    path_sep: str,
    host_config: Path,
    host_marker: str,
    denied_paths: dict[str, Path],
    expected_sid: str,
    *,
    include_descendant: bool,
) -> dict[str, object]:
    repo.mkdir(parents=True, exist_ok=True)
    environment = _private_environment(
        pb,
        private_home,
        runtime,
        workspace_parent=workspace_parent,
        path_sep=path_sep,
        across=True,
        ceiling=True,
    )
    gates: dict[str, object] = {}
    gates["version"] = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        repo,
        environment,
        ["--version"],
        stdout_contains=("git version",),
    )
    gates["init"] = _command(pb, api, profile, bootstrap, git, repo, environment, ["init", "."])
    tracked = repo / "tracked.txt"
    tracked.write_text("POC2E_TRACKED\n", encoding="utf-8")
    gates["add"] = _command(
        pb, api, profile, bootstrap, git, repo, environment, ["add", "tracked.txt"]
    )
    gates["status"] = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        repo,
        environment,
        ["status", "--short"],
        stdout_contains=("A  tracked.txt",),
    )
    gates["rev_parse"] = _rev_parse_gate(pb, api, profile, bootstrap, git, repo, environment, repo)
    gates["private_config_write"] = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        repo,
        environment,
        ["config", "--global", "neuro.private", "SANDBOX_PRIVATE"],
    )
    gates["private_config_read"] = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        repo,
        environment,
        ["config", "--global", "--get", "neuro.private"],
        stdout_contains=("SANDBOX_PRIVATE",),
    )
    host_default = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        repo,
        environment,
        ["config", "--global", "--get", "neuro.host-marker"],
        exit_code=None,
    )
    host_default_detail = cast(dict[str, object], host_default["detail"])
    host_default["status"] = (
        "PASS"
        if host_default_detail.get("target_exit_code") != 0
        and host_marker not in str(host_default_detail.get("stdout_text", ""))
        else "FAIL"
    )
    gates["host_config_not_loaded"] = host_default
    host_explicit = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        repo,
        environment,
        ["config", "--file", str(host_config), "--get", "neuro.host-marker"],
        exit_code=None,
    )
    host_explicit_detail = cast(dict[str, object], host_explicit["detail"])
    host_explicit["status"] = (
        "PASS"
        if host_explicit_detail.get("target_exit_code") != 0
        and host_marker not in str(host_explicit_detail.get("stdout_text", ""))
        and host_config.name in str(host_explicit_detail.get("stderr_text", ""))
        else "FAIL"
    )
    gates["host_config_denied"] = host_explicit
    denied: dict[str, object] = {}
    for name, path in denied_paths.items():
        gate = _command(
            pb,
            api,
            profile,
            bootstrap,
            git,
            repo,
            environment,
            ["hash-object", str(path)],
            exit_code=None,
        )
        detail = cast(dict[str, object], gate["detail"])
        gate["status"] = (
            "PASS"
            if detail.get("target_exit_code") != 0
            and path.name in str(detail.get("stderr_text", ""))
            else "FAIL"
        )
        denied[name] = gate
    gates["outside_denied"] = {
        "status": "PASS"
        if all(cast(dict[str, object], item)["status"] == "PASS" for item in denied.values())
        else "FAIL",
        "detail": denied,
    }
    if include_descendant:
        report = repo / "descendant.json"
        hook = repo / ".git" / "hooks" / "pre-commit"
        if hook.parent.is_dir():
            shutil.copy2(probe, hook)
            descendant_environment = environment.copy()
            descendant_environment["NEURO_DESCENDANT_REPORT"] = str(report)
            for key, value in (
                ("user.name", "Neuro Evidence"),
                ("user.email", "evidence@example.invalid"),
            ):
                _command(
                    pb,
                    api,
                    profile,
                    bootstrap,
                    git,
                    repo,
                    descendant_environment,
                    ["config", key, value],
                )
            commit = _command(
                pb,
                api,
                profile,
                bootstrap,
                git,
                repo,
                descendant_environment,
                ["commit", "-m", "evidence"],
            )
            facts: dict[str, object] = {}
            if report.exists():
                with contextlib.suppress(json.JSONDecodeError):
                    facts = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
            detail = cast(dict[str, object], commit["detail"])
            detail["descendant_facts"] = facts
            commit["status"] = (
                "PASS"
                if detail.get("target_exit_code") == 0
                and facts.get("is_appcontainer") is True
                and facts.get("integrity_rid") == 4096
                and facts.get("in_job") is True
                and facts.get("package_sid") == expected_sid
                else "FAIL"
            )
        else:
            commit = _status(False, {"reason": "git init failed; hook was not launched"})
        gates["descendant_confinement"] = commit
    required = [
        "version",
        "init",
        "add",
        "status",
        "rev_parse",
        "private_config_write",
        "private_config_read",
        "host_config_not_loaded",
        "host_config_denied",
        "outside_denied",
    ]
    if include_descendant:
        required.append("descendant_confinement")
    gates["overall"] = (
        "PASS"
        if all(cast(dict[str, object], gates[name])["status"] == "PASS" for name in required)
        else "FAIL"
    )
    return gates


def _git_dir_control(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    git: Path,
    runtime: Path,
    private_home: Path,
    repo: Path,
    cwd: Path,
    path_sep: str,
) -> dict[str, object]:
    environment = _private_environment(pb, private_home, runtime, path_sep=path_sep)
    environment["GIT_DIR"] = str(repo / ".git")
    environment["GIT_WORK_TREE"] = str(repo)
    status = _command(pb, api, profile, bootstrap, git, cwd, environment, ["status", "--short"])
    rev_parse = _rev_parse_gate(pb, api, profile, bootstrap, git, cwd, environment, repo)
    return {
        "status": "PASS"
        if status["status"] == "PASS" and rev_parse["status"] == "PASS"
        else "FAIL",
        "git_status": status,
        "rev_parse": rev_parse,
        "control_only": True,
    }


def _junction_gates(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    git: Path,
    runtime: Path,
    private_home: Path,
    workspace_parent: Path,
    path_sep: str,
    authorized_alias: Path,
    authorized_target: Path,
    denied_alias: Path,
    denied_target_file: Path,
) -> dict[str, object]:
    environment = _private_environment(
        pb,
        private_home,
        runtime,
        workspace_parent=workspace_parent,
        path_sep=path_sep,
        across=True,
        ceiling=True,
    )
    authorized = _rev_parse_gate(
        pb,
        api,
        profile,
        bootstrap,
        git,
        authorized_alias,
        environment,
        authorized_target,
    )
    denied = _command(
        pb,
        api,
        profile,
        bootstrap,
        git,
        authorized_target,
        environment,
        ["hash-object", str(denied_alias / denied_target_file.name)],
        exit_code=None,
    )
    denied_detail = cast(dict[str, object], denied["detail"])
    denied["status"] = (
        "PASS"
        if denied_detail.get("target_exit_code") != 0
        and denied_target_file.name in str(denied_detail.get("stderr_text", ""))
        else "FAIL"
    )
    return {
        "authorized_target_discovery": authorized,
        "unauthorized_target_denied": denied,
        "status": "PASS"
        if authorized["status"] == "PASS" and denied["status"] == "PASS"
        else "FAIL",
    }


def _grant_minimal_parent(pb: ModuleType, parent: Path, sid: str) -> dict[str, object]:
    rights = "(X,RA,RC)"
    completed = pb._run_checked(["icacls", str(parent), "/grant", f"*{sid}:{rights}"])
    return {
        "path": str(parent),
        "rights": rights,
        "mask_hex": f"0x{sum(MINIMAL_ANCESTOR_RIGHTS.values()):08x}",
        "rights_components": MINIMAL_ANCESTOR_RIGHTS,
        "inheritance": "NONE",
        "explicitly_excluded": [
            "FILE_LIST_DIRECTORY",
            "FILE_READ_DATA",
            "FILE_READ_EA",
            "WRITE*",
            "DELETE*",
            "WRITE_DAC",
            "WRITE_OWNER",
        ],
        "output": (completed.stdout + completed.stderr).strip(),
    }


def _remove_parent_grant(pb: ModuleType, parent: Path, sid: str) -> dict[str, object]:
    completed = pb._run_checked(["icacls", str(parent), "/remove:g", f"*{sid}"])
    return {
        "path": str(parent),
        "exact_sid": sid,
        "output": (completed.stdout + completed.stderr).strip(),
    }


def _minimal_parent_diagnostic(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    git: Path,
    runtime: Path,
    private_home: Path,
    parent: Path,
    workspace: Path,
    sibling: Path,
    protected_ancestor: Path,
    host_sentinel: Path,
    outside_secret: Path,
    controller_secret: Path,
    junction_alias: Path,
    path_sep: str,
) -> dict[str, object]:
    _init_normal(git, workspace)
    before = _direct_authority_probe(
        pb,
        api,
        profile,
        bootstrap,
        probe,
        workspace,
        parent,
        workspace,
        sibling,
        protected_ancestor,
        host_sentinel,
        outside_secret,
        controller_secret,
        junction_alias,
    )
    grant = _grant_minimal_parent(pb, parent, profile.sid_text)
    try:
        after = _direct_authority_probe(
            pb,
            api,
            profile,
            bootstrap,
            probe,
            workspace,
            parent,
            workspace,
            sibling,
            protected_ancestor,
            host_sentinel,
            outside_secret,
            controller_secret,
            junction_alias,
        )
        environment = _private_environment(pb, private_home, runtime, path_sep=path_sep)
        git_after = _rev_parse_gate(
            pb, api, profile, bootstrap, git, workspace, environment, workspace
        )
    finally:
        cleanup = _remove_parent_grant(pb, parent, profile.sid_text)
    after_result = cast(
        dict[str, object], cast(dict[str, object], after["detail"])["parsed_result"]
    )
    return {
        "conditional_on_candidate_a_failure": True,
        "before": before,
        "grant": grant,
        "after": after,
        "git_after": git_after,
        "cleanup": cleanup,
        "stat_and_traverse_pass": bool(
            cast(dict[str, object], after_result.get("stat_parent", {})).get("success") is True
            and cast(dict[str, object], after_result.get("traverse_known_child", {})).get("success")
            is True
        ),
        "metadata_leak": bool(
            after_result.get("enumerate_parent") is True
            or after_result.get("sibling_name_visible") is True
            or cast(dict[str, object], after_result.get("sibling_content_read", {})).get("success")
            is True
        ),
        "protected_ancestor_feasibility": {
            "paths": ["C:\\", r"C:\Users", os.environ.get("PROGRAMFILES", r"C:\Program Files")],
            "acl_mutation_attempted": False,
            "requires_admin_or_nonowned_acl_change": True,
            "production_eligible": False,
        },
    }


def _junction(alias: Path, target: Path) -> dict[str, object]:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", "mklink", "/J", str(alias), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "status": "PASS" if completed.returncode == 0 and alias.exists() else "FAIL",
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _core_fixture(
    args: argparse.Namespace,
    pb: ModuleType,
    api: Any,
    *,
    standard_user: bool,
) -> tuple[dict[str, object], bool]:
    path_sep = str(args.path_sep)
    if path_sep != ";":
        raise RuntimeError(f"unexpected Git for Windows PATH_SEP: {path_sep!r}")
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-git-discovery-poc2e-")).resolve(strict=True)
    helper = fixture / "trusted-helper"
    workspace_parent = fixture / "workspace-parent"
    root_repo = workspace_parent / "Root Repo 空间"
    nested_workspace = workspace_parent / "Nested Workspace 空间"
    no_repo = workspace_parent / "No Repo 空间"
    functional_repo = workspace_parent / "Functional Repo 空间"
    junction_workspace = workspace_parent / "Junction Workspace"
    junction_target = junction_workspace / "authorized-target"
    authorized_alias = junction_workspace / "authorized-alias"
    denied_alias = junction_workspace / "denied-alias"
    private_home = fixture / "private-home"
    outside = fixture / "outside"
    controller = fixture / "controller-state"
    sibling = workspace_parent / "sibling-private"
    minimal_parent = fixture / "minimal-parent"
    minimal_workspace = minimal_parent / "workspace"
    minimal_sibling_dir = minimal_parent / "sibling-private"
    for directory in (
        helper,
        root_repo,
        nested_workspace,
        no_repo,
        functional_repo,
        junction_target,
        private_home,
        outside,
        controller,
        sibling,
        minimal_workspace,
        minimal_sibling_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    runtime = Path(args.runtime).resolve(strict=True)
    git = runtime / "git.exe"
    bootstrap = helper / "runtime-bootstrap.exe"
    probe = helper / "git-discovery-probe.exe"
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    outside_secret = outside / "outside-secret.txt"
    controller_secret = controller / "controller-state.txt"
    sibling_secret = sibling / "sibling-secret.txt"
    minimal_sibling = minimal_sibling_dir / "sibling-secret.txt"
    host_sentinel = Path.home() / f"poc2e-host-sentinel-{uuid.uuid4().hex}.txt"
    denied_target_file = outside / "junction-target-secret.txt"
    for path, value in (
        (outside_secret, "OUTSIDE_SECRET"),
        (controller_secret, "CONTROLLER_SECRET"),
        (sibling_secret, "SIBLING_SECRET"),
        (minimal_sibling, "MINIMAL_SIBLING_SECRET"),
        (host_sentinel, "HOST_SENTINEL"),
        (denied_target_file, "JUNCTION_TARGET_SECRET"),
    ):
        path.write_text(value, encoding="utf-8")
    authorized_junction = _junction(authorized_alias, junction_target)
    denied_junction = _junction(denied_alias, outside)
    _init_normal(git, junction_target)
    host_config = Path.home() / ".gitconfig"
    host_marker = f"HOST_{uuid.uuid4().hex}"
    original_config = host_config.read_bytes() if host_config.exists() else None
    host_config.write_text(f"[neuro]\n\thost-marker = {host_marker}\n", encoding="utf-8")
    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "mode": "REAL_STANDARD_USER" if standard_user else "RUNNER_CONTROLLER",
        "runner": {
            "platform": platform.platform(),
            "release": platform.release(),
            "version": platform.version(),
            "python": sys.version,
        },
        "poc2b_harness_head": POC2B_HEAD,
        "poc2d_reference_head": POC2D_HEAD,
        "git_source": {"tag": args.git_tag, "commit": args.git_source_commit},
        "fixed_patch_sha256": {
            "sanitize_stdfds": args.nul_patch_sha256,
            "filenameinfo": args.path_patch_sha256,
        },
        "junction_creation": {
            "authorized": authorized_junction,
            "denied": denied_junction,
        },
    }
    profile = None
    roots: list[Any] = []
    cleanup: list[object] = []
    critical: list[str] = []
    try:
        token = pb._token_facts(api)
        result["controller_token"] = token
        if standard_user:
            token_ok = bool(
                token.get("administrator_group_enabled") is False
                and token.get("elevated") is False
                and token.get("integrity_level") == "MEDIUM"
            )
            if not token_ok:
                critical.append("STANDARD_USER_TOKEN")
        profile = pb._Profile(api, f"NeuroCode.Poc2E.{uuid.uuid4().hex}")
        root_specs = [
            pb._authority_root(api, helper, "RX", "evidence helpers", "USER_INSTALL"),
            pb._authority_root(api, runtime, "RX", "patched Git runtime", "USER_INSTALL"),
            pb._authority_root(api, private_home, "RW", "private HOME", "USER_INSTALL"),
        ]
        for workspace in (
            root_repo,
            nested_workspace,
            no_repo,
            functional_repo,
            junction_workspace,
            minimal_workspace,
        ):
            root_specs.append(
                pb._authority_root(api, workspace, "RW", "workspace", "WORKSPACE_RUNTIME")
            )
        roots = pb._deduplicate_roots(root_specs)
        result["authority_grants"] = [pb._grant_root(root, profile.sid_text) for root in roots]
        result["discovery"] = _discovery_scenarios(
            pb,
            api,
            profile,
            bootstrap,
            git,
            runtime,
            private_home,
            workspace_parent,
            root_repo,
            nested_workspace,
            no_repo,
            path_sep,
        )
        discovery_ok = cast(dict[str, object], result["discovery"])["overall"] == "PASS"
        result["functional"] = _functional_gate(
            pb,
            api,
            profile,
            bootstrap,
            probe,
            git,
            runtime,
            functional_repo,
            private_home,
            workspace_parent,
            path_sep,
            host_config,
            host_marker,
            {
                "outside": outside_secret,
                "controller": controller_secret,
                "host_userprofile": host_sentinel,
            },
            profile.sid_text,
            include_descendant=not standard_user,
        )
        functional_ok = cast(dict[str, object], result["functional"])["overall"] == "PASS"
        result["direct_authority"] = _direct_authority_probe(
            pb,
            api,
            profile,
            bootstrap,
            probe,
            functional_repo,
            workspace_parent,
            functional_repo,
            sibling_secret,
            Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\Users"),
            host_sentinel,
            outside_secret,
            controller_secret,
            denied_alias / denied_target_file.name,
        )
        direct_ok = _direct_boundary_pass(cast(dict[str, object], result["direct_authority"]))
        cast(dict[str, object], result["direct_authority"])["boundary_status"] = (
            "PASS" if direct_ok else "FAIL"
        )
        result["junction"] = _junction_gates(
            pb,
            api,
            profile,
            bootstrap,
            git,
            runtime,
            private_home,
            workspace_parent,
            path_sep,
            authorized_alias,
            junction_target,
            denied_alias,
            denied_target_file,
        )
        result["git_dir_control"] = _git_dir_control(
            pb,
            api,
            profile,
            bootstrap,
            git,
            runtime,
            private_home,
            root_repo,
            root_repo / "sub" / "dir",
            path_sep,
        )
        if not discovery_ok:
            result["minimal_parent_diagnostic"] = _minimal_parent_diagnostic(
                pb,
                api,
                profile,
                bootstrap,
                probe,
                git,
                runtime,
                private_home,
                minimal_parent,
                minimal_workspace,
                minimal_sibling,
                Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\Users"),
                host_sentinel,
                outside_secret,
                controller_secret,
                denied_alias / denied_target_file.name,
                path_sep,
            )
        else:
            result["minimal_parent_diagnostic"] = {
                "status": "NOT_RUN",
                "reason": "Candidate A passed; conditional diagnostic not authorized",
            }
        if not discovery_ok:
            critical.append("DISCOVERY")
        if not functional_ok:
            critical.append("FUNCTIONAL")
        if cast(dict[str, object], result["junction"])["status"] != "PASS":
            critical.append("JUNCTION")
        if not direct_ok:
            critical.append("DIRECT_AUTHORITY")
    except Exception as error:
        result["harness_error"] = {"type": type(error).__name__, "message": str(error)}
        critical.append("HARNESS")
    finally:
        if profile is not None:
            for root in reversed(roots):
                with contextlib.suppress(Exception):
                    cleanup.append(pb._cleanup_root(root, profile.sid_text))
            result["profile_delete_hresult"] = profile.close()
        result["authority_cleanup"] = cleanup
        with contextlib.suppress(Exception):
            shutil.rmtree(fixture)
        host_sentinel.unlink(missing_ok=True)
        if original_config is None:
            host_config.unlink(missing_ok=True)
        else:
            host_config.write_bytes(original_config)
    result["critical_failures"] = sorted(set(critical))
    result["overall"] = "PASS" if not critical else "FAIL"
    return result, not critical


def _create_standard_user(args: argparse.Namespace, pb: ModuleType, api: Any) -> dict[str, object]:
    username = f"neuro_poc2e_{uuid.uuid4().hex[:8]}"
    password = f"N5!{uuid.uuid4().hex[:8]}aA"
    public = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / f"NeuroPoc2E-{uuid.uuid4().hex}"
    public.mkdir(parents=True)
    script = public / Path(__file__).name
    poc2b = public / "windows_appcontainer_runtime_poc2b.py"
    bootstrap = public / "runtime-bootstrap.exe"
    probe = public / "git-discovery-probe.exe"
    runtime = public / "candidate-runtime"
    report = public / "standard-user-report.json"
    shutil.copy2(Path(__file__), script)
    shutil.copy2(Path(args.poc2b), poc2b)
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    shutil.copytree(Path(args.runtime), runtime)
    created = False
    process = None
    try:
        pb._run_checked(["net", "user", username, password, "/add", "/expires:never"])
        created = True
        pb._run_checked(["icacls", str(public), "/grant", f"{username}:(OI)(CI)(F)", "/T"])
        python = Path(sys.base_prefix) / "python.exe"
        command = ctypes.create_unicode_buffer(
            subprocess.list2cmdline(
                [
                    str(python),
                    str(script),
                    "--standard-user",
                    "--poc2b",
                    str(poc2b),
                    "--bootstrap",
                    str(bootstrap),
                    "--probe",
                    str(probe),
                    "--runtime",
                    str(runtime),
                    "--path-sep",
                    args.path_sep,
                    "--git-tag",
                    args.git_tag,
                    "--git-source-commit",
                    args.git_source_commit,
                    "--nul-patch-sha256",
                    args.nul_patch_sha256,
                    "--path-patch-sha256",
                    args.path_patch_sha256,
                    "--report",
                    str(report),
                ]
            )
        )
        startup = pb._StartupInfoW()
        startup.cb = ctypes.sizeof(startup)
        info = pb._ProcessInformation()
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        drive = os.environ.get("SYSTEMDRIVE", "C:").rstrip("\\/")
        profile = Path(f"{drive}\\") / "Users" / username
        environment = {
            "APPDATA": str(profile / "AppData" / "Roaming"),
            "COMSPEC": str(system_root / "System32" / "cmd.exe"),
            "HOME": str(profile),
            "LOCALAPPDATA": str(profile / "AppData" / "Local"),
            "PATH": os.pathsep.join([str(python.parent), str(system_root / "System32")]),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "SYSTEMDRIVE": drive,
            "SYSTEMROOT": str(system_root),
            "TEMP": str(profile / "AppData" / "Local" / "Temp"),
            "TMP": str(profile / "AppData" / "Local" / "Temp"),
            "USERPROFILE": str(profile),
            "WINDIR": str(system_root),
        }
        block = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0"
        )
        if not api.create_process_with_logon(
            username,
            ".",
            password,
            pb._LOGON_WITH_PROFILE,
            str(python),
            command,
            pb._CREATE_UNICODE_ENVIRONMENT | pb._CREATE_NO_WINDOW,
            block,
            str(public),
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            api.error("CreateProcessWithLogonW(real standard user)")
        process = pb._CreatedProcess(int(info.hProcess), int(info.hThread), int(info.dwProcessId))
        api.close(process.thread_handle)
        process.thread_handle = 0
        exit_code = pb._wait_exit(api, process, 300_000)
        child = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return {
            "status": "PASS" if exit_code == 0 and child.get("overall") == "PASS" else "FAIL",
            "username": username,
            "real_logon_process": True,
            "exit_code": exit_code,
            "report": child,
        }
    except Exception as error:
        return {
            "status": "FAIL",
            "username": username,
            "created_local_user": created,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    finally:
        pb._close_process(api, process)
        if created:
            subprocess.run(["net", "user", username, "/delete"], capture_output=True, check=False)
        with contextlib.suppress(Exception):
            shutil.rmtree(public)


def _decision(result: dict[str, object]) -> str:
    if (
        result.get("overall") == "PASS"
        and cast(dict[str, object], result.get("standard_user", {})).get("status") == "PASS"
    ):
        return "GIT_WINDOWS_DISCOVERY_BOUNDARY_VIABLE"
    diagnostic = cast(dict[str, object], result.get("minimal_parent_diagnostic", {}))
    if diagnostic.get("stat_and_traverse_pass") is True:
        if diagnostic.get("metadata_leak") is True:
            return "GIT_WINDOWS_PARENT_METADATA_LEAK_REQUIRED"
        if cast(dict[str, object], diagnostic.get("git_after", {})).get("status") == "PASS":
            return "GIT_WINDOWS_PROTECTED_ANCESTOR_BLOCKED"
    return "WINDOWS_GIT_DISCOVERY_ARCHITECTURE_BLOCKED"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--standard-user", action="store_true")
    parser.add_argument("--poc2b", required=True)
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--path-sep", required=True)
    parser.add_argument("--git-tag", required=True)
    parser.add_argument("--git-source-commit", required=True)
    parser.add_argument("--nul-patch-sha256", required=True)
    parser.add_argument("--path-patch-sha256", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    pb = _load_poc2b(Path(args.poc2b))
    api = pb._WinApi()
    result, passed = _core_fixture(args, pb, api, standard_user=args.standard_user)
    if not args.standard_user:
        result["standard_user"] = _create_standard_user(args, pb, api)
        if cast(dict[str, object], result["standard_user"])["status"] != "PASS":
            passed = False
            failures = cast(list[str], result["critical_failures"])
            failures.append("STANDARD_USER")
            result["critical_failures"] = sorted(set(failures))
            result["overall"] = "FAIL"
        result["architecture_decision"] = _decision(result)
        if result["architecture_decision"] not in DECISIONS:
            raise RuntimeError("invalid POC2E decision")
    else:
        result["architecture_decision"] = (
            "GIT_WINDOWS_DISCOVERY_BOUNDARY_VIABLE"
            if passed
            else "WINDOWS_GIT_DISCOVERY_ARCHITECTURE_BLOCKED"
        )
    Path(args.report).write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        failure = {
            "classification": CLASSIFICATION,
            "architecture_decision": "WINDOWS_GIT_DISCOVERY_ARCHITECTURE_BLOCKED",
            "overall": "FAIL",
            "critical_failures": ["TOP_LEVEL_HARNESS"],
            "harness_error": {"type": type(error).__name__, "message": str(error)},
        }
        report = next(
            (
                Path(sys.argv[index + 1])
                for index, value in enumerate(sys.argv[:-1])
                if value == "--report"
            ),
            None,
        )
        if report is not None:
            report.write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(failure, indent=2), file=sys.stderr)
        raise
