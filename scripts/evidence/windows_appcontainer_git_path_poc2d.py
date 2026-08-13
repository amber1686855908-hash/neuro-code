"""Evidence-only Git for Windows AppContainer cwd/path compatibility probe."""

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
POC2C_HEAD = "61aa649f2791fa102874bee39f1605eb42f02748"
CLASSIFICATION = "EVIDENCE_ONLY_DO_NOT_MERGE"
CANDIDATE_ORDER = ("A_OPENED", "B_FILENAMEINFO", "C_CURRENT_DIRECTORY_CONTROL")


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


def _command(
    pb: ModuleType,
    api: Any,
    launcher: Any,
    bootstrap: Path,
    command: list[str],
    *,
    exit_code: int | None = 0,
    stdout_contains: tuple[str, ...] = (),
) -> dict[str, object]:
    gate = pb._command_relay_gate(
        api,
        launcher,
        bootstrap,
        command,
        timeout_ms=90_000,
    )
    detail = cast(dict[str, object], gate["detail"])
    passed = gate["status"] == "PASS"
    if exit_code is not None:
        passed = passed and detail.get("target_exit_code") == exit_code
    stdout = str(detail.get("stdout_text", ""))
    passed = passed and all(value in stdout for value in stdout_contains)
    gate["status"] = "PASS" if passed else "FAIL"
    return cast(dict[str, object], gate)


def _parse_relay_json(gate: dict[str, object]) -> dict[str, object]:
    detail = cast(dict[str, object], gate.get("detail", {}))
    try:
        value = json.loads(str(detail.get("stdout_text", "")))
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _matrix_gate(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    cwd: str,
) -> dict[str, object]:
    launcher = pb._Launcher(api, profile, Path(cwd), None)
    gate = _command(pb, api, launcher, bootstrap, [str(probe), "path-matrix"])
    parsed = _parse_relay_json(gate)
    detail = cast(dict[str, object], gate["detail"])
    detail["parsed_result"] = parsed
    token = cast(dict[str, object], parsed.get("token", {}))
    passed = bool(
        gate["status"] == "PASS"
        and token.get("is_appcontainer") is True
        and token.get("in_job") is True
        and cast(dict[str, object], parsed.get("get_current_directory", {})).get("success") is True
        and cast(dict[str, object], parsed.get("cwd_create_file", {})).get("success") is True
    )
    gate["status"] = "PASS" if passed else "FAIL"
    return gate


def _parent_visibility_gate(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    cwd: Path,
    parent: Path,
    sibling: Path,
) -> dict[str, object]:
    launcher = pb._Launcher(api, profile, cwd, None)
    gate = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(probe), "parent-visibility", str(parent), sibling.parent.name, str(sibling)],
    )
    detail = cast(dict[str, object], gate["detail"])
    detail["parsed_result"] = _parse_relay_json(gate)
    return gate


def _grant_exact_parent(pb: ModuleType, parent: Path, sid: str) -> dict[str, object]:
    completed = pb._run_checked(["icacls", str(parent), "/grant", f"*{sid}:(RX)"])
    return {
        "path": str(parent),
        "rights": "(RX)",
        "inheritance": "NONE",
        "output": (completed.stdout + completed.stderr).strip(),
    }


def _remove_exact_parent(pb: ModuleType, parent: Path, sid: str) -> dict[str, object]:
    completed = pb._run_checked(["icacls", str(parent), "/remove:g", f"*{sid}"])
    return {
        "path": str(parent),
        "exact_sid": sid,
        "output": (completed.stdout + completed.stderr).strip(),
    }


def _short_path(path: Path) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(function(str(path), buffer, len(buffer)))
    if not 0 < length < len(buffer):
        return None
    return buffer.value if os.path.normcase(buffer.value) != os.path.normcase(str(path)) else None


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


def _alias_spellings(workspace: Path, target: Path, junction: Path) -> dict[str, str | None]:
    text = str(target)
    case_different = "".join(char.swapcase() if char.isalpha() else char for char in text)
    return {
        "normal_space_unicode": text,
        "case_different": case_different,
        "extended": "\\\\?\\" + text,
        "short_8dot3": _short_path(target),
        "junction": str(junction),
        "workspace_root": str(workspace),
    }


