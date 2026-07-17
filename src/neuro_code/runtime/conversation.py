from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from pathlib import Path

from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.messages import SessionItem
from neuro_code.errors import ConfigurationError
from neuro_code.ports.storage import SessionStore
from neuro_code.runtime.agent import AgentRunResult, AgentRuntime, EventSink
from neuro_code.workspace import workspaces_match


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
        resume_id: str | None = None,
    ) -> AgentConversation:
        if resume_id is None:
            return cls(runtime=runtime, store=store)

        summary = await store.get_session(resume_id)
        if not workspaces_match(summary.cwd, cwd):
            raise ConfigurationError(
                f"session workspace is {summary.cwd}, not the requested cwd {cwd}"
            )
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

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult:
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
            await self._reload_provider_origin()
            return result

    async def _reload_persisted_state(self) -> None:
        if self._session_id is None:
            return
        self._items = tuple(await self._store.load_session_items(self._session_id))
        await self._reload_provider_origin()

    async def _reload_provider_origin(self) -> None:
        if self._session_id is None:
            return
        summary = await self._store.get_session(self._session_id)
        self._source_provider = summary.provider
        self._source_model = summary.model
        self._source_context_affinity = summary.context_affinity


__all__ = ["AgentConversation"]
