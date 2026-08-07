"""Typed application use cases for starting and inspecting sessions.

The service is deliberately small.  It owns neither SQLite details nor
conversation execution; those remain behind :class:`SessionStore` and
``AgentConversation`` respectively.  This gives inbound interfaces a stable
application seam without changing the existing runtime path.

用于启动和检查会话的类型化应用用例.
该服务保持精简:SQLite 细节和会话执行分别由 `SessionStore` 与 `AgentConversation` 承担,
从而为入站接口提供稳定的应用层接缝,并且不改变现有运行时路径.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.sessions.catalog import (
    ListSessionsPageRequest,
    ListSessionsRequest,
    SearchSessionsRequest,
    SessionCatalogApplicationService,
    SessionInspection,
    SessionSearchInspection,
    SessionSearchInspectionPage,
    SessionWorkspaceMatcher,
)
from neuro_code.application.sessions.event_queries import (
    LoadSessionEventsRequest,
    SessionEventQueryController,
    SessionEventQueryService,
)
from neuro_code.application.sessions.execution_queries import (
    LoadExecutionRecordRequest,
    LoadExecutionRecordsRequest,
    SessionExecutionQueryController,
    SessionExecutionQueryService,
)
from neuro_code.application.sessions.item_queries import (
    LoadSessionItemsRequest,
    SessionItemQueryController,
    SessionItemQueryService,
)
from neuro_code.application.sessions.lifecycle import (
    DeleteSessionRequest,
    ForkSessionRequest,
    ImportSessionRequest,
    RenameSessionRequest,
    SessionLifecycleController,
    SessionLifecycleService,
    StartSessionRequest,
)
from neuro_code.application.sessions.summary import (
    GetSessionSummaryRequest,
    SessionSummaryQueryController,
    SessionSummaryQueryService,
)
from neuro_code.application.sessions.task_queries import (
    GetSessionTaskRequest,
    ListSessionTasksRequest,
    SessionTaskQueryController,
    SessionTaskQueryService,
)
from neuro_code.application.sessions.turns import (
    RunTurnRequest,
    SessionTurnRunner,
    SessionTurnService,
)
from neuro_code.domain.conversation.messages import SessionItem
from neuro_code.domain.execution import SessionExecutionRecord
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.session_tasks import SessionTask
from neuro_code.domain.sessions import SessionSearchHit, SessionSnapshot, SessionSummary


@dataclass(frozen=True, slots=True)
class ResumeSessionRequest:
    """Validated, read-only preflight input for an existing session resume.

    为已有会话恢复执行而准备的,只读且经过验证的输入."""

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


@dataclass(frozen=True, slots=True)
class BindSessionAliasRequest:
    """Validated intent to bind one external session identifier.

    绑定一个外部会话标识的,经过验证的意图.
    """

    namespace: str
    external_id: str
    session_id: str

    def __post_init__(self) -> None:
        for field_name in ("namespace", "external_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class ResolveSessionAliasRequest:
    """Validated lookup of one external session identifier.

    查询一个外部会话标识的,经过验证的输入.
    """

    namespace: str
    external_id: str

    def __post_init__(self) -> None:
        for field_name in ("namespace", "external_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class GetOrCreateSessionAliasRequest:
    """Validated request for an existing or newly allocated session alias.

    获取已有或分配新会话别名的,经过验证的请求.
    """

    namespace: str
    session_id: str
    proposed_external_id: str

    def __post_init__(self) -> None:
        for field_name in ("namespace", "session_id", "proposed_external_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class LoadSessionPlanRequest:
    """Validated input for loading one persisted session plan.

    用于加载一个已持久化会话计划的,经过验证的输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


@dataclass(frozen=True, slots=True)
class ListPlanCommentsRequest:
    """Validated input for loading comments attached to one plan revision.

    用于加载绑定到一个计划版本的评论的,经过验证的输入.
    """

    session_id: str
    plan: SessionPlan

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not isinstance(self.plan, SessionPlan):
            raise ValueError("plan must be canonical")


