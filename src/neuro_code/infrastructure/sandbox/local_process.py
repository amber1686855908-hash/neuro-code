"""Process-tree implementation of the canonical local-process sandbox port.

The adapter is the only non-PTY owner of ``ProcessTree`` creation during the
PR1 migration.  It preserves the established process-group / Windows Job
ownership semantics while later platform adapters add child-scoped filesystem,
network, and environment isolation.

在 PR1 迁移期间,该适配器是唯一拥有非 PTY ``ProcessTree`` 创建的实现.它保留
既有的进程组 / Windows Job 所有权语义;后续平台适配器会增加子进程范围的文件系统、
网络和环境隔离.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from neuro_code.application.ports.sandbox import (
    LocalProcessOutput,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessStdioMode,
    OwnedLocalProcess,
    SandboxedProcessRequest,
)
from neuro_code.domain.terminal.models import TerminalSize
from neuro_code.infrastructure.sandbox.process_tree import ProcessTree
from neuro_code.shared.errors import SandboxError

if TYPE_CHECKING:
    from neuro_code.application.ports.terminal import (
        TerminalEofHandler,
        TerminalErrorHandler,
        TerminalOutputHandler,
        TerminalPlatform,
        TerminalPlatformSession,
    )


@dataclass(slots=True)
class ProcessTreeOwnedLocalProcess(OwnedLocalProcess):
    """Hide a concrete ``ProcessTree`` behind the canonical owned-process port.

    将具体 ``ProcessTree`` 隐藏在规范的受管进程端口之后.
    """

    _tree: ProcessTree
    _request: SandboxedProcessRequest

    @property
    def process_id(self) -> int:
        """Return the direct child process identifier.

        返回直接子进程标识符.
        """

        return self._tree.process.pid

    @property
    def stdout(self) -> LocalProcessOutput | None:
        """Return the owned stdout stream, when the selected mode exposes one.

        返回受管 stdout 流(若所选模式公开该流).
        """

        return self._tree.process.stdout

    @property
    def stderr(self) -> LocalProcessOutput | None:
        """Return the owned stderr stream, when it is not merged into stdout.

        返回受管 stderr 流(若未合并到 stdout).
        """

        return self._tree.process.stderr

    @property
    def returncode(self) -> int | None:
        """Return the direct child's current return code, if it has exited.

        返回直接子进程当前退出码(若其已退出).
        """

        return self._tree.process.returncode

    async def write_stdin(self, data: bytes) -> None:
        """Write a protocol frame through the owned stdin pipe.

        通过受管 stdin 管道写入一个协议帧.
        """

        await self._tree.write_stdin(data)

    async def close_stdin(self) -> None:
        """Close the owned stdin pipe once.

        关闭受管 stdin 管道一次.
        """

        await self._tree.close_stdin()

    async def wait(self) -> int:
        """Wait for the process boundary enforced by the selected profile.

        等待所选 profile 能够强制执行的进程边界.
        """

        return await self._tree.wait()

    async def terminate(self, *, grace_seconds: float | None = None) -> None:
        """Terminate the enforceable process boundary using lifecycle bounds.

        使用请求生命周期边界终止能够强制执行的进程边界.
        """

        await self._tree.terminate(
            grace_seconds=(
                self._request.lifecycle.termination_grace_seconds
                if grace_seconds is None
                else grace_seconds
            ),
            force_wait_seconds=self._request.lifecycle.force_wait_seconds,
        )


class ProcessTreeLocalProcessSandbox(LocalProcessSandbox):
    """Bridge canonical requests to the existing owned process-tree adapter.

    This adapter intentionally does *not* claim child-scoped OS isolation or
    ownership of a POSIX descendant that escapes the original process group
    with ``setsid``.
    Enabled profiles therefore reject instead of silently falling back to a
    host process. The Linux child adapter owns enabled pipe/protocol requests;
    PTY requests are routed through the same port to the platform terminal
    adapter.

    将规范请求桥接到现有的受管进程树适配器.

    该适配器刻意不声称提供子进程范围的 OS 隔离.因此启用的 profile 会被拒绝,
    而不是静默回退到宿主进程.Linux 子适配器拥有启用 profile 的管道/协议请求;
    PTY 请求也通过该端口路由到平台终端适配器.
    """

    def __init__(self, *, terminal_platform: TerminalPlatform | None = None) -> None:
        self._terminal_platform = terminal_platform

    async def spawn(self, request: SandboxedProcessRequest) -> OwnedLocalProcess:
        """Create one owned pipe-based child from a validated canonical request.

        从经过验证的规范请求创建一个受管的基于管道子进程.
        """

        if request.sandbox_profile.enabled:
            raise SandboxError(
                "an enabled sandbox profile requires a platform child-sandbox adapter"
            )
        if request.purpose is LocalProcessPurpose.INTERACTIVE_TERMINAL:
            raise SandboxError("interactive terminal requests require spawn_terminal")
        if request.stdio_mode is LocalProcessStdioMode.PTY:
            raise SandboxError("PTY process creation requires a platform terminal sandbox adapter")
        if request.stdio_mode is LocalProcessStdioMode.PROTOCOL and request.uses_shell:
            raise SandboxError("protocol local processes require an argv-safe executable request")

        merge_output = request.stdio_mode is LocalProcessStdioMode.MERGED_CAPTURE
        if request.uses_shell:
            assert request.shell_command is not None
            tree = await ProcessTree.spawn_shell(
                request.shell_command,
                cwd=request.cwd,
                env=request.environment_policy.variables,
                merge_output=merge_output,
            )
        else:
            assert request.executable is not None
            tree = await ProcessTree.spawn_exec(
                request.executable,
                request.arguments,
                cwd=request.cwd,
                env=request.environment_policy.variables,
                merge_output=merge_output,
                pipe_stdin=request.stdio_mode is LocalProcessStdioMode.PROTOCOL,
            )
        return ProcessTreeOwnedLocalProcess(tree, request)

    def spawn_terminal(
        self,
        request: SandboxedProcessRequest,
        *,
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession:
        """Create one local PTY through the canonical process-sandbox port.

        The ``off`` profile still uses the established POSIX PTY or Windows
        ConPTY adapter, while the application layer no longer owns it.

        通过规范本地进程沙箱端口创建一个本地 PTY.

        ``off`` profile 仍使用既有 POSIX PTY 或 Windows ConPTY 适配器,但应用层
        不再拥有该适配器.
        """

        if request.sandbox_profile.enabled:
            raise SandboxError(
                "an enabled sandbox profile requires a platform child-sandbox adapter"
            )
        if request.purpose is not LocalProcessPurpose.INTERACTIVE_TERMINAL:
            raise SandboxError("terminal sandbox requests must use interactive-terminal purpose")
        if request.stdio_mode is not LocalProcessStdioMode.PTY:
            raise SandboxError("interactive terminal requests require PTY stdio")
        if request.uses_shell:
            raise SandboxError("interactive terminal requests require an argv-safe executable")
        assert request.executable is not None
        platform = self._terminal_platform or _default_terminal_platform()
        return platform.spawn_exec(
            request.executable,
            request.arguments,
            cwd=request.cwd,
            env=request.environment_policy.variables,
            size=size,
            on_output=on_output,
            on_eof=on_eof,
            on_error=on_error,
        )


def _default_terminal_platform() -> TerminalPlatform:
    if os.name == "nt":
        from neuro_code.infrastructure.sandbox.windows_pty import WindowsConPtyPlatform

        return WindowsConPtyPlatform()
    if os.name == "posix":
        from neuro_code.infrastructure.sandbox.posix_pty import PosixPtyPlatform

        return PosixPtyPlatform()
    raise SandboxError(f"interactive terminal sandbox is unavailable on {os.name!r}")


__all__ = ["ProcessTreeLocalProcessSandbox", "ProcessTreeOwnedLocalProcess"]
