from __future__ import annotations

import asyncio
import unittest
from collections.abc import MutableMapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions import (
    BindSessionAliasRequest,
    DeleteSessionRequest,
    ExportSessionRequest,
    ForkSessionRequest,
    GetOrCreateSessionAliasRequest,
    GetSessionSummaryRequest,
    GetSessionTaskRequest,
    ImportSessionRequest,
    ListPlanCommentsRequest,
    ListSessionsPageRequest,
    ListSessionsRequest,
    ListSessionTasksRequest,
    LoadExecutionRecordRequest,
    LoadExecutionRecordsRequest,
    LoadSessionEventsRequest,
    LoadSessionItemsRequest,
    LoadSessionPlanRequest,
    RenameSessionRequest,
    ResolveSessionAliasRequest,
    ResumeSessionRequest,
    RunTurnRequest,
    SearchSessionsRequest,
    SessionApplicationService,
    SessionExport,
    SessionInspection,
    SessionSearchInspection,
    SessionSearchInspectionPage,
    SessionSelectionService,
    StartSessionRequest,
)
from neuro_code.application.sessions.catalog import SessionCatalogApplicationService
from neuro_code.application.sessions.contracts import SessionOption, SessionSelectionResult
from neuro_code.application.sessions.event_queries import SessionEventQueryService
from neuro_code.application.sessions.execution_queries import SessionExecutionQueryService
from neuro_code.application.sessions.item_queries import SessionItemQueryService
from neuro_code.application.sessions.lifecycle import SessionLifecycleService
from neuro_code.application.sessions.summary import SessionSummaryQueryService
from neuro_code.application.sessions.turns import SessionTurnService as CanonicalSessionTurnService
from neuro_code.domain.conversation.messages import ContentPart, Message, Role, SessionItem
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SessionExecutionRecord,
    SupervisorReasonCode,
    TurnCancellationPolicy,
    TurnSource,
)
from neuro_code.domain.plans import PlanComment, PlanStep, SessionPlan
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary
from neuro_code.domain.sessions.search import SessionSearchHit, SessionSearchPage
from neuro_code.shared.errors import SessionError


