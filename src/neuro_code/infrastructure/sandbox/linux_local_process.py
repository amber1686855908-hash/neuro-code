"""Linux child-scoped Bubblewrap implementation of the local-process port.

The trusted Neuro Code controller stays on the host.  This adapter creates one
fresh Bubblewrap boundary for each Bash child, so model-controlled descendants
receive only an explicit workspace view, private HOME and temporary storage,
and a small environment allowlist.

Linux 子进程范围的 Bubblewrap 本地进程端口实现.

受信任的 Neuro Code controller 保持在宿主上.该适配器为每个 Bash 子进程创建
一个全新的 Bubblewrap 边界,使模型可控的后代进程只能得到显式工作区视图、私有 HOME
和临时存储,以及小型环境白名单.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import select
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    OwnedLocalProcess,
    SandboxedProcessRequest,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.terminal.models import TerminalSize
from neuro_code.infrastructure.sandbox.linux_pidfd import (
    LinuxPidfdOps,
    default_linux_pidfd_ops,
)
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeOwnedLocalProcess
from neuro_code.infrastructure.sandbox.process_tree import ProcessTree
from neuro_code.infrastructure.sandbox.sandbox import _trusted_system_executable, _within
from neuro_code.shared.errors import SandboxError

if TYPE_CHECKING:
    from neuro_code.application.ports.terminal import (
        TerminalEofHandler,
        TerminalErrorHandler,
        TerminalOutputHandler,
        TerminalPlatform,
        TerminalPlatformSession,
    )

_SUPPORTED_PURPOSES = frozenset(
    {
        LocalProcessPurpose.BASH,
        LocalProcessPurpose.BACKGROUND_BASH,
        LocalProcessPurpose.MCP_STDIO,
        LocalProcessPurpose.INTERACTIVE_TERMINAL,
    }
)
_SUPPORTED_STDIO_MODES = frozenset(
    {
        LocalProcessStdioMode.CAPTURE,
        LocalProcessStdioMode.MERGED_CAPTURE,
        LocalProcessStdioMode.PROTOCOL,
        LocalProcessStdioMode.PTY,
    }
)
_FORWARDED_ENVIRONMENT_NAMES = frozenset(
    {
        "COLORTERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "TERM",
    }
)
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
_CHILD_HOME = "/home/neuro-code"
_SYSTEM_RUNTIME_PATHS = (
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/etc"),
)
_PRIVATE_MOUNT_DESTINATIONS = (
    Path("/tmp"),
    Path("/var/tmp"),
    Path(_CHILD_HOME),
)
_MAX_CONTROLLER_STATE_ENTRIES = 100_000


@dataclass(slots=True)
class _LinuxBubblewrapOwnedLocalProcess(ProcessTreeOwnedLocalProcess):
    """Own the Bubblewrap namespace supervisor as the lifecycle boundary.

    Bubblewrap's command intentionally starts a new session, so graceful
    process-group signalling cannot be the authority for enabled profiles.
    Killing the trusted Bubblewrap supervisor activates ``--die-with-parent``
    and tears down the PID namespace, including descendants that called
    ``setsid()``. The shared tree cleanup then reaps the launcher and its pipes.

    将 Bubblewrap namespace supervisor 作为生命周期边界.启用 profile 直接终止
    可信 supervisor,从而触发 ``--die-with-parent`` 并清理包括 ``setsid()`` 后代在内
    的 PID namespace.
    """

    _boundary_pidfd: int
    _pidfd_ops: LinuxPidfdOps
    _pidfd_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def wait(self) -> int:
        try:
            return await ProcessTreeOwnedLocalProcess.wait(self)
        finally:
            async with self._pidfd_lock:
                self._close_pidfd()

    async def terminate(self, *, grace_seconds: float | None = None) -> None:
        del grace_seconds  # An enabled sandbox boundary is fail-closed, not best-effort TERM.
        async with self._pidfd_lock:
            if self._boundary_pidfd >= 0:
                try:
                    self._pidfd_ops.send_signal(self._boundary_pidfd, signal.SIGKILL)
                except OSError as error:
                    if error.errno != errno.ESRCH:
                        raise
                self._tree.kill_direct_boundary()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self._request.lifecycle.force_wait_seconds
                try:
                    while not select.select((self._boundary_pidfd,), (), (), 0)[0]:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise TimeoutError("Bubblewrap sandbox boundary did not terminate")
                        await asyncio.sleep(min(0.02, remaining))
                finally:
                    self._close_pidfd()
        # The pidfd is the kernel-owned proof that the Bubblewrap boundary has
        # exited. asyncio's subprocess waiter may remain pending while pipe
        # transports finish closing, so it is deliberately not the security
        # boundary and must not delay cancellation/shutdown completion.
        await asyncio.sleep(0)

    def _close_pidfd(self) -> None:
        if self._boundary_pidfd >= 0:
            os.close(self._boundary_pidfd)
            self._boundary_pidfd = -1


class LinuxBubblewrapLocalProcessSandbox(LocalProcessSandbox):
    """Launch supported local children in a private Linux namespace.

    The adapter supports pipe-based Bash, background Bash, protocol-based MCP
    stdio, and PTY terminals. PTY requests are launched as a Bubblewrap child
    through the platform terminal adapter, not through the controller shell.

    在私有且以子进程为范围的 Linux 命名空间中启动受支持的本地子进程.

    该适配器支持基于管道的 Bash、后台 Bash、基于协议的 MCP stdio 和 PTY
    终端.PTY 请求作为 Bubblewrap child 通过平台终端适配器启动,而不是通过
    controller shell 启动.
    """

    def __init__(
        self,
        profile: SandboxProfile,
        workspace: Path,
        state_dir: Path,
        terminal_platform: TerminalPlatform | None = None,
        pidfd_ops: LinuxPidfdOps | None = None,
    ) -> None:
        if not profile.enabled:
            raise ValueError("LinuxBubblewrapLocalProcessSandbox requires an enabled profile")
        if not sys.platform.startswith("linux"):
            raise SandboxError(
                f"sandbox profile {profile.value!r} is not enforceable on {sys.platform}"
            )
        self._profile = profile
        self._terminal_platform = terminal_platform
        try:
            self._pidfd_ops = pidfd_ops or default_linux_pidfd_ops()
        except OSError as error:
            raise SandboxError(
                f"enabled Linux sandbox requires pidfd lifecycle ownership: {error}"
            ) from error
        self._workspace = self._resolve_directory(workspace, "sandbox workspace")
        self._state_dir = state_dir.expanduser().resolve()
        self._bubblewrap = _trusted_system_executable("bwrap", self._workspace)
        self._runtime_mounts = self._runtime_mounts_for_host()
        self._validate_controller_private_state()
        self._validate_pidfd_support(self._pidfd_ops)
        self._preflight()

    @property
    def profile(self) -> SandboxProfile:
        """Return the immutable profile enforced for every child request.

        返回为每个子进程请求强制执行的不可变 profile.
        """

        return self._profile

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess:
        """Create one Bubblewrap-owned Bash process tree.

        创建一个由 Bubblewrap 拥有的 Bash 进程树.
        """

        self._validate_request(request)
        if request.stdio_mode is LocalProcessStdioMode.PTY:
            raise SandboxError("PTY process creation requires spawn_terminal")
        status_reader, status_writer = os.pipe()
        tree: ProcessTree | None = None
        try:
            launch = self.build_launch_argv(request, status_fd=status_writer)
            tree = await ProcessTree.spawn_exec(
                str(self._bubblewrap),
                tuple(launch[1:]),
                cwd=request.cwd,
                # Bubblewrap itself is trusted infrastructure.  Its child always
                # receives ``--clearenv`` plus only _child_environment().
                env={},
                merge_output=request.stdio_mode is LocalProcessStdioMode.MERGED_CAPTURE,
                pipe_stdin=request.stdio_mode is LocalProcessStdioMode.PROTOCOL,
                pass_fds=(status_writer,),
            )
            boundary_pidfd = self._pidfd_ops.open(tree.process.pid)
        except OSError as error:
            if tree is not None:
                await tree.terminate(grace_seconds=0.01)
            raise SandboxError(f"cannot own Bubblewrap boundary with pidfd: {error}") from error
        finally:
            os.close(status_writer)
        assert tree is not None
        try:
            child_pid = await self._read_child_pid(status_reader, timeout_seconds=5)
        except (OSError, TimeoutError, ValueError) as error:
            with contextlib.suppress(OSError, ProcessLookupError):
                self._pidfd_ops.send_signal(boundary_pidfd, signal.SIGKILL)
            os.close(boundary_pidfd)
            await tree.terminate(grace_seconds=0.01)
            raise SandboxError(f"cannot attest Bubblewrap child process: {error}") from error
        finally:
            os.close(status_reader)
        del child_pid  # The status proves launch; the outer bwrap pidfd owns the full boundary.
        return _LinuxBubblewrapOwnedLocalProcess(
            tree,
            request,
            boundary_pidfd,
            self._pidfd_ops,
        )

    def spawn_terminal(
        self,
        request: SandboxedProcessRequest,
        *,
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession:
        """Launch an interactive PTY inside one Bubblewrap child.

        在一个 Bubblewrap child 内启动交互式 PTY.
        """

        self._validate_request(request)
        if request.stdio_mode is not LocalProcessStdioMode.PTY:
            raise SandboxError("interactive terminal requests require PTY stdio")
        if request.uses_shell:
            raise SandboxError("interactive terminal requests require an argv-safe executable")
        assert request.executable is not None
        launch = self.build_launch_argv(request)
        platform = self._terminal_platform or _default_terminal_platform(self._pidfd_ops)
        return platform.spawn_exec(
            str(self._bubblewrap),
            tuple(launch[1:]),
            cwd=request.cwd,
            # Bubblewrap receives an empty host environment and reconstructs
            # the child environment through --clearenv/--setenv.
            env={},
            size=size,
            on_output=on_output,
            on_eof=on_eof,
            on_error=on_error,
        )

    def build_launch_argv(
        self,
        request: SandboxedProcessRequest,
        *,
        status_fd: int | None = None,
    ) -> list[str]:
        """Build the complete trusted Bubblewrap argv for one validated child.

        为一个已验证的子进程构建完整且受信任的 Bubblewrap argv.
        """

        self._validate_request(request)
        args = [
            str(self._bubblewrap),
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ]
        if request.network_policy is LocalProcessNetworkPolicy.ISOLATED:
            args.append("--unshare-net")
        if status_fd is not None:
            args.extend(("--info-fd", str(status_fd)))

        # An empty root avoids exposing controller HOME, state, /run, or /var.
        # Each destination directory is created before its mount is attached.
        args.extend(("--tmpfs", "/"))
        created_directories = {Path("/")}
        for destination in _PRIVATE_MOUNT_DESTINATIONS:
            self._append_destination_directories(args, destination, created_directories)
            args.extend(("--tmpfs", str(destination)))

        for source, destination in self._runtime_mounts:
            self._append_destination_directories(args, destination, created_directories)
            args.extend(("--ro-bind", str(source), str(destination)))
        self._append_destination_directories(args, Path("/proc"), created_directories)
        args.extend(("--proc", "/proc"))
        self._append_destination_directories(args, Path("/dev"), created_directories)
        args.extend(("--dev", "/dev"))

        # Workspace binds are the only host data mounts requested by the
        # model-controlled process.  A remount of the empty root does not
        # weaken writable child submounts such as workspace and /tmp.
        for root in request.filesystem_policy.workspace_roots:
            destination = root.path
            self._append_destination_directories(args, destination, created_directories)
            mount_flag = (
                "--ro-bind" if root.mode is LocalWorkspaceAccessMode.READ_ONLY else "--bind"
            )
            args.extend((mount_flag, str(destination), str(destination)))
        args.extend(("--remount-ro", "/"))

        args.append("--clearenv")
        for name, value in self._child_environment(request.environment_policy).items():
            args.extend(("--setenv", name, value))
        args.extend(("--chdir", str(request.cwd), "--"))
        if request.uses_shell:
            assert request.shell_command is not None
            args.extend(("/bin/sh", "-c", request.shell_command))
        else:
            assert request.executable is not None
            args.extend((request.executable, *request.arguments))
        return args

    def _validate_request(self, request: SandboxedProcessRequest) -> None:
        if request.sandbox_profile is not self._profile:
            raise SandboxError("local process request sandbox profile does not match its launcher")
        if request.purpose not in _SUPPORTED_PURPOSES:
            raise SandboxError(
                f"sandbox profile {self._profile.value!r} does not support "
                f"local process purpose {request.purpose.value!r} yet"
            )
        if request.stdio_mode not in _SUPPORTED_STDIO_MODES:
            raise SandboxError(
                f"sandbox profile {self._profile.value!r} does not support "
                f"stdio mode {request.stdio_mode.value!r} yet"
            )
        if request.purpose is LocalProcessPurpose.INTERACTIVE_TERMINAL:
            if request.stdio_mode is not LocalProcessStdioMode.PTY:
                raise SandboxError("interactive terminal requests require PTY stdio")
        elif request.stdio_mode is LocalProcessStdioMode.PTY:
            raise SandboxError("PTY requests require interactive-terminal purpose")
        if request.stdio_mode is LocalProcessStdioMode.PROTOCOL and request.uses_shell:
            raise SandboxError("protocol local processes require an argv-safe executable request")
        if not request.filesystem_policy.private_home:
            raise SandboxError("enabled sandbox children require a private HOME")
        if not request.filesystem_policy.private_temporary_directory:
            raise SandboxError("enabled sandbox children require a private temporary directory")
        expected_network = (
            LocalProcessNetworkPolicy.ISOLATED
            if self._profile.restricts_child_network
            else LocalProcessNetworkPolicy.INHERIT
        )
        if request.network_policy is not expected_network:
            raise SandboxError(
                f"sandbox profile {self._profile.value!r} requires child network policy "
                f"{expected_network.value!r}"
            )
        resolved_cwd = self._resolve_directory(request.cwd, "sandbox child cwd")
        if resolved_cwd != request.cwd:
            raise SandboxError("sandbox child cwd must use a canonical resolved path")
        self._validate_workspace_roots(request.filesystem_policy)
        if not any(
            resolved_cwd == root.path or resolved_cwd.is_relative_to(root.path)
            for root in request.filesystem_policy.workspace_roots
        ):
            raise SandboxError("sandbox child cwd must be inside an authorized workspace root")

    def _validate_workspace_roots(self, policy: LocalProcessFilesystemPolicy) -> None:
        roots = tuple(policy.workspace_roots)
        resolved_roots = tuple(
            self._resolve_directory(root.path, "sandbox workspace root") for root in roots
        )
        if any(
            resolved != declared.path
            for resolved, declared in zip(resolved_roots, roots, strict=True)
        ):
            raise SandboxError("sandbox workspace roots must use canonical resolved paths")
        if self._workspace not in resolved_roots:
            raise SandboxError("sandbox request must expose its configured workspace root")
        for root, resolved in zip(roots, resolved_roots, strict=True):
            expected_mode = (
                LocalWorkspaceAccessMode.READ_ONLY
                if self._profile is SandboxProfile.READ_ONLY
                else root.mode
            )
            if root.mode is not expected_mode:
                raise SandboxError("read-only sandbox profile cannot grant workspace write access")
            if self._paths_overlap(resolved, self._state_dir):
                raise SandboxError("sandbox workspace roots must not expose controller state")
            if any(
                self._paths_overlap(resolved, runtime_source)
                for runtime_source, _ in self._runtime_mounts
            ):
                raise SandboxError("sandbox workspace root overlaps a trusted runtime mount")
        for index, resolved_root in enumerate(resolved_roots):
            for other in resolved_roots[index + 1 :]:
                if self._paths_overlap(resolved_root, other):
                    raise SandboxError("sandbox workspace roots must not overlap")

    def _validate_controller_private_state(self) -> None:
        if self._state_dir == Path("/"):
            raise SandboxError("sandbox state_dir cannot be the filesystem root")
        if self._paths_overlap(self._workspace, self._state_dir):
            raise SandboxError("sandbox workspace must not overlap controller state_dir")
        for source, _ in self._runtime_mounts:
            if self._paths_overlap(source, self._state_dir):
                raise SandboxError("sandbox state_dir would be exposed by a runtime mount")
        self._validate_controller_state_hardlinks()

    def _validate_controller_state_hardlinks(self) -> None:
        """Reject aliases that would expose controller files through a workspace bind.

        A bind mount is path based, but a pre-existing hardlink inside an
        authorized workspace names the same inode as its source.  Scanning the
        entire workspace would be both expensive and insufficiently precise.
        Controller state is the smaller security boundary, so enabled Linux
        profiles fail closed when any regular controller-state file has more
        than one link.  The sandboxed child cannot create a new link to an
        unmounted state path after launch.

        拒绝会通过工作区绑定暴露 controller 文件的 inode 别名.

        绑定挂载以路径为边界,但工作区中预先存在的硬链接与源文件指向同一个 inode.
        扫描整个工作区既昂贵也无法精确表达这一边界,因此只扫描规模更小的 controller
        状态目录.启用的 Linux profile 遇到链接数大于一的常规状态文件时失败关闭.
        """

        if not self._state_dir.exists():
            return
        pending = [self._state_dir]
        visited = 0
        try:
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > _MAX_CONTROLLER_STATE_ENTRIES:
                            raise SandboxError(
                                "sandbox state_dir exceeds the bounded hardlink audit"
                            )
                        metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(metadata.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                            raise SandboxError(
                                "controller state file has a hardlink outside its trusted path: "
                                f"{entry.path}"
                            )
        except SandboxError:
            raise
        except OSError as error:
            raise SandboxError(f"cannot audit controller state hardlinks: {error}") from error

    def _runtime_mounts_for_host(self) -> tuple[tuple[Path, Path], ...]:
        candidates: list[tuple[Path, Path]] = []
        for logical_path in _SYSTEM_RUNTIME_PATHS:
            if logical_path.exists():
                candidates.append((logical_path.resolve(), logical_path))
        package_root = Path(__file__).resolve().parents[3]
        for runtime_path in (Path(sys.base_prefix), Path(sys.prefix), package_root):
            resolved = runtime_path.expanduser().resolve()
            if (
                resolved.exists()
                and resolved.is_dir()
                # A project-local virtual environment or this package's source
                # tree is already covered by the explicitly mounted workspace.
                # Mounting it again as trusted runtime would both be redundant
                # and make the workspace/runtimes overlap ambiguous.
                and not _within(resolved, self._workspace)
            ):
                candidates.append((resolved, resolved))

        mounts: list[tuple[Path, Path]] = []
        for source, destination in candidates:
            if any(
                _within(source, mounted_source) and _within(destination, mounted_destination)
                for mounted_source, mounted_destination in mounts
            ):
                continue
            # On usr-merged Linux hosts, /bin and /lib resolve below /usr but
            # still need their own child destinations.  A mount is redundant
            # only when both the host source and child destination are already
            # covered by an earlier mount.
            nested = [
                entry
                for entry in mounts
                if _within(entry[0], source) and _within(entry[1], destination)
            ]
            if nested:
                mounts = [entry for entry in mounts if entry not in nested]
            mounts.append((source, destination))
        return tuple(mounts)

    def _preflight(self) -> None:
        """Prove that Bubblewrap can establish this profile before use.

        在使用前证明 Bubblewrap 能够建立此 profile.
        """

        request = SandboxedProcessRequest.exec(
            "/bin/true",
            purpose=LocalProcessPurpose.BASH,
            cwd=self._workspace,
            sandbox_profile=self._profile,
            filesystem_policy=LocalProcessFilesystemPolicy(
                (
                    LocalWorkspaceAccess(
                        self._workspace,
                        (
                            LocalWorkspaceAccessMode.READ_ONLY
                            if self._profile is SandboxProfile.READ_ONLY
                            else LocalWorkspaceAccessMode.READ_WRITE
                        ),
                    ),
                ),
            ),
            network_policy=(
                LocalProcessNetworkPolicy.ISOLATED
                if self._profile.restricts_child_network
                else LocalProcessNetworkPolicy.INHERIT
            ),
            environment_policy=LocalProcessEnvironmentPolicy(),
            stdio_mode=LocalProcessStdioMode.CAPTURE,
            lifecycle=LocalProcessLifecycle(),
        )
        try:
            completed = subprocess.run(
                self.build_launch_argv(request),
                cwd=self._workspace,
                env={},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SandboxError(f"cannot validate child Bubblewrap support: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            raise SandboxError(f"Bubblewrap cannot enforce child sandbox{suffix}")

    @staticmethod
    def _validate_pidfd_support(pidfd_ops: LinuxPidfdOps) -> None:
        try:
            pidfd_ops.probe()
        except OSError as error:
            raise SandboxError(
                f"enabled Linux sandbox requires pidfd lifecycle ownership: {error}"
            ) from error

    @staticmethod
    async def _read_child_pid(status_fd: int, *, timeout_seconds: float) -> int:
        payload = bytearray()
        os.set_blocking(status_fd, False)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            try:
                chunk = os.read(status_fd, 4096)
            except BlockingIOError:
                if loop.time() >= deadline:
                    raise TimeoutError("Bubblewrap launch attestation timed out") from None
                await asyncio.sleep(0.01)
                continue
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > 65_536:
                raise ValueError("Bubblewrap status exceeds its safety limit")
        status = json.loads(bytes(payload).decode("utf-8"))
        child_pid = status.get("child-pid")
        if not isinstance(child_pid, int) or isinstance(child_pid, bool) or child_pid <= 1:
            raise ValueError("Bubblewrap status omitted a valid child PID")
        return child_pid

    @staticmethod
    def _resolve_directory(path: Path, label: str) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as error:
            raise SandboxError(f"cannot resolve {label}: {error}") from error
        if not resolved.is_dir():
            raise SandboxError(f"{label} must be an existing directory: {resolved}")
        if resolved == Path("/"):
            raise SandboxError(f"{label} must not be the filesystem root")
        return resolved

    @staticmethod
    def _append_destination_directories(
        args: list[str],
        destination: Path,
        created: set[Path],
    ) -> None:
        current = Path("/")
        for component in destination.parts[1:]:
            current /= component
            if current not in created:
                args.extend(("--dir", str(current)))
                created.add(current)

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        return _within(first, second) or _within(second, first)

    @staticmethod
    def _child_environment(policy: LocalProcessEnvironmentPolicy) -> dict[str, str]:
        environment = {
            name: value
            for name, value in policy.variables.items()
            if name in _FORWARDED_ENVIRONMENT_NAMES or name in policy.explicitly_authorized_names
        }
        environment["PATH"] = environment.get("PATH") or _DEFAULT_PATH
        environment.update(
            {
                "HOME": _CHILD_HOME,
                "PAGER": "cat",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "TMPDIR": "/tmp",
                "TMP": "/tmp",
                "TEMP": "/tmp",
            }
        )
        return dict(sorted(environment.items()))


def _default_terminal_platform(pidfd_ops: LinuxPidfdOps) -> TerminalPlatform:
    if sys.platform.startswith("linux"):
        from neuro_code.infrastructure.sandbox.posix_pty import PosixPtyPlatform

        return PosixPtyPlatform(pidfd_ops=pidfd_ops)
    raise SandboxError(f"interactive terminal sandbox is unavailable on {sys.platform}")


__all__ = ["LinuxBubblewrapLocalProcessSandbox"]
