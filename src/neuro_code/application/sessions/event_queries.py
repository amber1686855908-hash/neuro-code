"""Typed application owner for read-only session-event projections.

该模块定义只读会话事件投影的类型化应用 owner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from neuro_code.application.ports.storage import SessionStore


@dataclass(frozen=True, slots=True)
class LoadSessionEventsRequest:
    """Validated input for loading copied, immutable event rows.

    用于加载复制且不可变事件行的、经过验证的输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


class SessionEventQueryController(Protocol):
    """Minimal read-only owner for persisted event projections.

    表示持久化事件投影使用的最小只读 owner 契约.
    """

    async def load_session_events(
        self,
        request: LoadSessionEventsRequest,
    ) -> tuple[Mapping[str, Any], ...]: ...


class SessionEventQueryService:
    """Copy untrusted event rows through the storage port.

    通过存储端口复制不可信的事件行.
    """

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def load_session_events(
        self,
        request: LoadSessionEventsRequest,
    ) -> tuple[Mapping[str, Any], ...]:
        """Load immutable outer mappings without decoding domain events.

        加载外层不可变映射,但不解码领域事件.
        """

        if not isinstance(request, LoadSessionEventsRequest):
            raise ValueError("load session events request must be canonical")
        return tuple(
            MappingProxyType(dict(event))
            for event in await self._store.load_events(request.session_id)
        )


__all__ = [
    "LoadSessionEventsRequest",
    "SessionEventQueryController",
    "SessionEventQueryService",
]
