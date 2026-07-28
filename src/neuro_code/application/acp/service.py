"""ACP-specific application orchestration over narrow ports."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from neuro_code.application.acp.contracts import (
    AcpBinding,
    AcpBindingFactory,
    AcpMcpServerConfig,
    AcpMcpToolFactory,
    AcpMcpTools,
    AcpPreparedSession,
    AcpSessionMetadata,
    AcpWorkspaceValidator,
)
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import Tool
from neuro_code.domain.sessions import SessionSummary


class AcpApplicationService:
    """Operations ACP needs without exposing config, storage, or composition."""

    def __init__(
        self,
        *,
        metadata: AcpSessionMetadata,
        store: SessionStore,
        bindings: AcpBindingFactory,
        mcp_tools: AcpMcpToolFactory,
        workspace: AcpWorkspaceValidator,
    ) -> None:
        self._metadata = metadata
        self._store = store
        self._bindings = bindings
        self._mcp_tools = mcp_tools
        self._workspace = workspace

    @property
    def protected_environment_variables(self) -> frozenset[str]:
        return self._metadata.protected_environment_variables

    @property
    def context_window_tokens(self) -> int | None:
        return self._metadata.context_window_tokens

    async def validate_workspace(self, cwd: str) -> None:
        await self._workspace.validate(cwd)

    def is_current_workspace(self, cwd: str) -> bool:
        try:
            recorded = Path(cwd)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return recorded.is_absolute() and self._workspace.matches(recorded)

    async def open_mcp_tools(
        self,
        configurations: Sequence[AcpMcpServerConfig],
    ) -> AcpMcpTools:
        return await self._mcp_tools.open(
            configurations,
            cwd=self._metadata.workspace,
            explicit_redactions=self.explicit_redactions(),
        )

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None,
        additional_tools: Sequence[Tool],
    ) -> AcpBinding:
        return await self._bindings.create_binding(
            approver=approver,
            additional_tools=additional_tools,
        )

    async def prepare_session_resume(self, session_id: str) -> AcpPreparedSession:
        return await self._bindings.prepare_session_resume(session_id)

    async def bind_session_alias(
        self,
        namespace: str,
        external_id: str,
        session_id: str,
    ) -> None:
        await self._store.bind_session_alias(namespace, external_id, session_id)

    async def resolve_session_alias(self, namespace: str, external_id: str) -> str:
        return await self._store.resolve_session_alias(namespace, external_id)

    async def get_or_create_session_alias(
        self,
        namespace: str,
        session_id: str,
        proposed_external_id: str,
    ) -> str:
        return await self._store.get_or_create_session_alias(
            namespace,
            session_id,
            proposed_external_id,
        )

    async def list_sessions_page(
        self,
        *,
        limit: int,
        before_updated_at: datetime | None,
        before_id: str | None,
    ) -> list[SessionSummary]:
        return await self._store.list_sessions_page(
            limit=limit,
            before_updated_at=before_updated_at,
            before_id=before_id,
        )

    def explicit_redactions(self) -> tuple[str, ...]:
        protected = {name.casefold() for name in self._metadata.protected_environment_variables}
        return tuple(
            dict.fromkeys(
                value
                for name, value in os.environ.items()
                if name.casefold() in protected and value
            )
        )
