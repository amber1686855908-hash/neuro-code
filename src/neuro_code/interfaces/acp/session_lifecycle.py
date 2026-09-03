"""ACP session construction, persistence activation, and cleanup lifecycle.

ACP 会话创建、持久化激活和清理生命周期.

The controller coordinates application-service calls with the connection
registry.  A published session still owns its mutable state and resource
cleanup through :class:`AcpSessionRuntime`; this module owns only the
connection-level construction and lifecycle transitions.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from acp.exceptions import RequestError
from acp.schema import (
    CloseSessionResponse,
    DeleteSessionResponse,
    ForkSessionResponse,
    LoadSessionResponse,
    NewSessionResponse,
    ResumeSessionResponse,
)

from neuro_code.application.acp.contracts import (
    AcpMcpServerConfig,
    AcpMcpToolError,
    AcpMcpTools,
    AcpResumeUnavailableError,
    AcpWorkspaceValidationError,
)
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.interfaces.acp.errors import (
    invalid_params as _invalid_params,
)
from neuro_code.interfaces.acp.errors import (
    session_busy as _session_busy,
)
from neuro_code.interfaces.acp.errors import (
    session_not_active as _session_not_active,
)
from neuro_code.interfaces.acp.errors import (
    session_not_found as _session_not_found,
)
from neuro_code.interfaces.acp.errors import (
    validated_session_id as _validated_session_id,
)
from neuro_code.interfaces.acp.mcp import AcpMcpController
from neuro_code.interfaces.acp.mcp_config import McpServer
from neuro_code.interfaces.acp.negotiation import AcpConnectionState
from neuro_code.interfaces.acp.session import AcpSessionRuntime
from neuro_code.interfaces.acp.session_registry import (
    ACP_SESSION_ALIAS_NAMESPACE,
    AcpSessionRegistry,
)
from neuro_code.interfaces.acp.updates import _history_updates
from neuro_code.shared.errors import ConfigurationError, SessionError, ToolError


class AcpSessionLifecycleController:
    """Own connection-level session creation, activation, and shutdown."""

    __slots__ = (
        "_client_file_system",
        "_client_terminal",
        "_connection",
        "_mcp",
        "_open_mcp_tools",
        "_publish_session",
        "_registry",
        "_release_session_reservation",
        "_request_permission",
        "_reserve_session_id",
        "_service",
    )

    def __init__(
        self,
        service: AcpApplicationService,
        registry: AcpSessionRegistry,
        connection: AcpConnectionState,
        mcp: AcpMcpController,
        *,
        request_permission: Callable[[str, PermissionRequest], Awaitable[PermissionApproval]],
        client_file_system: Callable[[str], ClientFileSystem | None],
        client_terminal: Callable[[str], ClientTerminal | None],
        open_mcp_tools: Callable[[tuple[AcpMcpServerConfig, ...]], Awaitable[AcpMcpTools | None]],
        publish_session: Callable[[AcpSessionRuntime], Awaitable[bool]],
        reserve_session_id: Callable[[str], Awaitable[None]],
        release_session_reservation: Callable[[str], Awaitable[None]],
    ) -> None:
        self._service = service
        self._registry = registry
        self._connection = connection
        self._mcp = mcp
        self._request_permission = request_permission
        self._client_file_system = client_file_system
        self._client_terminal = client_terminal
        self._open_mcp_tools = open_mcp_tools
        self._publish_session = publish_session
        self._reserve_session_id = reserve_session_id
        self._release_session_reservation = release_session_reservation

    async def validate_workspace(
        self,
        cwd: str,
        additional_directories: Sequence[str] = (),
    ) -> tuple[Path, ...]:
        try:
            return await self._service.validate_workspace(cwd, additional_directories)
        except AcpWorkspaceValidationError as error:
            raise _invalid_params(error.reason, error.details) from None

    async def _validate_session_workspace(
        self,
        cwd: str,
        additional_directories: list[str] | None,
        mcp_servers: list[McpServer] | None,
    ) -> tuple[tuple[Path, ...], tuple[AcpMcpServerConfig, ...]]:
        additional_workspace_roots = await self.validate_workspace(
            cwd,
            additional_directories or (),
        )
        configurations = self._mcp.server_configurations(mcp_servers)
        return additional_workspace_roots, configurations

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> NewSessionResponse:
        self._connection.require_initialized()
        additional_workspace_roots, mcp_configurations = await self._validate_session_workspace(
            cwd,
            additional_directories,
            mcp_servers,
        )

        session_id = f"acp-{uuid.uuid4().hex}"
        await self._reserve_session_id(session_id)
        approvals = SessionApprovalBroker()
        approvals.set_handler(lambda request: self._request_permission(session_id, request))
        binding: ConversationBinding | None = None
        mcp_tools: AcpMcpTools | None = None
        client_terminal = self._client_terminal(session_id)
        try:
            mcp_tools = await self._open_mcp_tools(mcp_configurations)
            opened_binding = await self._service.create_binding(
                approver=approvals,
                additional_tools=mcp_tools.tools if mcp_tools is not None else (),
                additional_workspace_roots=additional_workspace_roots,
                client_file_system=self._client_file_system(session_id),
                client_terminal=client_terminal,
            )
            binding = opened_binding.binding
        except asyncio.CancelledError:
            raise
        except AcpMcpToolError as error:
            raise _invalid_params(error.reason) from None
        except ToolError:
            raise _invalid_params("mcp_tool_name_collision") from None
        except Exception:
            raise RequestError.internal_error({"reason": "session_creation_failed"}) from None
        else:
            session = AcpSessionRuntime(
                session_id,
                binding,
                approvals,
                opened_binding.context_window_tokens,
                mcp_tools,
                mcp_tool_names=(
                    tuple(tool.definition.name for tool in mcp_tools.tools)
                    if mcp_tools is not None
                    else ()
                ),
                client_terminal=client_terminal,
            )
            if await self._publish_session(session):
                binding = None
                mcp_tools = None
                client_terminal = None
                return NewSessionResponse(session_id=session_id)
            raise RequestError.internal_error({"reason": "connection_closing"})
        finally:
            await self._release_session_reservation(session_id)
            if binding is not None:
                await asyncio.shield(binding.close())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if client_terminal is not None:
                await asyncio.shield(client_terminal.shutdown())

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServer] | None = None,
        additional_directories: list[str] | None = None,
        **_kwargs: Any,
    ) -> LoadSessionResponse:
        self._connection.require_initialized()
        additional_workspace_roots, mcp_configurations = await self._validate_session_workspace(
            cwd,
            additional_directories,
            mcp_servers,
        )
        external_session_id = _validated_session_id(session_id)
        await self._activate_persisted_session(
            external_session_id,
            mcp_configurations,
            additional_workspace_roots,
            replay_history=True,
            failure_reason="session_load_failed",
        )
        return LoadSessionResponse()

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> ResumeSessionResponse:
        self._connection.require_initialized()
        additional_workspace_roots, mcp_configurations = await self._validate_session_workspace(
            cwd,
            additional_directories,
            mcp_servers,
        )
        external_session_id = _validated_session_id(session_id)
        await self._activate_persisted_session(
            external_session_id,
            mcp_configurations,
            additional_workspace_roots,
            replay_history=False,
            failure_reason="session_resume_failed",
        )
        return ResumeSessionResponse()

    async def _activate_persisted_session(
        self,
        external_session_id: str,
        mcp_configurations: tuple[AcpMcpServerConfig, ...],
        additional_workspace_roots: tuple[Path, ...],
        *,
        replay_history: bool,
        failure_reason: str,
    ) -> None:
        client = self._connection.client
        if replay_history and client is None:
            raise RequestError.internal_error({"reason": "client_unavailable"})
        await self._reserve_session_id(external_session_id)
        binding: ConversationBinding | None = None
        mcp_tools: AcpMcpTools | None = None
        client_terminal = self._client_terminal(external_session_id)
        try:
            try:
                internal_session_id = await self._service.resolve_session_alias(
                    ACP_SESSION_ALIAS_NAMESPACE,
                    external_session_id,
                )
            except SessionError:
                raise _session_not_found(external_session_id) from None

            try:
                prepared_session = await self._service.prepare_session_resume(internal_session_id)
            except AcpResumeUnavailableError as error:
                raise _invalid_params(error.reason) from None

            approvals = SessionApprovalBroker()
            approvals.set_handler(
                lambda request: self._request_permission(external_session_id, request)
            )
            try:
                mcp_tools = await self._open_mcp_tools(mcp_configurations)
                binding = await prepared_session.create_binding(
                    approver=approvals,
                    additional_tools=mcp_tools.tools if mcp_tools is not None else (),
                    additional_workspace_roots=additional_workspace_roots,
                    client_file_system=self._client_file_system(external_session_id),
                    client_terminal=client_terminal,
                )
            except asyncio.CancelledError:
                raise
            except AcpMcpToolError as error:
                raise _invalid_params(error.reason) from None
            except ToolError:
                raise _invalid_params("mcp_tool_name_collision") from None
            except ConfigurationError:
                raise _invalid_params("session_provider_unavailable") from None
            except Exception:
                raise RequestError.internal_error({"reason": failure_reason}) from None

            if binding.runner.session_id != internal_session_id:
                raise RequestError.internal_error({"reason": "session_identity_mismatch"})
            try:
                await self._service.bind_session_alias(
                    ACP_SESSION_ALIAS_NAMESPACE,
                    external_session_id,
                    internal_session_id,
                )
            except SessionError:
                raise RequestError.internal_error({"reason": "session_alias_failed"}) from None
            if replay_history:
                assert client is not None
                updates = _history_updates(
                    binding.runner.items,
                    explicit_redactions=self._connection.explicit_redactions(),
                )
                try:
                    for update in updates:
                        await client.session_update(external_session_id, update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RequestError.internal_error(
                        {"reason": "session_history_replay_failed"}
                    ) from None

            session = AcpSessionRuntime(
                external_session_id,
                binding,
                approvals,
                prepared_session.context_window_tokens,
                mcp_tools,
                mcp_tool_names=(
                    tuple(tool.definition.name for tool in mcp_tools.tools)
                    if mcp_tools is not None
                    else ()
                ),
                client_terminal=client_terminal,
                internal_session_id=internal_session_id,
            )
            if await self._publish_session(session):
                binding = None
                mcp_tools = None
                client_terminal = None
                return
            raise RequestError.internal_error({"reason": "connection_closing"})
        finally:
            await self._release_session_reservation(external_session_id)
            if binding is not None:
                await asyncio.shield(binding.close())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if client_terminal is not None:
                await asyncio.shield(client_terminal.shutdown())

    async def delete_session(
        self,
        session_id: str,
        **_kwargs: Any,
    ) -> DeleteSessionResponse:
        self._connection.require_initialized()
        external_session_id = _validated_session_id(session_id)
        pending, active = await self._registry.delete_snapshot(external_session_id)
        if pending:
            raise _session_busy(external_session_id, "session_creation_in_progress")

        internal_session_id: str | None = None
        if active is not None:
            started, internal_session_id = await active.begin_close()
            if not started:
                raise _session_not_active(external_session_id)
            await self.cleanup_session(active)
            await self._registry.remove_if(external_session_id, active)

        if internal_session_id is None:
            try:
                internal_session_id = await self._service.resolve_session_alias(
                    ACP_SESSION_ALIAS_NAMESPACE,
                    external_session_id,
                )
            except SessionError:
                if active is not None:
                    return DeleteSessionResponse()
                raise _session_not_found(external_session_id) from None

        try:
            await self._service.delete_session(internal_session_id)
        except SessionError:
            raise _session_not_found(external_session_id) from None
        return DeleteSessionResponse()

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> ForkSessionResponse:
        self._connection.require_initialized()
        additional_workspace_roots, mcp_configurations = await self._validate_session_workspace(
            cwd,
            additional_directories,
            mcp_servers,
        )
        source_external_session_id = _validated_session_id(session_id)
        source_internal_session_id = await self._registry.fork_source_session_id(
            source_external_session_id
        )
        forked_external_session_id = f"acp-{uuid.uuid4().hex}"
        await self._reserve_session_id(forked_external_session_id)

        binding: ConversationBinding | None = None
        mcp_tools: AcpMcpTools | None = None
        client_terminal = self._client_terminal(forked_external_session_id)
        forked_internal_session_id: str | None = None
        try:
            try:
                forked_internal_session_id = await self._service.fork_session(
                    source_internal_session_id
                )
            except SessionError:
                raise _session_not_found(source_external_session_id) from None

            try:
                prepared_session = await self._service.prepare_session_resume(
                    forked_internal_session_id
                )
            except AcpResumeUnavailableError as error:
                raise _invalid_params(error.reason) from None

            approvals = SessionApprovalBroker()
            approvals.set_handler(
                lambda request: self._request_permission(
                    forked_external_session_id,
                    request,
                )
            )
            try:
                mcp_tools = await self._open_mcp_tools(mcp_configurations)
                binding = await prepared_session.create_binding(
                    approver=approvals,
                    additional_tools=mcp_tools.tools if mcp_tools is not None else (),
                    additional_workspace_roots=additional_workspace_roots,
                    client_file_system=self._client_file_system(forked_external_session_id),
                    client_terminal=client_terminal,
                )
            except asyncio.CancelledError:
                raise
            except AcpMcpToolError as error:
                raise _invalid_params(error.reason) from None
            except ToolError:
                raise _invalid_params("mcp_tool_name_collision") from None
            except ConfigurationError:
                raise _invalid_params("session_provider_unavailable") from None
            except Exception:
                raise RequestError.internal_error({"reason": "session_fork_failed"}) from None

            if binding.runner.session_id != forked_internal_session_id:
                raise RequestError.internal_error({"reason": "session_identity_mismatch"})
            try:
                await self._service.bind_session_alias(
                    ACP_SESSION_ALIAS_NAMESPACE,
                    forked_external_session_id,
                    forked_internal_session_id,
                )
            except SessionError:
                raise RequestError.internal_error({"reason": "session_alias_failed"}) from None

            session = AcpSessionRuntime(
                forked_external_session_id,
                binding,
                approvals,
                prepared_session.context_window_tokens,
                mcp_tools,
                mcp_tool_names=(
                    tuple(tool.definition.name for tool in mcp_tools.tools)
                    if mcp_tools is not None
                    else ()
                ),
                client_terminal=client_terminal,
                internal_session_id=forked_internal_session_id,
            )
            if await self._publish_session(session):
                binding = None
                mcp_tools = None
                client_terminal = None
                forked_internal_session_id = None
                return ForkSessionResponse(session_id=forked_external_session_id)
            raise RequestError.internal_error({"reason": "connection_closing"})
        finally:
            await self._release_session_reservation(forked_external_session_id)
            if binding is not None:
                await asyncio.shield(binding.close())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if client_terminal is not None:
                await asyncio.shield(client_terminal.shutdown())
            if forked_internal_session_id is not None:
                with contextlib.suppress(SessionError):
                    await asyncio.shield(self._service.delete_session(forked_internal_session_id))

    async def cleanup_session(self, session: AcpSessionRuntime) -> None:
        await session.cleanup()

    async def close_session(
        self,
        session_id: str,
        **_kwargs: Any,
    ) -> CloseSessionResponse:
        self._connection.require_initialized()
        session = await self._registry.lookup(session_id)
        if session is None:
            raise _session_not_active(session_id)
        started, _internal_session_id = await session.begin_close()
        if not started:
            raise _session_not_active(session_id)
        await self.cleanup_session(session)
        await self._registry.remove_if(session_id, session)
        return CloseSessionResponse()

    async def shutdown(self) -> None:
        sessions, pending_tasks = await self._registry.begin_shutdown()
        await self._registry.clear_cursors()
        current = asyncio.current_task()
        for task in pending_tasks:
            if task is not current and not task.done():
                task.cancel()
        for session in sessions:
            await session.mark_closing()
        if sessions:
            await asyncio.gather(
                *(self.cleanup_session(session) for session in sessions),
                return_exceptions=True,
            )
        if pending_tasks:
            await asyncio.gather(
                *(task for task in pending_tasks if task is not current),
                return_exceptions=True,
            )


__all__ = ["AcpSessionLifecycleController"]
