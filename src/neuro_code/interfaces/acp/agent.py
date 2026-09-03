"""Canonical ACP inbound adapter facade.

规范 ACP 入站适配器 facade.

The facade owns the public SDK entry point and high-level composition for one
connection.  Protocol negotiation, connection registry, live MCP handling,
session lifecycle, private extensions, and prompt execution each live in the
focused controllers imported below.
"""

from __future__ import annotations

import uuid  # noqa: F401 - private compatibility for existing ACP tests
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from acp.interfaces import Client
from acp.schema import (
    ClientCapabilities,
    CloseSessionResponse,
    DeleteSessionResponse,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    ResumeSessionResponse,
)

import neuro_code.interfaces.acp.errors as _acp_errors
import neuro_code.interfaces.acp.extensions as _acp_extensions
import neuro_code.interfaces.acp.serialization as _acp_serialization
import neuro_code.interfaces.acp.session_registry as _acp_session_registry
from neuro_code.application.acp.contracts import (  # noqa: F401 - private compatibility aliases
    MAX_MCP_SERVERS,
    AcpMcpHttpServerConfig,
    AcpMcpQuery,
    AcpMcpQueryError,
    AcpMcpServerConfig,
    AcpMcpStdioServerConfig,
    AcpMcpToolError,
    AcpMcpTools,
    AcpReadOnlySubagentQuery,
    AcpReadOnlySubagentQueryError,
    AcpResumeUnavailableError,
    AcpSessionCommandQuery,
    AcpSubagentLifecycleQuery,
    AcpSubagentLifecycleQueryError,
    AcpToolOutputArtifactQuery,
    AcpToolOutputArtifactQueryError,
    AcpTurnRecoveryQuery,
    AcpWorkspaceValidationError,
)
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import (  # noqa: F401 - compatibility aliases
    MAX_CLIENT_TERMINAL_OUTPUT_BYTES,
    ClientTerminal,
    ClientTerminalResult,
)
from neuro_code.application.ports.tools import (  # noqa: F401 - compatibility aliases
    MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
)
from neuro_code.interfaces.acp.client_io import (  # noqa: F401 - private compatibility aliases
    MAX_CLIENT_FILE_BYTES,
    MAX_CLIENT_TERMINAL_ARGUMENT_BYTES,
    MAX_CLIENT_TERMINAL_ARGUMENT_TOTAL_BYTES,
    MAX_CLIENT_TERMINAL_ARGUMENTS,
    MAX_CLIENT_TERMINAL_COMMAND_BYTES,
    MAX_CLIENT_TERMINAL_ID_BYTES,
    MAX_CLIENT_TERMINAL_RETAINED_TASKS,
    MAX_CLIENT_TERMINAL_SIGNAL_BYTES,
    MAX_CLIENT_TERMINAL_TASKS,
    _AcpClientFileSystem,
    _AcpClientTerminal,
    _AcpClientTerminalTask,
    _client_terminal_background_limits,
    _client_terminal_command,
    _client_terminal_cwd,
    _client_terminal_exit_status,
    _client_terminal_id,
    _client_terminal_limits,
    _client_terminal_task_id,
    _client_terminal_wait_seconds,
)
from neuro_code.interfaces.acp.content import (
    MAX_ANNOTATION_AUDIENCE,
    MAX_ANNOTATION_AUDIENCE_BYTES,
    MAX_ANNOTATIONS_BYTES,
    MAX_AUDIO_BLOCK_BYTES,
    MAX_AUDIO_BLOCKS,
    MAX_AUDIO_TOTAL_BYTES,
    MAX_EMBEDDED_BINARY_RESOURCE_BYTES,
    MAX_EMBEDDED_BINARY_TOTAL_BYTES,
    MAX_EMBEDDED_TEXT_RESOURCE_BYTES,
    MAX_EMBEDDED_TEXT_RESOURCES,
    MAX_EMBEDDED_TEXT_TOTAL_BYTES,
    MAX_IMAGE_BLOCK_BYTES,
    MAX_IMAGE_BLOCKS,
    MAX_IMAGE_TOTAL_BYTES,
    MAX_PROMPT_BLOCKS,
    MAX_PROMPT_BYTES,
    MAX_RESOURCE_LINK_BYTES,
    MAX_RESOURCE_LINKS,
    MAX_RESOURCE_NAME_BYTES,
    MAX_RESOURCE_URI_BYTES,
    MAX_TEXT_BLOCK_BYTES,
    MAX_TEXT_BLOCKS,
    ConvertedPrompt,
    PromptBlock,
    convert_prompt_content,
)
from neuro_code.interfaces.acp.errors import (  # noqa: F401 - private compatibility aliases
    MAX_SESSION_ID_BYTES,
)
from neuro_code.interfaces.acp.extensions import (  # noqa: F401 - public protocol aliases
    ACP_CONTEXT_COMPACTION_EXTENSION,
    ACP_MCP_EXTENSION,
    ACP_READ_ONLY_SUBAGENT_EXTENSION,
    ACP_SUBAGENT_LIFECYCLE_EXTENSION,
    ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION,
    ACP_TURN_RECOVERY_EXTENSION,
    AcpExtensionController,
)
from neuro_code.interfaces.acp.mcp import (  # noqa: F401 - private compatibility aliases
    MAX_MCP_CALLBACK_BYTES,
    MAX_MCP_ELICITATION_MESSAGE_BYTES,
    MAX_MCP_RESOURCE_BYTES,
    MAX_MCP_SAMPLING_MESSAGES,
    MAX_MCP_SAMPLING_TOKENS,
    AcpMcpController,
    _safe_mcp_extension_value,
)
from neuro_code.interfaces.acp.mcp_config import (  # noqa: F401 - private compatibility aliases
    MAX_MCP_ARGUMENT_BYTES,
    MAX_MCP_ARGUMENT_TOTAL_BYTES,
    MAX_MCP_ARGUMENTS,
    MAX_MCP_COMMAND_BYTES,
    MAX_MCP_CONFIGURATION_BYTES,
    MAX_MCP_ENVIRONMENT_NAME_BYTES,
    MAX_MCP_ENVIRONMENT_TOTAL_BYTES,
    MAX_MCP_ENVIRONMENT_VALUE_BYTES,
    MAX_MCP_ENVIRONMENT_VARIABLES,
    MAX_MCP_HTTP_HEADER_NAME_BYTES,
    MAX_MCP_HTTP_HEADER_TOTAL_BYTES,
    MAX_MCP_HTTP_HEADER_VALUE_BYTES,
    MAX_MCP_HTTP_HEADERS,
    MAX_MCP_SERVER_NAME_BYTES,
    MAX_MCP_URL_BYTES,
    McpServer,
    _mcp_http_headers,
    _mcp_http_url,
    _mcp_server_configurations,
    _mcp_string,
)
from neuro_code.interfaces.acp.negotiation import ACP_PROTOCOL_VERSION, AcpConnectionState
from neuro_code.interfaces.acp.prompt import AcpPromptController
from neuro_code.interfaces.acp.serialization import (  # noqa: F401 - compatibility aliases
    MAX_RESOURCE_FIELD_BYTES,
    _bounded_identifier,
    execution_outcome_metadata,
    execution_outcome_stop_reason,
    serialize_subagent_lifecycle_action,
    serialize_subagent_result,
    serialized_size_bytes,
)
from neuro_code.interfaces.acp.session import (  # noqa: F401 - private compatibility aliases
    AcpSessionApprovalAlreadyPendingError,
    AcpSessionIdentityConflictError,
    AcpSessionIdentityUnavailableError,
    AcpSessionInactiveError,
    AcpSessionPromptAlreadyActiveError,
    AcpSessionRuntime,
)
from neuro_code.interfaces.acp.session_lifecycle import AcpSessionLifecycleController
from neuro_code.interfaces.acp.session_registry import (
    ACP_SESSION_LIST_PAGE_SIZE,
    AcpSessionRegistry,
    _SessionListCursor,
)
from neuro_code.interfaces.acp.transport import (  # noqa: F401 - private compatibility aliases
    ACP_STDIO_BUFFER_LIMIT_BYTES,
    _AcpSdkConnection,
    _build_acp_router,
    _WebSocketWriter,
    stdio_streams,
)
from neuro_code.interfaces.acp.transport import serve_stdio as _serve_stdio
from neuro_code.interfaces.acp.transport import serve_websocket as _serve_websocket
from neuro_code.interfaces.acp.updates import _AcpEventMapper, _history_updates  # noqa: F401

