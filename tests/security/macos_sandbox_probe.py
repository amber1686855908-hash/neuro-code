"""Evidence-only probes for the macOS child sandbox boundary.

This module is intentionally independent from ``LocalProcessSandbox``.  It
generates fixed Seatbelt policies and records observable operating-system
behaviour for the PR-M0 research workflow; it is not a production adapter.

本模块仅用于 macOS 子进程沙箱的证据探测.
它刻意独立于 ``LocalProcessSandbox``:生成固定的 Seatbelt 策略并记录操作系统
行为,不会成为正式生产适配器.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import pty
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

CAPABILITY_FAILURE: Final[int] = 2
INFRASTRUCTURE_FAILURE: Final[int] = 3


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Capture one bounded command result for the evidence report.

    记录一个有界命令结果,供证据报告使用.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    pid: int | None = None
    signal_number: int | None = None
    argv: tuple[str, ...] = ()
    target_exec_state: str = "unknown"


class ProbeInfrastructureError(RuntimeError):
    """The probe itself could not be executed reliably.

    表示探针本身无法可靠执行,而不是被测能力不存在.
    """


class ProbeHarness:
    """Build temporary fixtures and execute fixed Seatbelt probes.

    构建临时 fixture 并执行固定 Seatbelt 探针.
    """

    _SYSTEM_READ_ROOTS: Final[tuple[Path, ...]] = (
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/private/etc"),
        Path("/dev"),
    )

    def __init__(self, sandbox_exec: Path) -> None:
        self.sandbox_exec = sandbox_exec
        self.root = Path(tempfile.mkdtemp(prefix="neuro-code-macos-probe-"))
        self.workspace = self.root / "workspace Ω with spaces"
        self.state_dir = self.root / "controller state"
        self.host_home = self.root / "host home"
        self.additional_root = self.root / "additional read-only root"
        self.synthetic_home = self.root / "synthetic home"
        self.private_tmp = self.root / "private tmp"
        for path in (
            self.workspace,
            self.state_dir,
            self.host_home,
            self.additional_root,
            self.synthetic_home,
            self.private_tmp,
        ):
            path.mkdir(parents=True)
        self._write_fixture(self.state_dir / "credentials.json", "controller-secret")
        self._write_fixture(self.state_dir / "sessions.db", "session-secret")
        self._write_fixture(self.host_home / "host-home-sentinel", "host-home-secret")
        self._write_fixture(self.additional_root / "read-only.txt", "additional-root")
        self._write_fixture(self.workspace / "readable.txt", "workspace-readable")
        # The real runner home is probed separately.  A unique sentinel keeps
        # the check honest without relying on a guessed host path.
        self.real_host_home_sentinel = (
            Path.home() / f".neuro-code-macos-probe-{os.getpid()}-host-home"
        )
        self._write_fixture(self.real_host_home_sentinel, "real-host-home-secret")
        self.workspace_alias = self._var_alias(self.workspace)
        self.private_tmp_alias = self._var_alias(self.private_tmp)
        self._result_paths: list[Path] = []

    def __enter__(self) -> ProbeHarness:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        with contextlib.suppress(OSError):
            self.real_host_home_sentinel.unlink()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _write_fixture(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _var_alias(path: Path) -> Path:
        canonical_var = Path("/var").resolve(strict=False)
        try:
            relative = path.resolve(strict=False).relative_to(canonical_var)
        except ValueError:
            return path
        return Path("/var") / relative

    @staticmethod
    def _sbpl_literal(path: Path | str) -> str:
        value = str(path)
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _canonical(path: Path) -> Path:
        try:
            return path.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ProbeInfrastructureError(f"cannot canonicalize {path}: {error}") from error

    def _result_path(self, label: str) -> Path:
        path = self.private_tmp / f"{label}-{len(self._result_paths)}.json"
        self._result_paths.append(path)
        return path

    def _minimal_environment(self) -> dict[str, str]:
        return {
            "HOME": str(self._canonical(self.synthetic_home)),
            "TMPDIR": str(self._canonical(self.private_tmp)),
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "NO_COLOR": "1",
        }

    def _policy(
        self,
        *,
        workspace_mode: str,
        network_allowed: bool,
        extra_read_roots: tuple[Path, ...] = (),
        extra_exec_roots: tuple[Path, ...] = (),
    ) -> str:
        if workspace_mode not in {"read-only", "read-write"}:
            raise ValueError(f"unsupported workspace mode {workspace_mode!r}")
        workspace = self._canonical(self.workspace)
        workspace_alias = self.workspace_alias
        synthetic_home = self._canonical(self.synthetic_home)
        private_tmp = self._canonical(self.private_tmp)
        private_tmp_alias = self.private_tmp_alias
        additional = tuple(self._canonical(path) for path in extra_read_roots)
        executable_roots = tuple(self._canonical(path) for path in extra_exec_roots)
        read_roots = self._SYSTEM_READ_ROOTS + additional + executable_roots
        lines = ["(version 1)", "(deny default)", "(allow process-fork)", "(allow process-exec)"]
        lines.append("(allow signal (target self))")
        # macOS's dyld/runtime startup performs a data access on the root
        # directory object itself.  This is deliberately a literal rule: a
        # recursive ``(subpath \"/\")`` read would expose the host filesystem
        # and would invalidate the evidence for controller-state isolation.
        lines.append('(allow file-read-data (literal "/"))')
        lines.append('(allow file-read-metadata (literal "/"))')
        lines.append("(allow sysctl-read)")
        metadata_roots: set[Path] = set()
        for root in read_roots:
            lines.append(f"(allow file-read* (subpath {self._sbpl_literal(root)}))")
            lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(root)}))")
            # Seatbelt path matchers still need metadata access on every
            # ancestor while a launcher resolves its executable/framework.
            # Keep these metadata-only grants bounded to the explicit root;
            # never broaden data access to an ancestor directory.
            metadata_roots.update(root.parents)
        for root in sorted(metadata_roots, key=lambda path: (len(path.parts), str(path))):
            lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(root)}))")
        lines.append(f"(allow file-read* (subpath {self._sbpl_literal(workspace)}))")
        lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(workspace)}))")
        # Seatbelt does not necessarily collapse /var and /private/var in a
        # dynamic matcher.  Grant both canonical spellings explicitly so the
        # evidence covers the same filesystem identity through either alias.
        if workspace_alias != workspace:
            lines.append(f"(allow file-read* (subpath {self._sbpl_literal(workspace_alias)}))")
            lines.append(
                f"(allow file-read-metadata (subpath {self._sbpl_literal(workspace_alias)}))"
            )
            for parent in workspace_alias.parents:
                lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(parent)}))")
                if parent == Path("/var"):
                    break
        lines.append(f"(allow file-read* (subpath {self._sbpl_literal(synthetic_home)}))")
        lines.append(f"(allow file-write* (subpath {self._sbpl_literal(synthetic_home)}))")
        lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(synthetic_home)}))")
        lines.append(f"(allow file-read* (subpath {self._sbpl_literal(private_tmp)}))")
        lines.append(f"(allow file-write* (subpath {self._sbpl_literal(private_tmp)}))")
        lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(private_tmp)}))")
        if private_tmp_alias != private_tmp:
            lines.append(f"(allow file-read* (subpath {self._sbpl_literal(private_tmp_alias)}))")
            lines.append(f"(allow file-write* (subpath {self._sbpl_literal(private_tmp_alias)}))")
            lines.append(
                f"(allow file-read-metadata (subpath {self._sbpl_literal(private_tmp_alias)}))"
            )
            for parent in private_tmp_alias.parents:
                lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(parent)}))")
                if parent == Path("/var"):
                    break
        if workspace_mode == "read-write":
            lines.append(f"(allow file-write* (subpath {self._sbpl_literal(workspace)}))")
            if workspace_alias != workspace:
                lines.append(f"(allow file-write* (subpath {self._sbpl_literal(workspace_alias)}))")
        # Shells and diagnostics commonly use /dev/null for bounded output.
        # Permit only that device node, never recursive writes to /dev.
        lines.append('(allow file-write-data (literal "/dev/null"))')
        if network_allowed:
            lines.append("(allow network-outbound)")
        policy = "\n".join(lines)
        if '(allow file-read* (subpath "/"))' in policy:
            raise ProbeInfrastructureError(
                "macOS evidence policy accidentally grants recursive host-root read"
            )
        if '(allow file-read-data (literal "/"))' not in policy:
            raise ProbeInfrastructureError(
                "macOS evidence policy is missing the verified literal root-data rule"
            )
        return policy

    @staticmethod
    def _subpath_rule(operation: str, path: Path | str) -> str:
        return f"(allow {operation} (subpath {ProbeHarness._sbpl_literal(path)}))"

    @staticmethod
    def _deny_subpath_rule(operation: str, path: Path | str) -> str:
        return f"(deny {operation} (subpath {ProbeHarness._sbpl_literal(path)}))"

    @staticmethod
    def _deny_literal_rule(operation: str, path: Path | str) -> str:
        return f"(deny {operation} (literal {ProbeHarness._sbpl_literal(path)}))"

    def _policy_with_rules(
        self,
        *,
        read_rules: tuple[str, ...] = (),
        write_rules: tuple[str, ...] = (),
        deny_rules: tuple[str, ...] = (),
        network_allowed: bool = False,
    ) -> str:
        """Build a differential-only policy without changing production policy."""
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            "(allow process-exec)",
            "(allow signal (target self))",
            *read_rules,
            *write_rules,
            *deny_rules,
        ]
        if network_allowed:
            lines.append("(allow network-outbound)")
        return "\n".join(lines)

    def _root_cause_profile(self, *, roots: tuple[str, ...] = (), broad_read: bool = False) -> str:
        read_rules = (
            ("(allow file-read*)",)
            if broad_read
            else tuple(self._subpath_rule("file-read*", root) for root in roots)
        )
        return self._policy_with_rules(read_rules=read_rules)

    def _root_cause_matrix(self) -> dict[str, object]:
        """Run only /usr/bin/true from / while narrowing runtime read roots."""
        root_groups: dict[str, tuple[str, ...]] = {
            "system": ("/System", "/usr", "/bin", "/sbin", "/private/etc", "/dev"),
            "private": ("/private",),
            "private_var": ("/private/var",),
            "private_var_db": ("/private/var/db",),
            "private_var_folders": ("/private/var/folders",),
            "library": ("/Library",),
            "system_volumes": ("/System/Volumes",),
            "preboot_cryptex": (
                "/System/Volumes/Preboot",
                "/System/Volumes/Preboot/Cryptexes/OS",
            ),
            "apple_system_library": (
                "/System/Library",
                "/Library/Apple/System/Library",
            ),
        }
        all_roots = tuple(root for group in root_groups.values() for root in group)
        root_data_rule = '(allow file-read-data (literal "/"))'
        root_metadata_rule = '(allow file-read-metadata (literal "/"))'
        old_narrow_roots = root_groups["system"] + ("/private",)
        candidate_read_rules = tuple(self._subpath_rule("file-read*", root) for root in all_roots)
        profiles: dict[str, str] = {
            "A_allow_default": "(version 1) (allow default)",
            "B_broad_file_read": self._root_cause_profile(broad_read=True),
            "deny_default_only": "(version 1) (deny default)",
            "old_narrow_system": self._root_cause_profile(roots=old_narrow_roots),
            "all_candidate_roots": self._root_cause_profile(roots=all_roots),
            "old_narrow_plus_root_data": self._policy_with_rules(
                read_rules=(
                    *(self._subpath_rule("file-read*", root) for root in old_narrow_roots),
                    root_data_rule,
                )
            ),
            "all_candidate_plus_root_data": self._policy_with_rules(
                read_rules=(*candidate_read_rules, root_data_rule)
            ),
            "old_narrow_plus_root_metadata": self._policy_with_rules(
                read_rules=(
                    *(self._subpath_rule("file-read*", root) for root in old_narrow_roots),
                    root_metadata_rule,
                )
            ),
            "root_control": self._root_cause_profile(roots=("/",)),
        }
        for root_name, root_group in root_groups.items():
            for root in root_group:
                profiles[f"single_{root_name}_{root.lstrip('/').replace('/', '_')}"] = (
                    self._root_cause_profile(roots=(root,))
                )
        prefix_roots: list[str] = []
        for index, root in enumerate(all_roots, start=1):
            prefix_roots.append(root)
            profiles[f"prefix_{index:02d}_{root.lstrip('/').replace('/', '_')}"] = (
                self._root_cause_profile(roots=tuple(prefix_roots))
            )
        for group_name, group_roots in root_groups.items():
            remaining = tuple(root for root in all_roots if root not in group_roots)
            profiles[f"all_without_{group_name}"] = self._root_cause_profile(roots=remaining)

        matrix: dict[str, object] = {}
        for name, policy in profiles.items():
            command_result = _run_process(
                [str(self.sandbox_exec), "-p", policy, "/usr/bin/true"],
                cwd="/",
                timeout=5,
            )
            evidence = {"policy": policy, **_command_evidence(command_result)}
            if command_result.signal_number is not None:
                evidence["process_diagnostics"] = _collect_process_diagnostics(command_result.pid)
            matrix[name] = evidence
        return {
            "target": "/usr/bin/true",
            "cwd": "/",
            "profiles": matrix,
            "root_groups": root_groups,
            "runtime_dependency_candidates": [
                "file-read-data literal /",
                "sysctl-read (denials are recorded separately)",
            ],
            "probe_strategy": {
                "single_roots": True,
                "incremental_prefixes": True,
                "negative_process_logs": True,
            },
            "interpretation": (
                "A profile with returncode 0 proves only that /usr/bin/true starts; "
                "a negative signal is recorded without attributing it to sandbox-exec "
                "or the target process. Per-process diagnostics are collected for "
                "negative probes when the runner exposes them."
            ),
        }

    def _targeted_deny_policy(self, *, workspace_mode: str, deny_form: str) -> str:
        workspace = self._canonical(self.workspace)
        private_tmp = self._canonical(self.private_tmp)
        synthetic_home = self._canonical(self.synthetic_home)
        state_dir = self._canonical(self.state_dir)
        host_home = self._canonical(self.host_home)
        state_file = self._canonical(self.state_dir / "credentials.json")
        read_rules = ("(allow file-read*)",)
        write_rules = (
            self._subpath_rule("file-write*", workspace),
            self._subpath_rule("file-write*", private_tmp),
            self._subpath_rule("file-write*", synthetic_home),
        )
        if workspace_mode == "read-only":
            write_rules = tuple(rule for rule in write_rules if str(workspace) not in rule)
        if deny_form == "subpath":
            deny_rules = (
                self._deny_subpath_rule("file-read*", state_dir),
                self._deny_subpath_rule("file-write*", state_dir),
                self._deny_subpath_rule("file-read*", host_home),
            )
        elif deny_form == "literal":
            deny_rules = (
                self._deny_literal_rule("file-read*", state_file),
                self._deny_literal_rule("file-write*", state_file),
                self._deny_subpath_rule("file-read*", host_home),
            )
        else:
            raise ValueError(f"unsupported deny form {deny_form!r}")
        deny_rules += (self._deny_literal_rule("file-read*", self.real_host_home_sentinel),)
        return self._policy_with_rules(
            read_rules=read_rules,
            write_rules=write_rules,
            deny_rules=deny_rules,
        )

    def _dynamic_path_probe(self) -> dict[str, object]:
        """Test dynamic path predicates after the known-good broad-read baseline."""
        result: dict[str, object] = {}
        state = shlex.quote(str(self._canonical(self.state_dir / "credentials.json")))
        state_alias = shlex.quote(str(self._var_alias(self.state_dir / "credentials.json")))
        host_home = shlex.quote(str(self._canonical(self.host_home / "host-home-sentinel")))
        real_host_home = shlex.quote(str(self.real_host_home_sentinel))
        workspace_file = shlex.quote(str(self._canonical(self.workspace / "unicode-空 格")))
        workspace_alias_file = shlex.quote(str(self.workspace_alias / "alias-unicode-空 格"))
        state_write_file = shlex.quote(str(self._canonical(self.state_dir / "credentials.json")))
        home_file = shlex.quote(str(self._canonical(self.synthetic_home / "home-write")))
        tmp_file = shlex.quote(str(self._canonical(self.private_tmp / "tmp-write")))
        result_path = self._result_path("dynamic-path")
        result_file = shlex.quote(str(self._canonical(result_path)))
        command = textwrap.dedent(
            f"""
            state_read=0; state_alias_read=0; host_read=0; real_home_read=0
            workspace_write=0; alias_workspace_write=0; state_write=0
            home_write=0; tmp_write=0
            if cat {state} >/dev/null 2>&1; then state_read=1; fi
            if cat {state_alias} >/dev/null 2>&1; then state_alias_read=1; fi
            if cat {host_home} >/dev/null 2>&1; then host_read=1; fi
            if cat {real_host_home} >/dev/null 2>&1; then real_home_read=1; fi
            if printf x > {workspace_file}; then workspace_write=1; fi
            if printf x > {workspace_alias_file}; then alias_workspace_write=1; fi
            if printf x > {state_write_file}; then state_write=1; fi
            if printf x > {home_file}; then home_write=1; fi
            if printf x > {tmp_file}; then tmp_write=1; fi
            printf '{{"state_read":%s,"state_alias_read":%s,"host_read":%s,"real_home_read":%s,"workspace_write":%s,"alias_workspace_write":%s,"state_write":%s,"home_write":%s,"tmp_write":%s}}' \\
              "$state_read" "$state_alias_read" "$host_read" "$real_home_read" "$workspace_write" \\
              "$alias_workspace_write" "$state_write" "$home_write" "$tmp_write" > {result_file}
            """
        ).strip()
        for workspace_mode in ("read-write", "read-only"):
            for deny_form in ("subpath", "literal"):
                policy = self._targeted_deny_policy(
                    workspace_mode=workspace_mode, deny_form=deny_form
                )
                command_result = self._run_sandboxed(policy, ["/bin/sh", "-c", command])
                try:
                    data = _read_json_result(result_path)
                except (ProbeInfrastructureError, ValueError):
                    data = {"result_available": False}
                result[f"{workspace_mode}_{deny_form}"] = {
                    "workspace_mode": workspace_mode,
                    "deny_form": deny_form,
                    "policy": policy,
                    **_command_evidence(command_result),
                    "data": data,
                }
                self._write_fixture(self.state_dir / "credentials.json", "controller-secret")
        return result

    def _profile_file_comparison(self, policy: str) -> dict[str, object]:
        """Compare -p and -f with byte-identical policy text."""
        profile_fd, profile_name = tempfile.mkstemp(
            prefix="neuro-code-seatbelt-root-cause-", suffix=".sb", dir=self.private_tmp
        )
        os.close(profile_fd)
        profile_path = Path(profile_name)
        try:
            profile_path.write_text(policy, encoding="utf-8")
            inline = self._run_sandboxed(policy, ["/usr/bin/true"])
            file_result = _run_process(
                [str(self.sandbox_exec), "-f", str(profile_path), "/usr/bin/true"], timeout=5
            )
            return {
                "policy_sha256": _sha256_text(policy),
                "profile_path": str(profile_path),
                "inline_p": _command_evidence(inline),
                "file_f": _command_evidence(file_result),
            }
        finally:
            with contextlib.suppress(OSError):
                profile_path.unlink()

    def root_cause(self) -> dict[str, object]:
        matrix = self._root_cause_matrix()
        profiles = matrix["profiles"]
        broad_policy = profiles["B_broad_file_read"]["policy"]  # type: ignore[index]
        return {
            "target": "/usr/bin/true",
            "differential_profiles": matrix,
            "dynamic_path": self._dynamic_path_probe(),
            "profile_invocation": self._profile_file_comparison(broad_policy),
            "os_diagnostics": _collect_os_diagnostics(),
        }

    def _run_sandboxed(
        self,
        policy: str,
        command: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: float = 15.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        child_env = self._minimal_environment()
        if env is not None:
            child_env.update(env)
        argv = [str(self.sandbox_exec), "-p", policy, *command]
        try:
            result = _run_process(
                argv,
                cwd=str(self._canonical(cwd or self.workspace)),
                env=child_env,
                input_text=input_text,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                returncode=124,
                stdout=_decode_output(error.stdout),
                stderr=_decode_output(error.stderr),
                timed_out=True,
                argv=tuple(argv),
            )
        except OSError as error:
            raise ProbeInfrastructureError(f"sandbox-exec failed to start: {error}") from error
        return result

    def _shell_probe(self, profile: str) -> dict[str, object]:
        result_path = self._result_path(f"filesystem-{profile}")
        workspace = shlex.quote(str(self._canonical(self.workspace)))
        workspace_alias = shlex.quote(str(self.workspace_alias))
        state = shlex.quote(str(self._canonical(self.state_dir / "credentials.json")))
        host_home = shlex.quote(str(self._canonical(self.host_home / "host-home-sentinel")))
        real_host_home = shlex.quote(str(self.real_host_home_sentinel))
        additional = shlex.quote(str(self._canonical(self.additional_root / "read-only.txt")))
        additional_write = shlex.quote(str(self._canonical(self.additional_root / "write-attempt")))
        result = shlex.quote(str(self._canonical(result_path)))
        private_tmp_alias = shlex.quote(str(self.private_tmp_alias / "alias-write"))
        command = textwrap.dedent(
            f"""
            set +e
            workspace_write=0
            workspace_alias_write=0
            workspace_read=0
            outside_read=0
            outside_write=0
            host_home_read=0
            real_host_home_read=0
            additional_read=0
            additional_write=0
            symlink_read=0
            hardlink_read=0
            hardlink_write=0
            dotdot_read=0
            home_write=0
            tmp_write=0
            alias_tmp_write=0
            forbidden_env=0
            if printf 'agent-write' > {workspace}/agent-write; then workspace_write=1; fi
            if printf 'alias-write' > {workspace_alias}/alias-write; then workspace_alias_write=1; fi
            if cat {workspace}/readable.txt >/dev/null 2>&1; then workspace_read=1; fi
            if cat {state} >/dev/null 2>&1; then outside_read=1; fi
            if printf 'outside-write' > {state}; then outside_write=1; fi
            if cat {host_home} >/dev/null 2>&1; then host_home_read=1; fi
            if cat {real_host_home} >/dev/null 2>&1; then real_host_home_read=1; fi
            if cat {additional} >/dev/null 2>&1; then additional_read=1; fi
            if printf 'additional-write' > {additional_write}; then additional_write=1; fi
            if cat {workspace}/symlink-to-state >/dev/null 2>&1; then symlink_read=1; fi
            if cat {workspace}/hardlink-to-state >/dev/null 2>&1; then hardlink_read=1; fi
            if printf 'hardlink-write' > {workspace}/hardlink-to-state; then hardlink_write=1; fi
            if cat {workspace}/../'controller state'/credentials.json >/dev/null 2>&1; then dotdot_read=1; fi
            if printf 'home-write' > "$HOME/home-write"; then home_write=1; fi
            if printf 'tmp-write' > "$TMPDIR/tmp-write"; then tmp_write=1; fi
            if printf 'alias-tmp-write' > {private_tmp_alias}; then alias_tmp_write=1; fi
            if env | /usr/bin/grep -E '(^|_)(API_KEY|TOKEN|SECRET|PASSWORD|PROXY|USERPROFILE|APPDATA|XDG_)' >/dev/null 2>&1; then forbidden_env=1; fi
            printf '{{"workspace_write":%s,"workspace_alias_write":%s,"workspace_read":%s,"outside_read":%s,"outside_write":%s,"host_home_read":%s,"real_host_home_read":%s,"additional_read":%s,"additional_write":%s,"symlink_read":%s,"hardlink_read":%s,"hardlink_write":%s,"dotdot_read":%s,"home_write":%s,"tmp_write":%s,"alias_tmp_write":%s,"forbidden_env":%s}}' \\
              "$workspace_write" "$workspace_alias_write" "$workspace_read" "$outside_read" "$outside_write" \\
              "$host_home_read" "$real_host_home_read" "$additional_read" "$additional_write" "$symlink_read" "$hardlink_read" \\
              "$hardlink_write" "$dotdot_read" "$home_write" "$tmp_write" "$alias_tmp_write" "$forbidden_env" > {result}
            """
        ).strip()
        command_result = self._run_sandboxed(
            policy := self._policy(
                workspace_mode="read-only" if profile == "read-only" else "read-write",
                network_allowed=profile == "workspace",
                extra_read_roots=(self.additional_root,),
            ),
            ["/bin/sh", "-c", command],
        )
        policy_assertions = {
            "literal_root_data_rule": '(allow file-read-data (literal "/"))' in policy,
            "recursive_root_read_absent": '(allow file-read* (subpath "/"))' not in policy,
        }
        try:
            data = _read_json_result(result_path)
        except ProbeInfrastructureError as error:
            # Keep the command evidence when Seatbelt denies even the result
            # path; evaluation will classify the missing capability explicitly.
            return {
                "result_available": False,
                "result_error": str(error),
                "returncode": command_result.returncode,
                "timed_out": command_result.timed_out,
                "stdout": command_result.stdout[-2_000:],
                "stderr": command_result.stderr[-2_000:],
                "policy": policy,
                "policy_assertions": policy_assertions,
            }
        # Restore the controller sentinel if a policy allowed a hardlink write;
        # the result remains evidence of the attempted escape.
        self._write_fixture(self.state_dir / "credentials.json", "controller-secret")
        data["returncode"] = command_result.returncode
        data["stderr"] = command_result.stderr[-2_000:]
        data["policy_assertions"] = policy_assertions
        return data

    def filesystem(self) -> dict[str, object]:
        symlink = self.workspace / "symlink-to-state"
        hardlink = self.workspace / "hardlink-to-state"
        with contextlib.suppress(FileNotFoundError, FileExistsError):
            symlink.unlink()
        symlink.symlink_to(self.state_dir / "credentials.json")
        with contextlib.suppress(FileNotFoundError, FileExistsError):
            (self.workspace / "hardlink-to-state").unlink()
        hardlink = self.workspace / "hardlink-to-state"
        hardlink_supported = True
        try:
            os.link(self.state_dir / "credentials.json", hardlink)
        except OSError as error:
            hardlink_supported = False
            hardlink_error = str(error)
        result: dict[str, object] = {
            "workspace": str(self.workspace),
            "workspace_canonical": str(self._canonical(self.workspace)),
            "workspace_var_alias": str(self.workspace_alias),
            "private_tmp": str(self.private_tmp),
            "private_tmp_var_alias": str(self.private_tmp_alias),
            "hardlink_created": hardlink_supported,
            "hardlink_target": str(self.state_dir / "credentials.json"),
        }
        if not hardlink_supported:
            result["hardlink_creation_error"] = hardlink_error
        for profile in ("workspace", "read-only", "strict"):
            result[profile] = self._shell_probe(profile)
        return result

    def network(self, listener_port: int) -> dict[str, object]:
        nc = shutil.which("nc", path="/usr/bin:/bin")
        if nc is None:
            raise ProbeInfrastructureError("macOS netcat (/usr/bin/nc or /bin/nc) is unavailable")
        result: dict[str, object] = {"nc": nc, "listener_port": listener_port}
        for profile in ("workspace", "read-only", "strict"):
            policy = self._policy(
                workspace_mode="read-only" if profile == "read-only" else "read-write",
                network_allowed=profile == "workspace",
            )
            direct = self._run_sandboxed(
                policy,
                [nc, "-z", "-w", "1", "127.0.0.1", str(listener_port)],
            )
            grandchild = self._run_sandboxed(
                policy,
                [
                    "/bin/sh",
                    "-c",
                    f"/bin/sh -c {shlex.quote(f'{shlex.quote(nc)} -z -w 1 127.0.0.1 {listener_port}')}",
                ],
            )
            dns = self._run_sandboxed(
                policy,
                ["/usr/bin/dscacheutil", "-q", "host", "-a", "name", "localhost"],
            )
            result[profile] = {
                "direct_tcp_returncode": direct.returncode,
                "direct_tcp_allowed": direct.returncode == 0,
                "grandchild_tcp_returncode": grandchild.returncode,
                "grandchild_tcp_allowed": grandchild.returncode == 0,
                "dns_returncode": dns.returncode,
                "dns_allowed": dns.returncode == 0,
                "stderr": (direct.stderr + grandchild.stderr + dns.stderr)[-2_000:],
            }
        return result

    def python_subprocess(self, listener_port: int) -> dict[str, object]:
        executable = self._canonical(Path(os.sys.executable))
        roots = self._python_runtime_roots(executable)
        result_path = self._result_path("python-subprocess")
        code = (
            "import json,pathlib,socket;"
            f"p=pathlib.Path({str(self._canonical(result_path))!r});"
            f"outside=pathlib.Path({str(self._canonical(self.state_dir / 'credentials.json'))!r});"
            "read_ok=False;"
            "\ntry: outside.read_text(); read_ok=True\nexcept OSError: pass;"
            "network_ok=False;"
            f"\ntry: socket.create_connection(('127.0.0.1',{listener_port}), timeout=1).close(); network_ok=True\nexcept OSError: pass;"
            "\np.write_text(json.dumps({'outside_read':read_ok,'network':network_ok}))"
        )
        policy = self._policy(
            workspace_mode="read-write",
            network_allowed=True,
            extra_read_roots=roots,
            extra_exec_roots=(executable.parent,),
        )
        command_result = self._run_sandboxed(
            policy,
            [str(executable), "-I", "-c", code],
            env={"PYTHONNOUSERSITE": "1"},
        )
        try:
            data = _read_json_result(result_path)
        except ProbeInfrastructureError as error:
            data = {"result_available": False, "result_error": str(error)}
        data["returncode"] = command_result.returncode
        data["stderr"] = command_result.stderr[-2_000:]
        data["target_exec_state"] = command_result.target_exec_state
        data["executable"] = str(executable)
        data["runtime_roots"] = [str(root) for root in roots]
        return data

    def strict_runtime(self) -> dict[str, object]:
        """Probe STRICT's narrow runtime roots without broadening them."""
        executable = self._canonical(Path(os.sys.executable))
        runtime_roots = self._python_runtime_roots(executable)
        probes: dict[str, object] = {}
        true_result = self._run_sandboxed(
            self._policy(workspace_mode="read-write", network_allowed=False),
            ["/usr/bin/true"],
            cwd=Path("/"),
        )
        probes["true"] = _command_evidence(true_result)
        shell_result = self._run_sandboxed(
            self._policy(workspace_mode="read-write", network_allowed=False),
            ["/bin/sh", "-c", "printf 'strict-shell-ok\\n'"],
        )
        probes["shell"] = _command_evidence(shell_result)
        python_result = self._run_sandboxed(
            self._policy(
                workspace_mode="read-write",
                network_allowed=False,
                extra_read_roots=runtime_roots,
                extra_exec_roots=(executable.parent,),
            ),
            [str(executable), "-I", "-c", "print('strict-python-ok')"],
            env={"PYTHONNOUSERSITE": "1"},
        )
        probes["python"] = {
            **_command_evidence(python_result),
            "executable": str(executable),
            "runtime_roots": [str(root) for root in runtime_roots],
        }
        return {
            "policy_contract": {
                "workspace_mode": "read-write",
                "network_allowed": False,
                "recursive_root_read": False,
                "literal_root_data": True,
            },
            "probes": probes,
        }

    def _python_runtime_roots(self, executable: Path) -> tuple[Path, ...]:
        """Return bounded interpreter roots, including launcher parent traversal."""
        roots: list[Path] = [
            self._canonical(Path(os.sys.prefix)),
            self._canonical(Path(os.sys.base_prefix)),
            executable.parent,
            executable.parent.parent,
        ]
        current = executable.parent
        # macOS Python launchers call realpath() through their framework/homebrew
        # parents.  Grant only the known runtime families, never the host root.
        while current != current.parent and str(current) not in {
            "/Library",
            "/opt/homebrew",
            "/opt",
        }:
            current = current.parent
            if current in {Path("/Library"), Path("/opt/homebrew"), Path("/opt")}:
                roots.append(current)
                break
        return tuple(dict.fromkeys(roots))

    def mcp_stdio(self, listener_port: int) -> dict[str, object]:
        nc = shutil.which("nc", path="/usr/bin:/bin")
        if nc is None:
            raise ProbeInfrastructureError("macOS netcat is unavailable for MCP-like probe")
        result: dict[str, object] = {}
        for profile in ("workspace", "read-only", "strict"):
            result_path = self._result_path(f"mcp-{profile}")
            policy = self._policy(
                workspace_mode="read-only" if profile == "read-only" else "read-write",
                network_allowed=profile == "workspace",
            )
            state = shlex.quote(str(self._canonical(self.state_dir / "credentials.json")))
            workspace = shlex.quote(str(self._canonical(self.workspace)))
            result_file = shlex.quote(str(self._canonical(result_path)))
            grandchild_command = shlex.quote(
                f"if cat {state} >/dev/null 2>&1; then printf '1 '; else printf '0 '; fi; "
                f"if printf x > {workspace}/mcp-grandchild-write; then printf '1 '; else printf '0 '; fi; "
                f"if {shlex.quote(nc)} -z -w 1 127.0.0.1 {listener_port} >/dev/null 2>&1; then printf 1; else printf 0; fi"
            )
            command = (
                "IFS= read -r request; "
                "fs=0; net=0; write=0; "
                "grandchild='000'; "
                f"if cat {state} >/dev/null 2>&1; then fs=1; fi; "
                f"if printf x > {workspace}/mcp-write; then write=1; fi; "
                f"if {shlex.quote(nc)} -z -w 1 127.0.0.1 {listener_port} >/dev/null 2>&1; then net=1; fi; "
                f"grandchild=$(/bin/sh -c {grandchild_command}); "
                f"set -- $grandchild; "
                f'printf \'{{"request":"%s","outside_read":%s,"workspace_write":%s,"network":%s,"grandchild_outside_read":%s,"grandchild_workspace_write":%s,"grandchild_network":%s}}\\n\' "$request" "$fs" "$write" "$net" "$1" "$2" "$3" > {result_file}; '
                "printf 'pong\\n'"
            )
            completed = self._run_sandboxed(
                policy,
                ["/bin/sh", "-c", command],
                input_text="ping\n",
            )
            try:
                data = _read_json_result(result_path)
            except ProbeInfrastructureError as error:
                data = {"result_available": False, "result_error": str(error)}
            result[profile] = {
                "returncode": completed.returncode,
                "protocol_pong": "pong" in completed.stdout,
                "stderr": completed.stderr[-2_000:],
                **data,
            }
        return result

    def access_inheritance(self, helper: Path, listener_port: int) -> dict[str, object]:
        result: dict[str, object] = {}
        for profile in ("workspace", "read-only", "strict"):
            result_path = self._result_path(f"access-{profile}")
            ready = self._result_path(f"access-ready-{profile}")
            release = self._result_path(f"access-release-{profile}")
            leaked = self._result_path(f"access-leaked-{profile}")
            policy = self._policy(
                workspace_mode="read-only" if profile == "read-only" else "read-write",
                network_allowed=profile == "workspace",
            )
            command_result = self._run_sandboxed(
                policy,
                [
                    str(self._canonical(helper)),
                    str(ready),
                    str(release),
                    str(leaked),
                    str(self._canonical(self.state_dir / "credentials.json")),
                    str(self._canonical(result_path)),
                    "access",
                    str(self._canonical(self.workspace)),
                    str(listener_port),
                ],
                timeout=15,
            )
            try:
                data = _read_json_result(result_path)
            except ProbeInfrastructureError as error:
                data = {"result_available": False, "result_error": str(error)}
            result[profile] = {
                "returncode": command_result.returncode,
                **data,
                "stderr": command_result.stderr[-2_000:],
            }
        return result

    def lifecycle(self, helper: Path) -> dict[str, object]:
        result: dict[str, object] = {
            "termination_model": (
                "timeout, cancellation, background shutdown, and explicit termination "
                "use the same process-group boundary in this evidence probe"
            ),
            "scenarios": {},
        }
        actions = ("explicit_terminate", "timeout", "cancellation", "background_shutdown")
        for action in actions:
            for mode in ("ordinary", "setsid"):
                label = f"lifecycle-{action}-{mode}"
                ready = self._result_path(f"{label}-ready")
                release = self._result_path(f"{label}-release")
                leaked = self._result_path(f"{label}-leaked")
                pid_file = self._result_path(f"{label}-pid")
                policy = self._policy(
                    workspace_mode="read-write",
                    network_allowed=True,
                )
                process = self._start_sandboxed_process(
                    policy,
                    [
                        str(self._canonical(helper)),
                        str(self._canonical(ready)),
                        str(self._canonical(release)),
                        str(self._canonical(leaked)),
                        str(self._canonical(self.state_dir / "credentials.json")),
                        str(self._canonical(pid_file)),
                        mode,
                    ],
                )
                ready_seen = _wait_for_path(ready, timeout=5)
                self._terminate_process_group(process)
                # A detached child observes this only after the evidence
                # window.  Do not release it before measuring survival.
                release.write_text("release", encoding="utf-8")
                time.sleep(0.4)
                child_pid = _read_pid(pid_file)
                pid_alive_before_cleanup = child_pid is not None and _pid_alive(child_pid)
                leaked_seen = leaked.exists()
                evidence = {
                    "action": action,
                    "mode": mode,
                    "ready_seen": ready_seen,
                    "leaked_after_group_termination": leaked_seen,
                    "pid_alive_before_cleanup": pid_alive_before_cleanup,
                    "outer_returncode": process.returncode,
                    "evidence_recorded_before_cleanup": True,
                }
                scenario_records = result["scenarios"]
                if isinstance(scenario_records, dict):
                    scenario_records[f"{action}:{mode}"] = evidence
                if action == "explicit_terminate":
                    result[mode] = evidence
                if child_pid is not None and _pid_alive(child_pid):
                    with contextlib.suppress(OSError):
                        os.kill(child_pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    process.wait(timeout=2)
        return result

    def pty(self, helper: Path, listener_port: int) -> dict[str, object]:
        nc = shutil.which("nc", path="/usr/bin:/bin")
        if nc is None:
            raise ProbeInfrastructureError("macOS netcat is unavailable for PTY probe")
        result: dict[str, object] = {}
        for profile in ("workspace", "read-only", "strict"):
            policy = self._policy(
                workspace_mode="read-only" if profile == "read-only" else "read-write",
                network_allowed=profile == "workspace",
            )
            command = (
                "stty rows 24 columns 80; "
                "printf 'READY\\n'; "
                f"if printf x > {shlex.quote(str(self._canonical(self.workspace / 'pty-write')))}; then printf 'WRITE=1\\n'; else printf 'WRITE=0\\n'; fi; "
                f"if cat {shlex.quote(str(self._canonical(self.state_dir / 'credentials.json')))} >/dev/null 2>&1; then printf 'READ=1\\n'; else printf 'READ=0\\n'; fi; "
                f"if {shlex.quote(nc)} -z -w 1 127.0.0.1 {listener_port} >/dev/null 2>&1; then printf 'NET=1\\n'; else printf 'NET=0\\n'; fi; "
                "IFS= read -r input; printf 'INPUT=%s\\n' \"$input\""
            )
            output, process = self._run_pty(policy, command)
            result[profile] = {
                "returncode": process.returncode,
                "ready": "READY" in output,
                "workspace_write": "WRITE=1" in output,
                "outside_read": "READ=1" in output,
                "network": "NET=1" in output,
                "input_roundtrip": "INPUT=pty-input" in output,
                "output_tail": output[-2_000:],
            }
        result["ctrl_c"] = self._pty_ctrl_c_probe()
        result["setsid_lifecycle"] = self._pty_setsid_lifecycle(helper)
        return result

    def _start_sandboxed_process(self, policy: str, command: list[str]) -> subprocess.Popen[bytes]:
        environment = self._minimal_environment()
        try:
            return subprocess.Popen(
                [str(self.sandbox_exec), "-p", policy, *command],
                cwd=self._canonical(self.workspace),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise ProbeInfrastructureError(f"cannot start lifecycle child: {error}") from error

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)

    def _run_pty(self, policy: str, command: str) -> tuple[str, subprocess.Popen[bytes]]:
        master_fd, slave_fd = pty.openpty()

        def set_controlling_tty() -> None:
            os.setsid()
            import fcntl
            import termios

            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        try:
            process = subprocess.Popen(
                [str(self.sandbox_exec), "-p", policy, "/bin/sh", "-c", command],
                cwd=self._canonical(self.workspace),
                env=self._minimal_environment(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=set_controlling_tty,
            )
        finally:
            os.close(slave_fd)
        import fcntl
        import termios

        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, b"\x18\x00\x50\x00\x00\x00\x00\x00")
        os.set_blocking(master_fd, False)
        output = bytearray()
        deadline = time.monotonic() + 10
        ready_sent = False
        while time.monotonic() < deadline and process.poll() is None:
            with contextlib.suppress(BlockingIOError, OSError):
                output.extend(os.read(master_fd, 64_000))
            text = output.decode("utf-8", errors="replace")
            if "READY" in text and not ready_sent:
                os.write(master_fd, b"pty-input\n")
                ready_sent = True
            time.sleep(0.01)
        if process.poll() is None:
            self._terminate_process_group(process)
        with contextlib.suppress(BlockingIOError, OSError):
            output.extend(os.read(master_fd, 64_000))
        os.close(master_fd)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        return output.decode("utf-8", errors="replace"), process

    def _pty_ctrl_c_probe(self) -> dict[str, object]:
        policy = self._policy(workspace_mode="read-write", network_allowed=True)
        master_fd, slave_fd = pty.openpty()

        def set_controlling_tty() -> None:
            os.setsid()
            import fcntl
            import termios

            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        command = "trap 'printf \\\"INTERRUPTED\\n\\\"; exit 130' INT; printf READY\\n; sleep 30"
        try:
            process = subprocess.Popen(
                [str(self.sandbox_exec), "-p", policy, "/bin/sh", "-c", command],
                cwd=self._canonical(self.workspace),
                env=self._minimal_environment(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=set_controlling_tty,
            )
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)
        output = bytearray()
        deadline = time.monotonic() + 5
        sent = False
        while time.monotonic() < deadline and process.poll() is None:
            with contextlib.suppress(BlockingIOError, OSError):
                output.extend(os.read(master_fd, 64_000))
            if b"READY" in output and not sent:
                os.write(master_fd, b"\x03")
                sent = True
            time.sleep(0.01)
        if process.poll() is None:
            self._terminate_process_group(process)
        with contextlib.suppress(BlockingIOError, OSError):
            output.extend(os.read(master_fd, 64_000))
        os.close(master_fd)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        text = output.decode("utf-8", errors="replace")
        return {"ready": "READY" in text, "interrupt_observed": "INTERRUPTED" in text}

    def _pty_setsid_lifecycle(self, helper: Path) -> dict[str, object]:
        ready = self._result_path("pty-lifecycle-ready")
        release = self._result_path("pty-lifecycle-release")
        leaked = self._result_path("pty-lifecycle-leaked")
        pid_file = self._result_path("pty-lifecycle-pid")
        policy = self._policy(
            workspace_mode="read-write",
            network_allowed=True,
        )
        master_fd, slave_fd = pty.openpty()

        def set_controlling_tty() -> None:
            os.setsid()
            import fcntl
            import termios

            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        try:
            process = subprocess.Popen(
                [
                    str(self.sandbox_exec),
                    "-p",
                    policy,
                    str(self._canonical(helper)),
                    str(ready),
                    str(release),
                    str(leaked),
                    str(self._canonical(self.state_dir / "credentials.json")),
                    str(self._canonical(pid_file)),
                    "setsid",
                ],
                cwd=self._canonical(self.workspace),
                env=self._minimal_environment(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=set_controlling_tty,
            )
        finally:
            os.close(slave_fd)
        ready_seen = _wait_for_path(ready, timeout=5)
        self._terminate_process_group(process)
        os.close(master_fd)
        release.write_text("release", encoding="utf-8")
        time.sleep(0.4)
        child_pid = _read_pid(pid_file)
        pid_alive_before_cleanup = child_pid is not None and _pid_alive(child_pid)
        leaked_seen = leaked.exists()
        if child_pid is not None and _pid_alive(child_pid):
            with contextlib.suppress(OSError):
                os.kill(child_pid, signal.SIGKILL)
        return {
            "ready": ready.exists(),
            "ready_seen": ready_seen,
            "leaked_after_pty_close": leaked_seen,
            "pid_alive_before_cleanup": pid_alive_before_cleanup,
            "evidence_recorded_before_cleanup": True,
        }


def _target_exec_state(returncode: int, stderr: str) -> str:
    """Classify whether the target reached exec without guessing on SIGABRT."""
    if returncode == 0:
        return "observed_success"
    if "execvp()" in stderr and "failed" in stderr:
        return "sandbox_exec_denied_before_target"
    if returncode < 0:
        return "unknown_after_signal"
    return "unknown"


def _run_process(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 5.0,
) -> CommandResult:
    """Run one bounded evidence command while retaining process identity facts."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return CommandResult(
            returncode=124,
            stdout=_decode_output(stdout),
            stderr=_decode_output(stderr),
            timed_out=True,
            pid=process.pid,
            argv=tuple(argv),
            target_exec_state="timeout",
        )
    returncode = process.returncode
    signal_number = -returncode if returncode is not None and returncode < 0 else None
    bounded_stdout = _decode_output(stdout)
    bounded_stderr = _decode_output(stderr)
    return CommandResult(
        returncode=returncode if returncode is not None else 125,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
        pid=process.pid,
        signal_number=signal_number,
        argv=tuple(argv),
        target_exec_state=_target_exec_state(
            returncode if returncode is not None else 125, bounded_stderr
        ),
    )


def _command_evidence(result: CommandResult) -> dict[str, object]:
    """Serialize process/target facts without interpreting SIGABRT as exec success."""
    return {
        "argv": list(result.argv),
        "pid": result.pid,
        "returncode": result.returncode,
        "signal_number": result.signal_number,
        "target_exec_state": result.target_exec_state,
        "timed_out": result.timed_out,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _collect_os_diagnostics() -> dict[str, object]:
    """Collect public diagnostics when the runner exposes them."""
    diagnostics: dict[str, object] = {}
    log_command = [
        "/usr/bin/log",
        "show",
        "--last",
        "2m",
        "--style",
        "compact",
        "--predicate",
        '(process == "sandbox-exec") OR (process == "sandboxd") OR '
        '(eventMessage CONTAINS[c] "sandbox")',
    ]
    diagnostics["unified_log"] = _safe_command(log_command)
    report_paths: list[str] = []
    for directory in (
        Path("/Library/Logs/DiagnosticReports"),
        Path.home() / "Library/Logs/DiagnosticReports",
    ):
        try:
            report_paths.extend(
                str(path)
                for path in directory.iterdir()
                if path.is_file() and path.stat().st_mtime >= time.time() - 180
            )
        except OSError:
            continue
    diagnostics["recent_diagnostic_reports"] = sorted(report_paths)
    return diagnostics


def _collect_process_diagnostics(pid: int) -> dict[str, object]:
    """Collect a bounded public unified-log slice for one failed child PID."""
    predicate = f'(processID == {pid}) OR (eventMessage CONTAINS[c] "true({pid})")'
    return _safe_command(
        [
            "/usr/bin/log",
            "show",
            "--last",
            "30s",
            "--style",
            "compact",
            "--predicate",
            predicate,
        ]
    )


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[-2_000:]
    return value[-2_000:]


def _read_json_result(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeInfrastructureError(f"probe result {path} is unavailable: {error}") from error


def _wait_for_path(path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _compile_lifecycle_helper(root: Path) -> Path:
    clang = shutil.which("clang") or shutil.which("cc")
    if clang is None:
        raise ProbeInfrastructureError("clang/cc is unavailable for detached-descendant probe")
    source = root / "lifecycle_helper.c"
    binary = root / "lifecycle_helper"
    source.write_text(
        textwrap.dedent(
            r"""
            #include <fcntl.h>
            #include <signal.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>
            #include <arpa/inet.h>
            #include <netinet/in.h>
            #include <stdint.h>
            #include <sys/socket.h>
            #include <sys/types.h>
            #include <unistd.h>

            static void mark(const char *path, const char *value) {
                int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
                if (fd >= 0) {
                    (void)write(fd, value, strlen(value));
                    close(fd);
                }
            }

            static int exists(const char *path) { return access(path, F_OK) == 0; }

            int main(int argc, char **argv) {
                if (argc < 7 || argc > 9) return 2;
                const char *ready = argv[1];
                const char *release = argv[2];
                const char *leaked = argv[3];
                const char *outside = argv[4];
                const char *aux_path = argv[5];
                const char *mode = argv[6];
                const char *workspace = argc >= 8 ? argv[7] : "";
                int listener_port = argc >= 9 ? atoi(argv[8]) : 0;
                pid_t child = fork();
                if (child < 0) return 3;
                if (child == 0) {
                    int setsid_ok = 1;
                    if (strcmp(mode, "setsid") == 0 || strcmp(mode, "access") == 0) {
                        if (setsid() < 0) setsid_ok = 0;
                    }
                    char pid[32];
                    snprintf(pid, sizeof(pid), "%ld", (long)getpid());
                    if (strcmp(mode, "access") != 0) mark(aux_path, pid);
                    if (strcmp(mode, "access") == 0) {
                        int fd = open(outside, O_RDONLY);
                        int read_ok = fd >= 0;
                        if (fd >= 0) close(fd);
                        fd = open(outside, O_WRONLY | O_APPEND);
                        int write_ok = fd >= 0;
                        if (fd >= 0) close(fd);
                        char workspace_file[1024];
                        snprintf(workspace_file, sizeof(workspace_file), "%s/readable.txt", workspace);
                        fd = open(workspace_file, O_RDONLY);
                        int workspace_read = fd >= 0;
                        if (fd >= 0) close(fd);
                        snprintf(workspace_file, sizeof(workspace_file), "%s/access-write", workspace);
                        fd = open(workspace_file, O_WRONLY | O_CREAT | O_TRUNC, 0600);
                        int workspace_write = fd >= 0;
                        if (fd >= 0) close(fd);
                        int network_ok = 0;
                        if (listener_port > 0) {
                            int sock = socket(AF_INET, SOCK_STREAM, 0);
                            if (sock >= 0) {
                                struct sockaddr_in address;
                                memset(&address, 0, sizeof(address));
                                address.sin_family = AF_INET;
                                address.sin_port = htons((uint16_t)listener_port);
                                inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
                                network_ok = connect(sock, (struct sockaddr *)&address, sizeof(address)) == 0;
                                close(sock);
                            }
                        }
                        char data[192];
                        snprintf(data, sizeof(data), "{\"setsid\":%d,\"outside_read\":%d,\"outside_write\":%d,\"workspace_read\":%d,\"workspace_write\":%d,\"network\":%d}", setsid_ok, read_ok, write_ok, workspace_read, workspace_write, network_ok);
                        mark(aux_path, data);
                        mark(ready, "ready");
                        close(STDIN_FILENO);
                        close(STDOUT_FILENO);
                        close(STDERR_FILENO);
                        _exit(0);
                    }
                    mark(ready, "ready");
                    while (!exists(release)) usleep(10000);
                    mark(leaked, "leaked");
                    _exit(0);
                }
                if (strcmp(mode, "access") == 0) {
                    while (!exists(ready)) usleep(10000);
                    return 0;
                }
                mark(ready, "parent-ready");
                while (!exists(release)) usleep(10000);
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [clang, str(source), "-O2", "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ProbeInfrastructureError(f"cannot compile lifecycle helper: {result.stderr[-2_000:]}")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _start_listener() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 0))
        # The probes intentionally leave the controller-owned listener open
        # while several child/grandchild checks run.  A generous backlog keeps
        # a failed Seatbelt connect distinguishable from a full queue.
        listener.listen(128)
    except OSError:
        listener.close()
        raise
    return listener, int(listener.getsockname()[1])


def _metadata() -> dict[str, object]:
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    info: dict[str, object] = {
        "sys_version": platform.python_version(),
        "sys_executable": os.sys.executable,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "uname": platform.uname()._asdict(),
        "uname_a": _safe_command(["/usr/bin/uname", "-a"]),
        "sw_vers": _safe_command(["/usr/bin/sw_vers"]),
        "sandbox_exec_path": str(sandbox_exec),
        "sandbox_exec_present": sandbox_exec.is_file(),
        "sandbox_exec_executable": os.access(sandbox_exec, os.X_OK),
        "sandbox_exec_mode": None,
        "sandbox_exec_usage": None,
        "sandbox_exec_preflight": None,
        "csrutil_status": None,
        "sandbox_exec_file": None,
        "sandbox_exec_codesign": None,
    }
    if sandbox_exec.exists():
        info["sandbox_exec_mode"] = oct(stat.S_IMODE(sandbox_exec.stat().st_mode))
        info["sandbox_exec_file"] = _safe_command(["/usr/bin/file", str(sandbox_exec)])
        info["sandbox_exec_codesign"] = _safe_command(
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(sandbox_exec)]
        )
        usage = subprocess.run(
            [str(sandbox_exec), "-h"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        info["sandbox_exec_usage"] = {
            "returncode": usage.returncode,
            "stdout": usage.stdout[-2_000:],
            "stderr": usage.stderr[-2_000:],
        }
        preflight_profiles = {
            "allow_default": "(version 1) (allow default)",
            "deny_default_only": "(version 1) (deny default)",
            "deny_default_process_no_signal": (
                "(version 1) (deny default) (allow process-fork) (allow process-exec)"
            ),
            "deny_default_process_star": (
                "(version 1) (deny default) (allow process-fork*) (allow process-exec*)"
            ),
            "deny_default_process_and_read": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                "(allow file-read*)"
            ),
            "deny_default_process_system": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                '(allow file-read* (subpath "/System")) '
                '(allow file-read* (subpath "/usr")) '
                '(allow file-read* (subpath "/bin")) '
                '(allow file-read* (subpath "/sbin")) '
                '(allow file-read* (subpath "/private/etc")) '
                '(allow file-read* (subpath "/dev")) '
                '(allow file-read-metadata (subpath "/private")) '
                "(allow sysctl-read)"
            ),
            "deny_default_process_system_var": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                '(allow file-read* (subpath "/System")) '
                '(allow file-read* (subpath "/usr")) '
                '(allow file-read* (subpath "/bin")) '
                '(allow file-read* (subpath "/sbin")) '
                '(allow file-read* (subpath "/private/etc")) '
                '(allow file-read* (subpath "/private/var/db")) '
                '(allow file-read* (subpath "/dev")) '
                '(allow file-read-metadata (subpath "/private")) '
                "(allow sysctl-read)"
            ),
            "deny_default_process_literal": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                '(allow file-read* (literal "/usr/bin/true"))'
            ),
            "deny_default_process_regex": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                '(allow file-read* (regex "^/usr/bin/true$"))'
            ),
            "deny_default_process_read_root": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                '(allow file-read* (subpath "/"))'
            ),
            "deny_default_process_read_private_var": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                '(allow file-read* (subpath "/System")) '
                '(allow file-read* (subpath "/usr")) '
                '(allow file-read* (subpath "/private/var"))'
            ),
            "deny_default_process": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                "(allow signal (target self))"
            ),
            "deny_default_usr_read": (
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                "(allow signal (target self)) "
                '(allow file-read* (subpath "/usr"))'
            ),
        }
        info["sandbox_exec_preflight"] = {
            name: _safe_command([str(sandbox_exec), "-p", profile, "/usr/bin/true"])
            for name, profile in preflight_profiles.items()
        }
        profile_fd, profile_name = tempfile.mkstemp(prefix="neuro-code-seatbelt-", suffix=".sb")
        os.close(profile_fd)
        profile_path = Path(profile_name)
        try:
            profile_path.write_text(
                "(version 1) (deny default) "
                "(allow process-fork) (allow process-exec) "
                '(allow file-read* (subpath "/usr"))',
                encoding="utf-8",
            )
            info["sandbox_exec_profile_file_preflight"] = _safe_command(
                [str(sandbox_exec), "-f", str(profile_path), "/usr/bin/true"]
            )
        finally:
            with contextlib.suppress(OSError):
                profile_path.unlink()
    csrutil = shutil.which("csrutil")
    if csrutil is not None:
        status = subprocess.run(
            [csrutil, "status"], capture_output=True, text=True, timeout=5, check=False
        )
        info["csrutil_status"] = {
            "returncode": status.returncode,
            "stdout": status.stdout[-2_000:],
            "stderr": status.stderr[-2_000:],
        }
    return info


