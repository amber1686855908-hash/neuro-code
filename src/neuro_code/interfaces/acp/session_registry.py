"""Connection-level ACP session registry and external identity projections.

ACP 连接级会话注册表以及外部 identity 投影.

This module owns the connection's published-session map, in-flight creation
reservations, pagination cursors, and the translation between external ACP
identities and application session identities.  Per-session mutable state and
resource cleanup remain owned by :mod:`neuro_code.interfaces.acp.session`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from acp.exceptions import RequestError
from acp.schema import ListSessionsResponse, SessionInfo

from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.domain.sessions import SessionSummary
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
from neuro_code.interfaces.acp.serialization import (
    MAX_RESOURCE_FIELD_BYTES,
)
from neuro_code.interfaces.acp.serialization import (
    safe_output_text as _safe_output_text,
)
from neuro_code.interfaces.acp.session import (
    AcpSessionIdentityUnavailableError,
    AcpSessionInactiveError,
    AcpSessionPromptAlreadyActiveError,
    AcpSessionRuntime,
)
from neuro_code.shared.errors import SessionError

ACP_SESSION_LIST_PAGE_SIZE = 50
MAX_SESSION_LIST_SCAN_ITEMS = 5_000
SESSION_LIST_SCAN_BATCH_SIZE = 250
MAX_SESSION_LIST_CURSORS = 256
MAX_SESSION_LIST_CURSOR_BYTES = 128
ACP_SESSION_ALIAS_NAMESPACE = "acp-v1"
ACP_SUBAGENT_LIFECYCLE_ALIAS_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class _SessionListCursor:
    updated_at: datetime
    internal_session_id: str


class AcpSessionRegistry:
    """Own published ACP sessions and connection-scoped registry state."""

    __slots__ = (
        "_list_cursor_lock",
        "_list_cursors",
        "_pending_session_tasks",
        "_registry_lock",
        "_service",
        "_sessions",
        "_shutting_down",
    )

    def __init__(self, service: AcpApplicationService) -> None:
        self._service = service
        self._sessions: dict[str, AcpSessionRuntime] = {}
        self._pending_session_tasks: dict[str, asyncio.Task[Any]] = {}
        self._registry_lock = asyncio.Lock()
        self._list_cursors: OrderedDict[str, _SessionListCursor] = OrderedDict()
        self._list_cursor_lock = asyncio.Lock()
        self._shutting_down = False

    @property
    def sessions(self) -> dict[str, AcpSessionRuntime]:
        """Return the live map for the narrow legacy inspection seam."""

        return self._sessions

    async def reserve(self, session_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RequestError.internal_error({"reason": "session_task_unavailable"})
        async with self._registry_lock:
            if self._shutting_down:
                raise RequestError.internal_error({"reason": "connection_closing"})
            if session_id in self._sessions or session_id in self._pending_session_tasks:
                raise _session_busy(session_id, "session_already_active")
            self._pending_session_tasks[session_id] = task

    async def release(self, session_id: str) -> None:
        task = asyncio.current_task()
        async with self._registry_lock:
            if self._pending_session_tasks.get(session_id) is task:
                del self._pending_session_tasks[session_id]

    async def publish(self, session: AcpSessionRuntime) -> bool:
        task = asyncio.current_task()
        async with self._registry_lock:
            if self._pending_session_tasks.get(session.session_id) is not task:
                return False
            del self._pending_session_tasks[session.session_id]
            if self._shutting_down:
                return False
            self._sessions[session.session_id] = session
            return True

    async def lookup(self, session_id: str) -> AcpSessionRuntime | None:
        async with self._registry_lock:
            return self._sessions.get(session_id)

    async def delete_snapshot(
        self,
        session_id: str,
    ) -> tuple[bool, AcpSessionRuntime | None]:
        async with self._registry_lock:
            return session_id in self._pending_session_tasks, self._sessions.get(session_id)

    async def remove_if(self, session_id: str, session: AcpSessionRuntime) -> None:
        async with self._registry_lock:
            if self._sessions.get(session_id) is session:
                del self._sessions[session_id]

    async def active(self, session_id: str) -> AcpSessionRuntime:
        session = await self.lookup(session_id)
        if session is None or not await session.is_active():
            raise _session_not_active(session_id)
        return session

    async def fork_source_session_id(self, external_session_id: str) -> str:
        pending, active = await self.delete_snapshot(external_session_id)
        if pending:
            raise _session_busy(external_session_id, "session_creation_in_progress")
        if active is not None:
            try:
                return await active.fork_source_identity()
            except AcpSessionInactiveError:
                raise _session_not_active(external_session_id) from None
            except AcpSessionPromptAlreadyActiveError:
                raise _session_busy(external_session_id, "session_prompt_active") from None
            except AcpSessionIdentityUnavailableError:
                raise _session_not_found(external_session_id) from None
        try:
            return await self._service.resolve_session_alias(
                ACP_SESSION_ALIAS_NAMESPACE,
                external_session_id,
            )
        except SessionError:
            raise _session_not_found(external_session_id) from None

    async def artifact_internal_session_id(self, external_session_id: str) -> str:
        """Resolve an ACP ID without exposing the internal session identity."""

        pending, active = await self.delete_snapshot(external_session_id)
        if pending:
            raise _session_busy(external_session_id, "session_creation_in_progress")
        if active is not None:
            try:
                internal_session_id = await active.active_internal_session_identity()
            except AcpSessionInactiveError:
                raise _session_not_active(external_session_id) from None
            if internal_session_id is None:
                raise _session_not_found(external_session_id)
            return internal_session_id
        try:
            return await self._service.resolve_session_alias(
                ACP_SESSION_ALIAS_NAMESPACE,
                external_session_id,
            )
        except SessionError:
            raise _session_not_found(external_session_id) from None

    async def lifecycle_external_session_id(self, internal_session_id: str) -> str:
        """Allocate a bounded ACP alias for a lifecycle result session."""

        for _attempt in range(ACP_SUBAGENT_LIFECYCLE_ALIAS_ATTEMPTS):
            try:
                external_session_id = (
                    await self._service.get_or_create_current_workspace_session_alias(
                        internal_session_id,
                        f"acp-{uuid.uuid4().hex}",
                    )
                )
                resolved_session_id = await self._service.resolve_session_alias(
                    ACP_SESSION_ALIAS_NAMESPACE,
                    external_session_id,
                )
                if resolved_session_id != internal_session_id:
                    raise SessionError("session alias resolved to another session")
                return external_session_id
            except SessionError:
                continue
        raise RequestError.internal_error({"reason": "session_alias_allocation_failed"})

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

    async def _remember_session_list_cursor(self, summary: SessionSummary) -> str:
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
                    ACP_SESSION_ALIAS_NAMESPACE,
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
        cwd: str | None,
        cursor: str | None,
        *,
        page_size: int,
        validate_workspace: Callable[[str], Awaitable[object]],
        explicit_redactions: Callable[[], tuple[str, ...]],
    ) -> ListSessionsResponse:
        if cwd is not None:
            await validate_workspace(cwd)
        position = await self._session_list_cursor(cursor)
        before_updated_at = position.updated_at if position is not None else None
        before_id = position.internal_session_id if position is not None else None

        matches: list[SessionSummary] = []
        last_scanned: SessionSummary | None = None
        remaining_scan = MAX_SESSION_LIST_SCAN_ITEMS
        exhausted = False
        try:
            while len(matches) <= page_size and remaining_scan > 0:
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
                        if len(matches) > page_size:
                            break
                if len(matches) > page_size:
                    break
                if len(batch) < batch_limit:
                    exhausted = True
                    break
        except SessionError:
            raise RequestError.internal_error({"reason": "session_list_failed"}) from None

        page = matches[:page_size]
        next_position: SessionSummary | None = None
        if len(matches) > page_size:
            next_position = page[-1]
        elif not exhausted:
            next_position = last_scanned

        redactions = explicit_redactions()
        sessions = [
            SessionInfo(
                session_id=await self._listed_session_id(summary.id),
                cwd=summary.cwd,
                title=(
                    _safe_output_text(
                        summary.title,
                        MAX_RESOURCE_FIELD_BYTES,
                        explicit_redactions=redactions,
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
        return ListSessionsResponse(sessions=sessions, next_cursor=next_cursor)

    async def begin_shutdown(
        self,
    ) -> tuple[tuple[AcpSessionRuntime, ...], tuple[asyncio.Task[Any], ...]]:
        async with self._registry_lock:
            self._shutting_down = True
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            pending_tasks = tuple(self._pending_session_tasks.values())
            self._pending_session_tasks.clear()
        return sessions, pending_tasks

    async def clear_cursors(self) -> None:
        async with self._list_cursor_lock:
            self._list_cursors.clear()


__all__ = [
    "ACP_SESSION_ALIAS_NAMESPACE",
    "ACP_SESSION_LIST_PAGE_SIZE",
    "ACP_SUBAGENT_LIFECYCLE_ALIAS_ATTEMPTS",
    "MAX_SESSION_LIST_CURSORS",
    "MAX_SESSION_LIST_SCAN_ITEMS",
    "SESSION_LIST_SCAN_BATCH_SIZE",
    "AcpSessionRegistry",
    "_SessionListCursor",
]
