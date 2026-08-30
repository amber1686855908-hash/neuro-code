from __future__ import annotations

import asyncio
import contextlib
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from acp.agent.router import build_agent_router
from acp.core import Connection
from acp.exceptions import RequestError
from acp.interfaces import Agent, Client
from acp.meta import AGENT_METHODS, CLIENT_METHODS
from acp.router import MessageRouter
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    ClientCapabilities,
    CloseSessionResponse,
    DeleteSessionRequest,
    DeleteSessionResponse,
    ForkSessionResponse,
    HttpMcpServer,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionDeleteCapabilities,
    SessionForkCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionNotification,
    SessionResumeCapabilities,
    SseMcpServer,
    ToolCallUpdate,
)
from acp.stdio import stdio_streams
from acp.utils import normalize_result, notify_model, request_model

from neuro_code import __version__
from neuro_code.application.acp.contracts import (
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
from neuro_code.application.permissions.broker import SessionApprovalBroker
from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionRequest,
)
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import (
    MAX_CLIENT_TERMINAL_OUTPUT_BYTES,  # noqa: F401 - legacy module compatibility
    ClientTerminal,
    ClientTerminalResult,  # noqa: F401 - legacy module compatibility
)
from neuro_code.application.ports.mcp import McpElicitationHandler, McpSamplingHandler
from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
)
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.subagent_queries import SubagentRelationshipAction
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.application.tools.service import SessionToolOutputArtifact
from neuro_code.domain.sessions import SessionSummary
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
from neuro_code.interfaces.acp.serialization import (
    MAX_RESOURCE_FIELD_BYTES,
    _bounded_identifier,
    execution_outcome_metadata,
    execution_outcome_stop_reason,
    safe_output_text,
    serialize_subagent_lifecycle_action,
    serialize_subagent_result,
    serialized_size_bytes,
)
from neuro_code.interfaces.acp.updates import _AcpEventMapper, _history_updates
from neuro_code.shared.errors import ConfigurationError, ProviderError, SessionError, ToolError

ACP_PROTOCOL_VERSION = 1
ACP_STDIO_BUFFER_LIMIT_BYTES = 1024 * 1024

MAX_SESSION_ID_BYTES = 512
ACP_SESSION_LIST_PAGE_SIZE = 50
MAX_SESSION_LIST_SCAN_ITEMS = 5_000
SESSION_LIST_SCAN_BATCH_SIZE = 250
MAX_SESSION_LIST_CURSORS = 256
MAX_SESSION_LIST_CURSOR_BYTES = 128
MAX_MCP_SERVER_NAME_BYTES = 128
MAX_MCP_COMMAND_BYTES = 4 * 1024
MAX_MCP_ARGUMENTS = 64
MAX_MCP_ARGUMENT_BYTES = 4 * 1024
MAX_MCP_ARGUMENT_TOTAL_BYTES = 32 * 1024
MAX_MCP_ENVIRONMENT_VARIABLES = 64
MAX_MCP_ENVIRONMENT_NAME_BYTES = 256
MAX_MCP_ENVIRONMENT_VALUE_BYTES = 16 * 1024
MAX_MCP_ENVIRONMENT_TOTAL_BYTES = 64 * 1024
MAX_MCP_URL_BYTES = 8 * 1024
MAX_MCP_HTTP_HEADERS = 64
MAX_MCP_HTTP_HEADER_NAME_BYTES = 256
MAX_MCP_HTTP_HEADER_VALUE_BYTES = 16 * 1024
MAX_MCP_HTTP_HEADER_TOTAL_BYTES = 64 * 1024
MAX_MCP_CONFIGURATION_BYTES = 256 * 1024
MAX_MCP_RESOURCE_BYTES = 512 * 1024
MAX_MCP_SAMPLING_MESSAGES = 128
MAX_MCP_SAMPLING_TOKENS = 1_000_000
MAX_MCP_ELICITATION_MESSAGE_BYTES = 64 * 1024
MAX_MCP_CALLBACK_BYTES = 256 * 1024
ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION = "neuro-code/session/artifacts"
ACP_READ_ONLY_SUBAGENT_EXTENSION = "neuro-code/session/subagent"
ACP_SUBAGENT_LIFECYCLE_EXTENSION = "neuro-code/session/subagents"
ACP_MCP_EXTENSION = "neuro-code/session/mcp"
ACP_CONTEXT_COMPACTION_EXTENSION = "neuro-code/session/compact"
ACP_TURN_RECOVERY_EXTENSION = "neuro-code/session/recovery"

