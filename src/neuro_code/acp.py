from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from acp.core import run_agent
from acp.exceptions import RequestError
from acp.interfaces import Agent, Client
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    Annotations,
    AudioContentBlock,
    ClientCapabilities,
    CloseSessionResponse,
    ContentToolCallContent,
    EmbeddedResourceContentBlock,
    FileEditToolCallContent,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    ResourceContentBlock,
    SessionCapabilities,
    SessionCloseCapabilities,
    SseMcpServer,
    TerminalToolCallContent,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
)

from neuro_code import __version__
from neuro_code.application import ApplicationComposition
from neuro_code.async_utils import run_blocking
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.errors import ProviderError
from neuro_code.permissions import (
    PermissionApproval,
    PermissionRequest,
)
from neuro_code.redaction import redact_sensitive_text
from neuro_code.runtime import ConversationBinding, SessionApprovalBroker
from neuro_code.workspace import workspaces_match

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

_SESSION_NOT_ACTIVE = -32001
_SESSION_BUSY = -32003
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


def _bounded_identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    if len(value.encode("utf-8")) <= 256 and all(
        ord(character) >= 32 and ord(character) != 127 for character in value
    ):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return f"id-{digest}"


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


class _AcpEventMapper:
    def __init__(
        self,
        *,
        client: Client,
        session_id: str,
        context_window_tokens: int | None,
        explicit_redactions: tuple[str, ...],
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._context_window_tokens = context_window_tokens
        self._explicit_redactions = explicit_redactions
        self._message_id = f"message-{uuid.uuid4().hex}"
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
        text = value if isinstance(value, str) else ""
        text = _sanitize_controls(text)
        text = redact_sensitive_text(text, explicit_values=self._explicit_redactions)
        return _truncate_utf8(text, limit)

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
    internal_session_id: str | None = None
    prompt_task: asyncio.Task[Any] | None = None
    mapper: _AcpEventMapper | None = None
    pending_approval_id: str | None = None
    cancel_requested: bool = False
    closing: bool = False
    closed: bool = False
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class NeuroCodeAcpAgent:
    """Official-SDK ACP v1 adapter for one workspace-bound process."""

    def __init__(self, application: ApplicationComposition) -> None:
        self._application = application
        self._client: Client | None = None
        self._client_capabilities: ClientCapabilities | None = None
        self._client_info: Implementation | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._sessions: dict[str, _AcpSession] = {}
        self._registry_lock = asyncio.Lock()

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
                load_session=None,
                prompt_capabilities=None,
                mcp_capabilities=None,
                auth=None,
                session_capabilities=SessionCapabilities(
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

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServer] | None = None,
        **_kwargs: Any,
    ) -> NewSessionResponse:
        self._require_initialized()
        if additional_directories:
            raise _invalid_params("additional_directories_unsupported")
        if mcp_servers:
            raise _invalid_params("mcp_servers_unsupported")
        try:
            requested = Path(cwd)
        except (OSError, RuntimeError, ValueError) as error:
            raise _invalid_params("cwd_invalid", type(error).__name__) from None
        if not requested.is_absolute():
            raise _invalid_params("cwd_not_absolute")
        try:
            normalized = await run_blocking(requested.resolve, strict=False)
        except (OSError, RuntimeError) as error:
            raise _invalid_params("cwd_invalid", type(error).__name__) from None
        if not workspaces_match(normalized, self._application.config.cwd):
            raise _invalid_params("cwd_workspace_mismatch")

        session_id = f"acp-{uuid.uuid4().hex}"
        approvals = SessionApprovalBroker()
        approvals.set_handler(lambda request: self._request_permission(session_id, request))
        try:
            binding = await self._application.create_binding(approver=approvals)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RequestError.internal_error({"reason": "session_creation_failed"}) from None
        session = _AcpSession(session_id, binding, approvals)
        async with self._registry_lock:
            self._sessions[session_id] = session
        return NewSessionResponse(session_id=session_id)

    async def _active_session(self, session_id: str) -> _AcpSession:
        async with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None or session.closed or session.closing:
            raise _session_not_active(session_id)
        return session

    def _explicit_redactions(self) -> tuple[str, ...]:
        protected = {
            name.casefold() for name in self._application.config.protected_environment_variables
        }
        return tuple(
            dict.fromkeys(
                value
                for name, value in os.environ.items()
                if name.casefold() in protected and value
            )
        )

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
        binding = session.binding
        if binding is None:
            raise _session_not_active(session_id)
        mapper = _AcpEventMapper(
            client=client,
            session_id=session_id,
            context_window_tokens=(self._application.config.provider.context_window_tokens),
            explicit_redactions=self._explicit_redactions(),
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

        try:
            result = await binding.runner.run(converted, sink=mapper)
            session.internal_session_id = result.session_id
            if session.cancel_requested or session.closing:
                return PromptResponse(stop_reason="cancelled")
            return PromptResponse(stop_reason=mapper.stop_reason)
        except asyncio.CancelledError:
            session.internal_session_id = binding.runner.session_id
            return PromptResponse(stop_reason="cancelled")
        except ProviderError as error:
            session.internal_session_id = binding.runner.session_id
            if session.cancel_requested or session.closing:
                return PromptResponse(stop_reason="cancelled")
            if "exceeded the maximum" in str(error):
                return PromptResponse(stop_reason="max_turn_requests")
            raise RequestError.internal_error({"reason": "provider_failure"}) from None
        except RequestError:
            raise
        except Exception:
            session.internal_session_id = binding.runner.session_id
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
            if binding is not None and binding.background_tasks is not None:
                await asyncio.shield(binding.background_tasks.shutdown())
            session.binding = None
            session.mapper = None
            session.pending_approval_id = None
            session.closed = True

    async def shutdown(self) -> None:
        async with self._registry_lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            async with session.state_lock:
                session.closing = True
                session.cancel_requested = True
        if sessions:
            await asyncio.gather(
                *(self._cleanup_session(session) for session in sessions),
                return_exceptions=True,
            )


async def serve_acp(application: ApplicationComposition) -> None:
    """Serve ACP on stdio using only the official SDK transport and router."""

    agent = NeuroCodeAcpAgent(application)
    try:
        await run_agent(
            cast(Agent, agent),
            use_unstable_protocol=True,
            stdio_buffer_limit_bytes=ACP_STDIO_BUFFER_LIMIT_BYTES,
        )
    finally:
        await asyncio.shield(agent.shutdown())


__all__ = [
    "ACP_PROTOCOL_VERSION",
    "ACP_STDIO_BUFFER_LIMIT_BYTES",
    "NeuroCodeAcpAgent",
    "convert_prompt_content",
    "serve_acp",
]
