"""Canonical local-process sandbox ports.

All local process creation uses :class:`LocalProcessSandbox`; it owns the
boundary between application code and platform process primitives.

定义规范的本地进程沙箱端口.

所有本地进程创建都使用 :class:`LocalProcessSandbox`;它拥有应用代码与平台进程原语之间的边界.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from neuro_code.domain.sandbox.models import SandboxProfile

if TYPE_CHECKING:
    from neuro_code.application.ports.terminal import (
        TerminalEofHandler,
        TerminalErrorHandler,
        TerminalOutputHandler,
        TerminalPlatformSession,
    )
    from neuro_code.domain.terminal.models import TerminalSize


class LocalProcessPurpose(StrEnum):
    """Classify a model-controlled local child by its product purpose.

    根据产品用途对模型可控的本地子进程分类.
    """

    BASH = "bash"
    BACKGROUND_BASH = "background-bash"
    MCP_STDIO = "mcp-stdio"
    INTERACTIVE_TERMINAL = "interactive-terminal"


class LocalProcessStdioMode(StrEnum):
    """Describe the requested child transport without exposing platform APIs.

    描述请求的子进程传输方式,而不暴露平台 API.
    """

    CAPTURE = "capture"
    MERGED_CAPTURE = "merged-capture"
    PROTOCOL = "protocol"
    PTY = "pty"


class LocalProcessNetworkPolicy(StrEnum):
    """Network authority requested for one child process.

    表示一个子进程请求的网络权限.
    """

    INHERIT = "inherit"
    ISOLATED = "isolated"


class LocalWorkspaceAccessMode(StrEnum):
    """Filesystem access granted to one explicitly mounted workspace root.

    表示为一个显式挂载的工作区根目录授予的文件系统访问权限.
    """

    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


class LocalProcessCancellationPolicy(StrEnum):
    """Cancellation semantics owned by a local process launcher.

    表示由本地进程启动器拥有的取消语义.
    """

    TERMINATE_PROCESS_TREE = "terminate-process-tree"


@dataclass(frozen=True, slots=True)
class LocalWorkspaceAccess:
    """One canonical workspace root exposed to a local child.

    一个向本地子进程公开的规范工作区根目录.
    """

    path: Path
    mode: LocalWorkspaceAccessMode

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("local workspace access path must be a pathlib.Path")
        if not self.path.is_absolute():
            raise ValueError("local workspace access path must be absolute")
        if self.path == Path("/"):
            raise ValueError("local workspace access path must not be the filesystem root")
        if not isinstance(self.mode, LocalWorkspaceAccessMode):
            raise TypeError("local workspace access mode must be canonical")


@dataclass(frozen=True, slots=True)
class LocalProcessFilesystemPolicy:
    """Requested child filesystem boundary.

    The policy deliberately lists only child-visible workspace roots.  It has
    no field for controller state, credentials, or provider configuration:
    those resources are never a child capability.

    表示请求的子进程文件系统边界.

    该策略只列出对子进程可见的工作区根目录.它没有 controller 状态、凭据或
    Provider 配置的字段:这些资源绝不属于子进程能力.
    """

    workspace_roots: tuple[LocalWorkspaceAccess, ...]
    private_home: bool = True
    private_temporary_directory: bool = True

    def __post_init__(self) -> None:
        if not self.workspace_roots:
            raise ValueError("local process filesystem policy requires a workspace root")
        if not all(isinstance(root, LocalWorkspaceAccess) for root in self.workspace_roots):
            raise TypeError("local process filesystem roots must be canonical")
        paths = tuple(root.path for root in self.workspace_roots)
        if len(set(paths)) != len(paths):
            raise ValueError("local process filesystem roots must be unique")
        if not isinstance(self.private_home, bool):
            raise TypeError("local process private_home must be boolean")
        if not isinstance(self.private_temporary_directory, bool):
            raise TypeError("local process private_temporary_directory must be boolean")


@dataclass(frozen=True, slots=True)
class LocalProcessEnvironmentPolicy:
    """Explicit child environment supplied by the trusted controller.

    Platform adapters must not implicitly merge ``os.environ`` into this
    mapping. A caller may choose which values to provide, while a later
    platform adapter enforces the profile's minimum allowlist. A narrowly
    scoped integration, such as one configured MCP server, may explicitly
    authorize additional names through ``explicitly_authorized_names``.

    表示由受信任 controller 显式提供的子进程环境.

    平台适配器不得把 ``os.environ`` 隐式合并到该映射中.调用者可以选择提供
    哪些值;后续平台适配器会执行 profile 的最小 allowlist.像已配置的单个 MCP
    server 这样的窄集成,可以通过 ``explicitly_authorized_names`` 明确授权额外变量.
    """

    variables: Mapping[str, str] = field(default_factory=dict, repr=False)
    explicitly_authorized_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        values = dict(self.variables)
        for name, value in values.items():
            if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
                raise ValueError("local process environment variable name is invalid")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("local process environment variable value is invalid")
        authorized = frozenset(self.explicitly_authorized_names)
        if not all(
            isinstance(name, str) and name and "=" not in name and "\x00" not in name
            for name in authorized
        ):
            raise ValueError("explicitly authorized environment variable name is invalid")
        if not authorized.issubset(values):
            raise ValueError(
                "explicitly authorized environment variables must be present in variables"
            )
        object.__setattr__(self, "variables", MappingProxyType(values))
        object.__setattr__(self, "explicitly_authorized_names", authorized)


@dataclass(frozen=True, slots=True)
class LocalProcessLifecycle:
    """Bounded ownership and cancellation requirements for one child.

    The selected platform adapter determines the enforceable descendant
    boundary.  Enabled Linux profiles and Windows Job Objects provide strong
    descendant ownership; the explicit POSIX ``off`` adapter provides only
    best-effort original-process-group cleanup.

    表示一个子进程的有界所有权和取消要求.启用的 Linux profile 与 Windows Job
    Object 提供强后代所有权;显式 POSIX ``off`` 仅尽力清理原进程组.
    """

    cancellation_policy: LocalProcessCancellationPolicy = (
        LocalProcessCancellationPolicy.TERMINATE_PROCESS_TREE
    )
    termination_grace_seconds: float = 1.0
    force_wait_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.cancellation_policy, LocalProcessCancellationPolicy):
            raise TypeError("local process cancellation policy must be canonical")
        if (
            not isinstance(self.termination_grace_seconds, int | float)
            or isinstance(self.termination_grace_seconds, bool)
            or not math.isfinite(self.termination_grace_seconds)
            or self.termination_grace_seconds <= 0
        ):
            raise ValueError("local process termination grace seconds must be positive")
        if (
            not isinstance(self.force_wait_seconds, int | float)
            or isinstance(self.force_wait_seconds, bool)
            or not math.isfinite(self.force_wait_seconds)
            or self.force_wait_seconds <= 0
        ):
            raise ValueError("local process force wait seconds must be positive")


@dataclass(frozen=True, slots=True)
class SandboxedProcessRequest:
    """Validated, platform-neutral request to create one local child process.

    This is the only process-creation input application and infrastructure
    callers should hold.  It intentionally carries policy rather than a raw
    ``ProcessTree`` or subprocess object.

    表示创建一个本地子进程的已验证、平台无关请求.

    这是应用层和基础设施调用方应持有的唯一进程创建输入.它刻意携带策略而不是
    原始 ``ProcessTree`` 或 subprocess 对象.
    """

    purpose: LocalProcessPurpose
    cwd: Path
    sandbox_profile: SandboxProfile
    filesystem_policy: LocalProcessFilesystemPolicy
    network_policy: LocalProcessNetworkPolicy
    environment_policy: LocalProcessEnvironmentPolicy
    stdio_mode: LocalProcessStdioMode
    lifecycle: LocalProcessLifecycle
    executable: str | None = None
    arguments: tuple[str, ...] = ()
    shell_command: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, LocalProcessPurpose):
            raise TypeError("local process purpose must be canonical")
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise ValueError("local process cwd must be an absolute pathlib.Path")
        if not isinstance(self.sandbox_profile, SandboxProfile):
            raise TypeError("local process sandbox profile must be canonical")
        if not isinstance(self.filesystem_policy, LocalProcessFilesystemPolicy):
            raise TypeError("local process filesystem policy must be canonical")
        if not isinstance(self.network_policy, LocalProcessNetworkPolicy):
            raise TypeError("local process network policy must be canonical")
        if not isinstance(self.environment_policy, LocalProcessEnvironmentPolicy):
            raise TypeError("local process environment policy must be canonical")
        if not isinstance(self.stdio_mode, LocalProcessStdioMode):
            raise TypeError("local process stdio mode must be canonical")
        if not isinstance(self.lifecycle, LocalProcessLifecycle):
            raise TypeError("local process lifecycle must be canonical")
        if not isinstance(self.arguments, tuple) or not all(
            isinstance(argument, str) and "\x00" not in argument for argument in self.arguments
        ):
            raise ValueError("local process arguments must be strings without null bytes")
        if not any(
            self.cwd == root.path or self.cwd.is_relative_to(root.path)
            for root in self.filesystem_policy.workspace_roots
        ):
            raise ValueError("local process cwd must be inside an authorized workspace root")
        if (
            self.sandbox_profile.restricts_child_network
            and self.network_policy is not LocalProcessNetworkPolicy.ISOLATED
        ):
            raise ValueError("restricted sandbox profiles require isolated child networking")
        if self.sandbox_profile is SandboxProfile.READ_ONLY and any(
            root.mode is not LocalWorkspaceAccessMode.READ_ONLY
            for root in self.filesystem_policy.workspace_roots
        ):
            raise ValueError("read-only sandbox profiles require read-only workspace roots")

        executable = self.executable
        shell_command = self.shell_command
        if (executable is None) == (shell_command is None):
            raise ValueError("local process request requires exactly one command form")
        if executable is not None:
            if not executable or "\x00" in executable:
                raise ValueError("local process executable must be non-empty without null bytes")
            return
        assert shell_command is not None
        if not shell_command.strip() or "\x00" in shell_command:
            raise ValueError("local process shell command must be non-empty without null bytes")
        if self.arguments:
            raise ValueError("local process shell command must not have argv arguments")

    @property
    def uses_shell(self) -> bool:
        """Return whether the request intentionally requires a shell parser.

        返回该请求是否刻意要求 Shell 解析器.
        """

        return self.shell_command is not None

    @classmethod
    def shell(
        cls,
        command: str,
        *,
        purpose: LocalProcessPurpose,
        cwd: Path,
        sandbox_profile: SandboxProfile,
        filesystem_policy: LocalProcessFilesystemPolicy,
        network_policy: LocalProcessNetworkPolicy,
        environment_policy: LocalProcessEnvironmentPolicy,
        stdio_mode: LocalProcessStdioMode,
        lifecycle: LocalProcessLifecycle,
    ) -> SandboxedProcessRequest:
        """Build one validated shell-command request.

        构建一个已验证的 Shell 命令请求.
        """

        return cls(
            purpose=purpose,
            cwd=cwd,
            sandbox_profile=sandbox_profile,
            filesystem_policy=filesystem_policy,
            network_policy=network_policy,
            environment_policy=environment_policy,
            stdio_mode=stdio_mode,
            lifecycle=lifecycle,
            shell_command=command,
        )

    @classmethod
    def exec(
        cls,
        executable: str,
        arguments: Sequence[str] = (),
        *,
        purpose: LocalProcessPurpose,
        cwd: Path,
        sandbox_profile: SandboxProfile,
        filesystem_policy: LocalProcessFilesystemPolicy,
        network_policy: LocalProcessNetworkPolicy,
        environment_policy: LocalProcessEnvironmentPolicy,
        stdio_mode: LocalProcessStdioMode,
        lifecycle: LocalProcessLifecycle,
    ) -> SandboxedProcessRequest:
        """Build one validated argv-safe executable request.

        构建一个已验证且 argv 安全的可执行文件请求.
        """

        return cls(
            purpose=purpose,
            cwd=cwd,
            sandbox_profile=sandbox_profile,
            filesystem_policy=filesystem_policy,
            network_policy=network_policy,
            environment_policy=environment_policy,
            stdio_mode=stdio_mode,
            lifecycle=lifecycle,
            executable=executable,
            arguments=tuple(arguments),
        )


class LocalProcessOutput(Protocol):
    """Minimal asynchronous byte stream exposed by an owned child.

    表示由受管子进程公开的最小异步字节流.
    """

    async def read(self, n: int = -1, /) -> bytes: ...


class OwnedLocalProcess(Protocol):
    """A process tree owned by the canonical local-process sandbox.

    表示由规范本地进程沙箱拥有的进程树.
    """

    @property
    def process_id(self) -> int: ...

    @property
    def stdout(self) -> LocalProcessOutput | None: ...

    @property
    def stderr(self) -> LocalProcessOutput | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def write_stdin(self, data: bytes) -> None: ...

    async def close_stdin(self) -> None: ...

    async def wait(self) -> int: ...

    async def terminate(self, *, grace_seconds: float | None = None) -> None: ...


class LocalProcessSandbox(Protocol):
    """Canonical owner of model-controlled local process creation.

    规范地拥有模型可控本地进程创建的端口.
    """

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess: ...

    def spawn_terminal(
        self,
        request: SandboxedProcessRequest,
        *,
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession: ...
