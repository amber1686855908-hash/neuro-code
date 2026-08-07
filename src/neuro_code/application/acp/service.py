"""ACP-specific application orchestration over narrow ports.

基于精简端口执行 ACP 专用的应用编排."""

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
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
    Tool,
    ToolOutputArtifactRead,
)
from neuro_code.application.sessions.catalog import ListSessionsPageRequest
from neuro_code.application.sessions.lifecycle import (
    DeleteSessionRequest,
    ForkSessionRequest,
    SessionLifecycleController,
)
from neuro_code.application.sessions.service import (
    BindSessionAliasRequest,
    GetOrCreateSessionAliasRequest,
    ResolveSessionAliasRequest,
    SessionApplicationService,
)
from neuro_code.application.sessions.summary import (
    GetSessionSummaryRequest,
    SessionSummaryQueryController,
    SessionSummaryQueryService,
)
from neuro_code.application.sessions.turns import SessionTurnRunner, SessionTurnService
from neuro_code.application.tools.service import (
    MAX_SESSION_TOOL_OUTPUT_ARTIFACTS,
    ListSessionToolOutputArtifactsRequest,
    ReadSessionToolOutputArtifactRequest,
    SessionToolOutputArtifact,
    SessionToolOutputArtifactApplicationService,
)
from neuro_code.domain.sessions import SessionSummary
from neuro_code.shared.errors import SessionError


class AcpApplicationService:
    """Operations ACP needs without exposing config, storage, or composition.

    提供 ACP 所需操作,但不暴露配置,存储或组合根."""

    def __init__(
        self,
        *,
        metadata: AcpSessionMetadata,
        store: SessionStore,
        bindings: AcpBindingFactory,
        mcp_tools: AcpMcpToolFactory,
        workspace: AcpWorkspaceValidator,
        sessions: SessionApplicationService | None = None,
        summary_queries: SessionSummaryQueryController | None = None,
        lifecycle: SessionLifecycleController | None = None,
        artifacts: SessionToolOutputArtifactApplicationService | None = None,
    ) -> None:
        self._metadata = metadata
        self._store = store
        self._bindings = bindings
        self._mcp_tools = mcp_tools
        self._workspace = workspace
        self._sessions = sessions or SessionApplicationService(store)
        self._summary_queries = summary_queries or (sessions or SessionSummaryQueryService(store))
        self._lifecycle: SessionLifecycleController = lifecycle or self._sessions
        self._artifacts = artifacts

    @property
    def protected_environment_variables(self) -> frozenset[str]:
        return self._metadata.protected_environment_variables

    @property
    def context_window_tokens(self) -> int | None:
        return self._metadata.context_window_tokens

    @property
    def tool_output_artifacts_available(self) -> bool:
        """Whether the composition supplied the bounded artifact read seam.

        当前组合是否提供了有界 artifact 读取接缝.
        """

        return self._artifacts is not None

    async def validate_workspace(
        self,
        cwd: str,
        additional_directories: Sequence[str] = (),
    ) -> tuple[Path, ...]:
        return await self._workspace.validate(cwd, additional_directories)

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
        additional_workspace_roots: Sequence[Path],
        client_file_system: ClientFileSystem | None,
        client_terminal: ClientTerminal | None,
    ) -> AcpBinding:
        return await self._bindings.create_binding(
            approver=approver,
            additional_tools=additional_tools,
            additional_workspace_roots=additional_workspace_roots,
            client_file_system=client_file_system,
            client_terminal=client_terminal,
        )

    async def prepare_session_resume(self, session_id: str) -> AcpPreparedSession:
        return await self._bindings.prepare_session_resume(session_id)

    def bind_runner(self, runner: SessionTurnRunner) -> SessionTurnService:
        """Bind an ACP conversation runner to the shared turn application seam.

        将 ACP 会话运行器绑定到共享的回合应用接缝."""

        return self._sessions.bind_runner(runner)

    async def delete_session(self, session_id: str) -> None:
        await self._require_current_workspace_session(session_id)
        await self._lifecycle.delete_session(DeleteSessionRequest(session_id))

    async def fork_session(self, session_id: str) -> str:
        await self._require_current_workspace_session(session_id)
        return await self._lifecycle.fork_session(ForkSessionRequest(session_id))

    async def bind_session_alias(
        self,
        namespace: str,
        external_id: str,
        session_id: str,
    ) -> None:
        await self._sessions.bind_session_alias(
            BindSessionAliasRequest(namespace, external_id, session_id)
        )

    async def resolve_session_alias(self, namespace: str, external_id: str) -> str:
        return await self._sessions.resolve_session_alias(
            ResolveSessionAliasRequest(namespace, external_id)
        )

    async def get_or_create_session_alias(
        self,
        namespace: str,
        session_id: str,
        proposed_external_id: str,
    ) -> str:
        return await self._sessions.get_or_create_session_alias(
            GetOrCreateSessionAliasRequest(
                namespace,
                session_id,
                proposed_external_id,
            )
        )

    async def list_sessions_page(
        self,
        *,
        limit: int,
        before_updated_at: datetime | None,
        before_id: str | None,
    ) -> list[SessionSummary]:
        return list(
            await self._sessions.list_sessions_page(
                ListSessionsPageRequest(
                    limit,
                    before_updated_at=before_updated_at,
                    before_id=before_id,
                )
            )
        )

    async def list_tool_output_artifacts(
        self,
        session_id: str,
        *,
        limit: int = MAX_SESSION_TOOL_OUTPUT_ARTIFACTS,
    ) -> tuple[SessionToolOutputArtifact, ...]:
        """List only artifacts associated with a current-workspace session.

        仅列出属于当前工作区会话的关联 artifact.
        """

        await self._require_current_workspace_session(session_id)
        artifacts = self._artifacts
        if artifacts is None:
            raise SessionError("tool output artifact service is unavailable")
        return await artifacts.list(ListSessionToolOutputArtifactsRequest(session_id, limit=limit))

    async def read_tool_output_artifact(
        self,
        session_id: str,
        artifact_id: str,
        *,
        max_bytes: int = MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
    ) -> ToolOutputArtifactRead:
        """Read a bounded, redacted artifact associated with one session.

        读取与单个会话关联的有界且已脱敏 artifact.
        """

        await self._require_current_workspace_session(session_id)
        artifacts = self._artifacts
        if artifacts is None:
            raise SessionError("tool output artifact service is unavailable")
        return await artifacts.read(
            ReadSessionToolOutputArtifactRequest(
                session_id,
                artifact_id,
                max_bytes=max_bytes,
            )
        )

    async def _require_current_workspace_session(self, session_id: str) -> SessionSummary:
        summary = await self._summary_queries.get_session_summary(
            GetSessionSummaryRequest(session_id)
        )
        if not self.is_current_workspace(summary.cwd):
            raise SessionError(f"unknown session: {session_id}")
        return summary

    def explicit_redactions(self) -> tuple[str, ...]:
        protected = {name.casefold() for name in self._metadata.protected_environment_variables}
        return tuple(
            dict.fromkeys(
                value
                for name, value in os.environ.items()
                if name.casefold() in protected and value
            )
        )
