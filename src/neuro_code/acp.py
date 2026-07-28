from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from acp.agent.router import build_agent_router
from acp.core import Connection
from acp.exceptions import RequestError
from acp.interfaces import Agent, Client
from acp.meta import AGENT_METHODS, CLIENT_METHODS
from acp.router import MessageRouter
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    Annotations,
    AudioContentBlock,
    ClientCapabilities,
    CloseSessionResponse,
    ContentToolCallContent,
    DeleteSessionRequest,
    DeleteSessionResponse,
    EmbeddedResourceContentBlock,
    FileEditToolCallContent,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResourceContentBlock,
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
    TerminalToolCallContent,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)
from acp.stdio import stdio_streams
from acp.utils import normalize_result, notify_model, request_model

from neuro_code import __version__
from neuro_code.application.acp.contracts import (
    MAX_MCP_SERVERS,
    AcpMcpServerConfig,
    AcpMcpToolError,
    AcpMcpTools,
    AcpResumeUnavailableError,
    AcpWorkspaceValidationError,
)
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionRequest,
)
from neuro_code.application.runtime.approval import SessionApprovalBroker
from neuro_code.application.runtime.profile_conversation import ConversationBinding
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.messages import Message, Role, SessionItem, ToolCall
from neuro_code.domain.sessions import SessionSummary
from neuro_code.shared.errors import ConfigurationError, ProviderError, SessionError, ToolError
from neuro_code.shared.redaction import redact_sensitive_text

ACP_PROTOCOL_VERSION = 1
ACP_STDIO_BUFFER_LIMIT_BYTES = 1024 * 1024

MAX_PROMPT_BLOCKS = 96
MAX_TEXT_BLOCKS = 64
MAX_TEXT_BLOCK_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 256 * 1024
MAX_RESOURCE_LINKS = 32
MAX_RESOURCE_LINK_BYTES = 64 * 1024
MAX_RESOURCE_URI_BYTES = 4 * 1024
MAX_RESOURCE_NAME_BYTES = 512
MAX_RESOURCE_FIELD_BYTES = 2 * 1024
MAX_ANNOTATIONS_BYTES = 4 * 1024
MAX_ANNOTATION_AUDIENCE = 16
MAX_ANNOTATION_AUDIENCE_BYTES = 128
MAX_UPDATE_TEXT_BYTES = 64 * 1024
MAX_TURN_UPDATE_BYTES = 1024 * 1024
MAX_TOOL_CONTENT_BYTES = 32 * 1024
MAX_SESSION_ID_BYTES = 512
MAX_LOAD_SESSION_ITEMS = 2_000
MAX_LOAD_SESSION_UPDATES = 4_096
MAX_LOAD_SESSION_BYTES = 2 * 1024 * 1024
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
MAX_MCP_CONFIGURATION_BYTES = 256 * 1024

_SESSION_NOT_ACTIVE = -32001
_SESSION_NOT_FOUND = -32002
_SESSION_BUSY = -32003
_ACP_SESSION_ALIAS_NAMESPACE = "acp-v1"
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ALLOWED_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)
_TOOL_KINDS: dict[str, Literal["read", "edit", "search", "execute", "other"]] = {
    "read_file": "read",
    "list_dir": "read",
    "grep": "search",
    "search_replace": "edit",
    "bash": "execute",
    "task_output": "execute",
    "wait_tasks": "execute",
    "kill_task": "execute",
}

PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
McpServer = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio
StopReason = Literal[
    "end_turn",
    "max_tokens",
    "max_turn_requests",
    "refusal",
    "cancelled",
]
HistoryUpdate = UserMessageChunk | AgentMessageChunk | ToolCallStart | ToolCallProgress


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


def _bounded_identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    if len(value.encode("utf-8")) <= 256 and all(
        ord(character) >= 32 and ord(character) != 127 for character in value
    ):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return f"id-{digest}"