def _path_api_evidence(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    spellings: dict[str, str | None],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, spelling in spellings.items():
        if name == "workspace_root":
            continue
        if spelling is None:
            result[name] = {"status": "UNAVAILABLE", "reason": "8.3 alias not available"}
            continue
        try:
            result[name] = _matrix_gate(pb, api, profile, bootstrap, probe, spelling)
        except BaseException as error:
            result[name] = {
                "status": "FAIL",
                "detail": {"error_type": type(error).__name__, "error": str(error)},
            }
    required = ("normal_space_unicode", "case_different", "extended", "junction")
    result["overall"] = (
        "PASS"
        if all(cast(dict[str, object], result[name]).get("status") == "PASS" for name in required)
        else "FAIL"
    )
    return result


def _parent_authority_control(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    parent: Path,
    workspace: Path,
    sibling: Path,
) -> dict[str, object]:
    before_matrix = _matrix_gate(pb, api, profile, bootstrap, probe, str(workspace))
    before_visibility = _parent_visibility_gate(
        pb, api, profile, bootstrap, probe, workspace, parent, sibling
    )
    grant = _grant_exact_parent(pb, parent, profile.sid_text)
    try:
        after_matrix = _matrix_gate(pb, api, profile, bootstrap, probe, str(workspace))
        after_visibility = _parent_visibility_gate(
            pb, api, profile, bootstrap, probe, workspace, parent, sibling
        )
    finally:
        cleanup = _remove_exact_parent(pb, parent, profile.sid_text)
    after_parsed = cast(
        dict[str, object], cast(dict[str, object], after_visibility["detail"])["parsed_result"]
    )
    return {
        "before": {"matrix": before_matrix, "visibility": before_visibility},
        "exact_parent_grant": grant,
        "after": {"matrix": after_matrix, "visibility": after_visibility},
        "cleanup": cleanup,
        "confidentiality_expansion": bool(
            after_parsed.get("enumerate_parent") is True
            or after_parsed.get("sibling_name_visible") is True
            or after_parsed.get("sibling_content_read") is True
        ),
    }


def _normal_git_regression(git: Path, root: Path) -> dict[str, object]:
    cases: dict[str, object] = {}
    for name in ("normal", "space path", "Unicode 路径"):
        directory = root / name
        directory.mkdir(parents=True)
        init = _run([str(git), "init", "."], cwd=directory)
        status = _run([str(git), "status", "--short"], cwd=directory)
        rev_parse = _run([str(git), "rev-parse", "--show-toplevel"], cwd=directory)
        cases[name] = {
            "init": init,
            "status": status,
            "rev_parse": rev_parse,
            "passed": init["exit_code"] == 0
            and status["exit_code"] == 0
            and rev_parse["exit_code"] == 0,
        }
    target = root / "junction-target"
    alias = root / "junction-alias"
    target.mkdir()
    junction = _junction(alias, target)
    init = _run([str(git), "init", "."], cwd=target)
    status = _run([str(git), "status", "--short"], cwd=alias if alias.exists() else target)
    rev_parse = _run(
        [str(git), "rev-parse", "--show-toplevel"], cwd=alias if alias.exists() else target
    )
    cases["junction"] = {
        "junction": junction,
        "init": init,
        "status": status,
        "rev_parse": rev_parse,
        "passed": junction["status"] == "PASS"
        and init["exit_code"] == 0
        and status["exit_code"] == 0
        and rev_parse["exit_code"] == 0,
    }
    version = _run([str(git), "--version"])
    return {
        "status": "PASS"
        if version["exit_code"] == 0
        and all(cast(dict[str, object], item)["passed"] is True for item in cases.values())
        else "FAIL",
        "version": version,
        "cases": cases,
    }


def _candidate_smoke(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    runtime: Path,
    workspace: Path,
    name: str,
    environment: dict[str, str],
) -> dict[str, object]:
    repo = workspace / f"candidate-{name.lower()}"
    repo.mkdir()
    launcher = pb._Launcher(api, profile, repo, environment)
    git = runtime / "git.exe"
    version = _command(
        pb, api, launcher, bootstrap, [str(git), "--version"], stdout_contains=("git version",)
    )
    init = _command(pb, api, launcher, bootstrap, [str(git), "init", "."])
    status = _command(pb, api, launcher, bootstrap, [str(git), "status", "--short"])
    passed = all(gate["status"] == "PASS" for gate in (version, init, status))
    return {
        "status": "PASS" if passed else "FAIL",
        "version": version,
        "init": init,
        "status_command": status,
        "repo": str(repo),
    }


def _original_git_nul_gate(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    original_git: Path,
    cwd: Path,
) -> dict[str, object]:
    launcher = pb._Launcher(api, profile, cwd, None)
    gate = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(original_git), "--version"],
        exit_code=None,
    )
    detail = cast(dict[str, object], gate["detail"])
    stderr = str(detail.get("stderr_text", ""))
    expected = bool(
        detail.get("target_exit_code") == 128
        and "/dev/null" in stderr
        and "Permission denied" in stderr
    )
    gate["status"] = "EXPECTED_NUL_BLOCKER" if expected else "FAIL"
    return gate


