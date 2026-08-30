"""Canonical ownership boundary for one ACP session runtime.

一个 ACP 会话运行时的规范所有权边界.

The connection-level ACP agent owns protocol routing, capabilities, and the
external session registry.  This module owns only the mutable state and
ephemeral resources that belong to one published ACP session.  It deliberately
does not know about the agent, bootstrap, providers, stores, or transport.
ACP 连接级 Agent 负责协议路由、能力和外部会话注册表;本模块只负责一个已发布
ACP 会话的可变状态和临时资源,且有意不感知 Agent、bootstrap、provider、store 或
transport.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from neuro_code.application.acp.contracts import AcpMcpTools
from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.interfaces.acp.updates import _AcpEventMapper


class AcpSessionRuntimeError(RuntimeError):
    """Base error for a rejected per-session state transition."""


class AcpSessionInactiveError(AcpSessionRuntimeError):
    """The session is already closing or closed."""


class AcpSessionPromptAlreadyActiveError(AcpSessionRuntimeError):
    """The session already has an owned ACP prompt task."""


class AcpSessionApprovalAlreadyPendingError(AcpSessionRuntimeError):
    """The session already has an owned ACP approval interaction."""


class AcpSessionIdentityConflictError(AcpSessionRuntimeError):
    """A session attempted to bind a different internal identity."""


class AcpSessionIdentityUnavailableError(AcpSessionRuntimeError):
    """The session has no internal identity available for the requested use."""


@dataclass(frozen=True, slots=True)
class AcpPromptStart:
    """State captured when one task acquires the ACP prompt gate."""

    binding: ConversationBinding
    context_window_tokens: int | None
    internal_session_id: str | None


class AcpSessionRuntime:
    """Own one published ACP session's state, resources, and cleanup.

    The application runner remains the authority for actual turn execution and
    durable recovery.  This runtime only coordinates the ACP-facing prompt,
    approval, cancellation, and resource lifecycle state around that runner.
    """

    __slots__ = (
        "_approvals",
        "_binding",
        "_cancel_requested",
        "_cleanup_lock",
        "_client_terminal",
        "_closed",
        "_closing",
        "_context_window_tokens",
        "_internal_session_id",
        "_mapper",
        "_mcp_tool_names",
        "_mcp_tools",
        "_pending_approval_id",
        "_pending_identity",
        "_prompt_task",
        "_session_id",
        "_state_lock",
    )

    def __init__(
        self,
        session_id: str,
        binding: ConversationBinding | None,
        approvals: SessionApprovalBroker,
        context_window_tokens: int | None,
        mcp_tools: AcpMcpTools | None,
        mcp_tool_names: tuple[str, ...] = (),
        client_terminal: ClientTerminal | None = None,
        internal_session_id: str | None = None,
        prompt_task: asyncio.Task[Any] | None = None,
        mapper: _AcpEventMapper | None = None,
        pending_approval_id: str | None = None,
        cancel_requested: bool = False,
        closing: bool = False,
        closed: bool = False,
        state_lock: asyncio.Lock | None = None,
        cleanup_lock: asyncio.Lock | None = None,
    ) -> None:
        """Initialize a session candidate before registry publication.

        The resource references are supplied by the Agent's construction
        rollback path.  They become runtime-owned when that candidate is
        successfully published in the Agent registry.
        """

        self._session_id = session_id
        self._binding = binding
        self._approvals = approvals
        self._context_window_tokens = context_window_tokens
        self._mcp_tools = mcp_tools
        self._mcp_tool_names = tuple(mcp_tool_names)
        self._client_terminal = client_terminal
        self._internal_session_id = internal_session_id
        self._pending_identity: tuple[str, object] | None = None
        self._prompt_task = prompt_task
        self._mapper = mapper
        self._pending_approval_id = pending_approval_id
        self._cancel_requested = cancel_requested
        self._closing = closing
        self._closed = closed
        self._state_lock = state_lock or asyncio.Lock()
        self._cleanup_lock = cleanup_lock or asyncio.Lock()

    @property
    def session_id(self) -> str:
        """Return the immutable external ACP session identity."""

        return self._session_id

    @property
    def binding(self) -> ConversationBinding | None:
        """Read-only private compatibility view for existing ACP tests."""

        return self._binding

    @property
    def approvals(self) -> SessionApprovalBroker:
        """Return the application-owned approval broker reference."""

        return self._approvals

    @property
    def context_window_tokens(self) -> int | None:
        """Return the immutable context-window snapshot for this session."""

        return self._context_window_tokens

    @property
    def internal_session_id(self) -> str | None:
        """Read-only private compatibility view of the internal identity.

        Runtime consumers that need a synchronized value must use the async
        identity methods below.  This view exists only for legacy private
        inspection compatibility and is never assigned by the Agent.
        """

        return self._internal_session_id

    @property
    def mcp_tool_names(self) -> tuple[str, ...]:
        """Read-only private compatibility view of the MCP name snapshot."""

        return self._mcp_tool_names

    async def is_active(self) -> bool:
        """Return whether the runtime can still accept a new operation."""

        async with self._state_lock:
            return not self._closing and not self._closed

    async def active_binding_snapshot(self) -> ConversationBinding | None:
        """Capture the live binding only while the session remains active."""

        async with self._state_lock:
            if self._closing or self._closed:
                return None
            return self._binding

    async def active_internal_session_identity(self) -> str | None:
        """Capture the internal identity or fail closed for inactive state."""

        async with self._state_lock:
            if self._closing or self._closed:
                raise AcpSessionInactiveError("ACP session is not active")
            return self._internal_session_id

    async def fork_source_identity(self) -> str:
        """Capture a forkable identity while holding the session state lock."""

        async with self._state_lock:
            if self._closing or self._closed:
                raise AcpSessionInactiveError("ACP session is not active")
            task = self._prompt_task
            if task is not None and not task.done():
                raise AcpSessionPromptAlreadyActiveError("ACP session prompt is active")
            if self._internal_session_id is None:
                raise AcpSessionIdentityUnavailableError(
                    "ACP session internal identity is unavailable"
                )
            return self._internal_session_id

    async def prompt_context(self) -> tuple[int | None, str | None]:
        """Capture prompt mapper context without exposing mutable state."""

        async with self._state_lock:
            if self._closing or self._closed or self._binding is None:
                raise AcpSessionInactiveError("ACP session is not active")
            return self._context_window_tokens, self._internal_session_id

    async def begin_prompt(
        self,
        task: asyncio.Task[Any],
        mapper: _AcpEventMapper,
    ) -> AcpPromptStart:
        """Atomically acquire the one-prompt interface gate."""

        async with self._state_lock:
            if self._closing or self._closed or self._binding is None:
                raise AcpSessionInactiveError("ACP session is not active")
            if self._prompt_task is not None:
                raise AcpSessionPromptAlreadyActiveError("ACP session prompt is active")
            self._prompt_task = task
            self._mapper = mapper
            self._cancel_requested = False
            return AcpPromptStart(
                binding=self._binding,
                context_window_tokens=self._context_window_tokens,
                internal_session_id=self._internal_session_id,
            )

    async def finish_prompt_if_owner(self, task: asyncio.Task[Any]) -> bool:
        """Clear prompt state only when ``task`` is still its owner."""

        async with self._state_lock:
            if self._prompt_task is not task:
                return False
            self._prompt_task = None
            self._mapper = None
            self._pending_approval_id = None
            self._cancel_requested = False
            return True

    async def prompt_should_stop(self) -> bool:
        """Return the cancellation/closing state observed by a prompt."""

        async with self._state_lock:
            return self._cancel_requested or self._closing or self._closed

    async def request_cancel(self) -> asyncio.Task[Any] | None:
        """Mark and return only the current live prompt task for cancellation."""

        async with self._state_lock:
            if self._closing or self._closed:
                return None
            task = self._prompt_task
            if task is None or task.done():
                return None
            self._cancel_requested = True
            return task

    async def begin_approval(self, call_id: str) -> _AcpEventMapper | None:
        """Claim one ACP approval presentation for the current mapper."""

        async with self._state_lock:
            if self._closing or self._closed or self._mapper is None:
                return None
            if self._pending_approval_id is not None:
                raise AcpSessionApprovalAlreadyPendingError(
                    "another ACP approval is already pending"
                )
            self._pending_approval_id = call_id
            return self._mapper

    async def finish_approval_if_owner(self, call_id: str) -> bool:
        """Clear only the approval interaction identified by ``call_id``."""

        async with self._state_lock:
            if self._pending_approval_id != call_id:
                return False
            self._pending_approval_id = None
            return True

    async def begin_close(self) -> tuple[bool, str | None]:
        """Mark a live session closing and capture its current identity."""

        async with self._state_lock:
            if self._closed or self._closing:
                return False, self._internal_session_id
            self._closing = True
            self._cancel_requested = True
            return True, self._internal_session_id

    async def mark_closing(self) -> None:
        """Mark shutdown cancellation before aggregate cleanup begins."""

        async with self._state_lock:
            if not self._closed:
                self._closing = True
                self._cancel_requested = True

    async def mcp_snapshot(self) -> tuple[AcpMcpTools, tuple[str, ...]] | None:
        """Capture the active MCP object and immutable name snapshot."""

        async with self._state_lock:
            if self._closing or self._closed or self._mcp_tools is None:
                return None
            return self._mcp_tools, self._mcp_tool_names

    async def update_mcp_tool_names(
        self,
        mcp_tools: AcpMcpTools,
        names: tuple[str, ...],
    ) -> bool:
        """Publish refreshed MCP names only for the same live MCP object."""

        async with self._state_lock:
            if self._closing or self._closed or self._mcp_tools is not mcp_tools:
                return False
            self._mcp_tool_names = tuple(names)
            return True

    async def begin_internal_session_identity(self, internal_session_id: str) -> object:
        """Reserve one in-memory identity transition before durable alias I/O."""

        async with self._state_lock:
            if self._closing or self._closed:
                raise AcpSessionInactiveError("ACP session is not active")
            if (
                self._internal_session_id is not None
                and self._internal_session_id != internal_session_id
            ):
                raise AcpSessionIdentityConflictError(
                    "ACP session changed its internal session identity"
                )
            if self._pending_identity is not None:
                pending_id, _token = self._pending_identity
                if pending_id != internal_session_id:
                    raise AcpSessionIdentityConflictError(
                        "ACP session changed its pending internal session identity"
                    )
                raise AcpSessionIdentityConflictError(
                    "ACP session internal identity binding is already pending"
                )
            token = object()
            self._pending_identity = (internal_session_id, token)
            return token

    async def commit_internal_session_identity(
        self,
        internal_session_id: str,
        token: object,
    ) -> None:
        """Commit a reserved in-memory identity after the alias write."""

        async with self._state_lock:
            if self._closing or self._closed:
                raise AcpSessionInactiveError("ACP session is not active")
            if self._pending_identity != (internal_session_id, token):
                raise AcpSessionIdentityConflictError(
                    "ACP session internal identity binding is no longer owned"
                )
            if (
                self._internal_session_id is not None
                and self._internal_session_id != internal_session_id
            ):
                raise AcpSessionIdentityConflictError(
                    "ACP session changed its internal session identity"
                )
            self._internal_session_id = internal_session_id
            self._pending_identity = None

    async def abort_internal_session_identity(self, token: object) -> None:
        """Release a failed durable identity transition owned by ``token``."""

        async with self._state_lock:
            if self._pending_identity is not None and self._pending_identity[1] is token:
                self._pending_identity = None

    async def cleanup(self) -> None:
        """Close prompt, MCP, terminal, and binding resources exactly once.

        Cleanup order and failure propagation intentionally retain the proven
        ACP behavior.  ``ConversationBinding.close`` remains the sole binding
        resource authority; failures are not converted into a new aggregation
        framework in this structural boundary slice.
        """

        await self.mark_closing()
        async with self._cleanup_lock:
            async with self._state_lock:
                if self._closed:
                    return
                task = self._prompt_task
            current = asyncio.current_task()
            if task is not None and task is not current and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

            async with self._state_lock:
                binding = self._binding
                mcp_tools = self._mcp_tools
                client_terminal = self._client_terminal

            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if client_terminal is not None:
                await asyncio.shield(client_terminal.shutdown())
            if binding is not None:
                await asyncio.shield(binding.close())

            async with self._state_lock:
                self._binding = None
                self._mcp_tools = None
                self._client_terminal = None
                self._mapper = None
                self._pending_approval_id = None
                self._pending_identity = None
                self._closed = True
