"""Typed application owner for read-only session-task queries.

该模块定义只读会话任务查询的类型化应用 owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.session_tasks import SessionTask


@dataclass(frozen=True, slots=True)
class ListSessionTasksRequest:
    """Validated input for a bounded read of one session's tasks.

    用于有界读取一个会话任务列表的、经过验证的输入.
    """

    session_id: str
    limit: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError("session task list limit must be a positive integer")


@dataclass(frozen=True, slots=True)
class GetSessionTaskRequest:
    """Validated input for reading one task owned by a session.

    用于读取一个会话所属任务的、经过验证的输入.
    """

    session_id: str
    task_id: str

    def __post_init__(self) -> None:
        for field_name in ("session_id", "task_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")


class SessionTaskQueryController(Protocol):
    """Minimal read-only owner consumed by runtime and conversation callers.

    表示 Runtime 与会话控制器使用的最小只读任务查询 owner 契约.
    """

    async def list_session_tasks(
        self, request: ListSessionTasksRequest
    ) -> tuple[SessionTask, ...]: ...

    async def get_session_task(self, request: GetSessionTaskRequest) -> SessionTask | None: ...


class SessionTaskQueryService:
    """Delegate typed task reads without owning task lifecycle state.

    通过类型化请求委托任务读取,但不接管任务生命周期状态.
    """

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def list_session_tasks(self, request: ListSessionTasksRequest) -> tuple[SessionTask, ...]:
        """Load a bounded task projection through the storage port.

        通过存储端口加载有界任务投影.
        """

        if not isinstance(request, ListSessionTasksRequest):
            raise ValueError("list session tasks request must be canonical")
        return tuple(await self._store.list_session_tasks(request.session_id, limit=request.limit))

    async def get_session_task(self, request: GetSessionTaskRequest) -> SessionTask | None:
        """Load one task projection without changing its lifecycle state.

        在不改变任务生命周期状态的情况下加载一个任务投影.
        """

        if not isinstance(request, GetSessionTaskRequest):
            raise ValueError("get session task request must be canonical")
        return await self._store.get_session_task(request.session_id, request.task_id)


__all__ = [
    "GetSessionTaskRequest",
    "ListSessionTasksRequest",
    "SessionTaskQueryController",
    "SessionTaskQueryService",
]
