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


class LocalProcessLifecycleCapability(StrEnum):
    """Descendant-lifecycle strength provided or required by a local child.

    Lifecycle strength is deliberately independent from filesystem and network
    policy.  A strong owner may satisfy a best-effort requirement, but a
    process-group owner must never be presented as a strong descendant owner.

    表示本地子进程提供或要求的后代生命周期强度.

    生命周期强度刻意独立于文件系统和网络策略.强所有者可以满足尽力而为要求,
    但进程组所有者绝不能被描述为强后代所有者.
    """

    STRONG_DESCENDANT_OWNERSHIP = "strong-descendant-ownership"
    PROCESS_GROUP_BEST_EFFORT = "process-group-best-effort"


_LIFECYCLE_CAPABILITY_STRENGTH = {
    LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT: 0,
    LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP: 1,
}


def lifecycle_capability_satisfies(
    provided: LocalProcessLifecycleCapability,
    required: LocalProcessLifecycleCapability,
) -> bool:
    """Return whether ``provided`` meets ``required`` without string ordering.

    唯一的生命周期 capability 满足性判断入口,不依赖 StrEnum 字符串排序.
    """

    if not isinstance(provided, LocalProcessLifecycleCapability):
        raise TypeError("provided lifecycle capability must be canonical")
    if not isinstance(required, LocalProcessLifecycleCapability):
        raise TypeError("required lifecycle capability must be canonical")
    return _LIFECYCLE_CAPABILITY_STRENGTH[provided] >= _LIFECYCLE_CAPABILITY_STRENGTH[required]


class LocalProcessSecurityCapability(StrEnum):
    """Independent security dimensions provided or required by a local child.

    Security authority is deliberately separate from lifecycle ownership.  The
    dimensions are named capabilities rather than a single platform label so a
    backend can expose, for example, strong write isolation while its read
    isolation remains limited during a compatibility-oriented rollout.

    表示本地子进程提供或要求的独立安全维度.

    安全权限刻意与生命周期所有权分离.这些维度是命名 capability,而不是单一
    平台标签,因此某个 backend 可以在兼容性 rollout 期间提供 strong write isolation,
    同时明确表示 read isolation 仍然 limited.
    """

    READ_ISOLATION = "read-isolation"
    WRITE_ISOLATION = "write-isolation"
    NETWORK_ISOLATION = "network-isolation"


class LocalProcessSecurityStrength(StrEnum):
    """Strength of one independent local-process security capability.

    Security authority has three levels.  Process lifecycle ownership remains
    a separate contract in ``LocalProcessLifecycleCapability``; it is not
    represented here and has no relationship to this ordering.

    表示一个独立本地进程安全 capability 的强度.
    """

    STRONG = "strong"
    LIMITED = "limited"
    UNSUPPORTED = "unsupported"


_SECURITY_STRENGTH = {
    LocalProcessSecurityStrength.UNSUPPORTED: 0,
    LocalProcessSecurityStrength.LIMITED: 1,
    LocalProcessSecurityStrength.STRONG: 2,
}


@dataclass(frozen=True, slots=True)
class LocalProcessSecurityCapabilities:
    """Actual or required strengths across the local-process security axes.

    The default value is ``UNSUPPORTED`` so a requirement can name only the
    axes it needs.  ``security_capability_satisfies`` checks every axis and
    fails closed when a required strong read contract is supplied by a limited
    backend.

    表示本地进程安全轴上的实际或要求强度.
    """

    read_isolation: LocalProcessSecurityStrength = LocalProcessSecurityStrength.UNSUPPORTED
    write_isolation: LocalProcessSecurityStrength = LocalProcessSecurityStrength.UNSUPPORTED
    network_isolation: LocalProcessSecurityStrength = LocalProcessSecurityStrength.UNSUPPORTED

    def __post_init__(self) -> None:
        if not all(
            isinstance(strength, LocalProcessSecurityStrength)
            for strength in (
                self.read_isolation,
                self.write_isolation,
                self.network_isolation,
            )
        ):
            raise TypeError("local process security strengths must be canonical")

    def strength_for(
        self,
        capability: LocalProcessSecurityCapability,
    ) -> LocalProcessSecurityStrength:
        """Return the strength for one named capability axis."""

        if not isinstance(capability, LocalProcessSecurityCapability):
            raise TypeError("local process security capability must be canonical")
        return {
            LocalProcessSecurityCapability.READ_ISOLATION: self.read_isolation,
            LocalProcessSecurityCapability.WRITE_ISOLATION: self.write_isolation,
            LocalProcessSecurityCapability.NETWORK_ISOLATION: self.network_isolation,
        }[capability]


def security_capability_satisfies(
    provided: LocalProcessSecurityCapabilities,
    required: LocalProcessSecurityCapabilities,
) -> bool:
    """Return whether every provided security axis meets its requirement.

    This helper is intentionally explicit and independent from
    ``lifecycle_capability_satisfies``.  A limited provider can never silently
    satisfy a strong requirement, and unsupported authority satisfies only an
    unsupported/no requirement axis.
    """

    if not isinstance(provided, LocalProcessSecurityCapabilities):
        raise TypeError("provided local process security capabilities must be canonical")
    if not isinstance(required, LocalProcessSecurityCapabilities):
        raise TypeError("required local process security capabilities must be canonical")
    return all(
        _SECURITY_STRENGTH[provided_strength] >= _SECURITY_STRENGTH[required_strength]
        for provided_strength, required_strength in zip(
            (
                provided.read_isolation,
                provided.write_isolation,
                provided.network_isolation,
            ),
            (
                required.read_isolation,
                required.write_isolation,
                required.network_isolation,
            ),
            strict=True,
        )
    )


class LocalProcessCancellationPolicy(StrEnum):
    """Cancellation semantics owned by a local process launcher.

    表示由本地进程启动器拥有的取消语义.
    """

    TERMINATE_OWNED_SCOPE = "terminate-owned-scope"
    # Deprecated compatibility member.  The canonical name above is used by
    # production code and documentation; the termination algorithm is
    # intentionally unchanged.
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
        try:
            canonical_path = self.path.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ValueError("local workspace access path must be resolvable") from error
        object.__setattr__(self, "path", canonical_path)
        if canonical_path == Path("/"):
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

    ``required_capability`` is the minimum contract requested by the caller.
    The selected adapter reports its actual capability separately.  Ordinary
    Neuro Code local workloads require only process-group best effort, while a
    future workload may explicitly require strong descendant ownership.

    表示一个子进程的有界所有权和取消要求. ``required_capability`` 是调用方
    请求的最低 contract;适配器会单独报告实际能力.普通 Neuro Code 本地 workload
    只要求进程组尽力而为,未来 workload 才应显式要求强后代所有权.
    """

    cancellation_policy: LocalProcessCancellationPolicy = (
        LocalProcessCancellationPolicy.TERMINATE_OWNED_SCOPE
    )
    termination_grace_seconds: float = 1.0
    force_wait_seconds: float = 5.0
    # Kept after the pre-existing positional fields so older callers that
    # construct ``LocalProcessLifecycle`` positionally retain their meaning.
    required_capability: LocalProcessLifecycleCapability = (
        LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT
    )

    def __post_init__(self) -> None:
        if not isinstance(self.required_capability, LocalProcessLifecycleCapability):
            raise TypeError("local process required lifecycle capability must be canonical")
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
        try:
            canonical_cwd = self.cwd.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ValueError("local process cwd must be resolvable") from error
        object.__setattr__(self, "cwd", canonical_cwd)
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
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability: ...

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

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability: ...

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