def _safe_command(command: list[str]) -> dict[str, object]:
    """Run a metadata-only command without making its failure an infrastructure error.

    仅用于环境元数据;命令失败会被记录,不会伪装成探针能力结果.
    """
    try:
        result = _run_process(command)
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "error": str(error)}
    return {"available": result.returncode == 0, **_command_evidence(result)}


def _evaluate_root_cause(report: dict[str, object]) -> tuple[str, list[str]]:
    failures: list[str] = []
    metadata = report.get("metadata", {})
    if not isinstance(metadata, dict) or not metadata.get("sandbox_exec_present"):
        return "BLOCKED_CAPABILITY", ["sandbox-exec is not present"]
    if not metadata.get("sandbox_exec_executable"):
        return "BLOCKED_CAPABILITY", ["sandbox-exec is not executable"]

    root_cause = report.get("root_cause")
    if not isinstance(root_cause, dict):
        return "BLOCKED_INFRASTRUCTURE", ["root-cause report is missing"]
    matrix = root_cause.get("differential_profiles")
    profiles = matrix.get("profiles") if isinstance(matrix, dict) else None
    broad = profiles.get("B_broad_file_read") if isinstance(profiles, dict) else None
    if not isinstance(broad, dict) or broad.get("returncode") != 0:
        failures.append("broad file-read baseline did not start /usr/bin/true")
    dependency_evidence = {
        "missing_capability": "file-read-data literal /",
        "causal_profile": (
            profiles.get("old_narrow_plus_root_data") if isinstance(profiles, dict) else None
        ),
        "insufficient_metadata_only_profile": (
            profiles.get("old_narrow_plus_root_metadata") if isinstance(profiles, dict) else None
        ),
    }
    root_cause["runtime_dependency_evidence"] = dependency_evidence
    causal_profile = dependency_evidence["causal_profile"]
    metadata_profile = dependency_evidence["insufficient_metadata_only_profile"]
    if not isinstance(causal_profile, dict) or causal_profile.get("returncode") != 0:
        failures.append("adding file-read-data literal / did not restore narrow startup")
    if isinstance(metadata_profile, dict) and metadata_profile.get("returncode") == 0:
        failures.append("file-read-metadata literal / unexpectedly hid the startup dependency")

    dynamic = root_cause.get("dynamic_path")
    profile_ready: dict[str, str] = {
        "WORKSPACE": "BLOCKED",
        "READ_ONLY": "BLOCKED",
        "STRICT": "BLOCKED",
    }
    if isinstance(dynamic, dict):
        for mode, expected_write in (("read-write", 1), ("read-only", 0)):
            evidence = dynamic.get(f"{mode}_subpath")
            data = evidence.get("data") if isinstance(evidence, dict) else None
            expected = {
                "state_read": 0,
                "state_alias_read": 0,
                "host_read": 0,
                "real_home_read": 0,
                "workspace_write": expected_write,
                "alias_workspace_write": expected_write,
                "state_write": 0,
                "home_write": 1,
                "tmp_write": 1,
            }
            if isinstance(data, dict) and data.get("result_available") is not False:
                mismatches = [
                    f"{key}={data.get(key)!r}, expected {value!r}"
                    for key, value in expected.items()
                    if data.get(key) != value
                ]
                if mismatches:
                    failures.extend(
                        f"{mode} broad-read targeted subpath mismatch: {item}"
                        for item in mismatches
                    )
                elif isinstance(evidence, dict) and evidence.get("returncode") == 0:
                    profile_ready["WORKSPACE" if mode == "read-write" else "READ_ONLY"] = "READY"
            else:
                failures.append(f"{mode} broad-read targeted subpath probe produced no result")

            literal = dynamic.get(f"{mode}_literal")
            literal_data = literal.get("data") if isinstance(literal, dict) else None
            if isinstance(literal_data, dict) and literal_data.get("result_available") is not False:
                for key in ("state_read", "state_alias_read", "state_write"):
                    if literal_data.get(key) != 0:
                        failures.append(
                            f"{mode} literal deny did not block {key}={literal_data.get(key)!r}"
                        )
            else:
                failures.append(f"{mode} broad-read targeted literal probe produced no result")
    else:
        failures.append("dynamic path report is missing")

    invocation = root_cause.get("profile_invocation")
    if isinstance(invocation, dict):
        inline = invocation.get("inline_p")
        file_result = invocation.get("file_f")
        if not isinstance(inline, dict) or not isinstance(file_result, dict):
            failures.append("profile invocation comparison is incomplete")
        elif (
            inline.get("returncode") != 0
            or file_result.get("returncode") != 0
            or inline.get("target_exec_state") != "observed_success"
            or file_result.get("target_exec_state") != "observed_success"
        ):
            failures.append("identical broad-read policy differs or fails between -p and -f")
    else:
        failures.append("profile invocation comparison is missing")

    root_cause["profile_assessment"] = profile_ready
    if failures:
        return "BLOCKED_CAPABILITY", failures
    return "READY_FOR_NEXT_MACOS_EVIDENCE", failures


