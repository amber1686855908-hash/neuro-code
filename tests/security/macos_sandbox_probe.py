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
        synthetic_home = self._canonical(self.synthetic_home)
        private_tmp = self._canonical(self.private_tmp)
        additional = tuple(self._canonical(path) for path in extra_read_roots)
        executable_roots = tuple(self._canonical(path) for path in extra_exec_roots)
        read_roots = self._SYSTEM_READ_ROOTS + additional + executable_roots
        lines = ["(version 1)", "(deny default)", "(allow process-fork)", "(allow process-exec)"]
        lines.append("(allow signal (target self))")
        for root in read_roots:
            lines.append(f"(allow file-read* (subpath {self._sbpl_literal(root)}))")
            lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(root)}))")
        lines.append(f"(allow file-read* (subpath {self._sbpl_literal(workspace)}))")
        lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(workspace)}))")
        lines.append(f"(allow file-read* (subpath {self._sbpl_literal(synthetic_home)}))")
        lines.append(f"(allow file-write* (subpath {self._sbpl_literal(synthetic_home)}))")
        lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(synthetic_home)}))")
        lines.append(f"(allow file-read* (subpath {self._sbpl_literal(private_tmp)}))")
        lines.append(f"(allow file-write* (subpath {self._sbpl_literal(private_tmp)}))")
        lines.append(f"(allow file-read-metadata (subpath {self._sbpl_literal(private_tmp)}))")
        if workspace_mode == "read-write":
            lines.append(f"(allow file-write* (subpath {self._sbpl_literal(workspace)}))")
        if network_allowed:
            lines.append("(allow network-outbound)")
        return "\n".join(lines)

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
        try:
            result = subprocess.run(
                [str(self.sandbox_exec), "-p", policy, *command],
                cwd=str(self._canonical(cwd or self.workspace)),
                env=child_env,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                returncode=124,
                stdout=_decode_output(error.stdout),
                stderr=_decode_output(error.stderr),
                timed_out=True,
            )
        except OSError as error:
            raise ProbeInfrastructureError(f"sandbox-exec failed to start: {error}") from error
        return CommandResult(result.returncode, result.stdout, result.stderr)

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
            }
        # Restore the controller sentinel if a policy allowed a hardlink write;
        # the result remains evidence of the attempted escape.
        self._write_fixture(self.state_dir / "credentials.json", "controller-secret")
        data["returncode"] = command_result.returncode
        data["stderr"] = command_result.stderr[-2_000:]
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

    def python_subprocess(self) -> dict[str, object]:
        executable = self._canonical(Path(os.sys.executable))
        roots = tuple(
            dict.fromkeys(
                [
                    self._canonical(Path(os.sys.prefix)),
                    self._canonical(Path(os.sys.base_prefix)),
                    executable.parent,
                    executable.parent.parent,
                ]
            )
        )
        result_path = self._result_path("python-subprocess")
        code = (
            "import json,pathlib;"
            f"p=pathlib.Path({str(self._canonical(result_path))!r});"
            f"outside=pathlib.Path({str(self._canonical(self.state_dir / 'credentials.json'))!r});"
            "read_ok=False;"
            "\ntry: outside.read_text(); read_ok=True\nexcept OSError: pass;"
            "p.write_text(json.dumps({'outside_read':read_ok}))"
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
        data = _read_json_result(result_path)
        data["returncode"] = command_result.returncode
        data["stderr"] = command_result.stderr[-2_000:]
        data["executable"] = str(executable)
        data["runtime_roots"] = [str(root) for root in roots]
        return data

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
            command = (
                "IFS= read -r request; "
                "fs=0; net=0; write=0; "
                f"if cat {state} >/dev/null 2>&1; then fs=1; fi; "
                f"if printf x > {workspace}/mcp-write; then write=1; fi; "
                f"if {shlex.quote(nc)} -z -w 1 127.0.0.1 {listener_port} >/dev/null 2>&1; then net=1; fi; "
                f'printf \'{{"request":"%s","outside_read":%s,"workspace_write":%s,"network":%s}}\\n\' "$request" "$fs" "$write" "$net" > {result_file}; '
                "printf 'pong\\n'"
            )
            completed = self._run_sandboxed(
                policy,
                ["/bin/sh", "-c", command],
                input_text="ping\n",
            )
            result[profile] = {
                "returncode": completed.returncode,
                "protocol_pong": "pong" in completed.stdout,
                "stderr": completed.stderr[-2_000:],
                **_read_json_result(result_path),
            }
        return result

    def access_inheritance(self, helper: Path) -> dict[str, object]:
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
                ],
                timeout=15,
            )
            result[profile] = {
                "returncode": command_result.returncode,
                "setsid_outside_read": _read_json_result(result_path).get("outside_read"),
                "stderr": command_result.stderr[-2_000:],
            }
        return result

    def lifecycle(self, helper: Path) -> dict[str, object]:
        result: dict[str, object] = {}
        for mode in ("ordinary", "setsid"):
            ready = self._result_path(f"lifecycle-ready-{mode}")
            release = self._result_path(f"lifecycle-release-{mode}")
            leaked = self._result_path(f"lifecycle-leaked-{mode}")
            pid_file = self._result_path(f"lifecycle-pid-{mode}")
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
            release.write_text("release", encoding="utf-8")
            time.sleep(0.4)
            child_pid = _read_pid(pid_file)
            pid_alive_before_cleanup = child_pid is not None and _pid_alive(child_pid)
            leaked_seen = leaked.exists()
            if child_pid is not None and _pid_alive(child_pid):
                with contextlib.suppress(OSError):
                    os.kill(child_pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                process.wait(timeout=2)
            result[mode] = {
                "ready_seen": ready_seen,
                "leaked_after_group_termination": leaked_seen,
                "pid_alive_before_cleanup": pid_alive_before_cleanup,
                "outer_returncode": process.returncode,
                "evidence_recorded_before_cleanup": True,
            }
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
                if (argc != 7) return 2;
                const char *ready = argv[1];
                const char *release = argv[2];
                const char *leaked = argv[3];
                const char *outside = argv[4];
                const char *aux_path = argv[5];
                const char *mode = argv[6];
                pid_t child = fork();
                if (child < 0) return 3;
                if (child == 0) {
                    if (strcmp(mode, "setsid") == 0 || strcmp(mode, "access") == 0) {
                        if (setsid() < 0) _exit(4);
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
                        char data[96];
                        snprintf(data, sizeof(data), "{\"outside_read\":%d,\"outside_write\":%d}", read_ok, write_ok);
                        mark(aux_path, data);
                        mark(ready, "ready");
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
        listener.listen(4)
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
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "error": str(error)}
    return {
        "available": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[-2_000:],
        "stderr": result.stderr[-2_000:],
    }


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
            ("workspace", {"workspace_write": 1, "outside_read": 0, "host_home_read": 0}),
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
            if isinstance(actual, dict) and actual.get("direct_tcp_allowed") is not expected:
                failures.append(f"network {profile} direct TCP result is not {expected!r}")
    python_subprocess = report.get("python_subprocess")
    if not isinstance(python_subprocess, dict) or python_subprocess.get("outside_read") != 0:
        failures.append("Python subprocess could read controller-private state")
    inheritance = report.get("access_inheritance")
    if isinstance(inheritance, dict):
        for profile in ("workspace", "read-only", "strict"):
            actual = inheritance.get(profile)
            if isinstance(actual, dict) and actual.get("setsid_outside_read") == 1:
                failures.append(
                    f"access control inherited by setsid child allowed outside read ({profile})"
                )
    lifecycle = report.get("lifecycle")
    if (
        isinstance(lifecycle, dict)
        and isinstance(lifecycle.get("setsid"), dict)
        and (
            lifecycle["setsid"].get("leaked_after_group_termination")
            or lifecycle["setsid"].get("pid_alive_before_cleanup")
        )
    ):
        failures.append("setsid descendant survived process-group termination")
    pty_result = report.get("pty")
    if isinstance(pty_result, dict):
        for profile in ("workspace", "read-only", "strict"):
            actual = pty_result.get(profile)
            if isinstance(actual, dict) and not actual.get("ready"):
                failures.append(f"PTY did not start ({profile})")
            if isinstance(actual, dict) and (
                (profile == "workspace" and not actual.get("workspace_write"))
                or (profile != "workspace" and actual.get("workspace_write"))
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
    mcp = report.get("mcp_stdio")
    if isinstance(mcp, dict):
        for profile in ("workspace", "read-only", "strict"):
            actual = mcp.get(profile)
            if isinstance(actual, dict) and not actual.get("protocol_pong"):
                failures.append(f"MCP-like stdio protocol failed ({profile})")
            if isinstance(actual, dict) and (
                actual.get("outside_read")
                or (profile == "workspace" and not actual.get("workspace_write"))
                or (profile != "workspace" and actual.get("workspace_write"))
                or (profile == "workspace" and not actual.get("network"))
                or (profile != "workspace" and actual.get("network"))
            ):
                failures.append(f"MCP-like stdio policy mismatch ({profile})")
    return ("BLOCKED_CAPABILITY" if failures else "PASS"), failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Run macOS Seatbelt evidence probes")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, object] = {
        "probe": "neuro-code-macos-sandbox-evidence-v1",
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
            listener, port = _start_listener()
            try:
                with ProbeHarness(Path("/usr/bin/sandbox-exec")) as harness:
                    report["filesystem"] = harness.filesystem()
                    report["python_subprocess"] = harness.python_subprocess()
                    report["network"] = harness.network(port)
                    helper = _compile_lifecycle_helper(harness.private_tmp)
                    report["access_inheritance"] = harness.access_inheritance(helper)
                    report["lifecycle"] = harness.lifecycle(helper)
                    report["pty"] = harness.pty(helper, port)
                    report["mcp_stdio"] = harness.mcp_stdio(port)
            finally:
                listener.close()
            status, failures = _evaluate(report)
            report["status"] = status
            report["failures"] = failures
            exit_code = 0 if status == "PASS" else CAPABILITY_FAILURE
    except ProbeInfrastructureError as error:
        report["status"] = "BLOCKED_INFRASTRUCTURE"
        report["failures"] = [str(error)]
        exit_code = INFRASTRUCTURE_FAILURE
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
