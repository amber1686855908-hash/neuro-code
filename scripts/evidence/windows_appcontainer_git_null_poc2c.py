"""Evidence-only Git for Windows / AppContainer NUL compatibility probe."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, cast

POC2B_HEAD = "7ac1b77e6cc0dc1d51ab7bd47fe4f0ac76d71336"
CLASSIFICATION = "EVIDENCE_ONLY_DO_NOT_MERGE"


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


def _json_stdout(command: list[str]) -> dict[str, object]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    detail: dict[str, object] = {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(completed.stdout)
        if isinstance(parsed, dict):
            detail["result"] = parsed
    return detail


def _command(
    pb: ModuleType,
    api: Any,
    launcher: Any,
    bootstrap: Path,
    command: list[str],
    *,
    exit_code: int | None = 0,
    stdout_contains: tuple[str, ...] = (),
    internet: bool = False,
) -> dict[str, object]:
    gate = pb._command_relay_gate(
        api,
        launcher,
        bootstrap,
        command,
        internet=internet,
        timeout_ms=75_000,
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


def _original_git_gate(
    pb: ModuleType,
    api: Any,
    launcher: Any,
    bootstrap: Path,
    original_git: Path,
) -> dict[str, object]:
    gate = _command(pb, api, launcher, bootstrap, [str(original_git), "--version"], exit_code=None)
    detail = cast(dict[str, object], gate["detail"])
    stderr = str(detail.get("stderr_text", ""))
    deterministic = bool(
        detail.get("target_exit_code") == 128
        and "/dev/null" in stderr
        and "Permission denied" in stderr
    )
    gate["status"] = "EXPECTED_BLOCKER" if deterministic else "FAIL"
    detail["deterministic_git_nul_failure"] = deterministic
    return gate


def _nul_matrix_gate(
    pb: ModuleType,
    api: Any,
    launcher: Any,
    bootstrap: Path,
    probe: Path,
) -> dict[str, object]:
    gate = _command(pb, api, launcher, bootstrap, [str(probe), "matrix"])
    parsed = _parse_relay_json(gate)
    detail = cast(dict[str, object], gate["detail"])
    detail["parsed_result"] = parsed
    matrix = cast(list[object], parsed.get("access_matrix", []))
    denied = bool(matrix) and all(
        isinstance(item, dict) and item.get("success") is False and item.get("error") == 5
        for item in matrix
    )
    token = cast(dict[str, object], parsed.get("token", {}))
    mapping = cast(dict[str, object], parsed.get("query_dos_device", {}))
    passed = bool(
        gate["status"] == "PASS"
        and detail.get("target_exit_code") == 0
        and token.get("is_appcontainer") is True
        and token.get("in_job") is True
        and mapping.get("success") is True
        and denied
    )
    gate["status"] = "PASS" if passed else "FAIL"
    return gate


def _missing_stdio_regression(probe: Path, patched_git: Path, workspace: Path) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for descriptor in range(3):
        report = workspace / f"closed-fd-{descriptor}.json"
        completed = subprocess.run(
            [str(probe), "closed-stdio", str(descriptor), str(patched_git), str(report)],
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


def _conpty_original_git(
    pb: ModuleType,
    api: Any,
    launcher: Any,
    original_git: Path,
) -> dict[str, object]:
    input_read = input_write = output_read = output_write = 0
    pseudoconsole = ctypes.c_void_p()
    process = None
    try:
        input_read, input_write = pb._make_anonymous_pipe(api, parent_reads=False)
        output_read, output_write = pb._make_anonymous_pipe(api, parent_reads=True)
        result = int(
            api.create_pseudoconsole(
                pb._Coord(80, 24), input_read, output_write, 0, ctypes.byref(pseudoconsole)
            )
        )
        if result != 0 or not pseudoconsole.value:
            raise OSError(result & 0xFFFFFFFF, "CreatePseudoConsole failed")
        with pb._EvidenceJob.create(api) as job:
            process = launcher.spawn_appcontainer(
                original_git,
                ["--version"],
                job_handle=job.process_creation_handle,
                pseudoconsole=int(pseudoconsole.value),
            )
            api.close(process.thread_handle)
            process.thread_handle = 0
            exact_job = pb._is_in_job(api, process.process_handle, job.process_creation_handle)
            exit_code = pb._wait_exit(api, process)
            api.close_pseudoconsole(pseudoconsole)
            pseudoconsole = ctypes.c_void_p()
            api.close(output_write)
            output_write = 0
            output = pb._read_all(api, output_read).decode("utf-8", errors="replace")
        expected = exit_code == 128 and "/dev/null" in output and exact_job
        return {
            "status": "EXPECTED_BLOCKER" if expected else "FAIL",
            "detail": {"exit_code": exit_code, "output": output, "exact_job": exact_job},
        }
    except BaseException as error:
        return _status(False, {"error_type": type(error).__name__, "error": str(error)})
    finally:
        if pseudoconsole.value:
            api.close_pseudoconsole(pseudoconsole)
        for handle in (input_read, input_write, output_read, output_write):
            api.close(handle)
        pb._close_process(api, process)


def _git_local_gates(
    pb: ModuleType,
    api: Any,
    launcher: Any,
    bootstrap: Path,
    patched_git: Path,
    probe: Path,
    workspace: Path,
    private_home: Path,
    outside_secret: Path,
    controller_secret: Path,
    host_config: Path,
    *,
    include_descendant: bool = True,
    include_network: bool = True,
) -> dict[str, object]:
    repo = workspace / "repo"
    gates: dict[str, object] = {}
    gates["version"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(patched_git), "--version"],
        stdout_contains=("git version",),
    )
    gates["init"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(patched_git), "init", str(repo)],
        stdout_contains=("Initialized empty Git repository",),
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("PATCHED_GIT_WORKSPACE\n", encoding="utf-8")
    gates["add"] = _command(
        pb, api, launcher, bootstrap, [str(patched_git), "-C", str(repo), "add", "tracked.txt"]
    )
    gates["status"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(patched_git), "-C", str(repo), "status", "--short"],
        stdout_contains=("A  tracked.txt",),
    )
    gates["private_config_write"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(patched_git), "config", "--global", "neuro.marker", "SANDBOX_LOCAL"],
    )
    gates["private_config_read"] = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(patched_git), "config", "--global", "--get", "neuro.marker"],
        stdout_contains=("SANDBOX_LOCAL",),
    )
    host_probe = _command(
        pb,
        api,
        launcher,
        bootstrap,
        [str(patched_git), "config", "--file", str(host_config), "--get", "neuro.marker"],
        exit_code=None,
    )
    host_detail = cast(dict[str, object], host_probe["detail"])
    host_probe["status"] = (
        "PASS"
        if host_detail.get("target_exit_code") != 0
        and "HOST_CONFIG_SENTINEL" not in str(host_detail.get("stdout_text", ""))
        else "FAIL"
    )
    gates["host_config_denied"] = host_probe

    denied: dict[str, object] = {}
    for name, path in {
        "outside_secret": outside_secret,
        "controller_state": controller_secret,
    }.items():
        gate = _command(
            pb,
            api,
            launcher,
            bootstrap,
            [str(patched_git), "hash-object", str(path)],
            exit_code=None,
        )
        detail = cast(dict[str, object], gate["detail"])
        gate["status"] = "PASS" if detail.get("target_exit_code") != 0 else "FAIL"
        denied[name] = gate
    gates["outside_denied"] = {
        "status": "PASS"
        if all(cast(dict[str, object], value)["status"] == "PASS" for value in denied.values())
        else "FAIL",
        "detail": denied,
    }

    required = [
        "version",
        "init",
        "add",
        "status",
        "private_config_write",
        "private_config_read",
        "host_config_denied",
        "outside_denied",
    ]
    if include_descendant:
        descendant_report = workspace / "descendant.json"
        descendant_hook = repo / ".git" / "hooks" / "pre-commit"
        shutil.copy2(probe, descendant_hook)
        os.environ.pop("NEURO_DESCENDANT_REPORT", None)
        launcher.environment["NEURO_DESCENDANT_REPORT"] = str(descendant_report)
        for key, value in (
            ("user.name", "Neuro Evidence"),
            ("user.email", "evidence@example.invalid"),
        ):
            _command(
                pb,
                api,
                launcher,
                bootstrap,
                [str(patched_git), "-C", str(repo), "config", key, value],
            )
        commit = _command(
            pb,
            api,
            launcher,
            bootstrap,
            [str(patched_git), "-C", str(repo), "commit", "-m", "evidence"],
        )
        descendant: dict[str, object] = {}
        if descendant_report.exists():
            with contextlib.suppress(json.JSONDecodeError):
                descendant = cast(
                    dict[str, object],
                    json.loads(descendant_report.read_text(encoding="utf-8")),
                )
        commit_detail = cast(dict[str, object], commit["detail"])
        commit["status"] = (
            "PASS"
            if commit_detail.get("target_exit_code") == 0
            and descendant.get("is_appcontainer") is True
            and descendant.get("in_job") is True
            else "FAIL"
        )
        commit_detail["descendant_facts"] = descendant
        gates["descendant_confinement"] = commit
        required.append("descendant_confinement")

    if include_network:
        no_network = _command(
            pb,
            api,
            launcher,
            bootstrap,
            [str(patched_git), "ls-remote", "git://github.com/git/git.git", "HEAD"],
            exit_code=None,
        )
        no_detail = cast(dict[str, object], no_network["detail"])
        no_network["status"] = "PASS" if no_detail.get("target_exit_code") != 0 else "FAIL"
        internet = _command(
            pb,
            api,
            launcher,
            bootstrap,
            [str(patched_git), "ls-remote", "git://github.com/git/git.git", "HEAD"],
            exit_code=None,
            internet=True,
        )
        gates["network"] = {
            "status": "PASS" if no_network["status"] == "PASS" else "FAIL",
            "detail": {"strict": no_network, "internet_client_observation": internet},
        }
        required.append("network")
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
    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "mode": "REAL_STANDARD_USER_LOGON",
        "token": token,
        "poc2b_harness_head": POC2B_HEAD,
    }
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-git-null-poc2c-standard-"))
    helper = fixture / "trusted-helper"
    runtime = fixture / "patched-git-runtime"
    workspace = fixture / "workspace"
    private_home = fixture / "private-home"
    outside = fixture / "outside"
    controller = fixture / "controller-state"
    for path in (helper, runtime, workspace, private_home, outside, controller):
        path.mkdir(parents=True)
    bootstrap = helper / "runtime-bootstrap.exe"
    probe = helper / "git-null-probe.exe"
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    shutil.copytree(Path(args.patched_runtime), runtime, dirs_exist_ok=True)
    patched_git = runtime / "git.exe"
    outside_secret = outside / "outside.txt"
    controller_secret = controller / "controller.txt"
    host_config = outside / ".gitconfig"
    outside_secret.write_text("STANDARD_OUTSIDE_SECRET", encoding="utf-8")
    controller_secret.write_text("STANDARD_CONTROLLER_SECRET", encoding="utf-8")
    host_config.write_text("[neuro]\n\tmarker = HOST_CONFIG_SENTINEL\n", encoding="utf-8")
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
        result["token_gate"] = _status(token_ok, token)
        profile = pb._Profile(api, f"NeuroCode.Poc2C.Standard.{uuid.uuid4().hex}")
        roots = pb._deduplicate_roots(
            [
                pb._authority_root(api, helper, "RX", "evidence helper", "USER_INSTALL"),
                pb._authority_root(api, runtime, "RX", "patched Git runtime", "USER_INSTALL"),
                pb._authority_root(api, workspace, "RW", "workspace", "WORKSPACE_RUNTIME"),
                pb._authority_root(api, private_home, "RW", "private HOME", "USER_INSTALL"),
            ]
        )
        result["authority_grants"] = [pb._grant_root(root, profile.sid_text) for root in roots]
        environment = pb._private_environment(private_home, [runtime])
        launcher = pb._Launcher(api, profile, workspace, environment)
        define = _json_stdout([str(probe), "define"])
        result["define_dos_device"] = define
        git_gates = _git_local_gates(
            pb,
            api,
            launcher,
            bootstrap,
            patched_git,
            probe,
            workspace,
            private_home,
            outside_secret,
            controller_secret,
            host_config,
            include_descendant=False,
            include_network=False,
        )
        result["git"] = git_gates
        critical.extend(
            name
            for name, passed in {
                "standard_token": token_ok,
                "patched_git": git_gates["overall"] == "PASS",
                "define_dos_device_control": cast(dict[str, object], define.get("result", {})).get(
                    "created"
                )
                is False,
            }.items()
            if not passed
        )
        result["profile_created_by_standard_user"] = True
        result["named_pipe_created_by_standard_user"] = True
        result["job_created_by_standard_user"] = True
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
    staging: Path,
) -> dict[str, object]:
    username = f"neuro_poc2c_{uuid.uuid4().hex[:8]}"
    password = f"N3!{uuid.uuid4().hex[:8]}aA"
    public = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / f"NeuroPoc2C-{uuid.uuid4().hex}"
    public.mkdir(parents=True)
    script = public / Path(__file__).name
    poc2b = public / "windows_appcontainer_runtime_poc2b.py"
    bootstrap = public / "runtime-bootstrap.exe"
    probe = public / "git-null-probe.exe"
    runtime = public / "patched-git-runtime"
    report = public / "standard-user-report.json"
    shutil.copy2(Path(__file__), script)
    shutil.copy2(Path(args.poc2b), poc2b)
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    shutil.copytree(Path(args.patched_runtime), runtime)
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
                    "--patched-runtime",
                    str(runtime),
                    "--report",
                    str(report),
                ]
            )
        )
        startup = pb._StartupInfoW()
        startup.cb = ctypes.sizeof(startup)
        info = pb._ProcessInformation()
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        system_drive = os.environ.get("SYSTEMDRIVE", "C:").rstrip("\\/")
        profile = Path(f"{system_drive}\\") / "Users" / username
        environment = {
            "APPDATA": str(profile / "AppData" / "Roaming"),
            "COMSPEC": str(system_root / "System32" / "cmd.exe"),
            "HOME": str(profile),
            "LOCALAPPDATA": str(profile / "AppData" / "Local"),
            "PATH": os.pathsep.join([str(python.parent), str(system_root / "System32")]),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "SYSTEMDRIVE": system_drive,
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
        exit_code = pb._wait_exit(api, process, 240_000)
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
    fixture = Path(tempfile.mkdtemp(prefix="neuro-code-git-null-poc2c-"))
    helper = fixture / "trusted-helper"
    workspace = fixture / "workspace"
    private_home = fixture / "private-home"
    outside = fixture / "outside"
    controller = fixture / "controller-state"
    for path in (helper, workspace, private_home, outside, controller):
        path.mkdir(parents=True)
    bootstrap = helper / "runtime-bootstrap.exe"
    probe = helper / "git-null-probe.exe"
    shutil.copy2(Path(args.bootstrap), bootstrap)
    shutil.copy2(Path(args.probe), probe)
    original_git = Path(args.original_git).resolve(strict=True)
    patched_runtime = Path(args.patched_runtime).resolve(strict=True)
    patched_git = patched_runtime / "git.exe"
    original_root = original_git.parent.parent
    outside_secret = outside / "outside.txt"
    controller_secret = controller / "controller.txt"
    host_config = outside / ".gitconfig"
    outside_secret.write_text("OUTSIDE_SECRET", encoding="utf-8")
    controller_secret.write_text("CONTROLLER_SECRET", encoding="utf-8")
    host_config.write_text("[neuro]\n\tmarker = HOST_CONFIG_SENTINEL\n", encoding="utf-8")
    result: dict[str, object] = {
        "classification": CLASSIFICATION,
        "poc2b_harness_head": POC2B_HEAD,
        "runner": {
            "platform": platform.platform(),
            "release": platform.release(),
            "version": platform.version(),
            "python": sys.version,
        },
        "git_source": {
            "tag": args.git_tag,
            "commit": args.git_source_commit,
            "patch": Path(args.patch).read_text(encoding="utf-8"),
        },
    }
    controller_probe = _json_stdout([str(probe), "matrix"])
    result["controller_nul"] = controller_probe
    result["installed_git"] = {
        "version": subprocess.run(
            [str(original_git), "--version", "--build-options"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout,
        "resolved_executable": str(original_git),
        "runtime_root": str(original_root),
    }
    result["missing_stdio_regression"] = _missing_stdio_regression(probe, patched_git, workspace)
    profile = None
    roots: list[Any] = []
    cleanup: list[object] = []
    critical: list[str] = []
    try:
        profile = pb._Profile(api, f"NeuroCode.Poc2C.{uuid.uuid4().hex}")
        roots = pb._deduplicate_roots(
            [
                pb._authority_root(api, helper, "RX", "evidence helpers", "USER_INSTALL"),
                pb._authority_root(
                    api, patched_runtime, "RX", "patched Git runtime", "USER_INSTALL"
                ),
                pb._authority_root(
                    api, original_root, "RX", "Git for Windows runtime", "MACHINE_INSTALL"
                ),
                pb._authority_root(api, workspace, "RW", "workspace", "WORKSPACE_RUNTIME"),
                pb._authority_root(api, private_home, "RW", "private HOME", "USER_INSTALL"),
            ]
        )
        result["authority_grants"] = [pb._grant_root(root, profile.sid_text) for root in roots]
        environment = pb._private_environment(private_home, [patched_runtime, original_git.parent])
        launcher = pb._Launcher(api, profile, workspace, environment)
        result["appcontainer_nul"] = _nul_matrix_gate(pb, api, launcher, bootstrap, probe)
        result["original_git"] = _original_git_gate(pb, api, launcher, bootstrap, original_git)
        result["original_git_conpty"] = _conpty_original_git(pb, api, launcher, original_git)
        result["patched_git"] = _git_local_gates(
            pb,
            api,
            launcher,
            bootstrap,
            patched_git,
            probe,
            workspace,
            private_home,
            outside_secret,
            controller_secret,
            host_config,
        )
        result["standard_user"] = _create_standard_user(args, pb, api, fixture)
        controller_matrix = cast(dict[str, object], controller_probe.get("result", {}))
        appcontainer_gate = cast(dict[str, object], result["appcontainer_nul"])
        appcontainer_matrix = cast(
            dict[str, object],
            cast(dict[str, object], appcontainer_gate["detail"]).get("parsed_result", {}),
        )
        sddl = str(
            cast(dict[str, object], controller_matrix.get("security_descriptor", {})).get("sddl")
            or ""
        )
        app_errors = {
            cast(int, cast(dict[str, object], item).get("error", -1))
            for item in cast(list[object], appcontainer_matrix.get("access_matrix", []))
            if isinstance(item, dict)
        }
        result["denial_classification"] = {
            "classification": "UNRESOLVED",
            "basis": {
                "resolved_device": cast(
                    dict[str, object], controller_matrix.get("query_dos_device", {})
                ),
                "controller_sddl": sddl or None,
                "ace_sid_tokens": sorted(set(re.findall(r"S-\d(?:-\d+)+|;;;[A-Z]+", sddl))),
                "appcontainer_errors": sorted(app_errors),
                "reason": (
                    "Documented Win32 observations do not isolate DACL, mandatory integrity, "
                    "and device-namespace policy without mutating the NUL device."
                ),
            },
        }
        required = {
            "appcontainer_nul_matrix": appcontainer_gate["status"] == "PASS",
            "original_git_deterministic": cast(dict[str, object], result["original_git"])["status"]
            == "EXPECTED_BLOCKER",
            "original_git_conpty": cast(dict[str, object], result["original_git_conpty"])["status"]
            == "EXPECTED_BLOCKER",
            "patched_git": cast(dict[str, object], result["patched_git"])["overall"] == "PASS",
            "missing_stdio": cast(dict[str, object], result["missing_stdio_regression"])["status"]
            == "PASS",
            "standard_user": cast(dict[str, object], result["standard_user"])["status"] == "PASS",
        }
        critical.extend(name for name, passed in required.items() if not passed)
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
        result["authority_roots"] = [pb._runtime_root_record(root) for root in roots]
        result["authority_scale"] = {
            "root_count": len(roots),
            "object_count": sum(int(root.inventory["object_count"]) for root in roots),
            "file_bytes": sum(int(root.inventory["total_file_bytes"]) for root in roots),
            "acl_mutations": len(roots),
        }
        with contextlib.suppress(BaseException):
            shutil.rmtree(fixture)
    result["documented_nul_capability"] = "NO_DOCUMENTED_NUL_CAPABILITY"
    result["windows11_win32_app_isolation"] = "FUTURE_PREVIEW_CANDIDATE_NOT_TESTED"
    result["critical_failures"] = sorted(set(critical))
    if not critical:
        decision = "GIT_FOR_WINDOWS_UPSTREAM_FIX_REQUIRED"
    elif "patched_git" in critical:
        decision = "WINDOWS_GIT_RUNTIME_DEEPER_BLOCKER"
    else:
        decision = "WINDOWS_GIT_NUL_INCONCLUSIVE"
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
    parser.add_argument("--patched-runtime", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--original-git")
    parser.add_argument("--git-tag", default="")
    parser.add_argument("--git-source-commit", default="")
    parser.add_argument("--patch", default="")
    parser.add_argument("--standard-user", action="store_true")
    args = parser.parse_args()
    pb = _load_poc2b(Path(args.poc2b).resolve(strict=True))
    try:
        _, passed = _standard_user_child(args, pb) if args.standard_user else _main_run(args, pb)
    except BaseException as error:
        failure = {
            "classification": CLASSIFICATION,
            "overall": "FAIL",
            "critical_failures": ["UNCAUGHT_HARNESS_ERROR"],
            "harness_error": {"type": type(error).__name__, "message": str(error)},
        }
        Path(args.report).write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(failure, indent=2))
        passed = False
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