class SessionStoreFixture:
    def __init__(self, summary: SessionSummary, record: SessionExecutionRecord | None) -> None:
        self.summary = summary
        self.record = record
        self.plan: SessionPlan | None = None
        self.plan_comments: list[PlanComment] = []
        self.tasks: list[SessionTask] = []
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.create_error: SessionError | None = None
        self.sessions = (summary,)
        self.search_page = SessionSearchPage((), None, 0)
        self.rename_result = summary
        self.items: list[SessionItem] = [Message(Role.USER, "fixture message")]
        self.events: list[dict[str, object]] = [
            {
                "sequence": 1,
                "kind": "session_started",
                "created_at": "2026-01-01T00:00:00+00:00",
                "data": {},
            }
        ]
        self.aliases: dict[tuple[str, str], str] = {}

    async def create_session(
        self,
        cwd: str,
        provider: str,
        model: str,
        context_affinity: str | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    ) -> str:
        self.calls.append(
            (
                "create_session",
                (cwd, provider, model, context_affinity, sandbox_profile),
            )
        )
        if self.create_error is not None:
            raise self.create_error
        return self.summary.id

    async def get_session(self, session_id: str) -> SessionSummary:
        self.calls.append(("get_session", (session_id,)))
        return self.summary

    async def bind_session_alias(
        self,
        namespace: str,
        external_id: str,
        session_id: str,
    ) -> None:
        self.calls.append(("bind_session_alias", (namespace, external_id, session_id)))
        self.aliases[(namespace, external_id)] = session_id

    async def resolve_session_alias(self, namespace: str, external_id: str) -> str:
        self.calls.append(("resolve_session_alias", (namespace, external_id)))
        return self.aliases[(namespace, external_id)]

    async def get_or_create_session_alias(
        self,
        namespace: str,
        session_id: str,
        proposed_external_id: str,
    ) -> str:
        self.calls.append(
            ("get_or_create_session_alias", (namespace, session_id, proposed_external_id))
        )
        for (saved_namespace, external_id), saved_session_id in self.aliases.items():
            if saved_namespace == namespace and saved_session_id == session_id:
                return external_id
        self.aliases[(namespace, proposed_external_id)] = session_id
        return proposed_external_id

    async def import_session(self, snapshot: SessionSnapshot) -> str:
        self.calls.append(("import_session", (snapshot,)))
        return snapshot.summary.id

    async def delete_session(self, session_id: str) -> None:
        self.calls.append(("delete_session", (session_id,)))

    async def load_execution_record(self, session_id: str) -> SessionExecutionRecord | None:
        self.calls.append(("load_execution_record", (session_id,)))
        return self.record

    async def load_execution_records(
        self,
        session_ids: Sequence[str],
    ) -> tuple[SessionExecutionRecord | None, ...]:
        normalized_ids = tuple(session_ids)
        self.calls.append(("load_execution_records", (normalized_ids,)))
        return tuple(self.record for _ in normalized_ids)

    async def load_session_items(self, session_id: str) -> list[SessionItem]:
        self.calls.append(("load_session_items", (session_id,)))
        return list(self.items)

    async def load_session_plan(self, session_id: str) -> SessionPlan | None:
        self.calls.append(("load_session_plan", (session_id,)))
        return self.plan

    async def list_plan_comments(
        self,
        session_id: str,
        plan: SessionPlan,
    ) -> list[PlanComment]:
        self.calls.append(("list_plan_comments", (session_id, plan)))
        return list(self.plan_comments)

    async def list_session_tasks(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[SessionTask]:
        self.calls.append(("list_session_tasks", (session_id, limit)))
        return list(self.tasks[:limit])

    async def get_session_task(self, session_id: str, task_id: str) -> SessionTask | None:
        self.calls.append(("get_session_task", (session_id, task_id)))
        return next((task for task in self.tasks if task.task_id == task_id), None)

    async def load_events(self, session_id: str) -> list[dict[str, object]]:
        self.calls.append(("load_events", (session_id,)))
        return [dict(event) for event in self.events]

    async def fork_session(self, session_id: str) -> str:
        self.calls.append(("fork_session", (session_id,)))
        return "forked-session"

    async def list_sessions(self, *, limit: int = 50) -> list[SessionSummary]:
        self.calls.append(("list_sessions", (limit,)))
        return list(self.sessions[:limit])

    async def list_sessions_page(
        self,
        *,
        limit: int,
        before_updated_at: datetime | None = None,
        before_id: str | None = None,
    ) -> list[SessionSummary]:
        self.calls.append(("list_sessions_page", (limit, before_updated_at, before_id)))
        return list(self.sessions[:limit])

    async def search_sessions(
        self,
        query: str,
        *,
        cwd: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_content: bool = False,
    ) -> SessionSearchPage:
        self.calls.append(("search_sessions", (query, cwd, limit, offset, include_content)))
        return self.search_page

    async def update_session_title(self, session_id: str, title: str) -> SessionSummary:
        self.calls.append(("update_session_title", (session_id, title)))
        return self.rename_result


class SessionTurnRunnerFixture:
    def __init__(self) -> None:
        self.session_id = "session-1"
        self.calls: list[tuple[object, ...]] = []
        self.result = AgentRunResult("session-1", "done", (), (), (), 1)
        self.cancel = False

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: Sequence[ContentPart] = (),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
    ) -> AgentRunResult:
        self.calls.append(
            (
                prompt,
                sink,
                tuple(content_parts),
                cancellation_policy,
                turn_source,
            )
        )
        if self.cancel:
            raise asyncio.CancelledError
        return self.result


def _summary() -> SessionSummary:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SessionSummary(
        id="session-1",
        cwd="/workspace",
        provider="fixture",
        model="fixture-model",
        created_at=now,
        updated_at=now,
        context_affinity="fixture-affinity",
        sandbox_profile=SandboxProfile.OFF,
    )