@dataclass(frozen=True, slots=True)
class ExportSessionRequest:
    """Validated input for reading one complete session export projection.

    用于读取一个完整会话导出投影的,经过验证的输入.
    """

    session_id: str
    include_events: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not isinstance(self.include_events, bool):
            raise ValueError("include_events must be boolean")


@dataclass(frozen=True, slots=True)
class SessionExport:
    """Immutable application projection used by explicit session export commands.

    The projection deliberately keeps the existing export payload shape: the
    CLI remains responsible for JSON/Markdown serialization and file writes.
    Events are copied at the outer mapping boundary so a storage-owned result
    cannot be mutated through the application projection.

    显式会话导出命令使用的不可变应用投影.
    该投影有意保留现有导出负载形状:JSON/Markdown 序列化和文件写入仍由 CLI 负责.
    事件会在外层映射边界复制,避免通过应用投影修改存储层结果.
    """

    snapshot: SessionSnapshot
    events: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, SessionSnapshot):
            raise ValueError("session export snapshot must be canonical")
        normalized_events: list[Mapping[str, Any]] = []
        for event in self.events:
            if not isinstance(event, Mapping):
                raise ValueError("session export events must be mappings")
            normalized_events.append(MappingProxyType(dict(event)))
        object.__setattr__(self, "events", tuple(normalized_events))