def _missing_stdio_regression(probe: Path, git: Path, workspace: Path) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for descriptor in range(3):
        report = workspace / f"closed-fd-{descriptor}.json"
        completed = subprocess.run(
            [str(probe), "closed-stdio", str(descriptor), str(git), str(report)],
            capture_output=True,
            text=True,
            check=False,
        )
        parsed: dict[str, object] = {}
        if report.exists():
            with contextlib.suppress(json.JSONDecodeError):
                parsed = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
        cases.append(
            {
                "closed_fd": descriptor,
                "wrapper_exit": completed.returncode,
                "child": parsed,
                "passed": completed.returncode == 0 and parsed.get("child_exit") == 0,
            }
        )
    return _status(all(bool(case["passed"]) for case in cases), cases)


def _full_git_gates(
    pb: ModuleType,
    api: Any,
    profile: Any,
    bootstrap: Path,
    probe: Path,
    runtime: Path,
    repo: Path,
    environment: dict[str, str],
    private_home: Path,
    host_config: Path,
    host_marker: str,
    denied_paths: dict[str, Path],
    expected_sid: str,
    *,
    include_descendant: bool,
) -> dict[str, object]:
    repo.mkdir(exist_ok=True)
    launcher = pb._Launcher(api, profile, repo, environment)
    git = runtime / "git.exe"
    gates: dict[str, object] = {}
    gates["path_matrix"] = _matrix_gate(pb, api, profile, bootstrap, probe, str(repo))
    gates["version"] = _command(
        pb, api, launcher, bootstrap, [str(git), "--version"], stdout_contains=("git version",)
    )
    gates["init"] = _command(pb, api, launcher, bootstrap, [str(git), "init", "."])
    tracked = repo / "tracked.txt"
    tracked.write_text("POC2D_TRACKED\n", encoding="utf-8")
    gates["add"] = _command(pb, api, launcher, bootstrap, [str(git), "add", "tracked.txt"])
    gates["status"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(git), "status", "--short"],
        stdout_contains=("A  tracked.txt",),
    )
    gates["rev_parse"] = _command(
        pb, api, launcher, bootstrap, [str(git), "rev-parse", "--show-toplevel"]
    )
    rev_detail = cast(dict[str, object], cast(dict[str, object], gates["rev_parse"])["detail"])
    rev_path = str(rev_detail.get("stdout_text", "")).strip()
    rev_detail["controller_expected_identity"] = str(repo.resolve(strict=True))
    rev_detail["returned_path"] = rev_path
    returned_windows_path = rev_path.replace("/", "\\")
    expected_windows_path = str(repo.resolve(strict=True))
    identity_matches = os.path.normcase(returned_windows_path) == os.path.normcase(
        expected_windows_path
    )
    rev_detail["controller_identity_matches"] = identity_matches
    if not identity_matches:
        cast(dict[str, object], gates["rev_parse"])["status"] = "FAIL"
    gates["private_config_write"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(git), "config", "--global", "neuro.private", "SANDBOX_PRIVATE"],
    )
    gates["private_config_read"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(git), "config", "--global", "--get", "neuro.private"],
        stdout_contains=("SANDBOX_PRIVATE",),
    )
    host_default = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(git), "config", "--global", "--get", "neuro.host-marker"],
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
        launcher,
        bootstrap,
        [str(git), "config", "--file", str(host_config), "--get", "neuro.host-marker"],
        exit_code=None,
    )
    host_explicit_detail = cast(dict[str, object], host_explicit["detail"])
    host_stderr = str(host_explicit_detail.get("stderr_text", ""))
    host_explicit["status"] = (
        "PASS"
        if host_explicit_detail.get("target_exit_code") != 0
        and host_marker not in str(host_explicit_detail.get("stdout_text", ""))
        and host_config.name in host_stderr
        else "FAIL"
    )
    gates["host_config_denied"] = host_explicit
    denied: dict[str, object] = {}
    for name, path in denied_paths.items():
        gate = _command(
            pb,
            api,
            launcher,
            bootstrap,
            [str(git), "hash-object", str(path)],
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
    required = [
        "path_matrix",
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
        report = repo / "descendant.json"
        hook = repo / ".git" / "hooks" / "pre-commit"
        shutil.copy2(probe, hook)
        launcher.environment["NEURO_DESCENDANT_REPORT"] = str(report)
        for key, value in (
            ("user.name", "Neuro Evidence"),
            ("user.email", "evidence@example.invalid"),
        ):
            _command(pb, api, launcher, bootstrap, [str(git), "config", key, value])
        commit = _command(pb, api, launcher, bootstrap, [str(git), "commit", "-m", "evidence"])
        descendant: dict[str, object] = {}
        if report.exists():
            with contextlib.suppress(json.JSONDecodeError):
                descendant = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
        detail = cast(dict[str, object], commit["detail"])
        detail["descendant_facts"] = descendant
        commit["status"] = (
            "PASS"
            if detail.get("target_exit_code") == 0
            and descendant.get("is_appcontainer") is True
            and descendant.get("in_job") is True
            and descendant.get("integrity_rid") == 4096
            and descendant.get("package_sid") == expected_sid
            else "FAIL"
        )
        gates["descendant_confinement"] = commit
        required.append("descendant_confinement")
    gates["overall"] = (
        "PASS"
        if all(cast(dict[str, object], gates[name])["status"] == "PASS" for name in required)
        else "FAIL"
    )
    gates["private_home"] = str(private_home)
    return gates


def _standard_user_child(
    args: argparse.Namespace, pb: ModuleType
) -> tuple[dict[str, object], bool]:
    api = pb._WinApi()
    token = pb._token_facts(api)
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-git-path-poc2d-standard-")).resolve(
        strict=True
    )
    helper = fixture / "trusted-helper"
    runtime = fixture / "candidate-runtime"
    workspace = fixture / "workspace"
    private_home = fixture / "private-home"
    outside = fixture / "outside"
    controller = fixture / "controller-state"
    for path in (helper, runtime, workspace, private_home, outside, controller):
        path.mkdir(parents=True)
    bootstrap = helper / "runtime-bootstrap.exe"
    probe = helper / "git-path-probe.exe"
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    shutil.copytree(Path(args.selected_runtime), runtime, dirs_exist_ok=True)
    outside_secret = outside / "outside.txt"
    controller_secret = controller / "controller.txt"
    outside_secret.write_text("STANDARD_OUTSIDE", encoding="utf-8")
    controller_secret.write_text("STANDARD_CONTROLLER", encoding="utf-8")
    host_config = Path.home() / ".gitconfig"
    host_sentinel = Path.home() / "poc2d-host-sentinel.txt"
    marker = f"HOST_{uuid.uuid4().hex}"
    original_config = host_config.read_bytes() if host_config.exists() else None
    host_config.write_text(f"[neuro]\n\thost-marker = {marker}\n", encoding="utf-8")
    host_sentinel.write_text("STANDARD_HOST_SENTINEL", encoding="utf-8")
    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "mode": "REAL_STANDARD_USER_LOGON",
        "token": token,
        "candidate": args.selected_candidate,
    }
    profile = None
    roots: list[Any] = []
    cleanup: list[object] = []
    critical: list[str] = []
    try:
        token_ok = bool(
            token.get("administrator_group_enabled") is False
            and token.get("elevated") is False
            and token.get("integrity_level") == "MEDIUM"
        )
        profile = pb._Profile(api, f"NeuroCode.Poc2D.Standard.{uuid.uuid4().hex}")
        roots = pb._deduplicate_roots(
            [
                pb._authority_root(api, helper, "RX", "evidence helper", "USER_INSTALL"),
                pb._authority_root(api, runtime, "RX", "candidate Git runtime", "USER_INSTALL"),
                pb._authority_root(api, workspace, "RW", "workspace", "WORKSPACE_RUNTIME"),
                pb._authority_root(api, private_home, "RW", "private HOME", "USER_INSTALL"),
            ]
        )
        result["authority_grants"] = [pb._grant_root(root, profile.sid_text) for root in roots]
        environment = pb._private_environment(private_home, [runtime])
        result["git"] = _full_git_gates(
            pb,
            api,
            profile,
            bootstrap,
            probe,
            runtime,
            workspace / "repo",
            environment,
            private_home,
            host_config,
            marker,
            {
                "outside": outside_secret,
                "controller": controller_secret,
                "host_userprofile": host_sentinel,
            },
            profile.sid_text,
            include_descendant=False,
        )
        critical.extend(
            name
            for name, passed in {
                "standard_token": token_ok,
                "standard_git": cast(dict[str, object], result["git"])["overall"] == "PASS",
            }.items()
            if not passed
        )
    except BaseException as error:
        result["harness_error"] = {"type": type(error).__name__, "message": str(error)}
        critical.append("STANDARD_USER_HARNESS")
    finally:
        if profile is not None:
            for root in reversed(roots):
                with contextlib.suppress(BaseException):
                    cleanup.append(pb._cleanup_root(root, profile.sid_text))
            result["profile_delete_hresult"] = profile.close()
        result["authority_cleanup"] = cleanup
        with contextlib.suppress(BaseException):
            shutil.rmtree(fixture)
        host_sentinel.unlink(missing_ok=True)
        if original_config is None:
            host_config.unlink(missing_ok=True)
        else:
            host_config.write_bytes(original_config)
    result["critical_failures"] = sorted(set(critical))
    result["overall"] = "PASS" if not critical else "FAIL"
    Path(args.report).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result, not critical


