"""Canonical session-storage port.

定义规范的会话存储端口."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from neuro_code.domain.background_tasks.models import BackgroundWakeState
from neuro_code.domain.conversation.compaction import DurableCompactionItem
from neuro_code.domain.conversation.events import AgentEvent
from neuro_code.domain.conversation.messages import Message, SessionItem
from neuro_code.domain.execution import (
    SessionExecutionRecord,
    TurnRecoveryAttempt,
    TurnRecoveryFact,
)
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.session_tasks import SessionTask, SubagentLink
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary
from neuro_code.domain.sessions.search import SessionSearchPage


class SessionStore(Protocol):
    async def initialize(self) -> None: ...

    async def peek_session_sandbox_profile(
        self,
        session_id: str,
    ) -> SandboxProfile | None: ...

    async def create_session(
        self,
        cwd: str,
        provider: str,
        model: str,
        context_affinity: str | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    ) -> str: ...

    async def import_session(self, snapshot: SessionSnapshot) -> str: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def fork_session(self, session_id: str) -> str: ...

    async def append_event(self, session_id: str, event: AgentEvent) -> None: ...

    async def update_session_provider(
        self,
        session_id: str,
        provider: str,
        model: str,
        context_affinity: str | None,
    ) -> None: ...

    async def update_session_title(
        self,
        session_id: str,
        title: str,
    ) -> SessionSummary: ...

    async def save_messages(self, session_id: str, messages: Sequence[Message]) -> None: ...

    async def save_session_items(self, session_id: str, items: Sequence[SessionItem]) -> None: ...

    async def start_turn_attempt(self, attempt: TurnRecoveryAttempt) -> None: ...

    async def append_turn_recovery_fact(
        self,
        session_id: str,
        turn_id: str,
        event: AgentEvent,
        fact: TurnRecoveryFact,
    ) -> None: ...

    async def finalize_turn(
        self,
        session_id: str,
        event: AgentEvent,
        items: Sequence[SessionItem],
        record: SessionExecutionRecord | None,
        turn_id: str | None = None,
        task: SessionTask | None = None,
        task_event: AgentEvent | None = None,
    ) -> None: ...

    async def finalize_turn_with_compaction(
        self,
        session_id: str,
        event: AgentEvent,
        items: Sequence[SessionItem],
        record: SessionExecutionRecord | None,
        compaction_item: DurableCompactionItem,
        turn_id: str | None = None,
        task: SessionTask | None = None,
        task_event: AgentEvent | None = None,
    ) -> None: ...

    async def finalize_turn_failure(
        self,
        session_id: str,
        turn_id: str | None,
        event: AgentEvent,
        items: Sequence[SessionItem],
        *,
        resolution: str,
        task: SessionTask | None = None,
        task_event: AgentEvent | None = None,
    ) -> None: ...

    async def abandon_turn_attempt(
        self,
        session_id: str,
        turn_id: str,
        event: AgentEvent,
        reason: str,
    ) -> None: ...

    async def load_turn_attempts(self, session_id: str) -> list[TurnRecoveryAttempt]: ...

    async def load_open_turn_attempts(self, session_id: str) -> list[TurnRecoveryAttempt]: ...

    async def save_session_plan(self, session_id: str, plan: SessionPlan | None) -> None: ...

    async def save_execution_record(
        self,
        session_id: str,
        record: SessionExecutionRecord,
    ) -> None: ...

    async def save_compaction_item(
        self,
        session_id: str,
        item: DurableCompactionItem,
    ) -> None: ...

    async def load_compaction_items(
        self,
        session_id: str,
    ) -> list[DurableCompactionItem]: ...

    async def load_messages(self, session_id: str) -> list[Message]: ...

    async def load_session_items(self, session_id: str) -> list[SessionItem]: ...

    async def load_session_plan(self, session_id: str) -> SessionPlan | None: ...

    async def load_execution_record(self, session_id: str) -> SessionExecutionRecord | None: ...

    async def load_execution_records(
        self,
        session_ids: Sequence[str],
    ) -> tuple[SessionExecutionRecord | None, ...]: ...

    async def save_background_wake_state(
        self,
        session_id: str,
        state: BackgroundWakeState,
    ) -> None: ...

    async def load_background_wake_state(self, session_id: str) -> BackgroundWakeState: ...

    async def add_plan_comment(
        self,
        session_id: str,
        plan: SessionPlan,
        comment: PlanComment,
    ) -> None: ...

    async def list_plan_comments(
        self,
        session_id: str,
        plan: SessionPlan,
    ) -> list[PlanComment]: ...

    async def create_session_task(self, session_id: str, task: SessionTask) -> None: ...

    async def start_session_task(
        self,
        session_id: str,
        task_id: str,
        started_at: datetime,
    ) -> SessionTask: ...

    async def update_session_task(self, session_id: str, task: SessionTask) -> None: ...

    async def list_session_tasks(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[SessionTask]: ...

    async def get_session_task(self, session_id: str, task_id: str) -> SessionTask | None: ...

    async def save_subagent_link(self, link: SubagentLink) -> None: ...

    async def load_subagent_link(
        self,
        parent_session_id: str,
        parent_task_id: str,
    ) -> SubagentLink | None: ...

    async def list_subagent_links(
        self,
        parent_session_id: str,
        *,
        limit: int = 50,
    ) -> list[SubagentLink]: ...

    async def load_events(self, session_id: str) -> list[dict[str, Any]]: ...

    async def bind_session_alias(
        self,
        namespace: str,
        external_id: str,
        session_id: str,
    ) -> None: ...

    async def resolve_session_alias(
        self,
        namespace: str,
        external_id: str,
    ) -> str: ...

    async def get_or_create_session_alias(
        self,
        namespace: str,
        session_id: str,
        proposed_external_id: str,
    ) -> str: ...

    async def next_event_sequence(self, session_id: str) -> int: ...

    async def list_sessions(self, *, limit: int = 50) -> list[SessionSummary]: ...

    async def list_sessions_page(
        self,
        *,
        limit: int,
        before_updated_at: datetime | None = None,
        before_id: str | None = None,
    ) -> list[SessionSummary]: ...

    async def search_sessions(
        self,
        query: str,
        *,
        cwd: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_content: bool = False,
    ) -> SessionSearchPage: ...

    async def get_session(self, session_id: str) -> SessionSummary: ...
