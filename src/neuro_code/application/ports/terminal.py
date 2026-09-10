"""Canonical interactive-terminal ports.

定义规范的交互式终端端口."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from neuro_code.application.ports.sandbox import LocalProcessLifecycleCapability
from neuro_code.domain.terminal.models import (
    DEFAULT_TERMINAL_OUTPUT_CAPACITY,
    MAX_TERMINAL_DIMENSION,
    TerminalOutputChunk,
    TerminalSignal,
    TerminalSize,
)


class TerminalCreationAuthorization:
    """Opaque, one-use authorization hand-off for model terminal creation.

    Only the application runtime can issue this capability.  It is bound to
    the complete normalized create-terminal request and cannot be constructed
    with a call ID alone.  The manager consumes it once after matching the
    request, so it cannot become a reusable permission bypass.

    模型创建终端的一次性不透明授权交接.只有应用运行时可以签发,并绑定完整规范化的
    创建终端请求,不能只用 call ID 构造.管理器匹配请求后只消费一次,不会成为可复用的
    权限旁路.
    """

    __slots__ = ("_call_id", "_consumed", "_request_key")

    def __init__(
        self,
        *,
        _call_id: str,
        _request_key: tuple[object, ...] | None,
        _issuer: object,
    ) -> None:
        if _issuer is not _TERMINAL_AUTHORIZATION_ISSUER:
            raise TypeError("terminal creation authorization is runtime-issued only")
        self._call_id = _call_id
        self._request_key = _request_key
        self._consumed = False

    @property
    def call_id(self) -> str:
        """Return the call identity carried by this runtime hand-off."""

        return self._call_id

    def _consume(
        self,
        *,
        call_id: str,
        executable: str,
        arguments: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        size: TerminalSize,
        output_capacity: int,
    ) -> bool:
        if self._consumed:
            return False
        request_key = _terminal_creation_request_key(
            call_id,
            executable,
            arguments,
            cwd,
            env,
            size,
            output_capacity,
        )
        if request_key is None or request_key != self._request_key:
            return False
        self._consumed = True
        return True


_TERMINAL_AUTHORIZATION_ISSUER = object()


def _terminal_creation_request_key(
    call_id: object,
    executable: object,
    arguments: object,
    cwd: object,
    env: object,
    size: object,
    output_capacity: object,
) -> tuple[object, ...] | None:
    """Build the bounded immutable request identity used by the hand-off.

    This intentionally validates only the shape needed for identity.  The
    tool and manager remain the owners of their existing detailed validation.
    Invalid raw tool arguments produce an unusable capability and therefore
    fail closed at the manager boundary.
    """

    if (
        not isinstance(call_id, str)
        or not call_id
        or "\x00" in call_id
        or not isinstance(executable, str)
        or not executable
        or "\x00" in executable
        or not isinstance(cwd, str)
        or not cwd
        or "\x00" in cwd
        or not isinstance(arguments, Sequence)
        or isinstance(arguments, str | bytes)
        or any(not isinstance(argument, str) or "\x00" in argument for argument in arguments)
        or not isinstance(env, Mapping)
        or any(
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\x00" in name
            or not isinstance(value, str)
            or "\x00" in value
            for name, value in env.items()
        )
        or not isinstance(size, TerminalSize)
        or isinstance(output_capacity, bool)
        or not isinstance(output_capacity, int)
    ):
        return None
    return (
        call_id,
        executable,
        tuple(arguments),
        cwd,
        tuple(sorted(env.items())),
        size.columns,
        size.rows,
        output_capacity,
    )


def _issue_terminal_creation_authorization(
    call_id: str,
    arguments: Mapping[str, object],
) -> TerminalCreationAuthorization:
    """Issue a capability after the application permission path has allowed it."""

    raw_args = arguments.get("args", ())
    raw_env = arguments.get("env", {})
    columns = arguments.get("columns", 80)
    rows = arguments.get("rows", 24)
    size: TerminalSize | None = None
    if (
        isinstance(columns, int)
        and not isinstance(columns, bool)
        and isinstance(rows, int)
        and not isinstance(rows, bool)
        and 1 <= columns <= MAX_TERMINAL_DIMENSION
        and 1 <= rows <= MAX_TERMINAL_DIMENSION
    ):
        size = TerminalSize(columns, rows)
    request_key = _terminal_creation_request_key(
        call_id,
        arguments.get("command"),
        raw_args,
        arguments.get("cwd", "."),
        raw_env,
        size,
        arguments.get("output_capacity", DEFAULT_TERMINAL_OUTPUT_CAPACITY),
    )
    return TerminalCreationAuthorization(
        _call_id=call_id,
        _request_key=request_key,
        _issuer=_TERMINAL_AUTHORIZATION_ISSUER,
    )


class InteractiveTerminalSession(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def process_id(self) -> int: ...

    @property
    def size(self) -> TerminalSize: ...

    async def read(
        self,
        *,
        after_offset: int = 0,
        max_bytes: int = 65_536,
        wait_seconds: float = 0.0,
    ) -> TerminalOutputChunk: ...

    async def write(self, data: bytes) -> None: ...

    async def resize(self, size: TerminalSize) -> None: ...

    async def send_signal(self, signal: TerminalSignal) -> None: ...

    async def wait(self, *, timeout_seconds: float | None = None) -> int | None: ...

    async def close(self) -> None: ...


class InteractiveTerminalManager(Protocol):
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
        authorization: TerminalCreationAuthorization | None = None,
    ) -> InteractiveTerminalSession: ...

    async def get_session(self, session_id: str) -> InteractiveTerminalSession | None: ...

    async def list_sessions(self) -> tuple[InteractiveTerminalSession, ...]: ...

    async def shutdown(self) -> None: ...


class TerminalPlatformSession(Protocol):
    @property
    def process_id(self) -> int: ...

    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability: ...

    def write(self, data: bytes) -> None: ...

    def resize(self, size: TerminalSize) -> None: ...

    def send_signal(self, signal: TerminalSignal) -> None: ...

    def poll_exit(self) -> int | None: ...

    def close(self) -> None: ...


TerminalOutputHandler = Callable[[bytes], None]
TerminalEofHandler = Callable[[], None]
TerminalErrorHandler = Callable[[BaseException], None]


class TerminalPlatform(Protocol):
    @property
    def lifecycle_capability(self) -> LocalProcessLifecycleCapability: ...

    def spawn_exec(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        size: TerminalSize,
        on_output: TerminalOutputHandler,
        on_eof: TerminalEofHandler,
        on_error: TerminalErrorHandler,
    ) -> TerminalPlatformSession: ...


__all__ = [
    "InteractiveTerminalManager",
    "InteractiveTerminalSession",
    "TerminalCreationAuthorization",
    "TerminalEofHandler",
    "TerminalErrorHandler",
    "TerminalOutputHandler",
    "TerminalPlatform",
    "TerminalPlatformSession",
]
