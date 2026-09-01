"""macOS Seatbelt implementation of the canonical local-process sandbox port.

Seatbelt enforces child-scoped filesystem, network, and environment authority.
Lifecycle ownership remains a POSIX process-group best effort: a descendant
that creates a new session can escape that lifecycle boundary.

macOS Seatbelt 的规范本地进程沙箱端口实现.

Seatbelt 强制执行子进程范围的文件系统、网络和环境权限.生命周期所有权
仍然只是 POSIX 进程组尽力而为:创建新会话的后代可以逃离该边界.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessStdioMode,
    LocalWorkspaceAccessMode,
    OwnedLocalProcess,
    SandboxedProcessRequest,
    lifecycle_capability_satisfies,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.terminal.models import TerminalSignal, TerminalSize
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeOwnedLocalProcess
from neuro_code.infrastructure.sandbox.posix_workspace_inode import PosixWorkspaceInodeAudit
from neuro_code.infrastructure.sandbox.process_tree import ProcessTree
from neuro_code.infrastructure.sandbox.sandbox import _within
from neuro_code.shared.errors import SandboxError

if TYPE_CHECKING:
    from neuro_code.application.ports.terminal import (
        TerminalEofHandler,
        TerminalErrorHandler,
        TerminalOutputHandler,
        TerminalPlatform,
        TerminalPlatformSession,
    )

_SANDBOX_EXEC: Final = Path("/usr/bin/sandbox-exec")
_TRUSTED_SHELL: Final = Path("/bin/sh")
_SYSTEM_READ_ROOTS: Final = (
    Path("/System"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/private/etc"),
    Path("/dev"),
)
_FORWARDED_ENVIRONMENT_NAMES: Final = frozenset({"COLORTERM", "LANG", "NO_COLOR", "PATH", "TERM"})
_DEFAULT_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
_SUPPORTED_PURPOSES: Final = frozenset(
    {
        LocalProcessPurpose.BASH,
        LocalProcessPurpose.BACKGROUND_BASH,
        LocalProcessPurpose.MCP_STDIO,
        LocalProcessPurpose.LSP_SERVER,
        LocalProcessPurpose.INTERACTIVE_TERMINAL,
    }
)
_SUPPORTED_STDIO_MODES: Final = frozenset(
    {
        LocalProcessStdioMode.CAPTURE,
        LocalProcessStdioMode.MERGED_CAPTURE,
        LocalProcessStdioMode.PROTOCOL,
        LocalProcessStdioMode.PTY,
    }
)


class _PrivateChildDirectories:
    """Own and idempotently remove one child's private HOME and TMP."""

    def __init__(self, workspace: Path, state_dir: Path) -> None:
        try:
            root = Path(
                tempfile.mkdtemp(prefix="neuro-code-seatbelt-", dir="/private/tmp")
            ).resolve(strict=True)
            home = root / "home"
            temporary = root / "tmp"
            home.mkdir(mode=0o700)
            temporary.mkdir(mode=0o700)
        except OSError as error:
            raise SandboxError(f"cannot create private macOS child directories: {error}") from error
        self.root = root
        self.home = home
        self.temporary = temporary
        self._lock = threading.RLock()
        self._closed = False
        if _paths_overlap(root, workspace) or _paths_overlap(root, state_dir):
            self.close()
            raise SandboxError("private macOS child directories overlap trusted application data")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            shutil.rmtree(self.root, ignore_errors=True)


@dataclass(slots=True)
class _MacOSSeatbeltOwnedLocalProcess(ProcessTreeOwnedLocalProcess):
    _private_directories: _PrivateChildDirectories

    async def wait(self) -> int:
        try:
            return await ProcessTreeOwnedLocalProcess.wait(self)
        finally:
            self._private_directories.close()

    async def terminate(self, *, grace_seconds: float | None = None) -> None:
        try:
            await ProcessTreeOwnedLocalProcess.terminate(self, grace_seconds=grace_seconds)
        finally:
            self._private_directories.close()


