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
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import ContentPart, SessionItem
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.session_tasks import SessionTask
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
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._items = tuple(items)
        self._session_id = session_id
        self._source_provider = source_provider
        self._source_model = source_model
        self._source_context_affinity = source_context_affinity
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
            return result

    async def execute_plan(self, *, sink: EventSink | None = None) -> AgentRunResult:
        """Record an explicit user handoff from a saved plan to one agent turn."""

        if self.plan is None:
            raise ConfigurationError("cannot execute a plan that has not been saved")
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
                    PLAN_EXECUTION_PROMPT,
                    sink=capture_session,
                    plan_execution_requested=True,
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
            return result

    async def _reload_persisted_state(self) -> None:
        if self._session_id is None:
            return
        self._items = tuple(await self._store.load_session_items(self._session_id))
        await self._reload_plan_state()
        await self._reload_provider_origin()

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


__all__ = ["PLAN_EXECUTION_PROMPT", "AgentConversation"]
