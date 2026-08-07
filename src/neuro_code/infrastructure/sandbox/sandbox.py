from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from neuro_code.application.ports.sandbox import ShellLaunch, ShellSandbox
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.shared.errors import SandboxError

_ACTIVE_PROFILE_ENV = "NEURO_CODE_SANDBOX_ACTIVE"
_STRICT_SYSTEM_READ_PATHS = (
    Path("/usr"),
    Path("/lib"),
    Path("/lib64"),
    Path("/bin"),
    Path("/sbin"),
    Path("/etc"),
    Path("/run"),
    Path("/var"),
    Path("/sys"),
)


def _within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _trusted_system_executable(name: str, workspace: Path) -> Path:
    discovered = shutil.which(name)
    if discovered is None:
        raise SandboxError(f"sandbox profile requires the {name!r} system executable")
    path = Path(discovered).resolve()
    try:
        details = path.stat()
    except OSError as error:
        raise SandboxError(f"cannot inspect sandbox executable {path}: {error}") from error
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SandboxError(f"sandbox executable is not runnable: {path}")
    if _within(path, workspace):
        raise SandboxError(f"refusing workspace-controlled sandbox executable: {path}")
    writable_by_caller = os.access(path, os.W_OK) or any(
        os.access(parent, os.W_OK) for parent in path.parents
    )
    owned_by_root_process = os.geteuid() == 0 and details.st_uid == 0
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or (
        writable_by_caller and not owned_by_root_process
    ):
        raise SandboxError(f"sandbox executable or its parent path is caller-writable: {path}")
    return path