class _MacOSSeatbeltTerminalSession:
    def __init__(
        self,
        session: TerminalPlatformSession,
        private_directories: _PrivateChildDirectories,
    ) -> None:
        self._session = session
        self._private_directories = private_directories

    @property
    def process_id(self) -> int:
        return self._session.process_id

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    def write(self, data: bytes) -> None:
        self._session.write(data)

    def resize(self, size: TerminalSize) -> None:
        self._session.resize(size)

    def send_signal(self, signal: TerminalSignal) -> None:
        self._session.send_signal(signal)

    def poll_exit(self) -> int | None:
        return self._session.poll_exit()

    def close(self) -> None:
        try:
            self._session.close()
        finally:
            self._private_directories.close()


class _MacOSSeatbeltPolicyBuilder:
    """Build one deny-by-default SBPL profile from canonical path authority."""

    def __init__(self, runtime_read_roots: Sequence[Path]) -> None:
        self._runtime_read_roots = tuple(runtime_read_roots)

    def build(
        self,
        filesystem_policy: LocalProcessFilesystemPolicy,
        *,
        private_home: Path,
        private_temporary_directory: Path,
        network_policy: LocalProcessNetworkPolicy,
    ) -> str:
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            "(allow process-exec)",
            "(allow signal (target self))",
            '(allow file-read-data (literal "/"))',
            '(allow file-read-metadata (literal "/"))',
            "(allow sysctl-read)",
        ]
        metadata_ancestors: set[Path] = set()
        for runtime_root in self._runtime_read_roots:
            for spelling in _path_spellings(runtime_root):
                self._append_read(lines, spelling)
                metadata_ancestors.update(spelling.parents)
        for root in filesystem_policy.workspace_roots:
            for spelling in _path_spellings(root.path):
                self._append_read(lines, spelling)
                metadata_ancestors.update(spelling.parents)
                if root.mode is LocalWorkspaceAccessMode.READ_WRITE:
                    lines.append(self._subpath_rule("file-write*", spelling))
        for private_path in (private_home, private_temporary_directory):
            for spelling in _path_spellings(private_path):
                self._append_read(lines, spelling)
                lines.append(self._subpath_rule("file-write*", spelling))
                metadata_ancestors.update(spelling.parents)
        for ancestor in sorted(metadata_ancestors, key=lambda path: (len(path.parts), str(path))):
            lines.append(self._literal_rule("file-read-metadata", ancestor))
        lines.append('(allow file-write-data (literal "/dev/null"))')
        if network_policy is LocalProcessNetworkPolicy.INHERIT:
            lines.append("(allow network-outbound)")
        policy = "\n".join(dict.fromkeys(lines))
        forbidden_root_grants = (
            self._subpath_rule("file-read*", Path("/")),
            self._subpath_rule("file-read-metadata", Path("/")),
            self._subpath_rule("file-write*", Path("/")),
        )
        if any(rule in policy for rule in forbidden_root_grants):
            raise SandboxError("macOS Seatbelt policy must not grant recursive host-root access")
        return policy

    @classmethod
    def _append_read(cls, lines: list[str], path: Path) -> None:
        lines.append(cls._subpath_rule("file-read*", path))
        lines.append(cls._subpath_rule("file-read-metadata", path))

    @staticmethod
    def _subpath_rule(operation: str, path: Path) -> str:
        return _MacOSSeatbeltPolicyBuilder._path_rule(operation, "subpath", path)

    @staticmethod
    def _literal_rule(operation: str, path: Path) -> str:
        return _MacOSSeatbeltPolicyBuilder._path_rule(operation, "literal", path)

    @staticmethod
    def _path_rule(operation: str, filter_name: str, path: Path) -> str:
        value = str(path)
        if "\x00" in value:
            raise SandboxError("macOS Seatbelt policy path contains a null byte")
        literal = json.dumps(value, ensure_ascii=False)
        return f"(allow {operation} ({filter_name} {literal}))"