def _create_standard_user(
    args: argparse.Namespace,
    pb: ModuleType,
    api: Any,
    selected_runtime: Path,
    selected_candidate: str,
) -> dict[str, object]:
    username = f"neuro_poc2d_{uuid.uuid4().hex[:8]}"
    password = f"N4!{uuid.uuid4().hex[:8]}aA"
    public = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / f"NeuroPoc2D-{uuid.uuid4().hex}"
    public.mkdir(parents=True)
    script = public / Path(__file__).name
    poc2b = public / "windows_appcontainer_runtime_poc2b.py"
    bootstrap = public / "runtime-bootstrap.exe"
    probe = public / "git-path-probe.exe"
    runtime = public / "candidate-runtime"
    report = public / "standard-user-report.json"
    shutil.copy2(Path(__file__), script)
    shutil.copy2(Path(args.poc2b), poc2b)
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    shutil.copytree(selected_runtime, runtime)
    created = False
    process = None
    try:
        pb._run_checked(["net", "user", username, password, "/add", "/expires:never"])
        created = True
        pb._run_checked(["icacls", str(public), "/grant", f"{username}:(OI)(CI)(M)"])
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
                    "--selected-runtime",
                    str(runtime),
                    "--selected-candidate",
                    selected_candidate,
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
            "created_local_user": True,
            "real_logon_process": True,
            "exit_code": exit_code,
            "report": child,
        }
    except BaseException as error:
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
        with contextlib.suppress(BaseException):
            shutil.rmtree(public)