_SESSION_NOT_ACTIVE = -32001
_SESSION_NOT_FOUND = -32002
_SESSION_BUSY = -32003
_ACP_SESSION_ALIAS_NAMESPACE = "acp-v1"
_ACP_SUBAGENT_LIFECYCLE_ALIAS_ATTEMPTS = 4
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED_MCP_HTTP_HEADERS = frozenset(
    {
        "accept",
        "connection",
        "content-length",
        "content-type",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
McpServer = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio


def _invalid_params(reason: str, details: str | None = None) -> RequestError:
    data = {"reason": reason}
    if details is not None:
        data["details"] = details
    return RequestError.invalid_params(data)


def _session_not_active(session_id: str) -> RequestError:
    return RequestError(
        _SESSION_NOT_ACTIVE,
        "Session not active",
        {"reason": "session_not_active", "sessionId": _bounded_identifier(session_id)},
    )


def _session_not_found(session_id: str) -> RequestError:
    return RequestError(
        _SESSION_NOT_FOUND,
        "Session not found",
        {"reason": "session_not_found", "sessionId": _bounded_identifier(session_id)},
    )


def _session_busy(session_id: str, reason: str) -> RequestError:
    return RequestError(
        _SESSION_BUSY,
        "Session is busy",
        {"reason": reason, "sessionId": _bounded_identifier(session_id)},
    )


def _validated_session_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_params("session_id_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid_params("session_id_invalid")
    if len(value.encode("utf-8")) > MAX_SESSION_ID_BYTES:
        raise _invalid_params("session_id_too_large")
    return value


def _safe_mcp_extension_value(
    value: object,
    *,
    explicit_redactions: tuple[str, ...],
    depth: int = 0,
) -> object:
    """Project untrusted MCP metadata into bounded, redacted JSON values."""

    if depth >= 5:
        return "<nested-value-omitted>"
    if isinstance(value, str):
        return safe_output_text(value, 16 * 1024, explicit_redactions=explicit_redactions)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, nested) in enumerate(value.items()):
            if index >= 64:
                result["<fields-omitted>"] = True
                break
            rendered_key = safe_output_text(
                str(key),
                512,
                explicit_redactions=explicit_redactions,
            )
            result[rendered_key] = _safe_mcp_extension_value(
                nested,
                explicit_redactions=explicit_redactions,
                depth=depth + 1,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [
            _safe_mcp_extension_value(
                nested,
                explicit_redactions=explicit_redactions,
                depth=depth + 1,
            )
            for nested in value[:64]
        ]
    return safe_output_text(str(value), 512, explicit_redactions=explicit_redactions)


def _mcp_string(
    value: object,
    *,
    limit: int,
    reason: str,
    allow_empty: bool = False,
    allow_controls: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise _invalid_params(reason)
    if (
        "\x00" in value
        or (
            not allow_controls
            and any(ord(character) < 32 or ord(character) == 127 for character in value)
        )
        or len(value.encode("utf-8")) > limit
    ):
        raise _invalid_params(reason)
    return value


def _mcp_server_configurations(
    servers: list[McpServer] | None,
    *,
    protected_environment_variables: frozenset[str],
) -> tuple[AcpMcpServerConfig, ...]:
    if not servers:
        return ()
    if len(servers) > MAX_MCP_SERVERS:
        raise _invalid_params("too_many_mcp_servers")

    protected = {name.casefold() for name in protected_environment_variables}
    configurations: list[AcpMcpServerConfig] = []
    server_names: set[str] = set()
    serialized: list[dict[str, object]] = []
    for server in servers:
        if not isinstance(server, HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio):
            raise _invalid_params("mcp_transport_unsupported")
        name = _mcp_string(
            server.name,
            limit=MAX_MCP_SERVER_NAME_BYTES,
            reason="mcp_server_name_invalid",
        )
        folded_name = name.casefold()
        if folded_name in server_names:
            raise _invalid_params("mcp_server_name_duplicate")
        server_names.add(folded_name)
        if isinstance(server, HttpMcpServer | SseMcpServer):
            url = _mcp_http_url(server.url)
            headers = _mcp_http_headers(server.headers)
            serialized.append(
                {
                    "name": name,
                    "transport": server.type,
                    "url": url,
                    "headers": dict(headers),
                }
            )
            configurations.append(
                AcpMcpHttpServerConfig(
                    name=name,
                    url=url,
                    headers=tuple(headers),
                    transport=server.type,
                )
            )
            continue
        if not isinstance(server, McpServerStdio):
            raise _invalid_params("mcp_transport_unsupported")
        command = _mcp_string(
            server.command,
            limit=MAX_MCP_COMMAND_BYTES,
            reason="mcp_server_command_invalid",
        )
        if len(server.args) > MAX_MCP_ARGUMENTS:
            raise _invalid_params("too_many_mcp_server_arguments")
        arguments: list[str] = []
        argument_bytes = 0
        for argument in server.args:
            rendered = _mcp_string(
                argument,
                limit=MAX_MCP_ARGUMENT_BYTES,
                reason="mcp_server_argument_invalid",
                allow_empty=True,
            )
            argument_bytes += len(rendered.encode("utf-8"))
            if argument_bytes > MAX_MCP_ARGUMENT_TOTAL_BYTES:
                raise _invalid_params("mcp_server_arguments_too_large")
            arguments.append(rendered)

        if len(server.env) > MAX_MCP_ENVIRONMENT_VARIABLES:
            raise _invalid_params("too_many_mcp_environment_variables")
        environment: list[tuple[str, str]] = []
        environment_names: set[str] = set()
        environment_bytes = 0
        for variable in server.env:
            variable_name = _mcp_string(
                variable.name,
                limit=MAX_MCP_ENVIRONMENT_NAME_BYTES,
                reason="mcp_environment_name_invalid",
            )
            folded_variable_name = variable_name.casefold()
            if (
                not _ENVIRONMENT_NAME.fullmatch(variable_name)
                or folded_variable_name in environment_names
            ):
                raise _invalid_params("mcp_environment_name_invalid")
            if folded_variable_name in protected:
                raise _invalid_params("mcp_environment_protected")
            environment_names.add(folded_variable_name)
            variable_value = _mcp_string(
                variable.value,
                limit=MAX_MCP_ENVIRONMENT_VALUE_BYTES,
                reason="mcp_environment_value_invalid",
                allow_empty=True,
                allow_controls=True,
            )
            environment_bytes += len(variable_name.encode("utf-8")) + len(
                variable_value.encode("utf-8")
            )
            if environment_bytes > MAX_MCP_ENVIRONMENT_TOTAL_BYTES:
                raise _invalid_params("mcp_environment_too_large")
            environment.append((variable_name, variable_value))
        serialized.append(
            {
                "name": name,
                "command": command,
                "args": arguments,
                "env": dict(environment),
            }
        )
        configurations.append(
            AcpMcpStdioServerConfig(
                name=name,
                command=command,
                args=tuple(arguments),
                env=tuple(environment),
            )
        )
    if serialized_size_bytes(serialized) > MAX_MCP_CONFIGURATION_BYTES:
        raise _invalid_params("mcp_configuration_too_large")
    return tuple(configurations)


def _mcp_http_url(value: object) -> str:
    url = _mcp_string(
        value,
        limit=MAX_MCP_URL_BYTES,
        reason="mcp_http_url_invalid",
    )
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise _invalid_params("mcp_http_url_invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 0 < port <= 65_535)
    ):
        raise _invalid_params("mcp_http_url_invalid")
    return url


def _mcp_http_headers(headers: Sequence[Any]) -> list[tuple[str, str]]:
    if len(headers) > MAX_MCP_HTTP_HEADERS:
        raise _invalid_params("too_many_mcp_http_headers")
    values: list[tuple[str, str]] = []
    names: set[str] = set()
    total_bytes = 0
    for header in headers:
        name = _mcp_string(
            header.name,
            limit=MAX_MCP_HTTP_HEADER_NAME_BYTES,
            reason="mcp_http_header_name_invalid",
        )
        folded_name = name.casefold()
        if not _HTTP_HEADER_NAME.fullmatch(name) or folded_name in names:
            raise _invalid_params("mcp_http_header_name_invalid")
        if folded_name in _RESERVED_MCP_HTTP_HEADERS:
            raise _invalid_params("mcp_http_header_reserved")
        value = _mcp_string(
            header.value,
            limit=MAX_MCP_HTTP_HEADER_VALUE_BYTES,
            reason="mcp_http_header_value_invalid",
            allow_empty=True,
        )
        total_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total_bytes > MAX_MCP_HTTP_HEADER_TOTAL_BYTES:
            raise _invalid_params("mcp_http_headers_too_large")
        names.add(folded_name)
        values.append((name, value))
    return values


def _safe_output_text(
    value: object,
    limit: int,
    *,
    explicit_redactions: tuple[str, ...],
) -> str:
    return safe_output_text(value, limit, explicit_redactions=explicit_redactions)


def _artifact_list_payload(
    artifacts: Sequence[SessionToolOutputArtifact],
) -> dict[str, list[dict[str, int | str | bool]]]:
    """Serialize only canonical, non-sensitive artifact facts for ACP.

    仅为 ACP 序列化规范且不含敏感信息的 artifact 事实.
    """

    payload: list[dict[str, int | str | bool]] = []
    for reference in artifacts:
        artifact = reference.artifact
        if not re.fullmatch(r"[0-9a-f]{32}", artifact.artifact_id):
            continue
        payload.append(
            {
                "artifactId": artifact.artifact_id,
                "byteCount": artifact.byte_count,
                "truncated": artifact.truncated,
                "eventSequence": reference.event_sequence,
            }
        )
    return {"artifacts": payload}


def _artifact_read_payload(
    artifact_id: str,
    content: str,
    read_truncated: bool,
    *,
    explicit_redactions: tuple[str, ...],
) -> dict[str, str | bool]:
    """Serialize one bounded redacted artifact without its storage path."""

    return {
        "artifactId": artifact_id,
        "content": _safe_output_text(
            content,
            MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
            explicit_redactions=explicit_redactions,
        ),
        "readTruncated": read_truncated,
    }


@dataclass(slots=True)
class _AcpSession:
    session_id: str
    binding: ConversationBinding | None
    approvals: SessionApprovalBroker
    context_window_tokens: int | None
    mcp_tools: AcpMcpTools | None
    mcp_tool_names: tuple[str, ...] = ()
    client_terminal: ClientTerminal | None = None
    internal_session_id: str | None = None
    prompt_task: asyncio.Task[Any] | None = None
    mapper: _AcpEventMapper | None = None
    pending_approval_id: str | None = None
    cancel_requested: bool = False
    closing: bool = False
    closed: bool = False
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class _SessionListCursor:
    updated_at: datetime
    internal_session_id: str


class NeuroCodeAcpAgent:
    """Official-SDK ACP v1 adapter for one workspace-bound process.

    为一个工作区绑定进程提供官方 SDK ACP v1 适配器."""

    def __init__(self, service: AcpApplicationService) -> None:
        self._service = service
        self._client: Client | None = None
        self._client_capabilities: ClientCapabilities | None = None
        self._client_info: Implementation | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._sessions: dict[str, _AcpSession] = {}
        self._pending_session_tasks: dict[str, asyncio.Task[Any]] = {}
        self._registry_lock = asyncio.Lock()
        self._list_cursors: OrderedDict[str, _SessionListCursor] = OrderedDict()
        self._list_cursor_lock = asyncio.Lock()
        self._shutting_down = False

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        return self._client_capabilities

    @property
    def client_info(self) -> Implementation | None:
        return self._client_info

    def on_connect(self, conn: Client) -> None:
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **_kwargs: Any,
    ) -> InitializeResponse:
        async with self._initialize_lock:
            if self._initialized:
                raise RequestError.invalid_request({"reason": "already_initialized"})
            self._client_capabilities = client_capabilities
            self._client_info = client_info
            self._initialized = True
        negotiated = (
            protocol_version if protocol_version == ACP_PROTOCOL_VERSION else ACP_PROTOCOL_VERSION
        )
        return InitializeResponse(
            protocol_version=negotiated,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    audio=True,
                    embedded_context=True,
                ),
                mcp_capabilities=McpCapabilities(http=True, sse=True),
                auth=None,
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    delete=SessionDeleteCapabilities(),
                    fork=SessionForkCapabilities(),
                    resume=SessionResumeCapabilities(),
                    close=SessionCloseCapabilities(),
                ),
            ),
            auth_methods=[],
            agent_info=Implementation(
                name="neuro-code",
                title="Neuro Code",
                version=__version__,
            ),
        )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RequestError.invalid_request({"reason": "not_initialized"})

    def _client_file_system(self, session_id: str) -> ClientFileSystem | None:
        client = self._client
        capabilities = self._client_capabilities
        filesystem = capabilities.fs if capabilities is not None else None
        if client is None or filesystem is None:
            return None
        supports_read = filesystem.read_text_file is True
        supports_write = filesystem.write_text_file is True
        if not supports_read and not supports_write:
            return None
        return _AcpClientFileSystem(
            client,
            session_id,
            supports_read=supports_read,
            supports_write=supports_write,
        )

    def _client_terminal(self, session_id: str) -> ClientTerminal | None:
        client = self._client
        capabilities = self._client_capabilities
        if client is None or capabilities is None or capabilities.terminal is not True:
            return None
        return _AcpClientTerminal(client, session_id)

    def _safe_mcp_callback_payload(self, value: object) -> dict[str, Any]:
        projected = _safe_mcp_extension_value(
            value,
            explicit_redactions=self._explicit_redactions(),
        )
        if not isinstance(projected, dict):
            raise ConfigurationError("MCP callback payload is not an object")
        if serialized_size_bytes(projected) > MAX_MCP_CALLBACK_BYTES:
            raise ConfigurationError("MCP callback payload is too large")
        return cast(dict[str, Any], projected)

    async def _mcp_sampling_handler(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_preferences: Mapping[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> Mapping[str, Any]:
        client = self._client
        if client is None:
            raise ConfigurationError("ACP client is unavailable for MCP sampling")
        if len(messages) > MAX_MCP_SAMPLING_MESSAGES:
            raise ConfigurationError("MCP sampling message count exceeds the limit")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ConfigurationError("MCP sampling system prompt is invalid")
        if (
            system_prompt is not None
            and len(system_prompt.encode("utf-8")) > MAX_MCP_ELICITATION_MESSAGE_BYTES
        ):
            raise ConfigurationError("MCP sampling system prompt is too large")
        if max_tokens is not None and (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= MAX_MCP_SAMPLING_TOKENS
        ):
            raise ConfigurationError("MCP sampling token limit is invalid")
        payload: dict[str, object] = {
            "messages": tuple(messages),
        }
        if model_preferences is not None:
            payload["modelPreferences"] = model_preferences
        if system_prompt is not None:
            payload["systemPrompt"] = system_prompt
        if max_tokens is not None:
            payload["maxTokens"] = max_tokens
        response = await client.ext_method(
            "neuro-code/mcp/sampling",
            self._safe_mcp_callback_payload(payload),
        )
        return self._safe_mcp_callback_payload(response)

    async def _mcp_elicitation_handler(
        self,
        message: str,
        schema: Mapping[str, Any] | None = None,
        *,
        url: str | None = None,
    ) -> Mapping[str, Any]:
        client = self._client
        if client is None:
            raise ConfigurationError("ACP client is unavailable for MCP elicitation")
        if (
            not isinstance(message, str)
            or len(message.encode("utf-8")) > MAX_MCP_ELICITATION_MESSAGE_BYTES
        ):
            raise ConfigurationError("MCP elicitation message is invalid")
        if url is not None and (
            not isinstance(url, str) or len(url.encode("utf-8")) > MAX_MCP_URL_BYTES
        ):
            raise ConfigurationError("MCP elicitation URL is invalid")
        payload: dict[str, object] = {"message": message}
        if schema is not None:
            payload["schema"] = schema
        if url is not None:
            payload["url"] = url
        response = await client.ext_method(
            "neuro-code/mcp/elicitation",
            self._safe_mcp_callback_payload(payload),
        )
        return self._safe_mcp_callback_payload(response)

    async def _validate_session_workspace(
        self,
        cwd: str,
        additional_directories: list[str] | None,
        mcp_servers: list[McpServer] | None,
    ) -> tuple[tuple[Path, ...], tuple[AcpMcpServerConfig, ...]]:
        additional_workspace_roots = await self._validate_workspace(
            cwd,
            additional_directories or (),
        )
        configurations = _mcp_server_configurations(
            mcp_servers,
            protected_environment_variables=(self._service.protected_environment_variables),
        )
        return additional_workspace_roots, configurations

    async def _open_mcp_tools(
        self,
        configurations: tuple[AcpMcpServerConfig, ...],
    ) -> AcpMcpTools | None:
        if not configurations:
            return None
        sampling_handler: McpSamplingHandler | None = (
            self._mcp_sampling_handler if self._client is not None else None
        )
        elicitation_handler: McpElicitationHandler | None = (
            self._mcp_elicitation_handler if self._client is not None else None
        )
        return await self._service.open_mcp_tools(
            configurations,
            sampling_handler=sampling_handler,
            elicitation_handler=elicitation_handler,
        )

    async def _validate_workspace(
        self,
        cwd: str,
        additional_directories: Sequence[str] = (),
    ) -> tuple[Path, ...]:
        try:
            return await self._service.validate_workspace(cwd, additional_directories)
        except AcpWorkspaceValidationError as error:
            raise _invalid_params(error.reason, error.details) from None

    async def _reserve_session_id(self, session_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RequestError.internal_error({"reason": "session_task_unavailable"})
        async with self._registry_lock:
            if self._shutting_down:
                raise RequestError.internal_error({"reason": "connection_closing"})
            if session_id in self._sessions or session_id in self._pending_session_tasks:
                raise _session_busy(session_id, "session_already_active")
            self._pending_session_tasks[session_id] = task

    async def _release_session_reservation(self, session_id: str) -> None:
        task = asyncio.current_task()
        async with self._registry_lock:
            if self._pending_session_tasks.get(session_id) is task:
                del self._pending_session_tasks[session_id]

    async def _publish_session(self, session: _AcpSession) -> bool:
        task = asyncio.current_task()
        async with self._registry_lock:
            if self._pending_session_tasks.get(session.session_id) is not task:
                return False
            del self._pending_session_tasks[session.session_id]
            if self._shutting_down:
                return False
            self._sessions[session.session_id] = session
            return True

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> NewSessionResponse:
        self._require_initialized()
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
            session = _AcpSession(
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
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
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
        self._require_initialized()
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
        self._require_initialized()
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
        client = self._client
        if replay_history and client is None:
            raise RequestError.internal_error({"reason": "client_unavailable"})
        await self._reserve_session_id(external_session_id)
        binding: ConversationBinding | None = None
        mcp_tools: AcpMcpTools | None = None
        client_terminal = self._client_terminal(external_session_id)
        try:
            try:
                internal_session_id = await self._service.resolve_session_alias(
                    _ACP_SESSION_ALIAS_NAMESPACE,
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
                    _ACP_SESSION_ALIAS_NAMESPACE,
                    external_session_id,
                    internal_session_id,
                )
            except SessionError:
                raise RequestError.internal_error({"reason": "session_alias_failed"}) from None
            if replay_history:
                assert client is not None
                updates = _history_updates(
                    binding.runner.items,
                    explicit_redactions=self._explicit_redactions(),
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

            session = _AcpSession(
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
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if client_terminal is not None:
                await asyncio.shield(client_terminal.shutdown())

    async def _session_list_cursor(
        self,
        cursor: str | None,
    ) -> _SessionListCursor | None:
        if cursor is None:
            return None
        if (
            not cursor
            or len(cursor.encode("utf-8")) > MAX_SESSION_LIST_CURSOR_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in cursor)
        ):
            raise _invalid_params("cursor_invalid")
        async with self._list_cursor_lock:
            position = self._list_cursors.get(cursor)
            if position is None:
                raise _invalid_params("cursor_invalid")
            self._list_cursors.move_to_end(cursor)
            return position

    async def _remember_session_list_cursor(
        self,
        summary: SessionSummary,
    ) -> str:
        token = f"cursor-{uuid.uuid4().hex}"
        async with self._list_cursor_lock:
            self._list_cursors[token] = _SessionListCursor(
                summary.updated_at,
                summary.id,
            )
            while len(self._list_cursors) > MAX_SESSION_LIST_CURSORS:
                self._list_cursors.popitem(last=False)
        return token

    async def _listed_session_id(self, internal_session_id: str) -> str:
        for _attempt in range(4):
            try:
                return await self._service.get_or_create_session_alias(
                    _ACP_SESSION_ALIAS_NAMESPACE,
                    internal_session_id,
                    f"acp-{uuid.uuid4().hex}",
                )
            except SessionError:
                continue
        raise RequestError.internal_error({"reason": "session_alias_allocation_failed"})

    def _is_listable_session(self, summary: SessionSummary) -> bool:
        return self._service.is_current_workspace(summary.cwd)

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **_kwargs: Any,
    ) -> ListSessionsResponse:
        self._require_initialized()
        if cwd is not None:
            await self._validate_workspace(cwd)
        position = await self._session_list_cursor(cursor)
        before_updated_at = position.updated_at if position is not None else None
        before_id = position.internal_session_id if position is not None else None

        matches: list[SessionSummary] = []
        last_scanned: SessionSummary | None = None
        remaining_scan = MAX_SESSION_LIST_SCAN_ITEMS
        exhausted = False
        try:
            while len(matches) <= ACP_SESSION_LIST_PAGE_SIZE and remaining_scan > 0:
                batch_limit = min(SESSION_LIST_SCAN_BATCH_SIZE, remaining_scan)
                batch = await self._service.list_sessions_page(
                    limit=batch_limit,
                    before_updated_at=before_updated_at,
                    before_id=before_id,
                )
                if not batch:
                    exhausted = True
                    break
                remaining_scan -= len(batch)
                for summary in batch:
                    last_scanned = summary
                    before_updated_at = summary.updated_at
                    before_id = summary.id
                    if self._is_listable_session(summary):
                        matches.append(summary)
                        if len(matches) > ACP_SESSION_LIST_PAGE_SIZE:
                            break
                if len(matches) > ACP_SESSION_LIST_PAGE_SIZE:
                    break
                if len(batch) < batch_limit:
                    exhausted = True
                    break
        except SessionError:
            raise RequestError.internal_error({"reason": "session_list_failed"}) from None

        page = matches[:ACP_SESSION_LIST_PAGE_SIZE]
        next_position: SessionSummary | None = None
        if len(matches) > ACP_SESSION_LIST_PAGE_SIZE:
            next_position = page[-1]
        elif not exhausted:
            next_position = last_scanned

        explicit_redactions = self._explicit_redactions()
        sessions = [
            SessionInfo(
                session_id=await self._listed_session_id(summary.id),
                cwd=summary.cwd,
                title=(
                    _safe_output_text(
                        summary.title,
                        MAX_RESOURCE_FIELD_BYTES,
                        explicit_redactions=explicit_redactions,
                    )
                    if summary.title is not None
                    else None
                ),
                updated_at=summary.updated_at.isoformat(),
            )
            for summary in page
        ]
        next_cursor = (
            await self._remember_session_list_cursor(next_position)
            if next_position is not None
            else None
        )
        return ListSessionsResponse(
            sessions=sessions,
            next_cursor=next_cursor,
        )

    async def delete_session(
        self,
        session_id: str,
        **_kwargs: Any,
    ) -> DeleteSessionResponse:
        self._require_initialized()
        external_session_id = _validated_session_id(session_id)
        async with self._registry_lock:
            if external_session_id in self._pending_session_tasks:
                raise _session_busy(external_session_id, "session_creation_in_progress")
            active = self._sessions.get(external_session_id)

        internal_session_id: str | None = None
        if active is not None:
            async with active.state_lock:
                if active.closed or active.closing:
                    raise _session_not_active(external_session_id)
                active.closing = True
                active.cancel_requested = True
                internal_session_id = active.internal_session_id
            await self._cleanup_session(active)
            async with self._registry_lock:
                if self._sessions.get(external_session_id) is active:
                    del self._sessions[external_session_id]

        if internal_session_id is None:
            try:
                internal_session_id = await self._service.resolve_session_alias(
                    _ACP_SESSION_ALIAS_NAMESPACE,
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
        self._require_initialized()
        additional_workspace_roots, mcp_configurations = await self._validate_session_workspace(
            cwd,
            additional_directories,
            mcp_servers,
        )
        source_external_session_id = _validated_session_id(session_id)
        source_internal_session_id = await self._fork_source_session_id(source_external_session_id)
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
                    _ACP_SESSION_ALIAS_NAMESPACE,
                    forked_external_session_id,
                    forked_internal_session_id,
                )
            except SessionError:
                raise RequestError.internal_error({"reason": "session_alias_failed"}) from None

            session = _AcpSession(
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
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if client_terminal is not None:
                await asyncio.shield(client_terminal.shutdown())
            if forked_internal_session_id is not None:
                with contextlib.suppress(SessionError):
                    await asyncio.shield(self._service.delete_session(forked_internal_session_id))

    async def _fork_source_session_id(self, external_session_id: str) -> str:
        async with self._registry_lock:
            if external_session_id in self._pending_session_tasks:
                raise _session_busy(external_session_id, "session_creation_in_progress")
            active = self._sessions.get(external_session_id)
        if active is not None:
            async with active.state_lock:
                task = active.prompt_task
                if task is not None and not task.done():
                    raise _session_busy(external_session_id, "session_prompt_active")
                if active.internal_session_id is None:
                    raise _session_not_found(external_session_id)
                return active.internal_session_id
        try:
            return await self._service.resolve_session_alias(
                _ACP_SESSION_ALIAS_NAMESPACE,
                external_session_id,
            )
        except SessionError:
            raise _session_not_found(external_session_id) from None

    async def _active_session(self, session_id: str) -> _AcpSession:
        async with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None or session.closed or session.closing:
            raise _session_not_active(session_id)
        return session

    def _explicit_redactions(self) -> tuple[str, ...]:
        return self._service.explicit_redactions()

    async def _artifact_internal_session_id(self, external_session_id: str) -> str:
        """Resolve an ACP ID without exposing the internal session identity."""

        async with self._registry_lock:
            if external_session_id in self._pending_session_tasks:
                raise _session_busy(external_session_id, "session_creation_in_progress")
            active = self._sessions.get(external_session_id)
        if active is not None:
            async with active.state_lock:
                if active.closed or active.closing:
                    raise _session_not_active(external_session_id)
                internal_session_id = active.internal_session_id
            if internal_session_id is None:
                raise _session_not_found(external_session_id)
            return internal_session_id
        try:
            return await self._service.resolve_session_alias(
                _ACP_SESSION_ALIAS_NAMESPACE,
                external_session_id,
            )
        except SessionError:
            raise _session_not_found(external_session_id) from None

    async def _lifecycle_external_session_id(self, internal_session_id: str) -> str:
        """Allocate a bounded ACP alias for a lifecycle result session."""

        for _attempt in range(_ACP_SUBAGENT_LIFECYCLE_ALIAS_ATTEMPTS):
            try:
                external_session_id = (
                    await self._service.get_or_create_current_workspace_session_alias(
                        internal_session_id,
                        f"acp-{uuid.uuid4().hex}",
                    )
                )
                resolved_session_id = await self._service.resolve_session_alias(
                    _ACP_SESSION_ALIAS_NAMESPACE,
                    external_session_id,
                )
                if resolved_session_id != internal_session_id:
                    raise SessionError("session alias resolved to another session")
                return external_session_id
            except SessionError:
                continue
        raise RequestError.internal_error({"reason": "session_alias_allocation_failed"})

    def _mcp_list_payload(self, mcp_tools: AcpMcpTools) -> dict[str, object]:
        explicit_redactions = self._explicit_redactions()
        payload = {
            "resources": [
                _safe_mcp_extension_value(
                    resource.to_dict(),
                    explicit_redactions=explicit_redactions,
                )
                for resource in tuple(mcp_tools.resources)[:256]
            ],
            "resourceTemplates": [
                _safe_mcp_extension_value(
                    template.to_dict(),
                    explicit_redactions=explicit_redactions,
                )
                for template in tuple(mcp_tools.resource_templates)[:256]
            ],
            "prompts": [
                _safe_mcp_extension_value(
                    prompt.to_dict(),
                    explicit_redactions=explicit_redactions,
                )
                for prompt in tuple(mcp_tools.prompts)[:128]
            ],
            "toolCount": len(tuple(mcp_tools.tools)),
        }
        if serialized_size_bytes(payload) > MAX_MCP_CONFIGURATION_BYTES:
            raise RequestError.internal_error({"reason": "mcp_metadata_too_large"})
        return payload

    async def _mcp_extension(self, query: AcpMcpQuery) -> dict[str, object]:
        external_session_id = _validated_session_id(query.session_id)
        session = await self._active_session(external_session_id)
        mcp_tools = session.mcp_tools
        if mcp_tools is None:
            raise RequestError.internal_error({"reason": "mcp_unavailable"})
        if query.operation == "list":
            return self._mcp_list_payload(mcp_tools)
        try:
            if query.operation == "refresh":
                await mcp_tools.refresh()
                binding = session.binding
                if binding is None:
                    raise ConfigurationError("MCP session binding is unavailable")
                binding.runner.replace_external_tools(
                    mcp_tools.tools,
                    session.mcp_tool_names,
                )
                session.mcp_tool_names = tuple(tool.definition.name for tool in mcp_tools.tools)
                payload = self._mcp_list_payload(mcp_tools)
                payload["refreshed"] = True
                return payload
            if query.operation == "read_resource":
                assert query.uri is not None
                contents = await mcp_tools.read_resource(query.uri)
                explicit_redactions = self._explicit_redactions()
                projected: list[dict[str, object]] = []
                for content in tuple(contents)[:32]:
                    raw = content.to_dict()
                    if "text" in raw:
                        raw["text"] = safe_output_text(
                            raw["text"],
                            MAX_MCP_RESOURCE_BYTES,
                            explicit_redactions=explicit_redactions,
                        )
                    if "blob" in raw:
                        raw["blob"] = safe_output_text(
                            raw["blob"],
                            MAX_MCP_RESOURCE_BYTES,
                            explicit_redactions=explicit_redactions,
                        )
                    projected.append(
                        cast(
                            dict[str, object],
                            _safe_mcp_extension_value(
                                raw,
                                explicit_redactions=explicit_redactions,
                            ),
                        )
                    )
                payload = {"contents": projected}
                if serialized_size_bytes(payload) > MAX_MCP_RESOURCE_BYTES:
                    raise RequestError.internal_error({"reason": "mcp_resource_too_large"})
                return payload
            if query.operation == "get_prompt":
                assert query.name is not None
                messages = await mcp_tools.get_prompt(query.name, dict(query.arguments))
                explicit_redactions = self._explicit_redactions()
                projected_messages = [
                    cast(
                        dict[str, object],
                        _safe_mcp_extension_value(
                            message.to_dict(),
                            explicit_redactions=explicit_redactions,
                        ),
                    )
                    for message in tuple(messages)[:128]
                ]
                payload = {"messages": projected_messages}
                if serialized_size_bytes(payload) > MAX_MCP_CONFIGURATION_BYTES:
                    raise RequestError.internal_error({"reason": "mcp_prompt_too_large"})
                return payload
        except asyncio.CancelledError:
            raise
        except RequestError:
            raise
        except Exception:
            raise RequestError.internal_error({"reason": "mcp_operation_failed"}) from None
        raise RequestError.internal_error({"reason": "mcp_operation_unsupported"})

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serve private, bounded session extensions.

        提供私有且有界的会话扩展.

        The extension is intentionally not advertised as a standard ACP
        capability.  It accepts an external ACP session ID and never exposes
        internal IDs, paths, raw metadata, or unbounded content.

        该扩展有意不作为标准 ACP 能力宣告.它接收 ACP 外部会话 ID,且绝不暴露内部 ID、
        路径、原始 metadata 或无界内容.
        """

        self._require_initialized()
        if method == ACP_MCP_EXTENSION:
            try:
                mcp_query = AcpMcpQuery.from_payload(params)
            except AcpMcpQueryError as error:
                raise _invalid_params(error.reason) from None
            return await self._mcp_extension(mcp_query)

        if method == ACP_CONTEXT_COMPACTION_EXTENSION:
            try:
                command_query = AcpSessionCommandQuery.from_payload(params)
            except AcpMcpQueryError as error:
                raise _invalid_params(error.reason) from None
            session = await self._active_session(_validated_session_id(command_query.session_id))
            if session.binding is None:
                raise RequestError.internal_error({"reason": "session_binding_unavailable"})
            try:
                compact_result = await session.binding.runner.compact_now()
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise RequestError.internal_error({"reason": "provider_failure"}) from None
            except ConfigurationError:
                raise RequestError.internal_error({"reason": "compaction_unavailable"}) from None
            except Exception:
                raise RequestError.internal_error({"reason": "compaction_failed"}) from None
            payload: dict[str, object] = {
                "status": compact_result.status.value,
                "triggered": compact_result.triggered,
            }
            for name in (
                "compaction_id",
                "source_item_count",
                "candidate_item_count",
                "summary_tokens",
                "summary_truncated",
            ):
                value = getattr(compact_result, name)
                if value is not None:
                    payload[name] = value
            if compact_result.outcome is not None:
                payload["outcome"] = {
                    "status": compact_result.outcome.status.value,
                    "reason_code": (
                        compact_result.outcome.reason_code.value
                        if compact_result.outcome.reason_code is not None
                        else None
                    ),
                    "finalized": compact_result.outcome.finalized,
                    "recoverable": compact_result.outcome.recoverable,
                }
            return payload

        if method == ACP_TURN_RECOVERY_EXTENSION:
            try:
                recovery_query = AcpTurnRecoveryQuery.from_payload(params)
            except AcpMcpQueryError as error:
                raise _invalid_params(error.reason) from None
            session = await self._active_session(_validated_session_id(recovery_query.session_id))
            binding = session.binding
            if binding is None:
                raise RequestError.internal_error({"reason": "session_binding_unavailable"})
            try:
                if recovery_query.operation == "inspect":
                    inspections = await binding.runner.inspect_recovery()
                    return {
                        "attempts": [inspection.to_dict() for inspection in inspections],
                    }
                assert recovery_query.turn_id is not None
                if recovery_query.operation == "abandon":
                    inspection = await binding.runner.abandon_recovery(
                        recovery_query.turn_id,
                        reason=recovery_query.reason,
                    )
                    return inspection.to_dict()
                result = await binding.runner.retry_recovery(recovery_query.turn_id)
                return {
                    "status": "retried",
                    "sessionId": recovery_query.session_id,
                    "steps": result.steps,
                }
            except ConfigurationError as error:
                message = str(error)
                if "retry" in message and "unavailable" in message:
                    reason = "recovery_retry_unavailable"
                elif "indeterminate" in message or "safely_retryable" not in message:
                    reason = "recovery_not_safe"
                else:
                    reason = "recovery_retry_unavailable"
                raise RequestError.internal_error({"reason": reason}) from None
            except SessionError:
                raise _session_not_found(recovery_query.session_id) from None
            except Exception:
                raise RequestError.internal_error({"reason": "recovery_operation_failed"}) from None

        if method == ACP_SUBAGENT_LIFECYCLE_EXTENSION:
            try:
                lifecycle_query = AcpSubagentLifecycleQuery.from_payload(params)
            except AcpSubagentLifecycleQueryError as error:
                raise _invalid_params(error.reason) from None
            external_session_id = _validated_session_id(lifecycle_query.session_id)
            if not self._service.subagent_lifecycle_available:
                raise RequestError.internal_error({"reason": "subagent_lifecycle_unavailable"})
            internal_session_id = await self._artifact_internal_session_id(external_session_id)
            try:
                lifecycle_result = await self._service.run_subagent_relationship_action(
                    internal_session_id,
                    lifecycle_query.task_id,
                    lifecycle_query.action,
                )
            except SessionError:
                raise _session_not_found(external_session_id) from None
            except ConfigurationError:
                raise RequestError.internal_error(
                    {"reason": "subagent_relationship_invalid"}
                ) from None
            except Exception:
                raise RequestError.internal_error({"reason": "subagent_lifecycle_failed"}) from None

            if (
                lifecycle_result.parent_session_id != internal_session_id
                or lifecycle_result.parent_task_id != lifecycle_query.task_id
                or lifecycle_result.action is not lifecycle_query.action
            ):
                raise RequestError.internal_error({"reason": "subagent_lifecycle_invalid_result"})
            if lifecycle_result.action is SubagentRelationshipAction.DELETE:
                return serialize_subagent_lifecycle_action(lifecycle_result.action, deleted=True)
            if lifecycle_result.action is SubagentRelationshipAction.RESUME:
                external_child_id = await self._lifecycle_external_session_id(
                    lifecycle_result.child_session_id
                )
                return serialize_subagent_lifecycle_action(
                    lifecycle_result.action,
                    session_id=external_child_id,
                )
            if lifecycle_result.forked_session_id is None:
                raise RequestError.internal_error({"reason": "subagent_lifecycle_invalid_result"})
            external_forked_id = await self._lifecycle_external_session_id(
                lifecycle_result.forked_session_id
            )
            return serialize_subagent_lifecycle_action(
                lifecycle_result.action,
                session_id=external_forked_id,
            )

        if method == ACP_READ_ONLY_SUBAGENT_EXTENSION:
            try:
                subagent_query = AcpReadOnlySubagentQuery.from_payload(params)
            except AcpReadOnlySubagentQueryError as error:
                raise _invalid_params(error.reason) from None
            external_session_id = _validated_session_id(subagent_query.session_id)
            if not self._service.read_only_subagent_available:
                raise RequestError.internal_error({"reason": "subagent_unavailable"})
            # A persisted session summary is not an authorization manifest.
            # The explicit child may run only while its actual ACP parent
            # binding is active and can supply immutable capability metadata.
            parent_session = await self._active_session(external_session_id)
            async with parent_session.state_lock:
                parent_binding = parent_session.binding
                if parent_binding is None or parent_binding.capabilities is None:
                    raise RequestError.internal_error({"reason": "parent_capability_unavailable"})
                parent_capabilities = parent_binding.capabilities
            internal_session_id = await self._artifact_internal_session_id(external_session_id)
            try:
                projection = await self._service.run_read_only_subagent(
                    internal_session_id,
                    subagent_query.prompt,
                    parent_capabilities=parent_capabilities,
                    max_steps=subagent_query.max_steps,
                )
            except SessionError:
                raise _session_not_found(external_session_id) from None
            except ProviderError:
                raise RequestError.internal_error({"reason": "provider_failure"}) from None
            except ConfigurationError:
                raise RequestError.internal_error({"reason": "subagent_unavailable"}) from None
            except Exception:
                raise RequestError.internal_error({"reason": "subagent_failed"}) from None
            return serialize_subagent_result(projection)

        if method != ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION:
            raise RequestError.method_not_found(f"_{method}")
        try:
            artifact_query = AcpToolOutputArtifactQuery.from_payload(params)
        except AcpToolOutputArtifactQueryError as error:
            raise _invalid_params(error.reason) from None
        external_session_id = _validated_session_id(artifact_query.session_id)
        if not self._service.tool_output_artifacts_available:
            raise RequestError.internal_error({"reason": "artifact_query_unavailable"})
        internal_session_id = await self._artifact_internal_session_id(external_session_id)
        if artifact_query.artifact_id is None:
            try:
                artifacts = await self._service.list_tool_output_artifacts(
                    internal_session_id,
                    limit=artifact_query.limit,
                )
            except SessionError:
                raise _session_not_found(external_session_id) from None
            return _artifact_list_payload(artifacts)

        try:
            artifact = await self._service.read_tool_output_artifact(
                internal_session_id,
                artifact_query.artifact_id,
                max_bytes=artifact_query.max_bytes,
            )
        except SessionError:
            raise _invalid_params("artifact_not_found") from None
        return _artifact_read_payload(
            artifact.artifact.artifact_id,
            artifact.content,
            artifact.read_truncated,
            explicit_redactions=self._explicit_redactions(),
        )

    async def _bind_internal_session(
        self,
        session: _AcpSession,
        internal_session_id: str,
    ) -> None:
        if (
            session.internal_session_id is not None
            and session.internal_session_id != internal_session_id
        ):
            raise SessionError("ACP session changed its internal session identity")
        await self._service.bind_session_alias(
            _ACP_SESSION_ALIAS_NAMESPACE,
            session.session_id,
            internal_session_id,
        )
        session.internal_session_id = internal_session_id

    async def _capture_runner_session(
        self,
        session: _AcpSession,
        binding: ConversationBinding,
        *,
        suppress_errors: bool,
    ) -> None:
        internal_session_id = binding.runner.session_id
        if internal_session_id is None:
            return
        try:
            await asyncio.shield(self._bind_internal_session(session, internal_session_id))
        except Exception:
            if not suppress_errors:
                raise

    async def prompt(
        self,
        session_id: str,
        prompt: list[PromptBlock],
        **_kwargs: Any,
    ) -> PromptResponse:
        self._require_initialized()
        session = await self._active_session(session_id)
        converted = convert_prompt_content(prompt)
        current_task = asyncio.current_task()
        if current_task is None:
            raise RequestError.internal_error({"reason": "prompt_task_unavailable"})
        client = self._client
        if client is None:
            raise RequestError.internal_error({"reason": "client_unavailable"})
        mapper = _AcpEventMapper(
            client=client,
            session_id=session_id,
            context_window_tokens=session.context_window_tokens,
            explicit_redactions=self._explicit_redactions(),
            on_session_started=lambda internal_id: self._bind_internal_session(
                session,
                internal_id,
            ),
        )
        async with session.state_lock:
            if session.closed or session.closing or session.binding is None:
                raise _session_not_active(session_id)
            if session.prompt_task is not None:
                raise RequestError(
                    _SESSION_BUSY,
                    "Session already has an active prompt",
                    {"reason": "prompt_already_active"},
                )
            session.prompt_task = current_task
            session.mapper = mapper
            session.cancel_requested = False
            binding = session.binding

        try:
            turn_service = self._service.bind_runner(binding.runner)
            result = await turn_service.run_turn(
                RunTurnRequest(
                    converted.content,
                    content_parts=converted.content_parts,
                    expected_session_id=session.internal_session_id,
                ),
                sink=mapper,
            )
            if result.session_id is None:
                raise RequestError.internal_error({"reason": "session_identity_unavailable"})
            await self._bind_internal_session(session, result.session_id)
            if session.cancel_requested or session.closing:
                return PromptResponse(stop_reason="cancelled")
            return PromptResponse(
                stop_reason=execution_outcome_stop_reason(result.outcome) or mapper.stop_reason,
                field_meta=execution_outcome_metadata(result.outcome),
            )
        except asyncio.CancelledError:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            return PromptResponse(stop_reason="cancelled")
        except ProviderError as error:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            if session.cancel_requested or session.closing:
                return PromptResponse(stop_reason="cancelled")
            if "exceeded the maximum" in str(error):
                return PromptResponse(stop_reason="max_turn_requests")
            raise RequestError.internal_error({"reason": "provider_failure"}) from None
        except ConfigurationError as error:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            if session.cancel_requested or session.closing:
                return PromptResponse(stop_reason="cancelled")
            if "unresolved interrupted turn" in str(error):
                raise RequestError.internal_error({"reason": "turn_recovery_required"}) from None
            raise RequestError.internal_error({"reason": "prompt_configuration"}) from None
        except RequestError:
            raise
        except Exception:
            await self._capture_runner_session(session, binding, suppress_errors=True)
            if session.cancel_requested or session.closing:
                return PromptResponse(stop_reason="cancelled")
            raise RequestError.internal_error({"reason": "prompt_failure"}) from None
        finally:
            async with session.state_lock:
                if session.prompt_task is current_task:
                    session.prompt_task = None
                    session.mapper = None
                    session.pending_approval_id = None
                    session.cancel_requested = False

    async def _request_permission(
        self,
        session_id: str,
        request: PermissionRequest,
    ) -> PermissionApproval:
        session = await self._active_session(session_id)
        client = self._client
        mapper = session.mapper
        if client is None or mapper is None:
            return PermissionApproval.deny("ACP client approval interface is unavailable")
        async with session.state_lock:
            if session.pending_approval_id is not None:
                return PermissionApproval.deny("another ACP approval is already pending")
            session.pending_approval_id = request.call_id
        try:
            options = [
                PermissionOption(
                    option_id="allow_once",
                    name="Allow once",
                    kind="allow_once",
                ),
                PermissionOption(
                    option_id="allow_session",
                    name="Allow identical actions for this session",
                    kind="allow_always",
                ),
                PermissionOption(
                    option_id="deny",
                    name="Deny",
                    kind="reject_once",
                ),
            ]
            response = await client.request_permission(
                session_id,
                mapper.permission_tool_call(request),
                options,
            )
            outcome = response.outcome
            if outcome.outcome != "selected":
                return PermissionApproval.deny("ACP client cancelled approval")
            if outcome.option_id == "allow_once":
                return PermissionApproval.allow_once("approved once by ACP client")
            if outcome.option_id == "allow_session":
                if request.scope_key is None:
                    return PermissionApproval.allow_once(
                        "unscoped action approved once by ACP client"
                    )
                return PermissionApproval.allow_session("approved for this ACP session by client")
            return PermissionApproval.deny("denied by ACP client")
        except asyncio.CancelledError:
            raise
        except Exception:
            return PermissionApproval.deny("ACP client approval failed")
        finally:
            async with session.state_lock:
                if session.pending_approval_id == request.call_id:
                    session.pending_approval_id = None

    async def cancel(self, session_id: str, **_kwargs: Any) -> None:
        async with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None or session.closed or session.closing:
            return
        async with session.state_lock:
            task = session.prompt_task
            if task is not None and not task.done():
                session.cancel_requested = True
        if task is not None and not task.done():
            task.cancel()

    async def close_session(
        self,
        session_id: str,
        **_kwargs: Any,
    ) -> CloseSessionResponse:
        self._require_initialized()
        async with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise _session_not_active(session_id)
        async with session.state_lock:
            if session.closed or session.closing:
                raise _session_not_active(session_id)
            session.closing = True
            session.cancel_requested = True
        await self._cleanup_session(session)
        async with self._registry_lock:
            if self._sessions.get(session_id) is session:
                del self._sessions[session_id]
        return CloseSessionResponse()

    async def _cleanup_session(self, session: _AcpSession) -> None:
        async with session.cleanup_lock:
            if session.closed:
                return
            async with session.state_lock:
                task = session.prompt_task
            current = asyncio.current_task()
            if task is not None and task is not current and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            binding = session.binding
            mcp_tools = session.mcp_tools
            client_terminal = session.client_terminal
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if client_terminal is not None:
                await asyncio.shield(client_terminal.shutdown())
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            session.binding = None
            session.mcp_tools = None
            session.client_terminal = None
            session.mapper = None
            session.pending_approval_id = None
            session.closed = True

    async def shutdown(self) -> None:
        async with self._registry_lock:
            self._shutting_down = True
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            pending_tasks = tuple(self._pending_session_tasks.values())
            self._pending_session_tasks.clear()
        async with self._list_cursor_lock:
            self._list_cursors.clear()
        current = asyncio.current_task()
        for task in pending_tasks:
            if task is not current and not task.done():
                task.cancel()
        for session in sessions:
            async with session.state_lock:
                session.closing = True
                session.cancel_requested = True
        if sessions:
            await asyncio.gather(
                *(self._cleanup_session(session) for session in sessions),
                return_exceptions=True,
            )
        if pending_tasks:
            await asyncio.gather(
                *(task for task in pending_tasks if task is not current),
                return_exceptions=True,
            )


def _build_acp_router(agent: NeuroCodeAcpAgent) -> MessageRouter:
    """Extend the SDK 0.11 router with its generated stable delete route.

    使用生成的稳定删除路由扩展 SDK 0.11 路由器."""

    router = build_agent_router(cast(Agent, agent), use_unstable_protocol=True)
    router.route_request(
        AGENT_METHODS["session_delete"],
        DeleteSessionRequest,
        agent,
        "delete_session",
        adapt_result=normalize_result,
    )
    return router


class _AcpSdkConnection:
    """Small SDK connection adapter until its agent router registers delete.

    在 Agent 路由器注册删除操作前使用的小型 SDK 连接适配器."""

    def __init__(
        self,
        agent: NeuroCodeAcpAgent,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
    ) -> None:
        self._connection = Connection(
            _build_acp_router(agent),
            writer,
            reader,
            listening=False,
        )
        agent.on_connect(cast(Client, self))

    async def listen(self) -> None:
        await self._connection.main_loop()

    async def close(self) -> None:
        await self._connection.close()

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,
    ) -> None:
        await notify_model(
            self._connection,
            CLIENT_METHODS["session_update"],
            SessionNotification(
                session_id=session_id,
                update=update,
                field_meta=kwargs or None,
            ),
        )

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        return await request_model(
            self._connection,
            CLIENT_METHODS["session_request_permission"],
            RequestPermissionRequest(
                session_id=session_id,
                tool_call=tool_call,
                options=options,
                field_meta=kwargs or None,
            ),
            RequestPermissionResponse,
        )


class _WebSocketWriter:
    """Minimal asyncio writer bridge for ACP's newline JSON sender."""

    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._pending = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("WebSocket ACP writer is closed")
        self._pending.extend(data)

    async def drain(self) -> None:
        if self._closed or not self._pending:
            return
        payload = bytes(self._pending)
        self._pending.clear()
        await self._websocket.send(payload)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return

    def is_closing(self) -> bool:
        return self._closed

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default


async def serve_acp_websocket(
    service: AcpApplicationService,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> None:
    """Serve the same ACP router over bounded WebSocket JSON messages.

    The WebSocket is only a transport bridge; ACP request validation,
    permissions, workspace checks, and session ownership remain unchanged.
    """

    if not isinstance(host, str) or not host or "\x00" in host or len(host.encode("utf-8")) > 256:
        raise ConfigurationError("WebSocket ACP host is invalid")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ConfigurationError("WebSocket ACP port is invalid")
    try:
        from websockets.asyncio.server import serve
    except ImportError:
        raise ConfigurationError(
            "WebSocket ACP support requires the websockets dependency"
        ) from None

    async def handle(websocket: Any) -> None:
        agent = NeuroCodeAcpAgent(service)
        reader = asyncio.StreamReader(limit=ACP_STDIO_BUFFER_LIMIT_BYTES)
        writer = _WebSocketWriter(websocket)
        connection = _AcpSdkConnection(agent, cast(asyncio.StreamWriter, writer), reader)
        feeder: asyncio.Task[None] | None = None

        async def feed_messages() -> None:
            try:
                async for message in websocket:
                    if isinstance(message, str):
                        data = message.encode("utf-8")
                    elif isinstance(message, bytes):
                        data = message
                    else:
                        raise ConnectionError("WebSocket ACP message type is unsupported")
                    if not data or len(data) > ACP_STDIO_BUFFER_LIMIT_BYTES:
                        raise ConnectionError("WebSocket ACP message exceeds the size limit")
                    if not data.endswith(b"\n"):
                        data += b"\n"
                    reader.feed_data(data)
            finally:
                reader.feed_eof()

        try:
            feeder = asyncio.create_task(feed_messages(), name="neuro-code-acp-websocket-reader")
            await connection.listen()
        finally:
            if feeder is not None and not feeder.done():
                feeder.cancel()
            if feeder is not None:
                await asyncio.gather(feeder, return_exceptions=True)
            await asyncio.shield(connection.close())
            await asyncio.shield(agent.shutdown())

    async with serve(
        handle,
        host,
        port,
        max_size=ACP_STDIO_BUFFER_LIMIT_BYTES,
        max_queue=16,
    ):
        await asyncio.Future()


async def serve_acp(service: AcpApplicationService) -> None:
    """Serve ACP on stdio through the official SDK framing and router.

    通过官方 SDK 帧协议和路由器在 stdio 上提供 ACP 服务."""

    agent = NeuroCodeAcpAgent(service)
    connection: _AcpSdkConnection | None = None
    try:
        reader, writer = await stdio_streams(limit=ACP_STDIO_BUFFER_LIMIT_BYTES)
        connection = _AcpSdkConnection(
            agent,
            writer,
            reader,
        )
        await connection.listen()
    finally:
        if connection is not None:
            await asyncio.shield(connection.close())
        await asyncio.shield(agent.shutdown())


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