def _validated_session_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_params("session_id_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid_params("session_id_invalid")
    if len(value.encode("utf-8")) > MAX_SESSION_ID_BYTES:
        raise _invalid_params("session_id_too_large")
    return value


def _sanitize_controls(text: str) -> str:
    return "".join(
        character
        if character in {"\n", "\r", "\t"} or ord(character) >= 32
        else "\N{REPLACEMENT CHARACTER}"
        for character in text
    ).replace("\x7f", "\N{REPLACEMENT CHARACTER}")


def _truncate_utf8(text: str, limit: int, *, marker: str = "\n… [truncated]") -> str:
    payload = text.encode("utf-8")
    if len(payload) <= limit:
        return text
    marker_bytes = marker.encode("utf-8")
    retained = payload[: max(0, limit - len(marker_bytes))]
    while retained:
        try:
            prefix = retained.decode("utf-8")
        except UnicodeDecodeError:
            retained = retained[:-1]
            continue
        return prefix + marker
    return marker[:limit]


def _bounded_input_text(value: str, *, limit: int, field_name: str) -> str:
    sanitized = _sanitize_controls(value)
    if len(sanitized.encode("utf-8")) > limit:
        raise _invalid_params(f"{field_name}_too_large")
    return sanitized


def _serialized_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


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
        if not isinstance(server, McpServerStdio):
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
            AcpMcpServerConfig(
                name=name,
                command=command,
                args=tuple(arguments),
                env=tuple(environment),
            )
        )
    if _serialized_size(serialized) > MAX_MCP_CONFIGURATION_BYTES:
        raise _invalid_params("mcp_configuration_too_large")
    return tuple(configurations)


def _annotations_payload(annotations: Annotations | None) -> dict[str, object] | None:
    if annotations is None:
        return None
    payload: dict[str, object] = {}
    if annotations.audience is not None:
        if len(annotations.audience) > MAX_ANNOTATION_AUDIENCE:
            raise _invalid_params("resource_annotations_too_large")
        payload["audience"] = [
            _bounded_input_text(
                audience,
                limit=MAX_ANNOTATION_AUDIENCE_BYTES,
                field_name="resource_annotation_audience",
            )
            for audience in annotations.audience
        ]
    if annotations.last_modified is not None:
        payload["lastModified"] = _bounded_input_text(
            annotations.last_modified,
            limit=MAX_RESOURCE_FIELD_BYTES,
            field_name="resource_annotation_last_modified",
        )
    if annotations.priority is not None:
        if not math.isfinite(annotations.priority):
            raise _invalid_params("resource_annotation_priority_invalid")
        payload["priority"] = annotations.priority
    if _serialized_size(payload) > MAX_ANNOTATIONS_BYTES:
        raise _invalid_params("resource_annotations_too_large")
    return payload or None


def _resource_payload(resource: ResourceContentBlock) -> dict[str, object]:
    payload: dict[str, object] = {
        "uri": _bounded_input_text(
            resource.uri,
            limit=MAX_RESOURCE_URI_BYTES,
            field_name="resource_uri",
        ),
        "name": _bounded_input_text(
            resource.name,
            limit=MAX_RESOURCE_NAME_BYTES,
            field_name="resource_name",
        ),
    }
    for source_name, wire_name in (
        ("title", "title"),
        ("description", "description"),
        ("mime_type", "mimeType"),
    ):
        value = getattr(resource, source_name)
        if value is not None:
            payload[wire_name] = _bounded_input_text(
                value,
                limit=MAX_RESOURCE_FIELD_BYTES,
                field_name=f"resource_{source_name}",
            )
    if resource.size is not None:
        if resource.size < 0:
            raise _invalid_params("resource_size_invalid")
        payload["size"] = resource.size
    annotations = _annotations_payload(resource.annotations)
    if annotations is not None:
        payload["annotations"] = annotations
    return payload