class MacOSSeatbeltLocalProcessSandbox(LocalProcessSandbox):
    """Launch enabled-profile local children through macOS Seatbelt."""

    def __init__(
        self,
        profile: SandboxProfile,
        workspace: Path,
        state_dir: Path,
        terminal_platform: TerminalPlatform | None = None,
    ) -> None:
        if not profile.enabled:
            raise ValueError("MacOSSeatbeltLocalProcessSandbox requires an enabled profile")
        if _runtime_platform() != "darwin":
            raise SandboxError(
                f"sandbox profile {profile.value!r} is not enforceable on {sys.platform}"
            )
        self._profile = profile
        self._workspace = self._resolve_directory(workspace, "sandbox workspace")
        self._state_dir = self._resolve_directory(state_dir, "sandbox state_dir")
        self._host_home = self._resolve_directory(Path.home(), "host HOME")
        if _paths_overlap(self._workspace, self._state_dir):
            raise SandboxError("sandbox workspace must not overlap controller state_dir")
        if _within(self._host_home, self._workspace):
            raise SandboxError("sandbox workspace must not expose the host HOME root")
        self._sandbox_exec = _trusted_fixed_executable(_SANDBOX_EXEC)
        self._shell = _trusted_fixed_executable(_TRUSTED_SHELL)
        self._terminal_platform = terminal_platform
        self._inode_audit = PosixWorkspaceInodeAudit()
        self._runtime_read_roots = self._runtime_roots_for_host()
        if any(_paths_overlap(root, self._state_dir) for root in self._runtime_read_roots):
            raise SandboxError("sandbox state_dir would be exposed by a trusted runtime root")
        self._policy_builder = _MacOSSeatbeltPolicyBuilder(self._runtime_read_roots)

    @property
    def profile(self) -> SandboxProfile:
        return self._profile

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        return LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess:
        self._validate_request(request)
        if request.stdio_mode is LocalProcessStdioMode.PTY:
            raise SandboxError("PTY process creation requires spawn_terminal")
        private_directories = _PrivateChildDirectories(self._workspace, self._state_dir)
        try:
            arguments, environment = self._launch_arguments_and_environment(
                request, private_directories
            )
            tree = await ProcessTree.spawn_exec(
                str(self._sandbox_exec),
                arguments,
                cwd=request.cwd,
                env=environment,
                merge_output=request.stdio_mode is LocalProcessStdioMode.MERGED_CAPTURE,
                pipe_stdin=request.stdio_mode is LocalProcessStdioMode.PROTOCOL,
            )
        except BaseException:
            private_directories.close()
            raise
        return _MacOSSeatbeltOwnedLocalProcess(
            tree,
            request,
            self.lifecycle_capability,
            private_directories,
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
        self._validate_request(request)
        if request.stdio_mode is not LocalProcessStdioMode.PTY:
            raise SandboxError("interactive terminal requests require PTY stdio")
        if request.uses_shell:
            raise SandboxError("interactive terminal requests require an argv-safe executable")
        platform = self._terminal_platform or _default_terminal_platform()
        if not lifecycle_capability_satisfies(
            platform.lifecycle_capability,
            request.lifecycle.required_capability,
        ):
            raise SandboxError(
                "terminal lifecycle capability "
                f"{platform.lifecycle_capability.value!r} does not satisfy required "
                f"{request.lifecycle.required_capability.value!r}"
            )
        private_directories = _PrivateChildDirectories(self._workspace, self._state_dir)
        try:
            arguments, environment = self._launch_arguments_and_environment(
                request, private_directories
            )
            session = platform.spawn_exec(
                str(self._sandbox_exec),
                arguments,
                cwd=request.cwd,
                env=environment,
                size=size,
                on_output=on_output,
                on_eof=on_eof,
                on_error=on_error,
            )
        except BaseException:
            private_directories.close()
            raise
        if session.lifecycle_capability is not self.lifecycle_capability:
            try:
                session.close()
            finally:
                private_directories.close()
            raise SandboxError("macOS PTY platform must report process-group best-effort lifecycle")
        return _MacOSSeatbeltTerminalSession(session, private_directories)

    def build_policy(
        self,
        request: SandboxedProcessRequest,
        *,
        private_home: Path,
        private_temporary_directory: Path,
    ) -> str:
        """Build a policy after all request and inode security gates pass."""

        self._validate_request(request)
        self._inode_audit.ensure(request.filesystem_policy)
        return self._policy_builder.build(
            request.filesystem_policy,
            private_home=private_home,
            private_temporary_directory=private_temporary_directory,
            network_policy=request.network_policy,
        )

    def _launch_arguments_and_environment(
        self,
        request: SandboxedProcessRequest,
        private_directories: _PrivateChildDirectories,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        policy = self.build_policy(
            request,
            private_home=private_directories.home,
            private_temporary_directory=private_directories.temporary,
        )
        target: tuple[str, ...]
        if request.uses_shell:
            assert request.shell_command is not None
            target = (str(self._shell), "-c", request.shell_command)
        else:
            assert request.executable is not None
            target = (request.executable, *request.arguments)
        environment = self._child_environment(
            request.environment_policy,
            private_home=private_directories.home,
            private_temporary_directory=private_directories.temporary,
            pty=request.stdio_mode is LocalProcessStdioMode.PTY,
        )
        return ("-p", policy, *target), environment

    def _validate_request(self, request: SandboxedProcessRequest) -> None:
        if not lifecycle_capability_satisfies(
            self.lifecycle_capability,
            request.lifecycle.required_capability,
        ):
            raise SandboxError(
                "local process lifecycle capability "
                f"{self.lifecycle_capability.value!r} does not satisfy required "
                f"{request.lifecycle.required_capability.value!r}"
            )
        if request.sandbox_profile is not self._profile:
            raise SandboxError("local process request sandbox profile does not match its launcher")
        if request.purpose not in _SUPPORTED_PURPOSES:
            raise SandboxError(f"unsupported macOS sandbox purpose: {request.purpose.value}")
        if request.stdio_mode not in _SUPPORTED_STDIO_MODES:
            raise SandboxError(f"unsupported macOS sandbox stdio mode: {request.stdio_mode.value}")
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

    def _validate_workspace_roots(self, policy: LocalProcessFilesystemPolicy) -> None:
        roots = tuple(policy.workspace_roots)
        resolved = tuple(
            self._resolve_directory(root.path, "sandbox workspace root") for root in roots
        )
        if any(path != root.path for path, root in zip(resolved, roots, strict=True)):
            raise SandboxError("sandbox workspace roots must use canonical resolved paths")
        if self._workspace not in resolved:
            raise SandboxError("sandbox request must expose its configured workspace root")
        for index, (root, path) in enumerate(zip(roots, resolved, strict=True)):
            if self._profile is SandboxProfile.READ_ONLY and (
                root.mode is not LocalWorkspaceAccessMode.READ_ONLY
            ):
                raise SandboxError("read-only sandbox profile cannot grant workspace write access")
            if _paths_overlap(path, self._state_dir):
                raise SandboxError("sandbox workspace roots must not expose controller state")
            if _within(self._host_home, path):
                raise SandboxError("sandbox workspace roots must not expose the host HOME root")
            if any(_paths_overlap(path, runtime) for runtime in self._runtime_read_roots):
                raise SandboxError("sandbox workspace root overlaps a trusted runtime root")
            for other in resolved[index + 1 :]:
                if _paths_overlap(path, other):
                    raise SandboxError("sandbox workspace roots must not overlap")

    @staticmethod
    def _child_environment(
        policy: LocalProcessEnvironmentPolicy,
        *,
        private_home: Path,
        private_temporary_directory: Path,
        pty: bool,
    ) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name, value in policy.variables.items():
            allowed = (
                name in _FORWARDED_ENVIRONMENT_NAMES
                or name.startswith("LC_")
                or name in policy.explicitly_authorized_names
            )
            if allowed and (pty or name not in {"TERM", "COLORTERM"}):
                environment[name] = value
        environment["PATH"] = environment.get("PATH") or _DEFAULT_PATH
        environment.update(
            {
                "HOME": str(private_home),
                "TMPDIR": str(private_temporary_directory),
                "TMP": str(private_temporary_directory),
                "TEMP": str(private_temporary_directory),
                "PAGER": "cat",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return dict(sorted(environment.items()))

    def _runtime_roots_for_host(self) -> tuple[Path, ...]:
        candidates = [path.resolve() for path in _SYSTEM_READ_ROOTS if path.exists()]
        for candidate in (Path(sys.base_prefix), Path(sys.prefix)):
            lexical = _absolute_without_symlink_resolution(candidate)
            for spelling in (lexical, lexical.resolve()):
                if spelling.is_dir() and not _within(spelling, self._workspace):
                    candidates.append(spelling)
        try:
            executable_spellings = _executable_path_spellings(Path(sys.executable))
        except (OSError, RuntimeError) as error:
            raise SandboxError(f"cannot resolve controller interpreter runtime: {error}") from error
        for executable in executable_spellings:
            interpreter_root = (
                executable.parent.parent if executable.parent.name == "bin" else executable.parent
            )
            if interpreter_root.is_dir() and not _within(interpreter_root, self._workspace):
                candidates.append(interpreter_root)
        result: list[Path] = []
        for candidate in candidates:
            if any(_within(candidate, existing) for existing in result):
                continue
            result = [existing for existing in result if not _within(existing, candidate)]
            result.append(candidate)
        return tuple(result)

    @staticmethod
    def _resolve_directory(path: Path, label: str) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SandboxError(f"cannot resolve {label}: {error}") from error
        if not resolved.is_dir() or resolved == Path("/"):
            raise SandboxError(f"{label} must be an existing non-root directory: {resolved}")
        return resolved


def _trusted_fixed_executable(path: Path) -> Path:
    if os.name != "posix":
        raise SandboxError("macOS sandbox executable validation requires POSIX")
    try:
        details = path.stat()
    except OSError as error:
        raise SandboxError(f"cannot inspect required system executable {path}: {error}") from error
    if not stat.S_ISREG(details.st_mode) or not os.access(path, os.X_OK):
        raise SandboxError(f"required system executable is not runnable: {path}")
    trusted_paths = (path, *path.parents)
    try:
        path_details = tuple(candidate.stat() for candidate in trusted_paths)
    except OSError as error:
        raise SandboxError(f"cannot inspect required system path for {path}: {error}") from error
    if any(
        metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        for metadata in path_details
    ):
        raise SandboxError(f"required system executable is not trusted: {path}")
    return path


def _runtime_platform() -> str:
    """Return the runtime platform without static platform branch folding."""

    return sys.platform


def _path_spellings(path: Path) -> tuple[Path, ...]:
    lexical = _absolute_without_symlink_resolution(path)
    canonical = path.expanduser().resolve(strict=False)
    spellings = [lexical]
    if canonical != lexical:
        spellings.append(canonical)
    for spelling in tuple(spellings):
        try:
            relative = spelling.relative_to(Path("/private/var"))
        except ValueError:
            continue
        alias = Path("/var") / relative
        if alias not in spellings:
            spellings.append(alias)
    return tuple(spellings)


def _executable_path_spellings(path: Path) -> tuple[Path, ...]:
    """Return lexical symlink targets plus the final canonical executable path."""

    current = _absolute_without_symlink_resolution(path)
    spellings: list[Path] = []
    seen: set[Path] = set()
    while current.is_symlink():
        if current in seen:
            raise SandboxError(f"controller interpreter symlink cycle: {current}")
        seen.add(current)
        spellings.append(current)
        target = Path(os.readlink(current))
        current = _absolute_without_symlink_resolution(
            target if target.is_absolute() else current.parent / target
        )
    spellings.append(current)
    canonical = current.resolve(strict=True)
    if canonical not in spellings:
        spellings.append(canonical)
    return tuple(spellings)


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _paths_overlap(first: Path, second: Path) -> bool:
    return _within(first, second) or _within(second, first)


def _default_terminal_platform() -> TerminalPlatform:
    if _runtime_platform() == "darwin":
        from neuro_code.infrastructure.sandbox.posix_pty import PosixPtyPlatform

        return PosixPtyPlatform(
            lifecycle_capability=LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT
        )
    raise SandboxError(f"interactive macOS sandbox is unavailable on {sys.platform}")


__all__ = ["MacOSSeatbeltLocalProcessSandbox"]