_SESSION_BUSY = _acp_errors.SESSION_BUSY
_SESSION_NOT_ACTIVE = _acp_errors.SESSION_NOT_ACTIVE
_SESSION_NOT_FOUND = _acp_errors.SESSION_NOT_FOUND
_invalid_params = _acp_errors.invalid_params
_session_busy = _acp_errors.session_busy
_session_not_active = _acp_errors.session_not_active
_session_not_found = _acp_errors.session_not_found
_validated_session_id = _acp_errors.validated_session_id
_safe_output_text = _acp_serialization.safe_output_text
_artifact_list_payload = _acp_extensions._artifact_list_payload
_artifact_read_payload = _acp_extensions._artifact_read_payload
ACP_SESSION_ALIAS_NAMESPACE = _acp_session_registry.ACP_SESSION_ALIAS_NAMESPACE
ACP_SUBAGENT_LIFECYCLE_ALIAS_ATTEMPTS = _acp_session_registry.ACP_SUBAGENT_LIFECYCLE_ALIAS_ATTEMPTS
MAX_SESSION_LIST_CURSORS = _acp_session_registry.MAX_SESSION_LIST_CURSORS
MAX_SESSION_LIST_SCAN_ITEMS = _acp_session_registry.MAX_SESSION_LIST_SCAN_ITEMS
SESSION_LIST_SCAN_BATCH_SIZE = _acp_session_registry.SESSION_LIST_SCAN_BATCH_SIZE
_AcpSession = AcpSessionRuntime


