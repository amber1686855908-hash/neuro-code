"""Typed application boundary for reading bounded tool-output artifacts.

有界工具输出 artifact 读取的类型化应用边界.

The service deliberately accepts an artifact handle rather than a filesystem
path.  It does not know the state directory, expose storage details, or grant
an inbound interface access to arbitrary files.  Session visibility is derived
from persisted terminal-event metadata, while lifecycle pruning remains an
explicit, separately bounded operation.

该服务有意接收 artifact 句柄而不是文件系统路径.它不知道状态目录,不暴露存储细节,
也不允许入站接口访问任意文件.会话可见性从已持久化的终态事件 metadata 推导,
生命周期清理则是独立且有界的显式操作.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
    MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
    ToolOutputArtifact,
    ToolOutputArtifactGarbageCollector,
    ToolOutputArtifactPruneResult,
    ToolOutputArtifactRead,
    ToolOutputArtifactReader,
)
from neuro_code.application.sessions.catalog import ListSessionsPageRequest
from neuro_code.application.sessions.event_queries import (
    LoadSessionEventsRequest,
    SessionEventQueryService,
)
from neuro_code.application.sessions.service import SessionApplicationService
from neuro_code.application.sessions.summary import (
    GetSessionSummaryRequest,
    SessionSummaryQueryService,
)
from neuro_code.domain.conversation.events import AgentEventKind
from neuro_code.shared.errors import SessionError

MAX_SESSION_TOOL_OUTPUT_ARTIFACTS = 100


@dataclass(frozen=True, slots=True)
class SessionToolOutputArtifact:
    """A bounded artifact handle proven to belong to one session event.

    已证明属于某个会话事件的有界 artifact 句柄.
    """

    event_sequence: int
    artifact: ToolOutputArtifact

    def __post_init__(self) -> None:
        if (
            isinstance(self.event_sequence, bool)
            or not isinstance(self.event_sequence, int)
            or self.event_sequence <= 0
        ):
            raise ValueError("session artifact event sequence must be positive")
        if not isinstance(self.artifact, ToolOutputArtifact):
            raise ValueError("session artifact handle must be canonical")


@dataclass(frozen=True, slots=True)
class ListSessionToolOutputArtifactsRequest:
    """Bounded query for artifact handles recorded by one session.

    查询某个会话记录的 artifact 句柄的有界请求.
    """

    session_id: str
    limit: int = MAX_SESSION_TOOL_OUTPUT_ARTIFACTS

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_SESSION_TOOL_OUTPUT_ARTIFACTS
        ):
            raise ValueError("session artifact limit must be bounded")


@dataclass(frozen=True, slots=True)
class ReadSessionToolOutputArtifactRequest:
    """Read one artifact only after proving its session association.

    只有在证明 artifact 属于指定会话后才读取它的请求.
    """

    session_id: str
    artifact_id: str
    max_bytes: int = MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if (
            not isinstance(self.artifact_id, str)
            or not self.artifact_id.strip()
            or "\x00" in self.artifact_id
        ):
            raise ValueError("artifact_id must be a non-empty opaque handle")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or not 1 <= self.max_bytes <= MAX_TOOL_OUTPUT_ARTIFACT_BYTES
        ):
            raise ValueError("artifact read limit must be within the artifact byte limit")


@dataclass(frozen=True, slots=True)
class ReadToolOutputArtifactRequest:
    """Validated input for one bounded artifact read.

    一次有界 artifact 读取的经过校验的输入.
    """

    artifact: ToolOutputArtifact
    max_bytes: int = MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ToolOutputArtifact):
            raise ValueError("tool output artifact must be canonical")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or not 1 <= self.max_bytes <= MAX_TOOL_OUTPUT_ARTIFACT_BYTES
        ):
            raise ValueError("artifact read limit must be within the artifact byte limit")


class ToolOutputArtifactApplicationService:
    """Expose bounded artifact reads without leaking infrastructure paths.

    在不泄露基础设施路径的前提下提供有界 artifact 读取.
    """

    __slots__ = ("_reader",)

    def __init__(self, reader: ToolOutputArtifactReader) -> None:
        self._reader = reader

    async def read(self, request: ReadToolOutputArtifactRequest) -> ToolOutputArtifactRead:
        """Read one caller-provided opaque handle through the reader port.

        通过读取端口读取一个由调用方提供的不透明句柄.
        """

        if not isinstance(request, ReadToolOutputArtifactRequest):
            raise ValueError("read tool output artifact request must be canonical")
        return await self._reader.read(
            request.artifact,
            max_bytes=request.max_bytes,
        )


class SessionToolOutputArtifactApplicationService:
    """List and read artifacts through a session-scoped application boundary.

    通过会话作用域的应用边界列出并读取 artifact.

    The association is derived only from persisted tool terminal events. The
    service exposes no filesystem path and does not treat a caller-supplied
    session ID as authorization unless the artifact handle is present in that
    session's event projection. Optional pruning is explicit and requires a
    complete session scan before the infrastructure collector is called.

    关联只从已持久化的工具终态事件中派生.服务不暴露文件系统路径,也不会仅因调用方提供
    会话 ID 就授予权限;句柄必须出现在该会话的事件投影中.清理只能显式触发,并且必须先完成
    全部会话扫描再调用基础设施 collector.
    """

    __slots__ = ("_collector", "_event_queries", "_reader", "_sessions", "_summary_queries")

    def __init__(
        self,
        store: SessionStore,
        reader: ToolOutputArtifactReader,
        *,
        garbage_collector: ToolOutputArtifactGarbageCollector | None = None,
    ) -> None:
        self._reader = reader
        self._collector = garbage_collector
        self._sessions = SessionApplicationService(store)
        self._event_queries = SessionEventQueryService(store)
        self._summary_queries = SessionSummaryQueryService(store)

    async def list(
        self,
        request: ListSessionToolOutputArtifactsRequest,
    ) -> tuple[SessionToolOutputArtifact, ...]:
        if not isinstance(request, ListSessionToolOutputArtifactsRequest):
            raise ValueError("session artifact list request must be canonical")
        # Verify that an empty event projection is not mistaken for an unknown
        # session through the shared safe session-summary seam.
        await self._summary_queries.get_session_summary(
            GetSessionSummaryRequest(request.session_id)
        )
        events = await self._event_queries.load_session_events(
            LoadSessionEventsRequest(request.session_id)
        )
        references: dict[str, SessionToolOutputArtifact] = {}
        for event in events:
            reference = _session_artifact_from_event(event)
            if reference is not None:
                references.setdefault(reference.artifact.artifact_id, reference)
        ordered = sorted(references.values(), key=lambda item: item.event_sequence)
        return tuple(ordered[-request.limit :])

    async def read(
        self,
        request: ReadSessionToolOutputArtifactRequest,
    ) -> ToolOutputArtifactRead:
        if not isinstance(request, ReadSessionToolOutputArtifactRequest):
            raise ValueError("session artifact read request must be canonical")
        references = await self.list(
            ListSessionToolOutputArtifactsRequest(
                request.session_id,
                limit=MAX_SESSION_TOOL_OUTPUT_ARTIFACTS,
            )
        )
        reference = next(
            (item for item in references if item.artifact.artifact_id == request.artifact_id),
            None,
        )
        if reference is None:
            # Do not reveal whether a handle exists in another session or on disk.
            raise SessionError("tool output artifact is not associated with this session")
        return await self._reader.read(reference.artifact, max_bytes=request.max_bytes)

    async def prune_unreferenced(self) -> ToolOutputArtifactPruneResult:
        """Explicitly prune old files absent from every persisted session.

        显式清理所有持久化会话均未引用的过期文件.
        """

        if self._collector is None:
            raise SessionError("tool output artifact pruning is unavailable")

        keep_ids: set[str] = set()
        seen_session_ids: set[str] = set()
        before_updated_at = None
        before_id = None
        while True:
            page = await self._sessions.list_sessions_page(
                ListSessionsPageRequest(
                    limit=1000,
                    before_updated_at=before_updated_at,
                    before_id=before_id,
                )
            )
            if not page:
                break
            for summary in page:
                if summary.id in seen_session_ids:
                    raise SessionError("session pagination did not make progress")
                seen_session_ids.add(summary.id)
                for event in await self._event_queries.load_session_events(
                    LoadSessionEventsRequest(summary.id)
                ):
                    reference = _session_artifact_from_event(event)
                    if reference is not None:
                        keep_ids.add(reference.artifact.artifact_id)
            if len(page) < 1000:
                break
            last = page[-1]
            before_updated_at = last.updated_at
            before_id = last.id

        return await self._collector.prune_unreferenced(keep_ids)


def _session_artifact_from_event(event: Mapping[str, object]) -> SessionToolOutputArtifact | None:
    """Extract only safe artifact metadata from an untrusted event projection.

    从不可信的事件投影中只提取安全的 artifact 元数据.
    """

    raw_sequence = event.get("sequence")
    if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int):
        return None
    if event.get("kind") not in {
        AgentEventKind.BACKEND_TOOL_COMPLETED.value,
        AgentEventKind.TOOL_COMPLETED.value,
        AgentEventKind.TOOL_FAILED.value,
    }:
        return None
    raw_data = event.get("data")
    if not isinstance(raw_data, Mapping):
        return None
    raw_metadata = raw_data.get("metadata")
    if not isinstance(raw_metadata, Mapping):
        return None
    raw_id = raw_metadata.get("output_artifact_id")
    raw_path = raw_metadata.get("output_artifact_path")
    raw_bytes = raw_metadata.get("output_artifact_bytes")
    raw_truncated = raw_metadata.get("output_artifact_truncated", False)
    if (
        not isinstance(raw_id, str)
        or not isinstance(raw_path, str)
        or isinstance(raw_bytes, bool)
        or not isinstance(raw_bytes, int)
        or not isinstance(raw_truncated, bool)
    ):
        return None
    try:
        artifact = ToolOutputArtifact(raw_id, raw_path, raw_bytes, raw_truncated)
        return SessionToolOutputArtifact(raw_sequence, artifact)
    except ValueError:
        return None


__all__ = [
    "MAX_SESSION_TOOL_OUTPUT_ARTIFACTS",
    "ListSessionToolOutputArtifactsRequest",
    "ReadSessionToolOutputArtifactRequest",
    "ReadToolOutputArtifactRequest",
    "SessionToolOutputArtifact",
    "SessionToolOutputArtifactApplicationService",
    "ToolOutputArtifactApplicationService",
    "ToolOutputArtifactPruneResult",
]