class SessionApplicationService:
    """Application-level session start and inspection operations.

    提供应用层的会话启动与检查操作."""

    __slots__ = (
        "_catalog",
        "_event_queries",
        "_execution_queries",
        "_item_queries",
        "_lifecycle",
        "_store",
        "_summary_queries",
        "_task_queries",
    )

    def __init__(
        self,
        store: SessionStore,
        *,
        workspace_matcher: SessionWorkspaceMatcher | None = None,
    ) -> None:
        self._store = store
        self._lifecycle = SessionLifecycleService(store)
        self._event_queries = SessionEventQueryService(store)
        self._execution_queries = SessionExecutionQueryService(store)
        self._item_queries = SessionItemQueryService(store)
        self._summary_queries = SessionSummaryQueryService(store)
        self._task_queries = SessionTaskQueryService(store)
        self._catalog = SessionCatalogApplicationService(
            store,
            workspace_matcher=workspace_matcher,
        )

    async def start_session(self, request: StartSessionRequest) -> SessionSummary:
        """Create and return a canonical summary for a new session.

        ``create_session`` remains the storage adapter's atomic create
        operation.  The subsequent projection read is intentionally separate;
        this service does not claim that it atomically includes a turn event
        or execution record.

        创建并返回新会话的规范摘要.
        `create_session` 仍是存储适配器提供的原子创建操作;后续投影读取有意独立进行,
        因此本服务不声称它与回合事件或执行记录处于同一个原子事务中.
        """

        return await self._lifecycle.start_session(request)

    async def inspect_session(self, session_id: str) -> SessionInspection:
        """Return only the safe summary and durable execution projection.

        仅返回安全摘要和持久化执行投影."""

        return await self._catalog.inspect_session(session_id)

    async def get_session_summary(self, request: GetSessionSummaryRequest) -> SessionSummary:
        """Load one safe session summary through the storage port.

        通过存储端口加载一个安全的会话摘要.

        This use case intentionally does not load messages, execution
        records, aliases, or workspace policy.  Inbound adapters can apply
        their own visibility and protocol rules to the returned summary.

        本用例有意不加载消息、执行记录、别名或工作区策略.入站适配器可以对返回的摘要
        应用各自的可见性和协议规则.
        """

        return await self._summary_queries.get_session_summary(request)

    async def bind_session_alias(self, request: BindSessionAliasRequest) -> None:
        """Bind an external session identifier through the storage port.

        通过存储端口绑定一个外部会话标识.

        Alias uniqueness and durable conflict handling remain owned by the
        storage adapter; this service only provides the typed session boundary.

        别名唯一性和持久化冲突处理仍由存储适配器负责;本服务只提供类型化的会话边界.
        """

        if not isinstance(request, BindSessionAliasRequest):
            raise ValueError("bind session alias request must be canonical")
        await self._store.bind_session_alias(
            request.namespace,
            request.external_id,
            request.session_id,
        )

    async def resolve_session_alias(self, request: ResolveSessionAliasRequest) -> str:
        """Resolve an external session identifier through the storage port.

        通过存储端口解析一个外部会话标识.
        """

        if not isinstance(request, ResolveSessionAliasRequest):
            raise ValueError("resolve session alias request must be canonical")
        return await self._store.resolve_session_alias(request.namespace, request.external_id)

    async def get_or_create_session_alias(
        self,
        request: GetOrCreateSessionAliasRequest,
    ) -> str:
        """Return or allocate a durable alias for one session.

        返回或为一个会话分配持久化别名.
        """

        if not isinstance(request, GetOrCreateSessionAliasRequest):
            raise ValueError("get or create session alias request must be canonical")
        return await self._store.get_or_create_session_alias(
            request.namespace,
            request.session_id,
            request.proposed_external_id,
        )

    async def load_session_items(
        self,
        request: LoadSessionItemsRequest,
    ) -> tuple[SessionItem, ...]:
        """Load ordered durable conversation items through the application seam.

        The service owns only the persisted item read.  Plan, execution-record,
        workspace, and sandbox policies remain separate concerns of the
        conversation/composition owners.

        通过应用接缝加载按顺序持久化的会话项.
        本服务只拥有持久化会话项读取;计划、执行记录、工作区和 sandbox 策略仍由会话/组合层分别负责.
        """

        return await self._item_queries.load_session_items(request)

    async def load_session_events(
        self,
        request: LoadSessionEventsRequest,
    ) -> tuple[Mapping[str, Any], ...]:
        """Load copied event rows through the typed application seam.

        The rows remain an untrusted storage projection rather than a second
        event domain model.  Each outer mapping is immutable to prevent a
        caller from mutating storage-owned data through a read result.

        通过类型化应用接缝加载复制后的事件行.
        这些行仍是不可信的存储投影,而不是第二套事件领域模型;每个外层映射不可变,
        从而避免调用方通过读取结果修改存储层数据.
        """

        return await self._event_queries.load_session_events(request)

    async def load_execution_record(
        self,
        request: LoadExecutionRecordRequest,
    ) -> SessionExecutionRecord | None:
        """Load one safe durable execution projection through the application seam.

        通过应用接缝加载一个安全的持久化执行投影.
        """

        return await self._execution_queries.load_execution_record(request)

    async def load_session_plan(
        self,
        request: LoadSessionPlanRequest,
    ) -> SessionPlan | None:
        """Load one persisted plan through the application session seam.

        The application service owns the typed read boundary; plan comments
        and plan execution remain separate conversation concerns.

        通过应用会话接缝加载一个已持久化计划.
        应用服务负责类型化读取边界;计划评论和计划执行仍由会话层分别负责.
        """

        if not isinstance(request, LoadSessionPlanRequest):
            raise ValueError("load session plan request must be canonical")
        return await self._store.load_session_plan(request.session_id)

    async def list_plan_comments(
        self,
        request: ListPlanCommentsRequest,
    ) -> tuple[PlanComment, ...]:
        """Load bounded comments for one persisted plan revision.

        The plan value is part of the request because storage uses its stable
        fingerprint to keep comments attached to the correct revision.

        加载一个已持久化计划版本的有界评论.
        计划值对象属于请求的一部分,因为存储层使用其稳定 fingerprint 将评论绑定到正确版本.
        """

        if not isinstance(request, ListPlanCommentsRequest):
            raise ValueError("list plan comments request must be canonical")
        return tuple(await self._store.list_plan_comments(request.session_id, request.plan))

    async def list_session_tasks(
        self,
        request: ListSessionTasksRequest,
    ) -> tuple[SessionTask, ...]:
        """Load a bounded task projection without taking task lifecycle ownership.

        The application seam owns only this read.  Task creation, start, finish,
        permissions, and execution remain with the existing conversation/runtime
        owners.

        加载有界任务投影,但不接管任务生命周期.
        应用接缝只拥有该读取;任务创建、启动、完成、权限和执行仍由现有会话/运行时 owner 负责.
        """

        if not isinstance(request, ListSessionTasksRequest):
            raise ValueError("list session tasks request must be canonical")
        return await self._task_queries.list_session_tasks(request)

    async def get_session_task(
        self,
        request: GetSessionTaskRequest,
    ) -> SessionTask | None:
        """Load one task projection without changing its lifecycle state.

        在不改变任务生命周期状态的情况下加载一个任务投影.
        """

        if not isinstance(request, GetSessionTaskRequest):
            raise ValueError("get session task request must be canonical")
        return await self._task_queries.get_session_task(request)

    async def export_session(self, request: ExportSessionRequest) -> SessionExport:
        """Read the durable projection required by an explicit export command.

        The application seam owns which persisted pieces belong to an export;
        inbound adapters still own rendering, output paths, and protocol
        details.  Event rows are loaded only when the request asks for them,
        keeping Markdown exports free of an unnecessary event read.

        读取显式导出命令所需的持久化投影.
        应用接缝负责确定导出所需的持久化内容;入站适配器仍负责渲染、输出路径和协议细节.
        只有请求明确要求时才读取事件行,从而避免 Markdown 导出执行不必要的事件查询.
        """

        if not isinstance(request, ExportSessionRequest):
            raise ValueError("export session request must be canonical")
        summary = await self._store.get_session(request.session_id)
        items = await self._item_queries.load_session_items(
            LoadSessionItemsRequest(request.session_id)
        )
        events: tuple[Mapping[str, Any], ...] = ()
        if request.include_events:
            events = await self._event_queries.load_session_events(
                LoadSessionEventsRequest(request.session_id)
            )
        return SessionExport(SessionSnapshot(summary, items), events)

    async def import_session(self, request: ImportSessionRequest) -> str:
        """Persist one already-parsed session snapshot through the storage port.

        The service deliberately does not read files, parse an upstream
        format, or render import statistics.  It owns only the typed inbound
        boundary for the durable import operation.

        通过存储端口持久化一个已经解析的会话快照.
        本服务有意不读取文件、解析上游格式或渲染导入统计;它只拥有持久化导入操作
        的类型化入站边界.
        """

        return await self._lifecycle.import_session(request)

    async def list_sessions(
        self,
        request: ListSessionsRequest | None = None,
    ) -> tuple[SessionInspection, ...]:
        """Delegate the bounded catalog read to the canonical catalog service.

        将有界目录读取委托给规范目录服务.
        """

        return await self._catalog.list_sessions(request)

    async def list_sessions_page(
        self,
        request: ListSessionsPageRequest,
    ) -> tuple[SessionSummary, ...]:
        """Delegate one typed keyset page to the catalog service.

        将一个类型化键集分页委托给目录服务.
        """

        return await self._catalog.list_sessions_page(request)

    async def list_sessions_in_workspace(
        self,
        workspace: str,
        *,
        limit: int = 1000,
        result_limit: int = 50,
    ) -> tuple[SessionSummary, ...]:
        """Delegate an injected-workspace catalog query.

        将注入工作区范围内的目录查询委托给目录服务.
        """

        return await self._catalog.list_sessions_in_workspace(
            workspace,
            limit=limit,
            result_limit=result_limit,
        )

    async def search_sessions(
        self,
        request: SearchSessionsRequest,
    ) -> SessionSearchInspectionPage:
        """Delegate safe session search to the canonical catalog service.

        将安全会话搜索委托给规范目录服务.
        """

        return await self._catalog.search_sessions(request)

    async def search_sessions_in_workspace(
        self,
        query: str,
        workspace: str,
        *,
        limit: int = 1000,
        result_limit: int = 50,
    ) -> tuple[SessionSearchHit, ...]:
        """Delegate an injected-workspace search query.

        将注入工作区范围内的搜索查询委托给目录服务.
        """

        return await self._catalog.search_sessions_in_workspace(
            query,
            workspace,
            limit=limit,
            result_limit=result_limit,
        )

    async def rename_session(self, request: RenameSessionRequest) -> SessionSummary:
        """Rename one session through the storage port.

        通过存储端口重命名一个会话.
        """

        return await self._lifecycle.rename_session(request)

    async def prepare_resume(self, request: ResumeSessionRequest) -> SessionInspection:
        """Validate a persisted session before a composition opens its runner.

        This is intentionally a read-only preflight.  It does not load model
        context, acquire a turn lock, run a model, or replay a tool.  The
        existing composition/runtime resume path remains the owner of those
        operations until a later integration slice.

        在组合层打开运行器之前验证已持久化会话.
        这是有意保持只读的预检:不会加载模型上下文,获取回合锁,运行模型或重放工具;
        这些操作仍由现有组合层和运行时负责,直到后续集成阶段再调整.
        """

        if not isinstance(request, ResumeSessionRequest):
            raise ValueError("resume request must be canonical")
        return await self.inspect_session(request.session_id)

    async def fork_session(self, request: ForkSessionRequest) -> str:
        """Create a durable independent copy through the storage port.

        Workspace, alias, binding, and publication policy remain owned by the
        inbound adapter that needs them. The shared application service only
        validates the typed intent and delegates the atomic storage operation.

        通过存储端口创建一个持久化的独立副本.
        工作区,别名,绑定和发布策略仍由需要它们的入站适配器负责;共享应用服务只验证
        类型化意图,并委托原子存储操作.
        """

        return await self._lifecycle.fork_session(request)

    async def delete_session(self, request: DeleteSessionRequest) -> None:
        """Delete one persisted session through the storage port.

        通过存储端口删除一个已持久化会话.

        Workspace visibility, ACP aliases, active bindings, and protocol
        cleanup remain owned by the inbound adapter.  This use case only
        validates the durable session intent and delegates the store-owned
        delete operation.

        工作区可见性、ACP 别名、活动绑定和协议清理由入站适配器负责.本用例只验证
        持久会话意图,并委托存储端口执行删除.
        """

        await self._lifecycle.delete_session(request)

    def bind_runner(self, runner: SessionTurnRunner) -> SessionTurnService:
        """Create a non-owning turn facade for an existing binding runner.

        为已有绑定运行器创建一个不拥有其生命周期的回合门面."""

        return SessionTurnService(runner)


