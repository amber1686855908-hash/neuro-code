"""Typed application owner for read-only session-summary queries.

该模块定义只读会话摘要查询的类型化应用 owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.sessions import SessionSummary


@dataclass(frozen=True, slots=True)
class GetSessionSummaryRequest:
    """Validated input for loading one safe session summary.

    用于加载单个安全会话摘要的、经过验证的输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


class SessionSummaryQueryController(Protocol):
    """Minimal read-only owner consumed by session-aware adapters.

    表示会话相关适配器使用的最小只读摘要查询 owner 契约.
    """

    async def get_session_summary(self, request: GetSessionSummaryRequest) -> SessionSummary: ...


class SessionSummaryQueryService:
    """Delegate safe summary reads through the storage port.

    通过存储端口委托安全的会话摘要读取.
    """

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def get_session_summary(self, request: GetSessionSummaryRequest) -> SessionSummary:
        """Load one summary without messages, events, or lifecycle changes.

        加载单个摘要,不读取消息、事件,也不改变生命周期状态.
        """

        if not isinstance(request, GetSessionSummaryRequest):
            raise ValueError("get session summary request must be canonical")
        return await self._store.get_session(request.session_id)


__all__ = [
    "GetSessionSummaryRequest",
    "SessionSummaryQueryController",
    "SessionSummaryQueryService",
]