def convert_prompt_content(prompt: list[PromptBlock]) -> str:
    """Convert ACP baseline prompt blocks to bounded, ordered model text."""

    if not prompt:
        raise _invalid_params("prompt_empty")
    if len(prompt) > MAX_PROMPT_BLOCKS:
        raise _invalid_params("too_many_prompt_blocks")

    rendered: list[str] = []
    text_count = 0
    resource_count = 0
    resource_bytes = 0
    for block in prompt:
        if isinstance(block, TextContentBlock):
            text_count += 1
            if text_count > MAX_TEXT_BLOCKS:
                raise _invalid_params("too_many_text_blocks")
            rendered.append(
                _bounded_input_text(
                    block.text,
                    limit=MAX_TEXT_BLOCK_BYTES,
                    field_name="text_block",
                )
            )
            continue
        if isinstance(block, ResourceContentBlock):
            resource_count += 1
            if resource_count > MAX_RESOURCE_LINKS:
                raise _invalid_params("too_many_resource_links")
            payload = _resource_payload(block)
            serialized = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            resource_bytes += len(serialized.encode("utf-8"))
            if resource_bytes > MAX_RESOURCE_LINK_BYTES:
                raise _invalid_params("resource_links_too_large")
            rendered.append(f"<resource_link>{serialized}</resource_link>")
            continue
        raise _invalid_params("unsupported_prompt_content")

    converted = "\n\n".join(rendered)
    if not converted.strip():
        raise _invalid_params("prompt_empty")
    if len(converted.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise _invalid_params("prompt_too_large")
    return converted


def _map_stop_reason(value: object) -> StopReason:
    if value in _ALLOWED_STOP_REASONS:
        return cast(StopReason, value)
    if value in {"length", "max_output_tokens"}:
        return "max_tokens"
    return "end_turn"


def _safe_output_text(
    value: object,
    limit: int,
    *,
    explicit_redactions: tuple[str, ...],
) -> str:
    text = value if isinstance(value, str) else ""
    text = _sanitize_controls(text)
    text = redact_sensitive_text(text, explicit_values=explicit_redactions)
    return _truncate_utf8(text, limit)


def _tool_location_from_call(
    tool_call: ToolCall,
    *,
    explicit_redactions: tuple[str, ...],
) -> list[ToolCallLocation] | None:
    path = tool_call.arguments.get("path")
    if not isinstance(path, str) or not path:
        return None
    return [
        ToolCallLocation(
            path=_safe_output_text(
                path,
                MAX_RESOURCE_FIELD_BYTES,
                explicit_redactions=explicit_redactions,
            )
        )
    ]


def _history_updates(
    items: Sequence[SessionItem],
    *,
    explicit_redactions: tuple[str, ...],
) -> tuple[HistoryUpdate, ...]:
    if len(items) > MAX_LOAD_SESSION_ITEMS:
        raise _invalid_params("session_history_too_large")

    updates: list[HistoryUpdate] = []
    pending_tools: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Message):
            continue
        if item.role is Role.USER:
            content = _safe_output_text(
                item.content,
                MAX_UPDATE_TEXT_BYTES,
                explicit_redactions=explicit_redactions,
            )
            if content:
                updates.append(
                    UserMessageChunk(
                        session_update="user_message_chunk",
                        content=TextContentBlock(type="text", text=content),
                        message_id=str(uuid.uuid4()),
                    )
                )
            continue
        if item.role is Role.ASSISTANT:
            content = _safe_output_text(
                item.content,
                MAX_UPDATE_TEXT_BYTES,
                explicit_redactions=explicit_redactions,
            )
            if content:
                updates.append(
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=content),
                        message_id=str(uuid.uuid4()),
                    )
                )
            for tool_call in item.tool_calls:
                call_id = _bounded_identifier(tool_call.id)
                name = (
                    _safe_output_text(
                        tool_call.name,
                        256,
                        explicit_redactions=explicit_redactions,
                    )
                    or "tool"
                )
                pending_tools[call_id] = name
                updates.append(
                    ToolCallStart(
                        session_update="tool_call",
                        tool_call_id=call_id,
                        title=name,
                        kind=_TOOL_KINDS.get(name, "other"),
                        status="pending",
                        locations=_tool_location_from_call(
                            tool_call,
                            explicit_redactions=explicit_redactions,
                        ),
                    )
                )
            continue
        if item.role is Role.TOOL:
            call_id = _bounded_identifier(item.tool_call_id)
            if call_id not in pending_tools:
                name = (
                    _safe_output_text(
                        item.name,
                        256,
                        explicit_redactions=explicit_redactions,
                    )
                    or "tool"
                )
                pending_tools[call_id] = name
                updates.append(
                    ToolCallStart(
                        session_update="tool_call",
                        tool_call_id=call_id,
                        title=name,
                        kind=_TOOL_KINDS.get(name, "other"),
                        status="pending",
                    )
                )
            content = _safe_output_text(
                item.content,
                MAX_TOOL_CONTENT_BYTES,
                explicit_redactions=explicit_redactions,
            )
            blocks: (
                list[ContentToolCallContent | FileEditToolCallContent | TerminalToolCallContent]
                | None
            ) = (
                [
                    ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text=content),
                    )
                ]
                if content
                else None
            )
            updates.append(
                ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=call_id,
                    status="completed",
                    content=blocks,
                )
            )
            pending_tools.pop(call_id, None)

    updates.extend(
        ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=call_id,
            status="failed",
        )
        for call_id in pending_tools
    )
    if len(updates) > MAX_LOAD_SESSION_UPDATES:
        raise _invalid_params("session_history_too_large")
    total_bytes = sum(
        _serialized_size(update.model_dump(by_alias=True, exclude_none=True)) for update in updates
    )
    if total_bytes > MAX_LOAD_SESSION_BYTES:
        raise _invalid_params("session_history_too_large")
    return tuple(updates)