def _evaluate(report: dict[str, object]) -> tuple[str, list[str]]:
    failures: list[str] = []
    metadata = report.get("metadata", {})
    if not isinstance(metadata, dict) or not metadata.get("sandbox_exec_present"):
        failures.append("sandbox-exec is not present")
        return "BLOCKED_CAPABILITY", failures
    if not metadata.get("sandbox_exec_executable"):
        failures.append("sandbox-exec is not executable")
        return "BLOCKED_CAPABILITY", failures
    filesystem = report.get("filesystem")
    if isinstance(filesystem, dict):
        for profile, expected in (
            (
                "workspace",
                {"workspace_read": 1, "workspace_write": 1, "outside_read": 0, "host_home_read": 0},
            ),
            ("read-only", {"workspace_write": 0, "workspace_read": 1, "outside_read": 0}),
            ("strict", {"workspace_write": 1, "outside_read": 0, "host_home_read": 0}),
        ):
            actual = filesystem.get(profile)
            if isinstance(actual, dict):
                for key, value in expected.items():
                    if actual.get(key) != value:
                        failures.append(
                            f"filesystem {profile} {key}={actual.get(key)!r}, expected {value!r}"
                        )
                alias_expected = profile != "read-only"
                if (actual.get("workspace_alias_write") == 1) is not alias_expected:
                    failures.append(f"filesystem {profile} canonical /var alias write mismatch")
                if actual.get("home_write") != 1 or actual.get("tmp_write") != 1:
                    failures.append(f"filesystem {profile} private HOME/TMP was not writable")
                if actual.get("alias_tmp_write") != 1:
                    failures.append(f"filesystem {profile} /var private-TMP alias mismatch")
                for key in ("real_host_home_read", "forbidden_env"):
                    if actual.get(key) != 0:
                        failures.append(f"filesystem {profile} {key} was exposed")
                if actual.get("symlink_read") != 0 or actual.get("dotdot_read") != 0:
                    failures.append(f"filesystem {profile} canonical path escape was readable")
                if actual.get("additional_read") != 1 or actual.get("additional_write") != 0:
                    failures.append(f"filesystem {profile} additional root policy mismatch")
                assertions = actual.get("policy_assertions")
                if (
                    not isinstance(assertions, dict)
                    or not assertions.get("literal_root_data_rule")
                    or not assertions.get("recursive_root_read_absent")
                ):
                    failures.append(f"filesystem {profile} policy root-read assertion failed")
            else:
                failures.append(f"filesystem {profile} produced no result")
        for profile in ("workspace", "read-only", "strict"):
            actual = filesystem.get(profile)
            if isinstance(actual, dict) and (
                actual.get("hardlink_read") == 1 or actual.get("hardlink_write") == 1
            ):
                failures.append(f"filesystem {profile} hardlink escaped outside inode")
        if filesystem.get("hardlink_created") is not True:
            failures.append("hardlink probe could not create an outside-inode link")
    network = report.get("network")
    if isinstance(network, dict):
        for profile, expected in (("workspace", True), ("read-only", False), ("strict", False)):
            actual = network.get(profile)
            if isinstance(actual, dict):
                for key in ("direct_tcp_allowed", "grandchild_tcp_allowed"):
                    if actual.get(key) is not expected:
                        failures.append(f"network {profile} {key} is not {expected!r}")
            else:
                failures.append(f"network {profile} report is missing")
    python_subprocess = report.get("python_subprocess")
    if not isinstance(python_subprocess, dict):
        failures.append("Python subprocess report is missing")
    else:
        if python_subprocess.get("returncode") != 0:
            failures.append("Python subprocess did not start under WORKSPACE")
        if python_subprocess.get("outside_read") != 0:
            failures.append("Python subprocess could read controller-private state")
        if python_subprocess.get("network") is not True:
            failures.append("Python subprocess could not use WORKSPACE localhost network")
    strict_runtime = report.get("strict_runtime")
    if not isinstance(strict_runtime, dict):
        failures.append("STRICT runtime report is missing")
    else:
        contract = strict_runtime.get("policy_contract")
        if not isinstance(contract, dict) or contract.get("recursive_root_read") is not False:
            failures.append("STRICT policy unexpectedly grants recursive host-root read")
        probes = strict_runtime.get("probes")
        if not isinstance(probes, dict):
            failures.append("STRICT runtime probes are missing")
        else:
            for name in ("true", "shell", "python"):
                probe = probes.get(name)
                if not isinstance(probe, dict) or probe.get("returncode") != 0:
                    failures.append(f"STRICT runtime probe failed: {name}")
    inheritance = report.get("access_inheritance")
    if isinstance(inheritance, dict):
        for profile in ("workspace", "read-only", "strict"):
            actual = inheritance.get(profile)
            if isinstance(actual, dict):
                if actual.get("setsid") != 1:
                    failures.append(f"setsid access child could not detach ({profile})")
                if actual.get("outside_read") == 1 or actual.get("outside_write") == 1:
                    failures.append(f"setsid access child reached controller state ({profile})")
                if actual.get("workspace_read") != 1:
                    failures.append(f"setsid access child lost workspace read ({profile})")
                expected_write = profile != "read-only"
                if (actual.get("workspace_write") == 1) is not expected_write:
                    failures.append(f"setsid access child workspace write mismatch ({profile})")
                expected_network = profile == "workspace"
                if (actual.get("network") == 1) is not expected_network:
                    failures.append(f"setsid access child network mismatch ({profile})")
            else:
                failures.append(f"setsid access inheritance report missing ({profile})")
    lifecycle = report.get("lifecycle")
    if isinstance(lifecycle, dict):
        records: list[dict[str, object]] = []
        for value in lifecycle.values():
            if isinstance(value, dict) and "mode" in value:
                records.append(value)
            elif isinstance(value, dict):
                records.extend(
                    item for item in value.values() if isinstance(item, dict) and "mode" in item
                )
        for record in records:
            if record.get("leaked_after_group_termination") or record.get(
                "pid_alive_before_cleanup"
            ):
                failures.append(
                    f"{record.get('mode')} descendant survived {record.get('action')} termination"
                )
    else:
        failures.append("lifecycle report is missing")
    pty_result = report.get("pty")
    if isinstance(pty_result, dict):
        for profile in ("workspace", "read-only", "strict"):
            actual = pty_result.get(profile)
            if isinstance(actual, dict) and not actual.get("ready"):
                failures.append(f"PTY did not start ({profile})")
            if isinstance(actual, dict) and (
                (profile != "read-only" and not actual.get("workspace_write"))
                or (profile == "read-only" and actual.get("workspace_write"))
                or actual.get("outside_read")
                or (profile == "workspace" and not actual.get("network"))
                or (profile != "workspace" and actual.get("network"))
            ):
                failures.append(f"PTY policy mismatch ({profile})")
        if isinstance(pty_result.get("setsid_lifecycle"), dict) and (
            pty_result["setsid_lifecycle"].get("leaked_after_pty_close")
            or pty_result["setsid_lifecycle"].get("pid_alive_before_cleanup")
        ):
            failures.append("PTY setsid descendant survived close")
        ctrl_c = pty_result.get("ctrl_c")
        if (
            not isinstance(ctrl_c, dict)
            or not ctrl_c.get("ready")
            or not ctrl_c.get("interrupt_observed")
        ):
            failures.append("PTY Ctrl-C lifecycle probe did not observe interruption")
    mcp = report.get("mcp_stdio")
    if isinstance(mcp, dict):
        for profile in ("workspace", "read-only", "strict"):
            actual = mcp.get(profile)
            if isinstance(actual, dict) and (
                actual.get("returncode") != 0 or not actual.get("protocol_pong")
            ):
                failures.append(f"MCP-like stdio protocol failed ({profile})")
            if isinstance(actual, dict) and (
                actual.get("outside_read")
                or (profile != "read-only" and not actual.get("workspace_write"))
                or (profile == "read-only" and actual.get("workspace_write"))
                or (profile == "workspace" and not actual.get("network"))
                or (profile != "workspace" and actual.get("network"))
                or actual.get("grandchild_outside_read")
                or (profile != "read-only" and not actual.get("grandchild_workspace_write"))
                or (profile == "read-only" and actual.get("grandchild_workspace_write"))
                or (profile == "workspace" and not actual.get("grandchild_network"))
                or (profile != "workspace" and actual.get("grandchild_network"))
            ):
                failures.append(f"MCP-like stdio policy mismatch ({profile})")
            if not isinstance(actual, dict):
                failures.append(f"MCP-like stdio report missing ({profile})")
    return ("BLOCKED_CAPABILITY" if failures else "PASS"), failures