__all__ = [
    "BindSessionAliasRequest",
    "DeleteSessionRequest",
    "ExportSessionRequest",
    "ForkSessionRequest",
    "GetOrCreateSessionAliasRequest",
    "GetSessionSummaryRequest",
    "GetSessionTaskRequest",
    "ImportSessionRequest",
    "ListPlanCommentsRequest",
    "ListSessionTasksRequest",
    "ListSessionsPageRequest",
    "ListSessionsRequest",
    "LoadExecutionRecordRequest",
    "LoadExecutionRecordsRequest",
    "LoadSessionEventsRequest",
    "LoadSessionItemsRequest",
    "LoadSessionPlanRequest",
    "RenameSessionRequest",
    "ResolveSessionAliasRequest",
    "ResumeSessionRequest",
    "RunTurnRequest",
    "SearchSessionsRequest",
    "SessionApplicationService",
    "SessionEventQueryController",
    "SessionEventQueryService",
    "SessionExecutionQueryController",
    "SessionExecutionQueryService",
    "SessionExport",
    "SessionInspection",
    "SessionItemQueryController",
    "SessionItemQueryService",
    "SessionLifecycleController",
    "SessionLifecycleService",
    "SessionSearchInspection",
    "SessionSearchInspectionPage",
    "SessionSummaryQueryController",
    "SessionSummaryQueryService",
    "SessionTaskQueryController",
    "SessionTaskQueryService",
    "SessionTurnRunner",
    "SessionTurnService",
    "SessionWorkspaceMatcher",
    "StartSessionRequest",
]