def _record() -> SessionExecutionRecord:
    return SessionExecutionRecord(
        AgentExecutionOutcome(
            AgentExecutionStatus.STUCK,
            SupervisorReasonCode.NO_PROGRESS,
            finalized=True,
            recoverable=True,
        ),
        event_sequence=7,
        completed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class SessionApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.summary = _summary()
        self.record = _record()
        self.store = SessionStoreFixture(self.summary, self.record)
        self.service = SessionApplicationService(cast(SessionStore, self.store))

    async def test_lifecycle_service_owns_durable_commands_without_policy_leakage(self) -> None:
        lifecycle = SessionLifecycleService(cast(SessionStore, self.store))
        snapshot = SessionSnapshot(self.summary, tuple(self.store.items))

        started = await lifecycle.start_session(
            StartSessionRequest("/workspace", "fixture", "fixture-model")
        )
        imported_id = await lifecycle.import_session(ImportSessionRequest(snapshot))
        renamed = await lifecycle.rename_session(RenameSessionRequest("session-1", "Renamed"))
        forked_id = await lifecycle.fork_session(ForkSessionRequest("session-1"))
        await lifecycle.delete_session(DeleteSessionRequest("session-1"))

        self.assertIs(started, self.summary)
        self.assertEqual(imported_id, snapshot.summary.id)
        self.assertIs(renamed, self.summary)
        self.assertEqual(forked_id, "forked-session")
        self.assertEqual(
            [name for name, _args in self.store.calls],
            [
                "create_session",
                "get_session",
                "import_session",
                "update_session_title",
                "fork_session",
                "delete_session",
            ],
        )

    async def test_session_selection_service_delegates_without_owning_lifecycle(self) -> None:
        option = SessionOption(
            session_id=self.summary.id,
            source_provider=self.summary.provider,
            source_model=self.summary.model,
            updated_at=self.summary.updated_at,
            resume_profile=self.summary.provider,
            current=True,
            source_profile_match=True,
            selectable=True,
            title=self.summary.title,
        )
        result = SessionSelectionResult(
            session_id=self.summary.id,
            source_provider=self.summary.provider,
            source_model=self.summary.model,
            profile_name=self.summary.provider,
            provider_name=self.summary.provider,
            model_name=self.summary.model,
            previous_session_id=None,
            changed=False,
            source_profile_match=True,
            items=tuple(self.store.items),
        )

        class Owner:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]:
                self.calls.append(("list", query))
                return (option,)

            async def select_session(self, session_id: str) -> SessionSelectionResult:
                self.calls.append(("select", session_id))
                return result

            async def rename_session(self, title: str) -> SessionSummary:
                self.calls.append(("rename", title))
                return replace(self_summary, title=title)

        self_summary = self.summary
        owner = Owner()
        service = SessionSelectionService(owner)

        self.assertEqual(await service.list_sessions("fixture"), (option,))
        self.assertIs(await service.select_session(self.summary.id), result)
        renamed = await service.rename_session("Renamed")

        self.assertEqual(renamed.title, "Renamed")
        self.assertEqual(
            owner.calls,
            [("list", "fixture"), ("select", self.summary.id), ("rename", "Renamed")],
        )

    async def test_start_session_forwards_typed_request_and_returns_summary(self) -> None:
        request = StartSessionRequest(
            cwd="/workspace",
            provider="fixture",
            model="fixture-model",
            context_affinity="fixture-affinity",
            sandbox_profile=SandboxProfile.OFF,
        )

        result = await self.service.start_session(request)

        self.assertIs(result, self.summary)
        self.assertEqual(
            self.store.calls,
            [
                (
                    "create_session",
                    (
                        "/workspace",
                        "fixture",
                        "fixture-model",
                        "fixture-affinity",
                        SandboxProfile.OFF,
                    ),
                ),
                ("get_session", ("session-1",)),
            ],
        )

    async def test_import_session_forwards_typed_snapshot(self) -> None:
        snapshot = SessionSnapshot(self.summary, tuple(self.store.items))

        result = await self.service.import_session(ImportSessionRequest(snapshot))

        self.assertEqual(result, "session-1")
        self.assertEqual(self.store.calls, [("import_session", (snapshot,))])

    async def test_inspect_session_returns_safe_execution_projection(self) -> None:
        inspection = await self.service.inspect_session("session-1")

        self.assertEqual(
            inspection,
            SessionInspection(self.summary, self.record),
        )
        self.assertEqual(
            self.store.calls,
            [
                ("get_session", ("session-1",)),
                ("load_execution_record", ("session-1",)),
            ],
        )

    async def test_delete_session_forwards_typed_request(self) -> None:
        await self.service.delete_session(DeleteSessionRequest("session-1"))

        self.assertEqual(self.store.calls, [("delete_session", ("session-1",))])

    async def test_get_session_summary_forwards_typed_request(self) -> None:
        result = await self.service.get_session_summary(GetSessionSummaryRequest("session-1"))

        self.assertIs(result, self.summary)
        self.assertEqual(self.store.calls, [("get_session", ("session-1",))])

    async def test_summary_query_service_forwards_typed_request(self) -> None:
        service = SessionSummaryQueryService(cast(SessionStore, self.store))

        result = await service.get_session_summary(GetSessionSummaryRequest("session-1"))

        self.assertIs(result, self.summary)
        self.assertEqual(self.store.calls, [("get_session", ("session-1",))])

    async def test_session_aliases_use_typed_application_requests(self) -> None:
        await self.service.bind_session_alias(
            BindSessionAliasRequest("acp-v1", "external-1", "session-1")
        )
        resolved = await self.service.resolve_session_alias(
            ResolveSessionAliasRequest("acp-v1", "external-1")
        )
        allocated = await self.service.get_or_create_session_alias(
            GetOrCreateSessionAliasRequest("acp-v1", "session-1", "external-2")
        )

        self.assertEqual(resolved, "session-1")
        self.assertEqual(allocated, "external-1")
        self.assertEqual(
            self.store.calls,
            [
                ("bind_session_alias", ("acp-v1", "external-1", "session-1")),
                ("resolve_session_alias", ("acp-v1", "external-1")),
                ("get_or_create_session_alias", ("acp-v1", "session-1", "external-2")),
            ],
        )

    async def test_load_session_items_forwards_typed_request(self) -> None:
        result = await self.service.load_session_items(LoadSessionItemsRequest("session-1"))

        self.assertEqual(result, tuple(self.store.items))
        self.assertEqual(self.store.calls, [("load_session_items", ("session-1",))])

    async def test_item_query_service_forwards_typed_request(self) -> None:
        query_service = SessionItemQueryService(cast(SessionStore, self.store))

        result = await query_service.load_session_items(LoadSessionItemsRequest("session-1"))

        self.assertEqual(result, tuple(self.store.items))
        self.assertEqual(self.store.calls, [("load_session_items", ("session-1",))])

    async def test_load_session_events_returns_immutable_projection(self) -> None:
        result = await self.service.load_session_events(LoadSessionEventsRequest("session-1"))

        self.assertEqual(result, (self.store.events[0],))
        event = cast(MutableMapping[str, object], result[0])
        with self.assertRaises(TypeError):
            event["kind"] = "changed"
        self.assertEqual(self.store.calls, [("load_events", ("session-1",))])

    async def test_event_query_service_forwards_typed_request(self) -> None:
        query_service = SessionEventQueryService(cast(SessionStore, self.store))

        result = await query_service.load_session_events(LoadSessionEventsRequest("session-1"))

        self.assertEqual(result, (self.store.events[0],))
        event = cast(MutableMapping[str, object], result[0])
        with self.assertRaises(TypeError):
            event["kind"] = "changed"
        self.assertEqual(self.store.calls, [("load_events", ("session-1",))])

    async def test_load_execution_record_forwards_typed_request(self) -> None:
        result = await self.service.load_execution_record(LoadExecutionRecordRequest("session-1"))

        self.assertIs(result, self.record)
        self.assertEqual(self.store.calls, [("load_execution_record", ("session-1",))])

    async def test_execution_query_service_forwards_single_and_bulk_requests(self) -> None:
        query_service = SessionExecutionQueryService(cast(SessionStore, self.store))

        result = await query_service.load_execution_record(LoadExecutionRecordRequest("session-1"))
        records = await query_service.load_execution_records(
            LoadExecutionRecordsRequest(("session-1",))
        )

        self.assertIs(result, self.record)
        self.assertEqual(records, (self.record,))
        self.assertEqual(
            self.store.calls,
            [
                ("load_execution_record", ("session-1",)),
                ("load_execution_records", (("session-1",),)),
            ],
        )

    async def test_load_session_plan_forwards_typed_request(self) -> None:
        result = await self.service.load_session_plan(LoadSessionPlanRequest("session-1"))

        self.assertIsNone(result)
        self.assertEqual(self.store.calls, [("load_session_plan", ("session-1",))])

    async def test_list_plan_comments_forwards_typed_request(self) -> None:
        plan = SessionPlan((PlanStep("fixture step"),))
        comment = PlanComment("comment-1", 1, "fixture comment", datetime(2026, 1, 1, tzinfo=UTC))
        self.store.plan_comments = [comment]

        result = await self.service.list_plan_comments(ListPlanCommentsRequest("session-1", plan))

        self.assertEqual(result, (comment,))
        self.assertEqual(self.store.calls, [("list_plan_comments", ("session-1", plan))])

    async def test_list_session_tasks_forwards_bounded_typed_request(self) -> None:
        task = SessionTask(
            "task-1",
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.QUEUED,
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.store.tasks = [task]

        result = await self.service.list_session_tasks(ListSessionTasksRequest("session-1", 3))

        self.assertEqual(result, (task,))
        self.assertEqual(self.store.calls, [("list_session_tasks", ("session-1", 3))])

    async def test_get_session_task_forwards_typed_request(self) -> None:
        task = SessionTask(
            "task-1",
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.QUEUED,
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.store.tasks = [task]

        result = await self.service.get_session_task(GetSessionTaskRequest("session-1", "task-1"))

        self.assertIs(result, task)
        self.assertEqual(self.store.calls, [("get_session_task", ("session-1", "task-1"))])

    async def test_export_session_reads_snapshot_without_events_by_default(self) -> None:
        result = await self.service.export_session(ExportSessionRequest("session-1"))

        self.assertEqual(
            result,
            SessionExport(SessionSnapshot(self.summary, tuple(self.store.items))),
        )
        self.assertEqual(
            self.store.calls,
            [
                ("get_session", ("session-1",)),
                ("load_session_items", ("session-1",)),
            ],
        )

    async def test_export_session_can_include_copied_events(self) -> None:
        result = await self.service.export_session(
            ExportSessionRequest("session-1", include_events=True)
        )

        self.assertEqual(result.snapshot, SessionSnapshot(self.summary, tuple(self.store.items)))
        self.assertEqual(result.events, (self.store.events[0],))
        self.assertEqual(
            self.store.calls,
            [
                ("get_session", ("session-1",)),
                ("load_session_items", ("session-1",)),
                ("load_events", ("session-1",)),
            ],
        )
        event = cast(MutableMapping[str, object], result.events[0])
        with self.assertRaises(TypeError):
            event["kind"] = "changed"
        self.assertEqual(result.events[0]["kind"], "session_started")

    async def test_list_sessions_returns_safe_catalog_projections(self) -> None:
        inspections = await self.service.list_sessions(ListSessionsRequest(limit=10))

        self.assertEqual(inspections, (SessionInspection(self.summary, self.record),))

    async def test_catalog_service_is_the_direct_read_owner(self) -> None:
        catalog = SessionCatalogApplicationService(cast(SessionStore, self.store))

        inspections = await catalog.list_sessions(ListSessionsRequest(limit=10))

        self.assertEqual(inspections, (SessionInspection(self.summary, self.record),))
        self.assertIn(("list_sessions", (10,)), self.store.calls)
        self.assertIn(("load_execution_records", (("session-1",),)), self.store.calls)
        self.assertEqual(
            self.store.calls,
            [
                ("list_sessions", (10,)),
                ("load_execution_records", (("session-1",),)),
            ],
        )

    async def test_list_sessions_page_returns_safe_summaries_through_typed_query(self) -> None:
        cursor_at = datetime(2026, 1, 2, tzinfo=UTC)

        result = await self.service.list_sessions_page(
            ListSessionsPageRequest(10, before_updated_at=cursor_at, before_id="cursor")
        )

        self.assertEqual(result, (self.summary,))
        self.assertEqual(
            self.store.calls,
            [("list_sessions_page", (10, cursor_at, "cursor"))],
        )

    async def test_workspace_catalog_uses_injected_matcher_and_bound(self) -> None:
        other = replace(self.summary, id="session-2", cwd="/other")
        self.store.sessions = (self.summary, other)
        service = SessionApplicationService(
            cast(SessionStore, self.store),
            workspace_matcher=lambda recorded, workspace: recorded == workspace,
        )

        sessions = await service.list_sessions_in_workspace("/workspace")

        self.assertEqual(sessions, (self.summary,))
        self.assertEqual(self.store.calls, [("list_sessions", (1000,))])

    async def test_workspace_search_uses_injected_matcher_and_bound(self) -> None:
        other = replace(self.summary, id="session-2", cwd="/other")
        first_hit = SessionSearchHit(self.summary, 2.0, ("title",), "workspace")
        second_hit = SessionSearchHit(other, 1.0, ("content",), "other")
        self.store.search_page = SessionSearchPage((first_hit, second_hit), None, 2)
        service = SessionApplicationService(
            cast(SessionStore, self.store),
            workspace_matcher=lambda recorded, workspace: recorded == workspace,
        )

        hits = await service.search_sessions_in_workspace("workspace", "/workspace")

        self.assertEqual(hits, (first_hit,))
        self.assertEqual(
            self.store.calls,
            [("search_sessions", ("workspace", None, 1000, 0, True))],
        )

    async def test_workspace_catalog_requires_an_injected_matcher(self) -> None:
        with self.assertRaisesRegex(ValueError, "matching is unavailable"):
            await self.service.list_sessions_in_workspace("/workspace")

    async def test_search_sessions_attaches_safe_execution_projection(self) -> None:
        hit = SessionSearchHit(self.summary, 1.0, ("title",), "fixture")
        self.store.search_page = SessionSearchPage((hit,), 20, 1)

        page = await self.service.search_sessions(
            SearchSessionsRequest(
                "fixture",
                cwd="/workspace",
                limit=20,
                offset=0,
                include_content=True,
            )
        )

        self.assertEqual(
            page,
            SessionSearchInspectionPage(
                (SessionSearchInspection(hit, self.record),),
                20,
                1,
            ),
        )
        self.assertEqual(
            self.store.calls,
            [
                ("search_sessions", ("fixture", "/workspace", 20, 0, True)),
                ("load_execution_records", (("session-1",),)),
            ],
        )

    async def test_rename_session_forwards_typed_request(self) -> None:
        renamed = await self.service.rename_session(
            RenameSessionRequest("session-1", "Renamed session")
        )

        self.assertIs(renamed, self.summary)
        self.assertEqual(
            self.store.calls,
            [("update_session_title", ("session-1", "Renamed session"))],
        )

    def test_catalog_requests_reject_invalid_values(self) -> None:
        invalid_factories = (
            lambda: DeleteSessionRequest(" "),
            lambda: ExportSessionRequest(" "),
            lambda: ImportSessionRequest(cast(SessionSnapshot, object())),
            lambda: LoadSessionItemsRequest(" "),
            lambda: LoadSessionEventsRequest(" "),
            lambda: LoadExecutionRecordRequest(" "),
            lambda: LoadExecutionRecordsRequest((" ",)),
            lambda: LoadSessionPlanRequest(" "),
            lambda: ListPlanCommentsRequest(" ", SessionPlan((PlanStep("step"),))),
            lambda: ListPlanCommentsRequest("session-1", cast(SessionPlan, object())),
            lambda: ListSessionTasksRequest(" ", 1),
            lambda: ListSessionTasksRequest("session-1", 0),
            lambda: ListSessionTasksRequest("session-1", cast(int, True)),
            lambda: GetSessionTaskRequest(" ", "task-1"),
            lambda: GetSessionTaskRequest("session-1", " "),
            lambda: ExportSessionRequest("session-1", include_events=cast(bool, 1)),
            lambda: GetSessionSummaryRequest(" "),
            lambda: BindSessionAliasRequest(" ", "external", "session-1"),
            lambda: BindSessionAliasRequest("acp-v1", " ", "session-1"),
            lambda: BindSessionAliasRequest("acp-v1", "external", " "),
            lambda: ResolveSessionAliasRequest("acp-v1", " "),
            lambda: GetOrCreateSessionAliasRequest("acp-v1", " ", "external"),
            lambda: GetOrCreateSessionAliasRequest("acp-v1", "session-1", " "),
            lambda: ListSessionsRequest(0),
            lambda: ListSessionsPageRequest(0),
            lambda: ListSessionsPageRequest(10, before_id="cursor"),
            lambda: SearchSessionsRequest("fixture", limit=0),
            lambda: SearchSessionsRequest("fixture", offset=-1),
            lambda: RenameSessionRequest("session-1", " "),
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

        with self.assertRaises(ValueError):
            SearchSessionsRequest(" ")
        with self.assertRaises(ValueError):
            ListSessionsRequest(cast(int, True))

    async def test_start_session_preserves_store_error(self) -> None:
        self.store.create_error = SessionError("session store unavailable")

        with self.assertRaisesRegex(SessionError, "session store unavailable"):
            await self.service.start_session(
                StartSessionRequest("/workspace", "fixture", "fixture-model")
            )

    async def test_request_and_inspection_reject_invalid_values(self) -> None:
        for values in (
            ("", "fixture", "fixture-model"),
            ("/workspace", "", "fixture-model"),
            ("/workspace", "fixture", ""),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                StartSessionRequest(*values)

        with self.assertRaises(ValueError):
            StartSessionRequest("/workspace", "fixture", "fixture-model", "")
        with self.assertRaises(ValueError):
            StartSessionRequest(
                "/workspace",
                "fixture",
                "fixture-model",
                sandbox_profile=cast(SandboxProfile, "workspace"),
            )
        with self.assertRaises(ValueError):
            await self.service.inspect_session("  ")

    async def test_noncanonical_request_is_rejected_before_storage(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.start_session(cast(StartSessionRequest, object()))
        self.assertEqual(self.store.calls, [])

    async def test_import_session_rejects_noncanonical_request(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.import_session(cast(ImportSessionRequest, object()))
        self.assertEqual(self.store.calls, [])

    def test_projection_rejects_noncanonical_values(self) -> None:
        with self.assertRaises(ValueError):
            SessionInspection(cast(SessionSummary, object()))
        with self.assertRaises(ValueError):
            SessionInspection(self.summary, cast(SessionExecutionRecord, object()))

    async def test_prepare_resume_is_read_only_and_returns_safe_projection(self) -> None:
        result = await self.service.prepare_resume(ResumeSessionRequest("session-1"))

        self.assertEqual(result, SessionInspection(self.summary, self.record))
        self.assertEqual(
            self.store.calls,
            [
                ("get_session", ("session-1",)),
                ("load_execution_record", ("session-1",)),
            ],
        )

    def test_resume_request_rejects_empty_session_id(self) -> None:
        with self.assertRaises(ValueError):
            ResumeSessionRequest(" ")

    async def test_fork_session_forwards_typed_request(self) -> None:
        result = await self.service.fork_session(ForkSessionRequest("session-1"))

        self.assertEqual(result, "forked-session")
        self.assertEqual(self.store.calls, [("fork_session", ("session-1",))])

    def test_fork_request_rejects_empty_session_id(self) -> None:
        with self.assertRaises(ValueError):
            ForkSessionRequest(" ")

    async def test_fork_session_rejects_noncanonical_request(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.fork_session(cast(ForkSessionRequest, object()))
        self.assertEqual(self.store.calls, [])

    async def test_bound_turn_service_forwards_request_without_owning_runner(self) -> None:
        runner = SessionTurnRunnerFixture()
        service = self.service.bind_runner(runner)

        self.assertIs(type(service), CanonicalSessionTurnService)
        request = RunTurnRequest(
            "inspect the workspace",
            content_parts=(ContentPart.from_text("extra context"),),
            cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
            turn_source=TurnSource.USER,
            expected_session_id="session-1",
        )

        result = await service.run_turn(request)

        self.assertIs(result, runner.result)
        self.assertEqual(service.session_id, "session-1")
        self.assertEqual(
            runner.calls,
            [
                (
                    "inspect the workspace",
                    None,
                    (ContentPart.from_text("extra context"),),
                    TurnCancellationPolicy.REWIND_PRISTINE,
                    TurnSource.USER,
                )
            ],
        )

    async def test_bound_turn_service_rejects_wrong_session_before_run(self) -> None:
        runner = SessionTurnRunnerFixture()
        service = self.service.bind_runner(runner)

        with self.assertRaises(ValueError):
            await service.run_turn(RunTurnRequest("continue", expected_session_id="other"))
        self.assertEqual(runner.calls, [])

    async def test_bound_turn_service_preserves_cancellation(self) -> None:
        runner = SessionTurnRunnerFixture()
        runner.cancel = True
        service = self.service.bind_runner(runner)

        with self.assertRaises(asyncio.CancelledError):
            await service.run_turn(RunTurnRequest("continue"))

    async def test_bound_turn_service_rejects_noncanonical_request(self) -> None:
        runner = SessionTurnRunnerFixture()
        service = self.service.bind_runner(runner)

        with self.assertRaises(ValueError):
            await service.run_turn(cast(RunTurnRequest, object()))
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
