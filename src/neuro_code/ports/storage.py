from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from neuro_code.domain.events import AgentEvent
from neuro_code.domain.messages import Message, SessionItem
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_search import SessionSearchPage
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary


class SessionStore(Protocol):
    async def create_session(
        self,
        cwd: str,
        provider: str,
        model: str,
        context_affinity: str | None = None,
        sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    ) -> str: ...

    async def import_session(self, snapshot: SessionSnapshot) -> str: ...

    async def append_event(self, session_id: str, event: AgentEvent) -> None: ...

    async def update_session_provider(
        self,
        session_id: str,
        provider: str,
        model: str,
        context_affinity: str | None,
    ) -> None: ...

    async def save_messages(self, session_id: str, messages: Sequence[Message]) -> None: ...

    async def save_session_items(self, session_id: str, items: Sequence[SessionItem]) -> None: ...

    async def load_messages(self, session_id: str) -> list[Message]: ...

    async def load_session_items(self, session_id: str) -> list[SessionItem]: ...

    async def next_event_sequence(self, session_id: str) -> int: ...

    async def list_sessions(self, *, limit: int = 50) -> list[SessionSummary]: ...

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