class NeuroCodeAcpAgent:
    """Official-SDK ACP v1 facade for one workspace-bound process."""

    def __init__(self, service: AcpApplicationService) -> None:
        self._service = service
        self._connection = AcpConnectionState(service)
        self._registry = AcpSessionRegistry(service)
        self._prompt = AcpPromptController(service, self._registry, self._connection)
        self._mcp = AcpMcpController(service, self._connection, self._registry)
        self._lifecycle = AcpSessionLifecycleController(
            service,
            self._registry,
            self._connection,
            self._mcp,
            request_permission=lambda session_id, request: self._request_permission(
                session_id,
                request,
            ),
            client_file_system=lambda session_id: self._client_file_system(session_id),
            client_terminal=lambda session_id: self._client_terminal(session_id),
            open_mcp_tools=lambda configurations: self._open_mcp_tools(configurations),
            publish_session=lambda session: self._publish_session(session),
            reserve_session_id=lambda session_id: self._reserve_session_id(session_id),
            release_session_reservation=lambda session_id: self._release_session_reservation(
                session_id
            ),
        )
        self._extensions = AcpExtensionController(
            service,
            self._connection,
            self._registry,
            self._mcp,
        )

    @property
    def _sessions(self) -> dict[str, AcpSessionRuntime]:
        """Return the registry map for the narrow legacy inspection seam."""

        return self._registry.sessions

    @property
    def _client(self) -> Client | None:
        return self._connection.client

    @property
    def _client_capabilities(self) -> ClientCapabilities | None:
        return self._connection.client_capabilities

    @property
    def _client_info(self) -> Implementation | None:
        return self._connection.client_info

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        return self._connection.client_capabilities

    @property
    def client_info(self) -> Implementation | None:
        return self._connection.client_info

    def on_connect(self, conn: Client) -> None:
        self._connection.on_connect(conn)

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **_kwargs: Any,
    ) -> InitializeResponse:
        return await self._connection.initialize(
            protocol_version,
            client_capabilities,
            client_info,
            **_kwargs,
        )

    def _require_initialized(self) -> None:
        self._connection.require_initialized()

    def _client_file_system(self, session_id: str) -> ClientFileSystem | None:
        return self._connection.client_file_system(session_id)

    def _client_terminal(self, session_id: str) -> ClientTerminal | None:
        return self._connection.client_terminal(session_id)

    def _safe_mcp_callback_payload(self, value: object) -> dict[str, Any]:
        return self._mcp.safe_callback_payload(value)

    async def _mcp_sampling_handler(self, *args: Any, **kwargs: Any) -> Any:
        return await self._mcp.sampling_handler(*args, **kwargs)

    async def _mcp_elicitation_handler(self, *args: Any, **kwargs: Any) -> Any:
        return await self._mcp.elicitation_handler(*args, **kwargs)

    async def _validate_session_workspace(
        self,
        cwd: str,
        additional_directories: list[str] | None,
        mcp_servers: list[McpServer] | None,
    ) -> tuple[tuple[Path, ...], tuple[AcpMcpServerConfig, ...]]:
        return await self._lifecycle._validate_session_workspace(
            cwd,
            additional_directories,
            mcp_servers,
        )

    async def _open_mcp_tools(
        self,
        configurations: tuple[AcpMcpServerConfig, ...],
    ) -> AcpMcpTools | None:
        return await self._mcp.open_tools(configurations)

    async def _validate_workspace(
        self,
        cwd: str,
        additional_directories: Sequence[str] = (),
    ) -> tuple[Path, ...]:
        return await self._lifecycle.validate_workspace(cwd, additional_directories)

    async def _reserve_session_id(self, session_id: str) -> None:
        await self._registry.reserve(session_id)

    async def _release_session_reservation(self, session_id: str) -> None:
        await self._registry.release(session_id)

    async def _publish_session(self, session: AcpSessionRuntime) -> bool:
        return await self._registry.publish(session)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> NewSessionResponse:
        return await self._lifecycle.new_session(
            cwd,
            additional_directories,
            mcp_servers,
            **_kwargs,
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServer] | None = None,
        additional_directories: list[str] | None = None,
        **_kwargs: Any,
    ) -> LoadSessionResponse:
        return await self._lifecycle.load_session(
            cwd,
            session_id,
            mcp_servers,
            additional_directories,
            **_kwargs,
        )

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> ResumeSessionResponse:
        return await self._lifecycle.resume_session(
            session_id,
            cwd,
            additional_directories,
            mcp_servers,
            **_kwargs,
        )

    async def _activate_persisted_session(
        self,
        external_session_id: str,
        mcp_configurations: tuple[AcpMcpServerConfig, ...],
        additional_workspace_roots: tuple[Path, ...],
        *,
        replay_history: bool,
        failure_reason: str,
    ) -> None:
        await self._lifecycle._activate_persisted_session(
            external_session_id,
            mcp_configurations,
            additional_workspace_roots,
            replay_history=replay_history,
            failure_reason=failure_reason,
        )

    async def _session_list_cursor(self, cursor: str | None) -> _SessionListCursor | None:
        return await self._registry._session_list_cursor(cursor)

    async def _remember_session_list_cursor(self, summary: Any) -> str:
        return await self._registry._remember_session_list_cursor(summary)

    async def _listed_session_id(self, internal_session_id: str) -> str:
        return await self._registry._listed_session_id(internal_session_id)

    def _is_listable_session(self, summary: Any) -> bool:
        return self._registry._is_listable_session(summary)

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **_kwargs: Any,
    ) -> ListSessionsResponse:
        self._require_initialized()
        return await self._registry.list_sessions(
            cwd,
            cursor,
            page_size=ACP_SESSION_LIST_PAGE_SIZE,
            validate_workspace=self._validate_workspace,
            explicit_redactions=self._explicit_redactions,
        )

    async def delete_session(
        self,
        session_id: str,
        **_kwargs: Any,
    ) -> DeleteSessionResponse:
        return await self._lifecycle.delete_session(session_id, **_kwargs)

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> ForkSessionResponse:
        return await self._lifecycle.fork_session(
            session_id,
            cwd,
            additional_directories,
            mcp_servers,
            **_kwargs,
        )

    async def _fork_source_session_id(self, external_session_id: str) -> str:
        return await self._registry.fork_source_session_id(external_session_id)

    async def _active_session(self, session_id: str) -> AcpSessionRuntime:
        return await self._registry.active(session_id)

    def _explicit_redactions(self) -> tuple[str, ...]:
        return self._connection.explicit_redactions()

    async def _artifact_internal_session_id(self, external_session_id: str) -> str:
        return await self._registry.artifact_internal_session_id(external_session_id)

    async def _lifecycle_external_session_id(self, internal_session_id: str) -> str:
        return await self._registry.lifecycle_external_session_id(internal_session_id)

    def _mcp_list_payload(self, mcp_tools: AcpMcpTools) -> dict[str, object]:
        return self._mcp.list_payload(mcp_tools)

    async def _mcp_extension(self, query: AcpMcpQuery) -> dict[str, object]:
        return await self._mcp.extension(query)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._extensions.ext_method(method, params)

    async def _bind_internal_session(
        self,
        session: AcpSessionRuntime,
        internal_session_id: str,
    ) -> None:
        await self._prompt._bind_internal_session(session, internal_session_id)

    async def _capture_runner_session(
        self,
        session: AcpSessionRuntime,
        binding: Any,
        *,
        suppress_errors: bool,
    ) -> None:
        await self._prompt._capture_runner_session(
            session,
            binding,
            suppress_errors=suppress_errors,
        )

    async def prompt(
        self,
        session_id: str,
        prompt: list[PromptBlock],
        **_kwargs: Any,
    ) -> PromptResponse:
        return await self._prompt.prompt(session_id, prompt, **_kwargs)

    async def _request_permission(
        self,
        session_id: str,
        request: Any,
    ) -> Any:
        return await self._prompt.request_permission(session_id, request)

    async def cancel(self, session_id: str, **_kwargs: Any) -> None:
        await self._prompt.cancel(session_id, **_kwargs)

    async def close_session(
        self,
        session_id: str,
        **_kwargs: Any,
    ) -> CloseSessionResponse:
        return await self._lifecycle.close_session(session_id, **_kwargs)

    async def _cleanup_session(self, session: AcpSessionRuntime) -> None:
        await self._lifecycle.cleanup_session(session)

    async def shutdown(self) -> None:
        await self._lifecycle.shutdown()