class LinuxBubblewrapSandbox(ShellSandbox):
    """Linux mount-namespace sandbox with per-shell child network isolation.

    提供 Linux 挂载命名空间沙箱,并为每个 Shell 子进程隔离网络."""

    def __init__(
        self,
        profile: SandboxProfile,
        workspace: Path,
        state_dir: Path,
    ) -> None:
        if not profile.enabled:
            raise ValueError("LinuxBubblewrapSandbox requires an enabled profile")
        if not sys.platform.startswith("linux"):
            raise SandboxError(
                f"sandbox profile {profile.value!r} is not enforceable on {sys.platform}"
            )
        self._profile = profile
        self._workspace = workspace.expanduser().resolve()
        self._state_dir = state_dir.expanduser().resolve()
        self._validate_paths()
        self._bubblewrap = _trusted_system_executable("bwrap", self._workspace)
        self._unshare = (
            _trusted_system_executable("unshare", self._workspace)
            if profile.restricts_child_network
            else None
        )

    @property
    def profile(self) -> SandboxProfile:
        return self._profile

    def _validate_paths(self) -> None:
        if self._workspace == Path("/") and self._profile.workspace_writable:
            raise SandboxError("a writable sandbox workspace cannot be the filesystem root")
        if self._state_dir == Path("/"):
            raise SandboxError("sandbox state_dir cannot be the filesystem root")
        if self._profile is SandboxProfile.READ_ONLY and _within(self._workspace, self._state_dir):
            raise SandboxError(
                "read-only sandbox state_dir cannot contain the workspace because that would "
                "make the workspace writable"
            )
        if self._profile is SandboxProfile.READ_ONLY and any(
            _within(self._workspace, temporary) for temporary in (Path("/tmp"), Path("/var/tmp"))
        ):
            raise SandboxError(
                "read-only sandbox workspace cannot be inside a writable temporary path"
            )

    def enforce_current_process(self, command: Sequence[str]) -> None:
        """Re-exec ``command`` inside the requested namespace or verify it is active.

        在请求的命名空间内重新执行 ``command``,或者验证该命名空间已经生效."""

        if not command:
            raise SandboxError("sandbox launch command must not be empty")
        active_profile = os.environ.get(_ACTIVE_PROFILE_ENV)
        if active_profile is not None:
            self.verify_current_process()
            self._preflight_child_network()
            return

        self._prepare_state_directory()
        launch = self.build_launch_argv(command)
        self._preflight_bubblewrap()
        self._preflight_child_network()
        try:
            os.execv(self._bubblewrap, launch)
        except OSError as error:
            raise SandboxError(
                f"could not enter sandbox profile {self._profile.value!r}: {error}"
            ) from error
        raise SandboxError("sandbox launcher returned without replacing the current process")

    def _prepare_state_directory(self) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SandboxError(f"cannot prepare sandbox state directory: {error}") from error

    def build_launch_argv(self, command: Sequence[str]) -> list[str]:
        args = [
            str(self._bubblewrap),
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ]
        if self._profile is SandboxProfile.STRICT:
            args.extend(("--tmpfs", "/"))
            for path in self._strict_read_paths():
                args.extend(("--ro-bind", str(path), str(path)))
        else:
            args.extend(("--ro-bind", "/", "/"))

        args.extend(("--proc", "/proc", "--dev", "/dev"))
        for temporary in (Path("/tmp"), Path("/var/tmp")):
            if temporary.is_dir():
                args.extend(("--bind", str(temporary), str(temporary)))
        if self._profile.workspace_writable:
            args.extend(("--bind", str(self._workspace), str(self._workspace)))
        if self._state_dir != self._workspace:
            args.extend(("--bind", str(self._state_dir), str(self._state_dir)))
        if self._profile is SandboxProfile.STRICT:
            args.extend(("--remount-ro", "/"))
        args.extend(
            (
                "--chdir",
                str(self._workspace),
                "--setenv",
                _ACTIVE_PROFILE_ENV,
                self._profile.value,
                "--",
                *command,
            )
        )
        return args

    def _strict_read_paths(self) -> tuple[Path, ...]:
        candidates = [path for path in _STRICT_SYSTEM_READ_PATHS if path.exists()]
        package_source = Path(__file__).resolve().parents[2]
        for runtime in (
            Path(sys.base_prefix).resolve(),
            Path(sys.prefix).resolve(),
            package_source,
        ):
            if runtime.exists() and not _within(runtime, self._workspace):
                candidates.append(runtime)
        unique: list[Path] = []
        for path in candidates:
            if path not in unique:
                unique.append(path)
        return tuple(unique)

    def _preflight_bubblewrap(self) -> None:
        probe = self.build_launch_argv(("/bin/true",))
        try:
            completed = subprocess.run(
                probe,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SandboxError(f"cannot validate bubblewrap support: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            raise SandboxError(f"bubblewrap cannot enforce this sandbox profile{suffix}")

    def _preflight_child_network(self) -> None:
        if self._unshare is None:
            return
        try:
            completed = subprocess.run(
                (str(self._unshare), "--net", "--map-root-user", "--", "/bin/true"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SandboxError(f"cannot validate child network isolation: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            raise SandboxError(f"child network isolation is unavailable{suffix}")

    def verify_current_process(self) -> None:
        active_profile = os.environ.get(_ACTIVE_PROFILE_ENV)
        if active_profile != self._profile.value:
            rendered = active_profile or "none"
            raise SandboxError(
                f"requested sandbox profile {self._profile.value!r} is not active "
                f"(active marker: {rendered!r})"
            )
        if not self._is_read_only(Path("/")):
            raise SandboxError("sandbox attestation failed: filesystem root is writable")
        workspace_read_only = self._is_read_only(self._workspace)
        if workspace_read_only == self._profile.workspace_writable:
            expected = "writable" if self._profile.workspace_writable else "read-only"
            raise SandboxError(f"sandbox attestation failed: workspace is not {expected}")
        if self._is_read_only(self._state_dir):
            raise SandboxError("sandbox attestation failed: state directory is read-only")
        if self._profile is SandboxProfile.STRICT and self._root_filesystem_type() != "tmpfs":
            raise SandboxError("sandbox attestation failed: strict root is not an allowlist tmpfs")

    @staticmethod
    def _is_read_only(path: Path) -> bool:
        try:
            return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
        except OSError as error:
            raise SandboxError(f"cannot attest sandbox path {path}: {error}") from error

    @staticmethod
    def _root_filesystem_type() -> str | None:
        try:
            lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise SandboxError(f"cannot attest strict sandbox mounts: {error}") from error
        for line in lines:
            before, separator, after = line.partition(" - ")
            fields = before.split()
            if separator and len(fields) > 4 and fields[4] == "/":
                mounted = after.split()
                return mounted[0] if mounted else None
        return None

    def shell_launch(self, command: str) -> ShellLaunch:
        self.verify_current_process()
        if self._unshare is None:
            return ShellLaunch("/bin/sh", ("-c", command))
        return ShellLaunch(
            str(self._unshare),
            ("--net", "--map-root-user", "--", "/bin/sh", "-c", command),
        )

    def exec_launch(self, executable: str, arguments: tuple[str, ...]) -> ShellLaunch:
        """Prepare an argv-safe child while preserving the active sandbox boundary.

        在保持活动沙箱边界的同时准备 argv 安全的子进程."""

        self.verify_current_process()
        if not executable or "\x00" in executable or any("\x00" in item for item in arguments):
            raise SandboxError("sandbox executable and arguments must not contain null bytes")
        if self._unshare is None:
            return ShellLaunch(executable, arguments)
        return ShellLaunch(
            str(self._unshare),
            ("--net", "--map-root-user", "--", executable, *arguments),
        )


def create_shell_sandbox(
    profile: SandboxProfile,
    workspace: Path,
    state_dir: Path,
) -> ShellSandbox | None:
    if not profile.enabled:
        return None
    sandbox = LinuxBubblewrapSandbox(profile, workspace, state_dir)
    sandbox.verify_current_process()
    sandbox._preflight_child_network()
    return sandbox


def enforce_configured_sandbox(
    profile: SandboxProfile,
    workspace: Path,
    state_dir: Path,
    command: Sequence[str],
) -> None:
    if not profile.enabled:
        return
    LinuxBubblewrapSandbox(profile, workspace, state_dir).enforce_current_process(command)
