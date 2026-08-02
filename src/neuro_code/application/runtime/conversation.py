from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.workspace import WorkspaceIdentity
from neuro_code.application.runtime.agent import AgentRunResult, AgentRuntime, EventSink
from neuro_code.domain.background_tasks import BackgroundWakeState
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.execution import SessionExecutionRecord, TurnCancellationPolicy, TurnSource
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import ContentPart, SessionItem
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.session_tasks import (
    MAX_QUEUED_SESSION_TASKS,
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
)
from neuro_code.shared.errors import ConfigurationError

PLAN_EXECUTION_PROMPT = (
    "The user has approved the current structured plan for execution. "
    "Continue from the in-progress step, or the first pending step. "
    "Keep the plan current with update_plan as work changes. "
    "Use tools only as needed and obey all permission, workspace, and sandbox boundaries."
)


class AgentConversation:
    """Own the durable context needed to run multiple turns in one session."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        store: SessionStore,
        items: Sequence[SessionItem] = (),
        session_id: str | None = None,
        source_provider: str | None = None,
        source_model: str | None = None,
        source_context_affinity: str | None = None,
        execution_record: SessionExecutionRecord | None = None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._items = tuple(items)
        self._session_id = session_id
        self._source_provider = source_provider
        self._source_model = source_model
        self._source_context_affinity = source_context_affinity
        self._execution_record = execution_record
        self._turn_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        *,
        runtime: AgentRuntime,
        store: SessionStore,
        cwd: Path,
        workspace_identity: WorkspaceIdentity,
        resume_id: str | None = None,
    ) -> AgentConversation:
        if resume_id is None:
            return cls(runtime=runtime, store=store)

        summary = await store.get_session(resume_id)
        if not workspace_identity.matches(summary.cwd, cwd):
            raise ConfigurationError(
                f"session workspace is {summary.cwd}, not the requested cwd {cwd}"
            )
        if (
            summary.sandbox_profile is not None
            and summary.sandbox_profile is not runtime.sandbox_profile
        ):
            raise ConfigurationError(
                f"session sandbox profile is {summary.sandbox_profile.value!r}, "
                f"not the active profile {runtime.sandbox_profile.value!r}"
            )
        plan = await store.load_session_plan(resume_id)
        runtime.set_plan(plan)
        if plan is not None:
            runtime.set_plan_comments(await store.list_plan_comments(resume_id, plan))
        return cls(
            runtime=runtime,
            store=store,
            items=await store.load_session_items(resume_id),
            session_id=resume_id,
            source_provider=summary.provider,
            source_model=summary.model,
            source_context_affinity=summary.context_affinity,
            execution_record=await store.load_execution_record(resume_id),
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def items(self) -> tuple[SessionItem, ...]:
        return self._items

    @property
    def plan(self) -> SessionPlan | None:
        return self._runtime.plan

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]:
        return self._runtime.plan_comments

    @property
    def execution_record(self) -> SessionExecutionRecord | None:
        """Return the latest safe terminal execution state restored for this session."""

        return self._execution_record

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment:
        """Persist user feedback for one current-plan step without starting a turn."""

        if self.plan is None or self._session_id is None:
            raise ConfigurationError("cannot comment on a plan that has not been saved")
        async with self._turn_lock:
            plan = self.plan
            if plan is None or self._session_id is None:
                raise ConfigurationError("cannot comment on a plan that has not been saved")
            comment = PlanComment(
                f"plan-comment-{uuid.uuid4().hex}",
                step_index,
                content,
                datetime.now(UTC),
            )
            if comment.step_index > len(plan.steps):
                raise ConfigurationError("plan comment refers to an unknown step")
            await self._store.add_plan_comment(self._session_id, plan, comment)
            self._runtime.set_plan_comments((*self.plan_comments, comment))
            return comment

    async def list_plan_comments(self) -> tuple[PlanComment, ...]:
        if self.plan is None or self._session_id is None:
            return ()
        return tuple(await self._store.list_plan_comments(self._session_id, self.plan))

    async def list_session_tasks(self) -> tuple[SessionTask, ...]:
        if self._session_id is None:
            return ()
        return tuple(await self._store.list_session_tasks(self._session_id))

    async def get_session_task(self, task_id: str) -> SessionTask | None:
        """Read one durable task from this conversation without starting a turn."""

        if self._session_id is None:
            return None
        return await self._store.get_session_task(self._session_id, task_id)

    async def schedule_plan(self) -> SessionTask:
        """Persist a bounded plan task without starting a model turn."""

        if self.plan is None:
            raise ConfigurationError("cannot schedule a plan that has not been saved")
        if self._session_id is None:
            raise ConfigurationError("a session is required before scheduling a plan")
        async with self._turn_lock:
            plan = self.plan
            if plan is None or self._session_id is None:
                raise ConfigurationError("cannot schedule a plan that has not been saved")
            tasks = await self._store.list_session_tasks(self._session_id)
            queued_count = sum(
                task.status is SessionTaskStatus.QUEUED
                for task in tasks
                if task.kind is SessionTaskKind.PLAN_EXECUTION
            )
            if queued_count >= MAX_QUEUED_SESSION_TASKS:
                raise ConfigurationError(
                    f"at most {MAX_QUEUED_SESSION_TASKS} plan tasks may be queued"
                )
            task = SessionTask(
                f"task-{uuid.uuid4().hex}",
                SessionTaskKind.PLAN_EXECUTION,
                SessionTaskStatus.QUEUED,
                datetime.now(UTC),
                plan_snapshot=plan,
            )
            await self._store.create_session_task(self._session_id, task)
            return task

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._runtime.reasoning_effort

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        self._runtime.set_reasoning_effort(effort)

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._runtime.interaction_mode

    @property
    def auto_mode_unrestricted(self) -> bool:
        return self._runtime.auto_mode_unrestricted

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        self._runtime.set_interaction_mode(mode)

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: Sequence[ContentPart] = (),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
    ) -> AgentRunResult:
        async with self._turn_lock:

            async def capture_session(event: AgentEvent) -> None:
                if event.kind is AgentEventKind.SESSION_STARTED:
                    session_id = event.data.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        self._session_id = session_id
                if sink is not None:
                    outcome = sink(event)
                    if inspect.isawaitable(outcome):
                        await outcome

            try:
                result = await self._runtime.run(
                    prompt,
                    sink=capture_session,
                    content_parts=content_parts,
                    initial_items=self._items,
                    source_provider=self._source_provider,
                    source_model=self._source_model,
                    source_context_affinity=self._source_context_affinity,
                    session_id=self._session_id,
                    cancellation_policy=cancellation_policy,
                    turn_source=turn_source,
                )
            except asyncio.CancelledError:
                await self._reload_persisted_state()
                raise
            except Exception:
                await self._reload_persisted_state()
                raise
            self._items = result.items
            self._session_id = result.session_id
            await self._reload_plan_state()
            await self._reload_provider_origin()
            await self._reload_execution_record()
            return result

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult:
        """Run one model turn for pending background completions without a user prompt."""

        return await self.run(
            "",
            sink=sink,
            turn_source=TurnSource.BACKGROUND_TASK_AUTO_WAKE,
        )

    async def load_background_wake_state(self) -> BackgroundWakeState:
        """Load the bounded wake ledger for the current durable session."""

        if self._session_id is None:
            return BackgroundWakeState()
        return await self._store.load_background_wake_state(self._session_id)

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None:
        """Persist the bounded wake ledger without retaining task output."""

        if self._session_id is None:
            return
        await self._store.save_background_wake_state(self._session_id, state)

    async def execute_plan(
        self,
        *,
        sink: EventSink | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult:
        """Record an explicit user handoff from a saved plan to one agent turn."""

        if self.plan is None:
            raise ConfigurationError("cannot execute a plan that has not been saved")
        async with self._turn_lock:
            if task_id is not None:
                if self._session_id is None:
                    raise ConfigurationError("a session is required before running a plan task")
                task = await self._store.get_session_task(self._session_id, task_id)
                if task is None:
                    raise ConfigurationError(f"unknown queued plan task: {task_id}")
                if task.kind is not SessionTaskKind.PLAN_EXECUTION:
                    raise ConfigurationError("only plan execution tasks can be started")
                if task.status is not SessionTaskStatus.QUEUED:
                    raise ConfigurationError(f"plan task {task_id} is not queued")
                if task.plan_snapshot is None:
                    raise ConfigurationError(f"plan task {task_id} has no saved plan snapshot")
                self._runtime.set_plan(task.plan_snapshot)

            async def capture_session(event: AgentEvent) -> None:
                if event.kind is AgentEventKind.SESSION_STARTED:
                    session_id = event.data.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        self._session_id = session_id
                if sink is not None:
                    outcome = sink(event)
                    if inspect.isawaitable(outcome):
                        await outcome

            try:
                result = await self._runtime.run(
                    PLAN_EXECUTION_PROMPT,
                    sink=capture_session,
                    plan_execution_requested=True,
                    plan_execution_task_id=task_id,
                    initial_items=self._items,
                    source_provider=self._source_provider,
                    source_model=self._source_model,
                    source_context_affinity=self._source_context_affinity,
                    session_id=self._session_id,
                )
            except asyncio.CancelledError:
                await self._reload_persisted_state()
                raise
            except Exception:
                await self._reload_persisted_state()
                raise
            self._items = result.items
            self._session_id = result.session_id
            await self._reload_plan_state()
            await self._reload_provider_origin()
            await self._reload_execution_record()
            return result

    async def run_session_task(
        self,
        task_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        """Start one queued plan task after explicit user selection."""

        return await self.execute_plan(sink=sink, task_id=task_id)

    async def _reload_persisted_state(self) -> None:
        if self._session_id is None:
            return
        self._items = tuple(await self._store.load_session_items(self._session_id))
        await self._reload_plan_state()
        await self._reload_provider_origin()
        await self._reload_execution_record()

    async def _reload_plan_state(self) -> None:
        if self._session_id is None:
            self._runtime.set_plan(None)
            return
        plan = await self._store.load_session_plan(self._session_id)
        self._runtime.set_plan(plan)
        if plan is not None:
            self._runtime.set_plan_comments(
                await self._store.list_plan_comments(self._session_id, plan)
            )

    async def _reload_provider_origin(self) -> None:
        if self._session_id is None:
            return
        summary = await self._store.get_session(self._session_id)
        self._source_provider = summary.provider
        self._source_model = summary.model
        self._source_context_affinity = summary.context_affinity

    async def _reload_execution_record(self) -> None:
        if self._session_id is None:
            self._execution_record = None
            return
        self._execution_record = await self._store.load_execution_record(self._session_id)


__all__ = ["PLAN_EXECUTION_PROMPT", "AgentConversation"]
