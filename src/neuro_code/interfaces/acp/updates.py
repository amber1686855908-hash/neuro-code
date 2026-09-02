"""Bounded ACP history and live session-update projections.

ACP 历史记录和实时 session update 的有界协议投影.

This module owns only the outward projection of durable history and typed agent
events into ACP ``session_update`` values. Session lifecycle, transport,
capabilities, permissions, and tool execution remain outside this boundary.
本模块只负责把持久化历史和类型化 agent event 投影为 ACP
``session_update`` 值. session 生命周期、transport、capabilities、权限和工具执行
仍由边界之外的适配器负责.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    ContentToolCallContent,
    FileEditToolCallContent,
    TerminalToolCallContent,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)

from neuro_code.application.permissions.contracts import PermissionRequest
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import Message, Role, SessionItem, ToolCall
from neuro_code.interfaces.acp.errors import invalid_params as _invalid_params
from neuro_code.interfaces.acp.serialization import (
    MAX_RESOURCE_FIELD_BYTES,
    AcpStopReason,
    _bounded_identifier,
    map_stop_reason,
    safe_output_text,
    serialized_size_bytes,
    truncate_utf8,
)

MAX_UPDATE_TEXT_BYTES = 64 * 1024
MAX_TURN_UPDATE_BYTES = 1024 * 1024
MAX_TOOL_CONTENT_BYTES = 32 * 1024
MAX_LOAD_SESSION_ITEMS = 2_000
MAX_LOAD_SESSION_UPDATES = 4_096
MAX_LOAD_SESSION_BYTES = 2 * 1024 * 1024

_TOOL_KINDS: dict[str, Literal["read", "edit", "search", "execute", "other"]] = {
    "read_file": "read",
    "list_dir": "read",
    "grep": "search",
    "search_replace": "edit",
    "bash": "execute",
    "terminal_exec": "execute",
    "task_output": "execute",
    "wait_tasks": "execute",
    "kill_task": "execute",
}

HistoryUpdate = UserMessageChunk | AgentMessageChunk | ToolCallStart | ToolCallProgress


def _safe_text(
    value: object,
    limit: int,
    *,
    explicit_redactions: tuple[str, ...],
) -> str:
    return safe_output_text(value, limit, explicit_redactions=explicit_redactions)


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
            path=_safe_text(
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
            content = _safe_text(
                item.model_content(),
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
            content = _safe_text(
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
                    _safe_text(
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
                    _safe_text(
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
            content = _safe_text(
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
        serialized_size_bytes(update.model_dump(by_alias=True, exclude_none=True))
        for update in updates
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
        self.stop_reason: AcpStopReason = "end_turn"

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
        return _safe_text(
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
            text = truncate_utf8(text, remaining)
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
            self.stop_reason = map_stop_reason(event.data.get("stop_reason"))