def _capability_matrix(report: dict[str, object]) -> dict[str, dict[str, str]]:
    """Project raw evidence into a per-profile capability matrix."""
    matrix: dict[str, dict[str, str]] = {}
    filesystem = report.get("filesystem")
    network = report.get("network")
    inheritance = report.get("access_inheritance")
    mcp = report.get("mcp_stdio")
    pty_report = report.get("pty")
    lifecycle = report.get("lifecycle")
    lifecycle_records: list[dict[str, object]] = []
    if isinstance(lifecycle, dict):
        for value in lifecycle.values():
            if isinstance(value, dict) and "mode" in value:
                lifecycle_records.append(value)
            elif isinstance(value, dict):
                lifecycle_records.extend(
                    item for item in value.values() if isinstance(item, dict) and "mode" in item
                )
    lifecycle_pass = bool(lifecycle_records) and not any(
        item.get("leaked_after_group_termination") or item.get("pid_alive_before_cleanup")
        for item in lifecycle_records
    )
    for profile in ("workspace", "read-only", "strict"):
        fs = filesystem.get(profile) if isinstance(filesystem, dict) else None
        net = network.get(profile) if isinstance(network, dict) else None
        access = inheritance.get(profile) if isinstance(inheritance, dict) else None
        mcp_item = mcp.get(profile) if isinstance(mcp, dict) else None
        pty_item = pty_report.get(profile) if isinstance(pty_report, dict) else None
        fs_pass = isinstance(fs, dict) and all(
            (
                fs.get("outside_read") == 0,
                fs.get("workspace_write") == (0 if profile == "read-only" else 1),
                fs.get("hardlink_read") == 0,
                fs.get("hardlink_write") == 0,
                isinstance(fs.get("policy_assertions"), dict),
            )
        )
        net_pass = isinstance(net, dict) and all(
            (
                net.get("direct_tcp_allowed") is (profile == "workspace"),
                net.get("grandchild_tcp_allowed") is (profile == "workspace"),
            )
        )
        access_pass = isinstance(access, dict) and all(
            (
                access.get("outside_read") == 0,
                access.get("outside_write") == 0,
                access.get("workspace_write") == (0 if profile == "read-only" else 1),
                access.get("network") == (1 if profile == "workspace" else 0),
            )
        )
        mcp_pass = isinstance(mcp_item, dict) and mcp_item.get("protocol_pong") is True
        pty_pass = isinstance(pty_item, dict) and pty_item.get("ready") is True
        matrix[profile.upper().replace("-", "_")] = {
            "filesystem": "PASS" if fs_pass else "BLOCKED",
            "network": "PASS" if net_pass else "BLOCKED",
            "access_control_inheritance": "PASS" if access_pass else "BLOCKED",
            "mcp_stdio": "PASS" if mcp_pass else "BLOCKED",
            "pty": "PASS" if pty_pass else "BLOCKED",
            "lifecycle": "PASS" if lifecycle_pass else "BLOCKED",
        }
    strict_runtime = report.get("strict_runtime")
    if isinstance(strict_runtime, dict):
        probes = strict_runtime.get("probes")
        strict_ready = isinstance(probes, dict) and all(
            isinstance(probes.get(name), dict) and probes[name].get("returncode") == 0
            for name in ("true", "shell", "python")
        )
        matrix.setdefault("STRICT", {})["runtime_policy"] = "PASS" if strict_ready else "BLOCKED"
    return matrix