def _main_run(args: argparse.Namespace, pb: ModuleType) -> tuple[dict[str, object], bool]:
    api = pb._WinApi()
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-git-path-poc2d-")).resolve(strict=True)
    helper = fixture / "trusted-helper"
    workspace = fixture / "workspace-root"
    target = workspace / "Git Path 空间"
    junction = workspace / "junction-alias"
    private_home = fixture / "private-home"
    outside = fixture / "outside"
    controller = fixture / "controller-state"
    control_parent = fixture / "parent-control"
    control_workspace = control_parent / "workspace"
    sibling_dir = control_parent / "sibling-private"
    for path in (
        helper,
        target,
        private_home,
        outside,
        controller,
        control_workspace,
        sibling_dir,
    ):
        path.mkdir(parents=True)
    bootstrap = helper / "runtime-bootstrap.exe"
    probe = helper / "git-path-probe.exe"
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    junction_result = _junction(junction, target)
    outside_secret = outside / "outside-secret.txt"
    controller_secret = controller / "controller-state.txt"
    sibling_secret = sibling_dir / "sibling-secret.txt"
    host_sentinel = Path.home() / "poc2d-host-sentinel.txt"
    for path, value in (
        (outside_secret, "OUTSIDE_SECRET"),
        (controller_secret, "CONTROLLER_SECRET"),
        (sibling_secret, "SIBLING_SECRET"),
        (host_sentinel, "HOST_SENTINEL"),
    ):
        path.write_text(value, encoding="utf-8")
    host_config = Path.home() / ".gitconfig"
    host_marker = f"HOST_{uuid.uuid4().hex}"
    original_config = host_config.read_bytes() if host_config.exists() else None
    host_config.write_text(f"[neuro]\n\thost-marker = {host_marker}\n", encoding="utf-8")
    runtimes = {
        "A_OPENED": Path(args.candidate_a).resolve(strict=True),
        "B_FILENAMEINFO": Path(args.candidate_b).resolve(strict=True),
        "C_CURRENT_DIRECTORY_CONTROL": Path(args.candidate_c).resolve(strict=True),
    }
    original_git = Path(args.original_git).resolve(strict=True)
    original_root = original_git.parent.parent
    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "poc2b_harness_head": POC2B_HEAD,
        "poc2c_reference_head": POC2C_HEAD,
        "runner": {
            "platform": platform.platform(),
            "release": platform.release(),
            "version": platform.version(),
            "python": sys.version,
        },
        "git_source": {
            "tag": args.git_tag,
            "commit": args.git_source_commit,
            "patches": {
                "NUL": Path(args.nul_patch).read_text(encoding="utf-8"),
                "A_OPENED": Path(args.patch_a).read_text(encoding="utf-8"),
                "B_FILENAMEINFO": Path(args.patch_b).read_text(encoding="utf-8"),
                "C_CURRENT_DIRECTORY_CONTROL": Path(args.patch_c).read_text(encoding="utf-8"),
            },
        },
        "junction_creation": junction_result,
    }
    profile = None
    roots: list[Any] = []
    cleanup: list[object] = []
    critical: list[str] = []
    try:
        profile = pb._Profile(api, f"NeuroCode.Poc2D.{uuid.uuid4().hex}")
        root_specs = [
            pb._authority_root(api, helper, "RX", "evidence helpers", "USER_INSTALL"),
            pb._authority_root(api, workspace, "RW", "workspace", "WORKSPACE_RUNTIME"),
            pb._authority_root(
                api, control_workspace, "RW", "control workspace", "WORKSPACE_RUNTIME"
            ),
            pb._authority_root(api, private_home, "RW", "private HOME", "USER_INSTALL"),
            pb._authority_root(api, original_root, "RX", "original Git runtime", "MACHINE_INSTALL"),
        ]
        for name, runtime in runtimes.items():
            root_specs.append(
                pb._authority_root(api, runtime, "RX", f"candidate {name}", "USER_INSTALL")
            )
        roots = pb._deduplicate_roots(root_specs)
        result["authority_grants"] = [pb._grant_root(root, profile.sid_text) for root in roots]
        spellings = _alias_spellings(workspace, target, junction)
        result["alias_spellings"] = spellings
        result["api_matrix"] = _path_api_evidence(pb, api, profile, bootstrap, probe, spellings)
        result["parent_authority_control"] = _parent_authority_control(
            pb,
            api,
            profile,
            bootstrap,
            probe,
            control_parent,
            control_workspace,
            sibling_secret,
        )
        result["original_git_nul"] = _original_git_nul_gate(
            pb, api, profile, bootstrap, original_git, target
        )
        normal_regressions: dict[str, object] = {}
        candidate_smokes: dict[str, object] = {}
        for name in CANDIDATE_ORDER:
            runtime = runtimes[name]
            normal_regressions[name] = _normal_git_regression(
                runtime / "git.exe", fixture / f"normal-regression-{name.lower()}"
            )
            environment = pb._private_environment(private_home, [runtime])
            candidate_smokes[name] = _candidate_smoke(
                pb,
                api,
                profile,
                bootstrap,
                runtime,
                target,
                name,
                environment,
            )
        result["normal_windows_regressions"] = normal_regressions
        result["candidate_appcontainer_smokes"] = candidate_smokes
        normal_matrix = cast(
            dict[str, object],
            cast(
                dict[str, object],
                cast(dict[str, object], result["api_matrix"])["normal_space_unicode"],
            )["detail"],
        )
        parsed_paths = cast(dict[str, object], normal_matrix.get("parsed_result", {})).get(
            "paths", {}
        )
        paths = cast(dict[str, object], parsed_paths)
        candidate_eligible = {
            "A_OPENED": cast(dict[str, object], paths.get("opened_dos", {})).get("success") is True,
            "B_FILENAMEINFO": cast(dict[str, object], paths.get("file_name_info", {})).get(
                "success"
            )
            is True,
            "C_CURRENT_DIRECTORY_CONTROL": False,
        }
        selected = next(
            (
                name
                for name in ("A_OPENED", "B_FILENAMEINFO")
                if candidate_eligible[name]
                and cast(dict[str, object], normal_regressions[name])["status"] == "PASS"
                and cast(dict[str, object], candidate_smokes[name])["status"] == "PASS"
            ),
            None,
        )
        result["candidate_eligibility"] = candidate_eligible
        result["selected_candidate"] = selected
        if selected is not None:
            selected_runtime = runtimes[selected]
            environment = pb._private_environment(private_home, [selected_runtime])
            result["selected_git"] = _full_git_gates(
                pb,
                api,
                profile,
                bootstrap,
                probe,
                selected_runtime,
                target / "selected-repo",
                environment,
                private_home,
                host_config,
                host_marker,
                {
                    "outside": outside_secret,
                    "controller": controller_secret,
                    "host_userprofile": host_sentinel,
                },
                profile.sid_text,
                include_descendant=True,
            )
            result["missing_stdio_regression"] = _missing_stdio_regression(
                probe, selected_runtime / "git.exe", target
            )
            result["standard_user"] = _create_standard_user(
                args, pb, api, selected_runtime, selected
            )
        else:
            result["selected_git"] = {"overall": "NOT_RUN", "reason": "no safe candidate"}
            result["missing_stdio_regression"] = {"status": "NOT_RUN"}
            result["standard_user"] = {"status": "NOT_RUN"}
        parent = cast(dict[str, object], result["parent_authority_control"])
        parent_before = cast(dict[str, object], parent["before"])
        parent_after = cast(dict[str, object], parent["after"])
        before_paths = cast(
            dict[str, object],
            cast(
                dict[str, object],
                cast(dict[str, object], parent_before["matrix"])["detail"],
            ).get("parsed_result", {}),
        ).get("paths", {})
        after_paths = cast(
            dict[str, object],
            cast(
                dict[str, object],
                cast(dict[str, object], parent_after["matrix"])["detail"],
            ).get("parsed_result", {}),
        ).get("paths", {})
        selected_git = cast(dict[str, object], result["selected_git"])
        standard = cast(dict[str, object], result["standard_user"])
        required = {
            "api_alias_matrix": cast(dict[str, object], result["api_matrix"])["overall"] == "PASS",
            "original_git_nul": cast(dict[str, object], result["original_git_nul"])["status"]
            == "EXPECTED_NUL_BLOCKER",
            "selected_candidate": selected is not None,
            "selected_git": selected_git.get("overall") == "PASS",
            "missing_stdio": cast(dict[str, object], result["missing_stdio_regression"]).get(
                "status"
            )
            == "PASS",
            "standard_user": standard.get("status") == "PASS",
        }
        critical.extend(name for name, passed in required.items() if not passed)
        before_normalized = cast(dict[str, object], before_paths).get("normalized_dos")
        after_normalized = cast(dict[str, object], after_paths).get("normalized_dos")
        parent_only = bool(
            selected is None
            and isinstance(before_normalized, dict)
            and before_normalized.get("success") is False
            and isinstance(after_normalized, dict)
            and after_normalized.get("success") is True
        )
        result["parent_only_candidate"] = parent_only
    except BaseException as error:
        result["harness_error"] = {"type": type(error).__name__, "message": str(error)}
        critical.append("HARNESS")
    finally:
        if profile is not None:
            for root in reversed(roots):
                with contextlib.suppress(BaseException):
                    cleanup.append(pb._cleanup_root(root, profile.sid_text))
            result["profile_delete_hresult"] = profile.close()
        result["authority_cleanup"] = cleanup
        with contextlib.suppress(BaseException):
            shutil.rmtree(fixture)
        host_sentinel.unlink(missing_ok=True)
        if original_config is None:
            host_config.unlink(missing_ok=True)
        else:
            host_config.write_bytes(original_config)
    result["critical_failures"] = sorted(set(critical))
    selected_git = cast(dict[str, object], result.get("selected_git", {}))
    standard = cast(dict[str, object], result.get("standard_user", {}))
    if not critical:
        decision = "GIT_WINDOWS_APPCONTAINER_COMPAT_FIX_VIABLE"
    elif result.get("parent_only_candidate") is True:
        decision = "GIT_WINDOWS_PARENT_AUTHORITY_REQUIRED"
    elif selected_git.get("overall") == "FAIL" or standard.get("status") == "FAIL":
        decision = "WINDOWS_GIT_PATH_ARCHITECTURE_BLOCKED"
    else:
        decision = "WINDOWS_GIT_PATH_INCONCLUSIVE"
    result["architecture_decision"] = decision
    result["overall"] = "PASS" if not critical else "FAIL"
    Path(args.report).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return result, not critical


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poc2b", required=True)
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--standard-user", action="store_true")
    parser.add_argument("--selected-runtime")
    parser.add_argument("--selected-candidate")
    parser.add_argument("--candidate-a")
    parser.add_argument("--candidate-b")
    parser.add_argument("--candidate-c")
    parser.add_argument("--original-git")
    parser.add_argument("--git-tag")
    parser.add_argument("--git-source-commit")
    parser.add_argument("--nul-patch")
    parser.add_argument("--patch-a")
    parser.add_argument("--patch-b")
    parser.add_argument("--patch-c")
    args = parser.parse_args()
    pb = _load_poc2b(Path(args.poc2b))
    if args.standard_user:
        _, passed = _standard_user_child(args, pb)
    else:
        _, passed = _main_run(args, pb)
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as error:
        failure = {
            "classification": CLASSIFICATION,
            "architecture_decision": "WINDOWS_GIT_PATH_INCONCLUSIVE",
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
