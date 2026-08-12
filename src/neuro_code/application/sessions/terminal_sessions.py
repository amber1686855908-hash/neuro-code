"""Application-owned bounded interactive terminal session lifecycle.

提供由应用层拥有的有界交互式终端会话生命周期."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import shlex
import subprocess
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from neuro_code.application.permissions.contracts import build_permission_request
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
)
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.terminal import TerminalPlatformSession
from neuro_code.application.ports.workspace import WorkspacePathResolver
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.terminal.models import (
    MAX_TERMINAL_OUTPUT_BYTES,
    MAX_TERMINAL_READ_BYTES,
    MAX_TERMINAL_WRITE_BYTES,
    TerminalOutputChunk,
    TerminalSignal,
    TerminalSize,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import PermissionDenied, TerminalError, ToolError

_MAX_READ_WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.01
_FORCED_ENVIRONMENT = {
    "COLORTERM": "truecolor",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "PAGER": "cat",
    "TERM": "xterm-256color",
}


class _TerminalOutputRing:
    """Thread-safe cursor-addressed tail buffer fed by native reader threads.

    提供由原生读取线程写入的线程安全游标定位尾部缓冲区."""

    def __init__(self, capacity: int) -> None:
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 1 <= capacity <= MAX_TERMINAL_OUTPUT_BYTES
        ):
            raise ValueError(
                f"output_capacity must be an integer from 1 to {MAX_TERMINAL_OUTPUT_BYTES}"
            )
        self._capacity = capacity
        self._data = bytearray()
        self._start_offset = 0
        self._next_offset = 0
        self._eof = False
        self._failure: BaseException | None = None
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._eof:
                return
            self._data.extend(data)
            self._next_offset += len(data)
            overflow = len(self._data) - self._capacity
            if overflow > 0:
                del self._data[:overflow]
                self._start_offset += overflow

    def finish(self) -> None:
        with self._lock:
            self._eof = True

    def fail(self, error: BaseException) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error
            self._eof = True

    def read(
        self,
        *,
        after_offset: int,
        max_bytes: int,
    ) -> tuple[TerminalOutputChunk, bool, BaseException | None]:
        with self._lock:
            if after_offset > self._next_offset:
                raise TerminalError(
                    f"terminal output offset {after_offset} exceeds current offset "
                    f"{self._next_offset}"
                )
            dropped = max(0, self._start_offset - after_offset)
            cursor = max(after_offset, self._start_offset)
            start = cursor - self._start_offset
            data = bytes(self._data[start : start + max_bytes])
            next_offset = cursor + len(data)
            eof = self._eof and next_offset >= self._next_offset
            chunk = TerminalOutputChunk(data, next_offset, dropped, eof)
            ready = bool(data) or dropped > 0 or eof
            failure = self._failure if not data and next_offset >= self._next_offset else None
            return chunk, ready, failure


class LocalInteractiveTerminalSession:
    """Async application session over one synchronously owned native PTY.

    提供建立在一个同步拥有的原生 PTY 之上的异步应用会话."""

    def __init__(
        self,
        *,
        session_id: str,
        platform_session: TerminalPlatformSession,
        size: TerminalSize,
        output: _TerminalOutputRing,
        on_close: Callable[[str], Awaitable[None]],
    ) -> None:
        self._session_id = session_id
        self._platform_session = platform_session
        self._size = size
        self._output = output
        self._on_close = on_close
        self._operation_lock = asyncio.Lock()
        self._closed = False
        self._exit_code: int | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def process_id(self) -> int:
        return self._platform_session.process_id

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability:
        """Return the capability of the local platform-owned PTY."""

        return self._platform_session.lifecycle_capability

    @property
    def size(self) -> TerminalSize:
        return self._size

    async def read(
        self,
        *,
        after_offset: int = 0,
        max_bytes: int = 65_536,
        wait_seconds: float = 0.0,
    ) -> TerminalOutputChunk:
        if isinstance(after_offset, bool) or not isinstance(after_offset, int) or after_offset < 0:
            raise TerminalError("after_offset must be a non-negative integer")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_TERMINAL_READ_BYTES
        ):
            raise TerminalError(f"max_bytes must be an integer from 1 to {MAX_TERMINAL_READ_BYTES}")
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, int | float)
            or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= _MAX_READ_WAIT_SECONDS
        ):
            raise TerminalError(
                f"wait_seconds must be finite and between 0 and {_MAX_READ_WAIT_SECONDS:g}"
            )

        deadline = asyncio.get_running_loop().time() + float(wait_seconds)
        while True:
            chunk, ready, failure = self._output.read(
                after_offset=after_offset,
                max_bytes=max_bytes,
            )
            if failure is not None:
                raise TerminalError("terminal output stream failed") from failure
            if ready or asyncio.get_running_loop().time() >= deadline:
                return chunk
            await asyncio.sleep(
                min(_POLL_SECONDS, max(0.0, deadline - asyncio.get_running_loop().time()))
            )

    async def write(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TerminalError("terminal input must be bytes")
        if len(data) > MAX_TERMINAL_WRITE_BYTES:
            raise TerminalError(
                f"terminal input cannot exceed {MAX_TERMINAL_WRITE_BYTES} bytes per write"
            )
        if not data:
            return
        async with self._operation_lock:
            self._ensure_open()
            await self._run_platform(self._platform_session.write, data)

    async def resize(self, size: TerminalSize) -> None:
        if not isinstance(size, TerminalSize):
            raise TerminalError("size must be a TerminalSize")
        async with self._operation_lock:
            self._ensure_open()
            await self._run_platform(self._platform_session.resize, size)
            self._size = size

    async def send_signal(self, signal: TerminalSignal) -> None:
        if not isinstance(signal, TerminalSignal):
            raise TerminalError("signal must be a TerminalSignal")
        async with self._operation_lock:
            self._ensure_open()
            await self._run_platform(self._platform_session.send_signal, signal)

    async def wait(self, *, timeout_seconds: float | None = None) -> int | None:
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise TerminalError("timeout_seconds must be finite and not negative")
        if self._exit_code is not None:
            return self._exit_code

        deadline = (
            None
            if timeout_seconds is None
            else asyncio.get_running_loop().time() + float(timeout_seconds)
        )
        while True:
            try:
                exit_code = self._platform_session.poll_exit()
            except BaseException as error:
                raise TerminalError("could not inspect terminal process state") from error
            if exit_code is not None:
                self._exit_code = exit_code
                return exit_code
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(
                _POLL_SECONDS
                if deadline is None
                else min(
                    _POLL_SECONDS,
                    max(0.0, deadline - asyncio.get_running_loop().time()),
                )
            )

    async def close(self) -> None:
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            failure: BaseException | None = None
            try:
                await self._run_platform(self._platform_session.close)
            except BaseException as error:
                failure = error
            with contextlib.suppress(BaseException):
                self._exit_code = self._platform_session.poll_exit()
            self._output.finish()
        await self._on_close(self._session_id)
        if failure is not None:
            raise failure

    def _ensure_open(self) -> None:
        if self._closed:
            raise TerminalError("interactive terminal session is closed")

    @staticmethod
    async def _run_platform[**P, R](
        function: Callable[P, R],
        *arguments: P.args,
        **keywords: P.kwargs,
    ) -> R:
        operation = asyncio.create_task(run_blocking(function, *arguments, **keywords))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await operation
            raise
        except TerminalError:
            raise
        except BaseException as error:
            raise TerminalError("interactive terminal platform operation failed") from error


class LocalInteractiveTerminalManager:
    """Create and own bounded terminal sessions behind application policy ports.

    通过应用策略端口创建并管理有界终端会话."""

    def __init__(
        self,
        *,
        workspace: Path,
        workspace_path_resolver: WorkspacePathResolver,
        permissions: PermissionManager,
        approver: PermissionApprover | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
        local_process_sandbox: LocalProcessSandbox,
        protected_environment_variables: frozenset[str] = frozenset(),
        max_sessions: int = 8,
    ) -> None:
        if not isinstance(workspace, Path):
            raise TypeError("workspace must be a pathlib.Path")
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int) or max_sessions <= 0:
            raise ValueError("max_sessions must be a positive integer")
        self._workspace = workspace.expanduser().resolve()
        self._workspace_path_resolver = workspace_path_resolver
        self._permissions = permissions
        self._approver = approver
        self._sandbox_profile = sandbox_profile
        self._local_process_sandbox = local_process_sandbox
        self._protected_environment = {name.casefold() for name in protected_environment_variables}
        self._max_sessions = max_sessions
        self._sessions: dict[str, LocalInteractiveTerminalSession] = {}
        self._pending_creations = 0
        self._pending_done = asyncio.Event()
        self._pending_done.set()
        self._registry_lock = asyncio.Lock()
        self._closed = False

    async def create_exec(
        self,
        call_id: str,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        size: TerminalSize,
        output_capacity: int,
    ) -> LocalInteractiveTerminalSession:
        argv = _validated_argv(executable, arguments)
        if not isinstance(call_id, str) or not call_id or "\x00" in call_id:
            raise TerminalError("call_id must be a non-empty string without null bytes")
        if not isinstance(size, TerminalSize):
            raise TerminalError("size must be a TerminalSize")
        output = _TerminalOutputRing(output_capacity)
        resolved_cwd = self._workspace_path_resolver.resolve_existing(self._workspace, cwd)
        try:
            # A delegated resolver may return a valid path using a different
            # platform spelling (for example macOS /var vs /private/var).
            # Canonicalize it again before building the process request so the
            # cwd and authorized root share one filesystem identity.
            # 委托 resolver 可能返回使用不同平台写法的有效路径(例如 macOS 的
            # /var 与 /private/var).构建进程请求前再次规范化,确保 cwd 与授权根
            # 使用同一个文件系统身份.
            resolved_cwd = resolved_cwd.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise TerminalError("terminal working directory cannot be resolved") from error
        if not (resolved_cwd == self._workspace or resolved_cwd.is_relative_to(self._workspace)):
            raise TerminalError("terminal working directory is outside the workspace")
        if not resolved_cwd.is_dir():
            raise TerminalError(f"terminal working directory is not a directory: {cwd!r}")
        environment = _terminal_environment(env, self._protected_environment)
        display_command = _display_command(argv)

        async with self._registry_lock:
            if self._closed:
                raise TerminalError("interactive terminal manager is closed")
            if len(self._sessions) + self._pending_creations >= self._max_sessions:
                raise TerminalError(
                    f"interactive terminal limit reached ({self._max_sessions} sessions)"
                )
            self._pending_creations += 1
            self._pending_done.clear()

        platform_session: TerminalPlatformSession | None = None
        try:
            permission_arguments = {
                "columns": size.columns,
                "command": display_command,
                "cwd": str(resolved_cwd),
                "environment_fingerprint": _environment_fingerprint(environment),
                "rows": size.rows,
            }
            await self._authorize(call_id, permission_arguments)
            request = self._build_process_request(
                argv,
                resolved_cwd=resolved_cwd,
                environment=environment,
            )

            spawn = asyncio.create_task(
                run_blocking(
                    self._local_process_sandbox.spawn_terminal,
                    request,
                    size=size,
                    on_output=output.append,
                    on_eof=output.finish,
                    on_error=output.fail,
                )
            )
            try:
                platform_session = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    platform_session = await spawn
                raise

            session_id = f"terminal-{uuid.uuid4().hex[:12]}"
            session = LocalInteractiveTerminalSession(
                session_id=session_id,
                platform_session=platform_session,
                size=size,
                output=output,
                on_close=self._remove_session,
            )
            close_after_registration = await self._register_session(session)
            if close_after_registration:
                await session.close()
                raise TerminalError("interactive terminal manager closed during creation")
            return session
        except asyncio.CancelledError:
            if platform_session is not None:
                with contextlib.suppress(BaseException):
                    await LocalInteractiveTerminalSession._run_platform(platform_session.close)
            raise
        except (PermissionDenied, TerminalError, ToolError):
            raise
        except BaseException as error:
            if platform_session is not None:
                with contextlib.suppress(BaseException):
                    await LocalInteractiveTerminalSession._run_platform(platform_session.close)
            raise TerminalError("could not create interactive terminal") from error
        finally:
            async with self._registry_lock:
                self._pending_creations -= 1
                if self._pending_creations == 0:
                    self._pending_done.set()

    async def shutdown(self) -> None:
        async with self._registry_lock:
            if self._closed and not self._sessions:
                return
            self._closed = True
            sessions = tuple(self._sessions.values())
        await self._pending_done.wait()
        results = await asyncio.gather(
            *(session.close() for session in sessions),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise TerminalError("one or more interactive terminals failed to close") from failures[
                0
            ]

    async def _authorize(self, call_id: str, arguments: Mapping[str, object]) -> None:
        decision = self._permissions.decide(
            "create_terminal",
            arguments,
            side_effecting=True,
        )
        if decision.effect is PermissionEffect.DENY:
            raise PermissionDenied(f"interactive terminal denied: {decision.reason}")
        if decision.effect is PermissionEffect.ALLOW:
            return
        if self._approver is None:
            raise PermissionDenied("interactive terminal denied: approval UI is unavailable")
        request = build_permission_request(
            call_id,
            "create_terminal",
            arguments,
            decision.reason,
        )
        approval = await self._approver.request(request)
        if not approval.allowed:
            raise PermissionDenied(f"interactive terminal denied: {approval.reason}")

    def _build_process_request(
        self,
        argv: tuple[str, ...],
        *,
        resolved_cwd: Path,
        environment: Mapping[str, str],
    ) -> SandboxedProcessRequest:
        access_mode = (
            LocalWorkspaceAccessMode.READ_ONLY
            if self._sandbox_profile is SandboxProfile.READ_ONLY
            else LocalWorkspaceAccessMode.READ_WRITE
        )
        return SandboxedProcessRequest.exec(
            argv[0],
            argv[1:],
            purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
            cwd=resolved_cwd,
            sandbox_profile=self._sandbox_profile,
            filesystem_policy=LocalProcessFilesystemPolicy(
                (LocalWorkspaceAccess(self._workspace, access_mode),)
            ),
            network_policy=(
                LocalProcessNetworkPolicy.ISOLATED
                if self._sandbox_profile.restricts_child_network
                else LocalProcessNetworkPolicy.INHERIT
            ),
            environment_policy=LocalProcessEnvironmentPolicy(environment),
            stdio_mode=LocalProcessStdioMode.PTY,
            lifecycle=LocalProcessLifecycle(
                required_capability=LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT,
            ),
        )

    async def _register_session(self, session: LocalInteractiveTerminalSession) -> bool:
        async with self._registry_lock:
            if self._closed:
                return True
            self._sessions[session.session_id] = session
            return False

    async def _remove_session(self, session_id: str) -> None:
        async with self._registry_lock:
            self._sessions.pop(session_id, None)


def _validated_argv(executable: str, arguments: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(executable, str):
        raise TerminalError("executable must be a string")
    if isinstance(arguments, str | bytes):
        raise TerminalError("arguments must be a sequence of strings")
    argv = (executable, *arguments)
    if not executable or any(not isinstance(item, str) for item in argv):
        raise TerminalError("terminal command must contain non-empty string arguments")
    if any("\x00" in item for item in argv):
        raise TerminalError("terminal command must not contain null bytes")
    return argv


def _terminal_environment(
    environment: Mapping[str, str],
    protected_names: set[str],
) -> dict[str, str]:
    if not isinstance(environment, Mapping):
        raise TerminalError("env must be a string mapping")
    forced_names = {name.casefold() for name in _FORCED_ENVIRONMENT}
    result: dict[str, str] = {}
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not name
            or "=" in name
            or "\x00" in name
            or "\x00" in value
        ):
            raise TerminalError("terminal environment contains an invalid name or value")
        normalized = name.casefold()
        if normalized not in protected_names and normalized not in forced_names:
            result[name] = value
    result.update(_FORCED_ENVIRONMENT)
    return result


def _display_command(argv: tuple[str, ...]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _environment_fingerprint(environment: Mapping[str, str]) -> str:
    payload = json.dumps(
        dict(environment),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["LocalInteractiveTerminalManager", "LocalInteractiveTerminalSession"]