def main() -> int:
    parser = argparse.ArgumentParser(description="Run macOS Seatbelt evidence probes")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("full", "root-cause"),
        default="full",
        help="Run the complete evidence probe or only the minimal /usr/bin/true differential probe.",
    )
    args = parser.parse_args()
    report: dict[str, object] = {
        "probe": "neuro-code-macos-sandbox-evidence-v2",
        "mode": args.mode,
        "metadata": _metadata(),
        "app_sandbox": {
            "probe_scope": "documentation/packaging feasibility only; no app was built",
            "child_inherit_entitlement": "com.apple.security.inherit",
            "dynamic_powerbox_inheritance": "not automatic; bookmark/data handoff required",
        },
    }
    exit_code = 0
    try:
        if not (
            Path("/usr/bin/sandbox-exec").is_file() and os.access("/usr/bin/sandbox-exec", os.X_OK)
        ):
            report["status"] = "BLOCKED_CAPABILITY"
            report["failures"] = ["sandbox-exec is not present"]
            exit_code = CAPABILITY_FAILURE
        else:
            with ProbeHarness(Path("/usr/bin/sandbox-exec")) as harness:
                if args.mode == "root-cause":
                    report["root_cause"] = harness.root_cause()
                    status, failures = _evaluate_root_cause(report)
                else:
                    listener, port = _start_listener()
                    try:
                        report["filesystem"] = harness.filesystem()
                        report["python_subprocess"] = harness.python_subprocess(port)
                        report["strict_runtime"] = harness.strict_runtime()
                        report["network"] = harness.network(port)
                        helper = _compile_lifecycle_helper(harness.private_tmp)
                        report["access_inheritance"] = harness.access_inheritance(helper, port)
                        report["lifecycle"] = harness.lifecycle(helper)
                        report["pty"] = harness.pty(helper, port)
                        report["mcp_stdio"] = harness.mcp_stdio(port)
                    finally:
                        listener.close()
                    status, failures = _evaluate(report)
            report["status"] = status
            report["failures"] = failures
            if args.mode == "full":
                report["capability_matrix"] = _capability_matrix(report)
            exit_code = (
                0 if status in {"PASS", "READY_FOR_NEXT_MACOS_EVIDENCE"} else CAPABILITY_FAILURE
            )
    except ProbeInfrastructureError as error:
        message = str(error)
        capability_failure = "probe result " in message
        report["status"] = "BLOCKED_CAPABILITY" if capability_failure else "BLOCKED_INFRASTRUCTURE"
        report["failures"] = [str(error)]
        exit_code = CAPABILITY_FAILURE if capability_failure else INFRASTRUCTURE_FAILURE
    except (OSError, subprocess.SubprocessError, TimeoutError) as error:
        report["status"] = "BLOCKED_INFRASTRUCTURE"
        report["failures"] = [f"unexpected probe infrastructure error: {error}"]
        exit_code = INFRASTRUCTURE_FAILURE
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": report.get("status"), "failures": report.get("failures", [])},
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