class _AcpEventMapper:
    def __init__(
        self,
        *,
        client: Client,
        session_id: str,
        context_window_tokens: int | None,
        explicit_redactions: tuple[str, ...],
        on_session_started: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._context_window_tokens = context_window_tokens
        self._explicit_redactions = explicit_redactions
        self._on_session_started = on_session_started
        self._message_id = str(uuid.uuid4())
        self._tool_names: dict[str, str] = {}
        self._started_tools: set[str] = set()
        self._sent_text_bytes = 0
        self.stop_reason: StopReason = "end_turn"

    def tool_call_id(self, value: object) -> str:
        return _bounded_identifier(value)

    def permission_tool_call(self, request: PermissionRequest) -> ToolCallUpdate:
        return ToolCallUpdate(
            tool_call_id=self.tool_call_id(request.call_id),
            kind=_TOOL_KINDS.get(request.tool_name, "other"),
            status="pending",
            title=self._safe_text(request.summary, MAX_RESOURCE_FIELD_BYTES),
        )

    def _safe_text(self, value: object, limit: int) -> str:
        return _safe_output_text(
            value,
            limit,
            explicit_redactions=self._explicit_redactions,
        )

    def _tool_location(self, event: AgentEvent) -> list[ToolCallLocation] | None:
        arguments = event.data.get("arguments")
        if not isinstance(arguments, dict):
            return None
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return None
        return [ToolCallLocation(path=self._safe_text(path, MAX_RESOURCE_FIELD_BYTES))]

    async def _send_tool_start(self, event: AgentEvent) -> None:
        call_id = self.tool_call_id(event.data.get("id"))
        name = self._safe_text(event.data.get("name"), 256) or "tool"
        self._tool_names[call_id] = name
        self._started_tools.add(call_id)
        await self._client.session_update(
            self._session_id,
            ToolCallStart(
                session_update="tool_call",
                tool_call_id=call_id,
                title=name,
                kind=_TOOL_KINDS.get(name, "other"),
                status="pending",
                locations=self._tool_location(event),
            ),
        )

    async def _ensure_tool_start(self, event: AgentEvent) -> str:
        call_id = self.tool_call_id(event.data.get("id"))
        if call_id not in self._started_tools:
            await self._send_tool_start(event)
        return call_id

    async def __call__(self, event: AgentEvent) -> None:
        if event.kind is AgentEventKind.SESSION_STARTED:
            session_id = event.data.get("session_id")
            if self._on_session_started is not None and isinstance(session_id, str) and session_id:
                await self._on_session_started(session_id)
            return
        if event.kind is AgentEventKind.TEXT_DELTA:
            text = self._safe_text(event.data.get("text"), MAX_UPDATE_TEXT_BYTES)
            remaining = MAX_TURN_UPDATE_BYTES - self._sent_text_bytes
            if remaining <= 0 or not text:
                return
            text = _truncate_utf8(text, remaining)
            self._sent_text_bytes += len(text.encode("utf-8"))
            await self._client.session_update(
                self._session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=text),
                    message_id=self._message_id,
                ),
            )
            return
        if event.kind is AgentEventKind.TOOL_REQUESTED:
            await self._send_tool_start(event)
            return
        if event.kind is AgentEventKind.TOOL_STARTED:
            call_id = await self._ensure_tool_start(event)
            await self._client.session_update(
                self._session_id,
                ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=call_id,
                    status="in_progress",
                ),
            )
            return
        if event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}:
            call_id = await self._ensure_tool_start(event)
            content = self._safe_text(event.data.get("content"), MAX_TOOL_CONTENT_BYTES)
            blocks: (
                list[ContentToolCallContent | FileEditToolCallContent | TerminalToolCallContent]
                | None
            ) = (
                [
                    ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text=content),
                    )
                ]
                if content
                else None
            )
            await self._client.session_update(
                self._session_id,
                ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=call_id,
                    status=(
                        "completed" if event.kind is AgentEventKind.TOOL_COMPLETED else "failed"
                    ),
                    content=blocks,
                ),
            )
            return
        if event.kind is AgentEventKind.CONTEXT_USAGE_UPDATED:
            used = event.data.get("used_tokens")
            if isinstance(used, int) and used >= 0 and self._context_window_tokens is not None:
                await self._client.session_update(
                    self._session_id,
                    UsageUpdate(
                        session_update="usage_update",
                        used=used,
                        size=self._context_window_tokens,
                    ),
                )
            return
        if event.kind is AgentEventKind.TURN_COMPLETED:
            self.stop_reason = _map_stop_reason(event.data.get("stop_reason"))


