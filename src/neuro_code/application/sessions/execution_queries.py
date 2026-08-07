"""Typed application owner for read-only execution-record queries.

该模块定义只读执行记录查询的类型化应用 owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.execution import SessionExecutionRecord


@dataclass(frozen=True, slots=True)
class LoadExecutionRecordRequest:
    """Validated input for loading one durable execution projection.

    用于加载一个持久化执行投影的、经过验证的输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


@dataclass(frozen=True, slots=True)
class LoadExecutionRecordsRequest:
    """Validated input for loading ordered execution projections in bulk.

    用于按顺序批量加载执行投影的、经过验证的输入.
    """

    session_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.session_ids, tuple):
            raise ValueError("session_ids must be a tuple")
        if not all(
            isinstance(session_id, str) and session_id.strip() for session_id in self.session_ids
        ):
            raise ValueError("session_ids must contain only non-empty strings")


class SessionExecutionQueryController(Protocol):
    """Minimal read-only owner for execution-record projections.

    表示执行记录投影使用的最小只读 owner 契约.
    """

    async def load_execution_record(
        self,
        request: LoadExecutionRecordRequest,
    ) -> SessionExecutionRecord | None: ...

    async def load_execution_records(
        self,
        request: LoadExecutionRecordsRequest,
    ) -> tuple[SessionExecutionRecord | None, ...]: ...


class SessionExecutionQueryService:
    """Delegate execution-record reads through the storage port.

    通过存储端口委托执行记录读取.
    """

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def load_execution_record(
        self,
        request: LoadExecutionRecordRequest,
    ) -> SessionExecutionRecord | None:
        """Load one durable execution projection without changing state.

        在不改变状态的情况下加载一个持久化执行投影.
        """

        if not isinstance(request, LoadExecutionRecordRequest):
            raise ValueError("load execution record request must be canonical")
        return await self._store.load_execution_record(request.session_id)

    async def load_execution_records(
        self,
        request: LoadExecutionRecordsRequest,
    ) -> tuple[SessionExecutionRecord | None, ...]:
        """Load ordered projections for a bounded session-id sequence.

        按有界会话 ID 序列加载有序执行投影.
        """

        if not isinstance(request, LoadExecutionRecordsRequest):
            raise ValueError("load execution records request must be canonical")
        return tuple(await self._store.load_execution_records(request.session_ids))


__all__ = [
    "LoadExecutionRecordRequest",
    "LoadExecutionRecordsRequest",
    "SessionExecutionQueryController",
    "SessionExecutionQueryService",
]
