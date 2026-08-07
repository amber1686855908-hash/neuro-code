"""Application-owned session catalog and inspection queries.

应用层拥有的会话目录与检查查询.

The catalog is deliberately read-only.  It returns bounded projections and
never owns conversation items, aliases, turn lifecycle, or storage writes.

该目录服务有意保持只读.它只返回有界投影,不拥有会话项、别名、回合生命周期或存储写入.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.sessions.execution_queries import (
    LoadExecutionRecordRequest,
    LoadExecutionRecordsRequest,
    SessionExecutionQueryService,
)
from neuro_code.domain.execution import SessionExecutionRecord
from neuro_code.domain.sessions import SessionSearchHit, SessionSummary

__all__ = [
    "ListSessionsPageRequest",
    "ListSessionsRequest",
    "SearchSessionsRequest",
    "SessionCatalogApplicationService",
    "SessionInspection",
    "SessionSearchInspection",
    "SessionSearchInspectionPage",
    "SessionWorkspaceMatcher",
]


@dataclass(frozen=True, slots=True)
class SessionInspection:
    """A safe session projection for application-facing inspectors.

    The projection intentionally excludes messages, prompts, tool arguments,
    output, and execution snapshots.  A caller may use the durable execution
    record to render a recoverable status without gaining access to sensitive
    turn contents.

    面向应用检查器的安全会话投影.
    该投影有意排除消息、提示词、工具参数、输出和执行快照;调用方可以使用持久化执行记录
    展示可恢复状态,但不能借此读取敏感的回合内容.
    """

    summary: SessionSummary
    execution_record: SessionExecutionRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.summary, SessionSummary):
            raise ValueError("session inspection summary must be canonical")
        if self.execution_record is not None and not isinstance(
            self.execution_record,
            SessionExecutionRecord,
        ):
            raise ValueError("session inspection execution record must be canonical")


@dataclass(frozen=True, slots=True)
class ListSessionsRequest:
    """Validated input for a bounded session catalog query.

    用于有界会话目录查询的经过验证的输入.
    """

    limit: int = 50

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError("session list limit must be a positive integer")


@dataclass(frozen=True, slots=True)
class ListSessionsPageRequest:
    """Validated keyset page input for safe session summaries.

    用于安全会话摘要键集分页的经过验证的输入.
    """

    limit: int
    before_updated_at: datetime | None = None
    before_id: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 1000
        ):
            raise ValueError("session page limit must be between 1 and 1000")
        if (self.before_updated_at is None) != (self.before_id is None):
            raise ValueError("session page cursor fields must be provided together")
        if self.before_updated_at is not None and self.before_updated_at.tzinfo is None:
            raise ValueError("session page cursor timestamp must be timezone-aware")
        if self.before_id is not None and (
            not isinstance(self.before_id, str) or not self.before_id
        ):
            raise ValueError("session page cursor ID must not be empty")


@dataclass(frozen=True, slots=True)
class SearchSessionsRequest:
    """Validated input for a session search query.

    用于会话搜索查询的经过验证的输入.
    """

    query: str
    cwd: str | None = None
    limit: int = 20
    offset: int = 0
    include_content: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("session search query must not be empty")
        if self.cwd is not None and (not isinstance(self.cwd, str) or not self.cwd.strip()):
            raise ValueError("session search cwd must be non-empty when provided")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError("session search limit must be a positive integer")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("session search offset must not be negative")
        if not isinstance(self.include_content, bool):
            raise ValueError("session search include_content must be boolean")


@dataclass(frozen=True, slots=True)
class SessionSearchInspection:
    """Search hit plus its safe durable execution projection.

    搜索命中项及其安全的持久化执行投影.
    """

    hit: SessionSearchHit
    execution_record: SessionExecutionRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.hit, SessionSearchHit):
            raise ValueError("session search inspection hit must be canonical")
        if self.execution_record is not None and not isinstance(
            self.execution_record,
            SessionExecutionRecord,
        ):
            raise ValueError("session search inspection execution record must be canonical")


@dataclass(frozen=True, slots=True)
class SessionSearchInspectionPage:
    """A bounded search page with safe execution projections.

    包含安全执行投影的有界搜索页面.
    """

    results: tuple[SessionSearchInspection, ...]
    next_offset: int | None
    total_estimate: int

    def __post_init__(self) -> None:
        results = tuple(self.results)
        if not all(isinstance(result, SessionSearchInspection) for result in results):
            raise ValueError("session search inspection results must be canonical")
        object.__setattr__(self, "results", results)
        if self.next_offset is not None and self.next_offset < 0:
            raise ValueError("session search inspection next offset must not be negative")
        if self.total_estimate < len(results):
            raise ValueError("session search inspection total must cover returned results")


SessionWorkspaceMatcher = Callable[[str, str], bool]


class SessionCatalogApplicationService:
    """Read-only session catalog and safe inspection use cases.

    The service owns only bounded reads and projections.  The storage port
    remains responsible for query execution, while conversation lifecycle,
    aliases, and writes stay with their existing owners.

    只读会话目录与安全检查应用用例.
    本服务只拥有有界读取和投影;存储端口仍负责查询执行,而会话生命周期、别名和写入
    继续由现有 owner 负责.
    """

    __slots__ = ("_execution_queries", "_store", "_workspace_matcher")

    def __init__(
        self,
        store: SessionStore,
        *,
        workspace_matcher: SessionWorkspaceMatcher | None = None,
    ) -> None:
        self._store = store
        self._execution_queries = SessionExecutionQueryService(store)
        self._workspace_matcher = workspace_matcher

    def _workspace_matcher_for(self, workspace: str) -> SessionWorkspaceMatcher:
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("session workspace must not be empty")
        if self._workspace_matcher is None:
            raise ValueError("session workspace matching is unavailable")
        return self._workspace_matcher

    async def inspect_session(self, session_id: str) -> SessionInspection:
        """Return only a safe summary and durable execution projection.

        仅返回安全摘要和持久化执行投影.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must not be empty")
        summary = await self._store.get_session(session_id)
        record = await self._execution_queries.load_execution_record(
            LoadExecutionRecordRequest(session_id)
        )
        return SessionInspection(summary, record)

    async def list_sessions(
        self,
        request: ListSessionsRequest | None = None,
    ) -> tuple[SessionInspection, ...]:
        """Return bounded safe projections for the session catalog.

        返回会话目录的有界安全投影.
        """

        if request is None:
            request = ListSessionsRequest()
        elif not isinstance(request, ListSessionsRequest):
            raise ValueError("list sessions request must be canonical")
        sessions = await self._store.list_sessions(limit=request.limit)
        records = await self._execution_queries.load_execution_records(
            LoadExecutionRecordsRequest(tuple(summary.id for summary in sessions))
        )
        if len(records) != len(sessions):
            raise RuntimeError("session execution projection length mismatch")
        return tuple(
            SessionInspection(summary, record)
            for summary, record in zip(sessions, records, strict=True)
        )

    async def list_sessions_page(
        self,
        request: ListSessionsPageRequest,
    ) -> tuple[SessionSummary, ...]:
        """Return a typed keyset page without exposing storage details.

        返回类型化的键集分页,不暴露存储细节.
        """

        if not isinstance(request, ListSessionsPageRequest):
            raise ValueError("session page request must be canonical")
        return tuple(
            await self._store.list_sessions_page(
                limit=request.limit,
                before_updated_at=request.before_updated_at,
                before_id=request.before_id,
            )
        )

    async def list_sessions_in_workspace(
        self,
        workspace: str,
        *,
        limit: int = 1000,
        result_limit: int = 50,
    ) -> tuple[SessionSummary, ...]:
        """Return bounded summaries belonging to an injected workspace scope.

        返回属于注入工作区作用域的有界会话摘要.
        """

        matcher = self._workspace_matcher_for(workspace)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("session workspace scan limit must be a positive integer")
        if isinstance(result_limit, bool) or not isinstance(result_limit, int) or result_limit < 1:
            raise ValueError("session workspace result limit must be a positive integer")
        summaries = await self._store.list_sessions(limit=limit)
        return tuple(summary for summary in summaries if matcher(summary.cwd, workspace))[
            :result_limit
        ]

    async def search_sessions(
        self,
        request: SearchSessionsRequest,
    ) -> SessionSearchInspectionPage:
        """Search sessions and attach only safe execution projections.

        搜索会话,并只附带安全的执行投影.
        """

        if not isinstance(request, SearchSessionsRequest):
            raise ValueError("search sessions request must be canonical")
        page = await self._store.search_sessions(
            request.query,
            cwd=request.cwd,
            limit=request.limit,
            offset=request.offset,
            include_content=request.include_content,
        )
        records = await self._execution_queries.load_execution_records(
            LoadExecutionRecordsRequest(tuple(hit.summary.id for hit in page.results))
        )
        if len(records) != len(page.results):
            raise RuntimeError("session execution projection length mismatch")
        results = tuple(
            SessionSearchInspection(hit, record)
            for hit, record in zip(page.results, records, strict=True)
        )
        return SessionSearchInspectionPage(
            results,
            page.next_offset,
            page.total_estimate,
        )

    async def search_sessions_in_workspace(
        self,
        query: str,
        workspace: str,
        *,
        limit: int = 1000,
        result_limit: int = 50,
    ) -> tuple[SessionSearchHit, ...]:
        """Return bounded search hits within an injected workspace scope.

        返回注入工作区作用域内的有界搜索命中项.
        """

        matcher = self._workspace_matcher_for(workspace)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("session workspace search limit must be a positive integer")
        if isinstance(result_limit, bool) or not isinstance(result_limit, int) or result_limit < 1:
            raise ValueError("session workspace search result limit must be a positive integer")
        request = SearchSessionsRequest(query, limit=limit, include_content=True)
        page = await self._store.search_sessions(
            request.query,
            limit=request.limit,
            include_content=request.include_content,
        )
        return tuple(hit for hit in page.results if matcher(hit.summary.cwd, workspace))[
            :result_limit
        ]
