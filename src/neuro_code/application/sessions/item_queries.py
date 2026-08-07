"""Typed application owner for read-only session-item queries.

该模块定义只读会话项查询的类型化应用 owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.messages import SessionItem


@dataclass(frozen=True, slots=True)
class LoadSessionItemsRequest:
    """Validated input for loading ordered persisted conversation items.

    用于加载按顺序持久化会话项的、经过验证的输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


class SessionItemQueryController(Protocol):
    """Minimal read-only owner for persisted conversation items.

    表示持久化会话项使用的最小只读 owner 契约.
    """

    async def load_session_items(
        self,
        request: LoadSessionItemsRequest,
    ) -> tuple[SessionItem, ...]: ...


class SessionItemQueryService:
    """Delegate ordered session-item reads through the storage port.

    通过存储端口委托按顺序读取会话项.
    """

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def load_session_items(
        self,
        request: LoadSessionItemsRequest,
    ) -> tuple[SessionItem, ...]:
        """Load the immutable application projection without changing state.

        加载不可变应用投影,但不改变状态.
        """

        if not isinstance(request, LoadSessionItemsRequest):
            raise ValueError("load session items request must be canonical")
        return tuple(await self._store.load_session_items(request.session_id))


__all__ = [
    "LoadSessionItemsRequest",
    "SessionItemQueryController",
    "SessionItemQueryService",
]