@dataclass(slots=True)
class _AcpSession:
    session_id: str
    binding: ConversationBinding | None
    approvals: SessionApprovalBroker
    context_window_tokens: int | None
    mcp_tools: AcpMcpTools | None
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
    """Official-SDK ACP v1 adapter for one workspace-bound process."""

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
                prompt_capabilities=None,
                mcp_capabilities=None,
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
        return await self._service.open_mcp_tools(configurations)

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
        try:
            mcp_tools = await self._open_mcp_tools(mcp_configurations)
            opened_binding = await self._service.create_binding(
                approver=approvals,
                additional_tools=mcp_tools.tools if mcp_tools is not None else (),
                additional_workspace_roots=additional_workspace_roots,
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
            )
            if await self._publish_session(session):
                binding = None
                mcp_tools = None
                return NewSessionResponse(session_id=session_id)
            raise RequestError.internal_error({"reason": "connection_closing"})
        finally:
            await self._release_session_reservation(session_id)
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())

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
                internal_session_id=internal_session_id,
            )
            if await self._publish_session(session):
                binding = None
                mcp_tools = None
                return
            raise RequestError.internal_error({"reason": "connection_closing"})
        finally:
            await self._release_session_reservation(external_session_id)
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())

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
                internal_session_id=forked_internal_session_id,
            )
            if await self._publish_session(session):
                binding = None
                mcp_tools = None
                forked_internal_session_id = None
                return ForkSessionResponse(session_id=forked_external_session_id)
            raise RequestError.internal_error({"reason": "connection_closing"})
        finally:
            await self._release_session_reservation(forked_external_session_id)
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
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
            result = await binding.runner.run(converted, sink=mapper)
            if result.session_id is None:
                raise RequestError.internal_error({"reason": "session_identity_unavailable"})
            await self._bind_internal_session(session, result.session_id)
            if session.cancel_requested or session.closing:
                return PromptResponse(stop_reason="cancelled")
            return PromptResponse(stop_reason=mapper.stop_reason)
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
            if mcp_tools is not None:
                await asyncio.shield(mcp_tools.close())
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            session.binding = None
            session.mcp_tools = None
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
    """Extend the SDK 0.11 router with its generated stable delete route."""

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
    """Small SDK connection adapter until its agent router registers delete."""

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


async def serve_acp(service: AcpApplicationService) -> None:
    """Serve ACP on stdio through the official SDK framing and router."""

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
    "ACP_PROTOCOL_VERSION",
    "ACP_STDIO_BUFFER_LIMIT_BYTES",
    "NeuroCodeAcpAgent",
    "convert_prompt_content",
    "serve_acp",
]