async def serve_acp_websocket(
    service: AcpApplicationService,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> None:
    """Construct one Agent per connection and delegate to canonical transport."""

    await _serve_websocket(
        lambda: NeuroCodeAcpAgent(service),
        host=host,
        port=port,
        connection_factory=_AcpSdkConnection,
        writer_factory=_WebSocketWriter,
    )


async def serve_acp(service: AcpApplicationService) -> None:
    """Serve ACP on stdio through the official SDK framing and router."""

    await _serve_stdio(
        NeuroCodeAcpAgent(service),
        streams_factory=stdio_streams,
        connection_factory=_AcpSdkConnection,
    )


__all__ = [
    "ACP_CONTEXT_COMPACTION_EXTENSION",
    "ACP_MCP_EXTENSION",
    "ACP_PROTOCOL_VERSION",
    "ACP_READ_ONLY_SUBAGENT_EXTENSION",
    "ACP_STDIO_BUFFER_LIMIT_BYTES",
    "ACP_SUBAGENT_LIFECYCLE_EXTENSION",
    "ACP_TURN_RECOVERY_EXTENSION",
    "MAX_ANNOTATIONS_BYTES",
    "MAX_ANNOTATION_AUDIENCE",
    "MAX_ANNOTATION_AUDIENCE_BYTES",
    "MAX_AUDIO_BLOCKS",
    "MAX_AUDIO_BLOCK_BYTES",
    "MAX_AUDIO_TOTAL_BYTES",
    "MAX_EMBEDDED_BINARY_RESOURCE_BYTES",
    "MAX_EMBEDDED_BINARY_TOTAL_BYTES",
    "MAX_EMBEDDED_TEXT_RESOURCES",
    "MAX_EMBEDDED_TEXT_RESOURCE_BYTES",
    "MAX_EMBEDDED_TEXT_TOTAL_BYTES",
    "MAX_IMAGE_BLOCKS",
    "MAX_IMAGE_BLOCK_BYTES",
    "MAX_IMAGE_TOTAL_BYTES",
    "MAX_PROMPT_BLOCKS",
    "MAX_PROMPT_BYTES",
    "MAX_RESOURCE_FIELD_BYTES",
    "MAX_RESOURCE_LINKS",
    "MAX_RESOURCE_LINK_BYTES",
    "MAX_RESOURCE_NAME_BYTES",
    "MAX_RESOURCE_URI_BYTES",
    "MAX_TEXT_BLOCKS",
    "MAX_TEXT_BLOCK_BYTES",
    "ConvertedPrompt",
    "NeuroCodeAcpAgent",
    "PromptBlock",
    "convert_prompt_content",
    "serve_acp",
    "serve_acp_websocket",
]
