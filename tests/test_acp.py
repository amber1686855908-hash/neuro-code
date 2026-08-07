from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from acp.exceptions import RequestError
from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    AudioContentBlock,
    BlobResourceContents,
    ClientCapabilities,
    CreateTerminalResponse,
    DeniedOutcome,
    EmbeddedResourceContentBlock,
    EnvVariable,
    FileSystemCapabilities,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    PermissionOption,
    ReadTextFileResponse,
    RequestPermissionResponse,
    ResourceContentBlock,
    SseMcpServer,
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    TextResourceContents,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

import neuro_code.acp as acp_module
from neuro_code.acp import (
    ACP_STDIO_BUFFER_LIMIT_BYTES,
    ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION,
    MAX_ANNOTATION_AUDIENCE,
    MAX_EMBEDDED_TEXT_RESOURCE_BYTES,
    MAX_EMBEDDED_TEXT_RESOURCES,
    MAX_IMAGE_BLOCKS,
    MAX_PROMPT_BLOCKS,
    MAX_PROMPT_BYTES,
    MAX_RESOURCE_LINKS,
    MAX_TEXT_BLOCK_BYTES,
    MAX_TEXT_BLOCKS,
    NeuroCodeAcpAgent,
    convert_prompt_content,
    serve_acp,
)
from neuro_code.application.acp.service import AcpApplicationService
from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import ToolOutputArtifact, ToolOutputArtifactRead
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions import (
    BindSessionAliasRequest,
    DeleteSessionRequest,
    ForkSessionRequest,
    GetOrCreateSessionAliasRequest,
    GetSessionSummaryRequest,
    ListSessionsPageRequest,
    ResolveSessionAliasRequest,
    SessionApplicationService,
    SessionTurnRunner,
    SessionTurnService,
)
from neuro_code.application.sessions.profile_conversation import ConversationBinding
from neuro_code.application.tools import (
    ListSessionToolOutputArtifactsRequest,
    ReadSessionToolOutputArtifactRequest,
    SessionToolOutputArtifact,
    SessionToolOutputArtifactApplicationService,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.bootstrap.entrypoints import BootstrapCliServices
from neuro_code.config import AppConfig, ProviderProfile
from neuro_code.domain.background_tasks import BackgroundTaskStatus, BackgroundTaskWaitMode
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SupervisorReasonCode,
    TurnCancellationPolicy,
    TurnSource,
)
from neuro_code.domain.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    ToolCall,
)
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelEvent
from neuro_code.domain.plans import SessionPlan
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.interfaces.acp.serialization import (
    execution_outcome_metadata,
    execution_outcome_stop_reason,
    map_stop_reason,
    safe_output_text,
    sanitize_controls,
    serialized_size_bytes,
    truncate_utf8,
)
from neuro_code.shared.errors import ConfigurationError, ProviderError, SessionError, ToolError


class AcpClientFixture:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.permission_requests: list[tuple[str, ToolCallUpdate, list[PermissionOption]]] = []
        self.permission_response = RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id="allow_once")
        )
        self.permission_error: Exception | None = None
        self.read_text_file_content = "client file contents"
        self.read_text_file_requests: list[tuple[str, str, int | None, int | None]] = []
        self.write_text_file_requests: list[tuple[str, str, str]] = []
        self.read_text_file_error: Exception | None = None
        self.write_text_file_error: Exception | None = None
        self.terminal_id = "client-terminal"
        self.create_terminal_requests: list[
            tuple[str, str, list[str] | None, str | None, int | None]
        ] = []
        self.terminal_output_requests: list[tuple[str, str]] = []
        self.terminal_wait_requests: list[tuple[str, str]] = []
        self.terminal_kill_requests: list[tuple[str, str]] = []
        self.terminal_release_requests: list[tuple[str, str]] = []
        self.terminal_envs: list[list[EnvVariable] | None] = []
        self.terminal_output_response = TerminalOutputResponse(
            output="client terminal output",
            truncated=False,
            exit_status=TerminalExitStatus(exit_code=0),
        )
        self.terminal_wait = WaitForTerminalExitResponse(exit_code=0)
        self.create_terminal_error: Exception | None = None
        self.terminal_output_error: Exception | None = None
        self.terminal_wait_error: Exception | None = None
        self.terminal_wait_gate: asyncio.Event | None = None
        self.terminal_wait_started = asyncio.Event()
        self.terminal_kill_error: Exception | None = None
        self.terminal_release_error: Exception | None = None

    async def session_update(
        self,
        session_id: str,
        update: object,
        **_kwargs: Any,
    ) -> None:
        self.updates.append((session_id, update))

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **_kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permission_requests.append((session_id, tool_call, options))
        if self.permission_error is not None:
            raise self.permission_error
        return self.permission_response

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **_kwargs: Any,
    ) -> ReadTextFileResponse:
        self.read_text_file_requests.append((session_id, path, line, limit))
        if self.read_text_file_error is not None:
            raise self.read_text_file_error
        return ReadTextFileResponse(content=self.read_text_file_content)

    async def write_text_file(
        self,
        session_id: str,
        path: str,
        content: str,
        **_kwargs: Any,
    ) -> WriteTextFileResponse:
        self.write_text_file_requests.append((session_id, path, content))
        if self.write_text_file_error is not None:
            raise self.write_text_file_error
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **_kwargs: Any,
    ) -> CreateTerminalResponse:
        self.create_terminal_requests.append((session_id, command, args, cwd, output_byte_limit))
        self.terminal_envs.append(env)
        if self.create_terminal_error is not None:
            raise self.create_terminal_error
        return CreateTerminalResponse(terminal_id=self.terminal_id)

    async def terminal_output(
        self,
        session_id: str,
        terminal_id: str,
        **_kwargs: Any,
    ) -> TerminalOutputResponse:
        self.terminal_output_requests.append((session_id, terminal_id))
        if self.terminal_output_error is not None:
            raise self.terminal_output_error
        return self.terminal_output_response

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **_kwargs: Any,
    ) -> WaitForTerminalExitResponse:
        self.terminal_wait_requests.append((session_id, terminal_id))
        self.terminal_wait_started.set()
        if self.terminal_wait_error is not None:
            raise self.terminal_wait_error
        if self.terminal_wait_gate is not None:
            await self.terminal_wait_gate.wait()
        return self.terminal_wait

    async def kill_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **_kwargs: Any,
    ) -> None:
        self.terminal_kill_requests.append((session_id, terminal_id))
        if self.terminal_kill_error is not None:
            raise self.terminal_kill_error

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **_kwargs: Any,
    ) -> None:
        self.terminal_release_requests.append((session_id, terminal_id))
        if self.terminal_release_error is not None:
            raise self.terminal_release_error


class ProviderFixture:
    provider_name = "fixture"
    model_name = "fixture-model"

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        if False:
            yield


class BackgroundTasksFixture:
    def __init__(self, cleanup_events: list[str] | None = None) -> None:
        self.shutdown_calls = 0
        self._cleanup_events = cleanup_events

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._cleanup_events is not None:
            self._cleanup_events.append("binding")


class McpToolFixture:
    definition = ToolDefinition(
        "remote_echo",
        "Fixture MCP tool",
        {"type": "object", "properties": {}},
    )
    side_effecting = True

    async def execute(self, arguments: object, context: object) -> ToolResult:
        del arguments, context
        return ToolResult("ok")


class McpCollectionFixture:
    def __init__(self, cleanup_events: list[str] | None = None) -> None:
        self.tools = (McpToolFixture(),)
        self.close_calls = 0
        self._cleanup_events = cleanup_events

    async def close(self) -> None:
        self.close_calls += 1
        if self._cleanup_events is not None:
            self._cleanup_events.append("mcp")


class RunnerFixture:
    def __init__(
        self,
        *,
        events: Sequence[AgentEvent] = (),
        block: bool = False,
        request_approval: bool = False,
        approval_scope: str | None = "scope",
        failure: BaseException | None = None,
        wrap_cancellation: bool = False,
        session_id: str | None = None,
        items: Sequence[SessionItem] = (),
        outcome: AgentExecutionOutcome | None = None,
    ) -> None:
        self._session_id = session_id
        self._items = tuple(items)
        self._events = tuple(events)
        self._block = block
        self._request_approval = request_approval
        self._approval_scope = approval_scope
        self._failure = failure
        self._wrap_cancellation = wrap_cancellation
        self._outcome = outcome
        self._approver: PermissionApprover | None = None
        self._started = asyncio.Event()
        self._release = asyncio.Event()
        self.approvals: list[PermissionApproval] = []
        self.prompts: list[tuple[str, tuple[ContentPart, ...]]] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def items(self) -> tuple[SessionItem, ...]:
        return self._items

    @property
    def plan(self) -> SessionPlan | None:
        return None

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return ReasoningEffort.HIGH

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        del effort

    @property
    def interaction_mode(self) -> InteractionMode:
        return InteractionMode.NORMAL

    @property
    def auto_mode_unrestricted(self) -> bool:
        return False

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        del mode

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        content_parts: Sequence[ContentPart] = (),
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
        turn_source: TurnSource = TurnSource.USER,
    ) -> AgentRunResult:
        del cancellation_policy, turn_source
        if self._session_id is None:
            self._session_id = f"internal-session-{id(self)}"
        self._started.set()
        events = list(self._events)
        if self._request_approval:
            requested = AgentEvent.create(
                1,
                AgentEventKind.TOOL_REQUESTED,
                {"id": "call-approval", "name": "bash", "arguments": {"command": "pwd"}},
            )
            events.append(requested)
        for event in events:
            if sink is not None:
                outcome = sink(event)
                if asyncio.iscoroutine(outcome):
                    await outcome
        if self._request_approval:
            assert self._approver is not None
            approval = await self._approver.request(
                PermissionRequest(
                    "call-approval",
                    "bash",
                    "Run shell command:\npwd",
                    "interactive approval required",
                    self._approval_scope,
                )
            )
            self.approvals.append(approval)
            terminal = AgentEvent.create(
                len(events) + 1,
                (AgentEventKind.TOOL_STARTED if approval.allowed else AgentEventKind.TOOL_FAILED),
                {
                    "id": "call-approval",
                    "name": "bash",
                    "content": "denied",
                },
            )
            if sink is not None:
                outcome = sink(terminal)
                if asyncio.iscoroutine(outcome):
                    await outcome
        if self._block:
            try:
                await self._release.wait()
            except asyncio.CancelledError:
                if self._wrap_cancellation:
                    raise RuntimeError("provider wrapped cancellation") from None
                raise
        if self._failure is not None:
            raise self._failure
        prompt_parts = tuple(content_parts)
        self.prompts.append((prompt, prompt_parts))
        return AgentRunResult(
            self._session_id,
            prompt,
            (*self._items, Message(Role.USER, prompt, content_parts=prompt_parts)),
            (*self._items, Message(Role.USER, prompt, content_parts=prompt_parts)),
            tuple(events),
            1,
            outcome=self._outcome,
        )

    def attach_approver(self, approver: PermissionApprover | None) -> None:
        self._approver = approver

    async def wait_started(self) -> None:
        await self._started.wait()

    def release(self) -> None:
        self._release.set()


class ArtifactServiceFixture:
    def __init__(
        self,
        reference: SessionToolOutputArtifact,
        content: str,
        session_id: str = "artifact-internal",
    ) -> None:
        self.reference = reference
        self.content = content
        self.session_id = session_id
        self.list_requests: list[ListSessionToolOutputArtifactsRequest] = []
        self.read_requests: list[ReadSessionToolOutputArtifactRequest] = []

    async def list(
        self,
        request: ListSessionToolOutputArtifactsRequest,
    ) -> tuple[SessionToolOutputArtifact, ...]:
        self.list_requests.append(request)
        return (self.reference,)

    async def read(
        self,
        request: ReadSessionToolOutputArtifactRequest,
    ) -> ToolOutputArtifactRead:
        self.read_requests.append(request)
        if (
            request.session_id != self.session_id
            or request.artifact_id != self.reference.artifact.artifact_id
        ):
            raise SessionError("tool output artifact is not associated with this session")
        return ToolOutputArtifactRead(
            self.reference.artifact,
            self.content,
            read_truncated=True,
        )


class ApplicationFixture:
    def __init__(self, root: Path, runners: Sequence[RunnerFixture]) -> None:
        profile = ProviderProfile(
            name="fixture",
            protocol="openai-chat",
            model="fixture-model",
            base_url="https://provider.invalid/v1",
            api_key_env="FIXTURE_KEY",
            context_window_tokens=32_000,
        )
        self.config = AppConfig(
            cwd=root.resolve(),
            state_dir=(root / "state").resolve(),
            providers={"fixture": profile},
            default_provider="fixture",
            selected_provider="fixture",
            sandbox_profile=SandboxProfile.OFF,
        )
        self._runners = list(runners)
        self.background_scopes: list[BackgroundTasksFixture] = []
        self.store = SessionAliasStoreFixture()
        self.session_service = SessionApplicationServiceFixture(self.store)
        self.session_summary_queries = self.session_service
        self.resume_ids: list[str | None] = []
        self.additional_tool_names: list[tuple[str, ...]] = []
        self.additional_workspace_roots: list[tuple[Path, ...]] = []
        self.client_file_systems: list[Any | None] = []
        self.client_terminals: list[Any | None] = []
        self.resume_error: ConfigurationError | None = None
        self.cleanup_events: list[str] | None = None
        self.artifact_service: SessionToolOutputArtifactApplicationService | None = None

    async def config_for_session_resume(self, session_id: str) -> AppConfig:
        del session_id
        if self.resume_error is not None:
            raise self.resume_error
        return self.config

    def create_tool_output_artifact_service(
        self,
        *,
        config: AppConfig | None = None,
    ) -> SessionToolOutputArtifactApplicationService | None:
        """Keep the ACP fixture explicit about its optional artifact seam."""

        del config
        return self.artifact_service

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None = None,
        resume_id: str | None = None,
        additional_tools: Sequence[Any] = (),
        additional_workspace_roots: Sequence[Path] = (),
        client_file_system: Any | None = None,
        client_terminal: Any | None = None,
        **_kwargs: Any,
    ) -> ConversationBinding:
        self.resume_ids.append(resume_id)
        self.additional_tool_names.append(tuple(tool.definition.name for tool in additional_tools))
        self.additional_workspace_roots.append(tuple(additional_workspace_roots))
        self.client_file_systems.append(client_file_system)
        self.client_terminals.append(client_terminal)
        runner = self._runners.pop(0)
        runner.attach_approver(approver)
        background = BackgroundTasksFixture(self.cleanup_events)
        self.background_scopes.append(background)
        return ConversationBinding(
            runner,
            cast(ModelProvider, ProviderFixture()),
            cast(Any, background),
        )


class SessionAliasStoreFixture:
    def __init__(self) -> None:
        self.aliases: dict[tuple[str, str], str] = {}
        self.session_ids: set[str] = set()
        self.summaries: list[SessionSummary] = []
        self.deleted_session_ids: list[str] = []
        self.forked_session_ids: list[tuple[str, str]] = []

    async def bind_session_alias(
        self,
        namespace: str,
        external_id: str,
        session_id: str,
    ) -> None:
        key = (namespace, external_id)
        current = self.aliases.get(key)
        if current is not None and current != session_id:
            raise SessionError("session alias is already bound")
        if any(
            saved_namespace == namespace
            and saved_session_id == session_id
            and saved_external_id != external_id
            for (saved_namespace, saved_external_id), saved_session_id in self.aliases.items()
        ):
            raise SessionError("session already has an alias in this namespace")
        self.session_ids.add(session_id)
        self.aliases[key] = session_id

    async def resolve_session_alias(self, namespace: str, external_id: str) -> str:
        resolved = self.aliases.get((namespace, external_id))
        if resolved is not None:
            return resolved
        if external_id in self.session_ids:
            return external_id
        raise SessionError("unknown session alias")

    async def get_or_create_session_alias(
        self,
        namespace: str,
        session_id: str,
        proposed_external_id: str,
    ) -> str:
        for (saved_namespace, external_id), saved_session_id in self.aliases.items():
            if saved_namespace == namespace and saved_session_id == session_id:
                return external_id
        if session_id not in self.session_ids:
            raise SessionError("unknown session")
        if (namespace, proposed_external_id) in self.aliases:
            raise SessionError("proposed session alias is unavailable")
        self.aliases[(namespace, proposed_external_id)] = session_id
        return proposed_external_id

    async def get_session(self, session_id: str) -> SessionSummary:
        for summary in self.summaries:
            if summary.id == session_id:
                return summary
        raise SessionError(f"unknown session: {session_id}")

    async def delete_session(self, session_id: str) -> None:
        await self.get_session(session_id)
        self.summaries = [summary for summary in self.summaries if summary.id != session_id]
        self.session_ids.discard(session_id)
        self.aliases = {
            key: saved_session_id
            for key, saved_session_id in self.aliases.items()
            if saved_session_id != session_id
        }
        self.deleted_session_ids.append(session_id)

    async def fork_session(self, session_id: str) -> str:
        source = await self.get_session(session_id)
        forked_session_id = f"forked-{len(self.forked_session_ids) + 1}"
        timestamp = datetime.now(UTC)
        self.summaries.append(
            SessionSummary(
                id=forked_session_id,
                cwd=source.cwd,
                provider=source.provider,
                model=source.model,
                created_at=timestamp,
                updated_at=timestamp,
                context_affinity=source.context_affinity,
                sandbox_profile=source.sandbox_profile,
                title=source.title,
            )
        )
        self.session_ids.add(forked_session_id)
        self.forked_session_ids.append((session_id, forked_session_id))
        return forked_session_id

    async def list_sessions_page(
        self,
        *,
        limit: int,
        before_updated_at: datetime | None = None,
        before_id: str | None = None,
    ) -> list[SessionSummary]:
        summaries = sorted(
            self.summaries,
            key=lambda summary: (summary.updated_at, summary.id),
            reverse=True,
        )
        if before_updated_at is not None:
            assert before_id is not None
            summaries = [
                summary
                for summary in summaries
                if (summary.updated_at, summary.id) < (before_updated_at, before_id)
            ]
        return summaries[:limit]


class SessionApplicationServiceFixture:
    def __init__(self, store: object) -> None:
        self._service = SessionApplicationService(cast(SessionStore, store))
        self.bound_runners: list[SessionTurnRunner] = []
        self.delete_requests: list[DeleteSessionRequest] = []
        self.fork_requests: list[ForkSessionRequest] = []
        self.summary_requests: list[GetSessionSummaryRequest] = []

    def bind_runner(self, runner: SessionTurnRunner) -> SessionTurnService:
        self.bound_runners.append(runner)
        return self._service.bind_runner(runner)

    async def fork_session(self, request: ForkSessionRequest) -> str:
        self.fork_requests.append(request)
        return await self._service.fork_session(request)

    async def delete_session(self, request: DeleteSessionRequest) -> None:
        self.delete_requests.append(request)
        await self._service.delete_session(request)

    async def get_session_summary(
        self,
        request: GetSessionSummaryRequest,
    ) -> SessionSummary:
        self.summary_requests.append(request)
        return await self._service.get_session_summary(request)

    async def bind_session_alias(self, request: BindSessionAliasRequest) -> None:
        await self._service.bind_session_alias(request)

    async def resolve_session_alias(self, request: ResolveSessionAliasRequest) -> str:
        return await self._service.resolve_session_alias(request)

    async def get_or_create_session_alias(
        self,
        request: GetOrCreateSessionAliasRequest,
    ) -> str:
        return await self._service.get_or_create_session_alias(request)

    async def list_sessions_page(
        self,
        request: ListSessionsPageRequest,
    ) -> tuple[SessionSummary, ...]:
        return await self._service.list_sessions_page(request)


async def initialized_agent(
    root: Path,
    runners: Sequence[RunnerFixture],
) -> tuple[NeuroCodeAcpAgent, ApplicationFixture, AcpClientFixture]:
    application = ApplicationFixture(root, runners)
    agent = NeuroCodeAcpAgent(_acp_service(application))
    client = AcpClientFixture()
    agent.on_connect(cast(Client, client))
    await agent.initialize(
        1,
        ClientCapabilities(terminal=True),
        Implementation(name="fixture-client", version="1.0"),
    )
    return agent, application, client


async def initialized_artifact_agent(
    root: Path,
) -> tuple[NeuroCodeAcpAgent, ApplicationFixture, ArtifactServiceFixture]:
    artifact_id = "a" * 32
    reference = SessionToolOutputArtifact(
        event_sequence=7,
        artifact=ToolOutputArtifact(
            artifact_id=artifact_id,
            relative_path=f"tool-output/{artifact_id}.log",
            byte_count=64,
            truncated=True,
        ),
    )
    artifact_service = ArtifactServiceFixture(reference, "redacted output\n")
    application = ApplicationFixture(root, [])
    application.artifact_service = cast(
        SessionToolOutputArtifactApplicationService,
        artifact_service,
    )
    summary = SessionSummary(
        id="artifact-internal",
        cwd=str(root),
        provider="fixture",
        model="fixture-model",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 1, tzinfo=UTC),
        title="Artifact session",
    )
    application.store.summaries.append(summary)
    application.store.session_ids.add(summary.id)
    await application.store.bind_session_alias("acp-v1", "acp-artifacts", summary.id)
    agent = NeuroCodeAcpAgent(_acp_service(application))
    client = AcpClientFixture()
    agent.on_connect(cast(Client, client))
    await agent.initialize(1, ClientCapabilities(terminal=True))
    return agent, application, artifact_service


def _acp_service(application: ApplicationFixture) -> AcpApplicationService:
    return BootstrapCliServices().create_acp_service(cast(ApplicationComposition, application))


class PromptContentTests(unittest.TestCase):
    def test_acp_outcome_projection_is_bounded_and_protocol_safe(self) -> None:
        budget_limited = AgentExecutionOutcome(
            AgentExecutionStatus.BUDGET_LIMITED,
            SupervisorReasonCode.OUTPUT_TOKEN_BUDGET,
            finalized=True,
            recoverable=True,
        )
        self.assertEqual(map_stop_reason("length"), "max_tokens")
        self.assertEqual(map_stop_reason("unexpected"), "end_turn")
        self.assertEqual(execution_outcome_stop_reason(budget_limited), "max_tokens")
        metadata = execution_outcome_metadata(budget_limited)
        self.assertEqual(
            metadata,
            {
                "neuro_code.execution_status": "budget_limited",
                "neuro_code.execution_reason": "output_token_budget",
                "neuro_code.finalized": True,
                "neuro_code.recoverable": True,
            },
        )
        self.assertNotIn("snapshot", repr(metadata))
        self.assertNotIn("digest", repr(metadata))

    def test_acp_bounded_serialization_helpers_preserve_safety_order(self) -> None:
        self.assertEqual(sanitize_controls("before\x00after\x7f"), "before�after�")
        self.assertEqual(
            truncate_utf8("前缀-abcdefghijklmnop", 20),
            "前\n… [truncated]",
        )
        rendered = safe_output_text(
            "secret-token\n" + ("x" * 200),
            80,
            explicit_redactions=("secret-token",),
        )
        self.assertNotIn("secret-token", rendered)
        self.assertLessEqual(len(rendered.encode("utf-8")), 80)
        self.assertEqual(
            serialized_size_bytes({"b": "中文", "a": 1}),
            len('{"a":1,"b":"中文"}'.encode()),
        )

    def test_text_and_resource_links_preserve_order_and_drop_meta(self) -> None:
        resource = ResourceContentBlock.model_validate(
            {
                "type": "resource_link",
                "uri": "https://example.invalid/reference",
                "name": "reference",
                "title": "Title",
                "annotations": {
                    "audience": ["assistant"],
                    "_meta": {"annotation-secret": "never-visible"},
                },
                "_meta": {"resource-secret": "never-visible"},
            }
        )
        converted = convert_prompt_content(
            [
                TextContentBlock(type="text", text="before"),
                resource,
                TextContentBlock(type="text", text="after"),
            ]
        )
        self.assertLess(converted.content.index("before"), converted.content.index("resource_link"))
        self.assertLess(converted.content.index("resource_link"), converted.content.index("after"))
        self.assertNotIn("resource-secret", converted.content)
        self.assertNotIn("annotation-secret", converted.content)
        self.assertIn('"audience":["assistant"]', converted.content)

    def test_resource_link_is_not_dereferenced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "resource.txt"
            target.write_text("CONTENT-MUST-NOT-BE-READ", encoding="utf-8")
            converted = convert_prompt_content(
                [
                    ResourceContentBlock(
                        type="resource_link",
                        uri=target.as_uri(),
                        name="local reference",
                    )
                ]
            )
        self.assertIn(target.as_uri(), converted.content)
        self.assertNotIn("CONTENT-MUST-NOT-BE-READ", converted.content)

    def test_embedded_text_resource_preserves_order_and_does_not_dereference_uri(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "resource.txt"
            target.write_text("CONTENT-MUST-NOT-BE-READ", encoding="utf-8")
            resource = EmbeddedResourceContentBlock.model_validate(
                {
                    "type": "resource",
                    "resource": {
                        "uri": target.as_uri(),
                        "mimeType": "text/plain",
                        "text": "provided text",
                        "_meta": {"resource-secret": "never-visible"},
                    },
                    "annotations": {
                        "audience": ["assistant"],
                        "_meta": {"annotation-secret": "never-visible"},
                    },
                    "_meta": {"block-secret": "never-visible"},
                }
            )
            converted = convert_prompt_content(
                [
                    TextContentBlock(type="text", text="before"),
                    resource,
                    TextContentBlock(type="text", text="after"),
                ]
            )

        header = (
            f'<embedded_resource>{{"mimeType":"text/plain","uri":"{target.as_uri()}"}}'
            "</embedded_resource>"
        )
        self.assertEqual(
            [part.to_dict() for part in converted.content_parts],
            [
                {"type": "text", "text": "before"},
                {"type": "text", "text": f"{header}\nprovided text"},
                {"type": "text", "text": "after"},
            ],
        )
        self.assertEqual(converted.content, f"before\n{header}\nprovided text\nafter")
        self.assertNotIn("CONTENT-MUST-NOT-BE-READ", converted.content)
        self.assertNotIn("resource-secret", converted.content)
        self.assertNotIn("annotation-secret", converted.content)
        self.assertNotIn("block-secret", converted.content)

    def test_embedded_text_resource_validation_and_limits_fail_closed(self) -> None:
        blob = EmbeddedResourceContentBlock(
            type="resource",
            resource=BlobResourceContents(
                uri="memory://binary",
                blob=base64.b64encode(b"binary").decode("ascii"),
                mime_type="application/octet-stream",
            ),
        )
        cases = (
            (blob, "embedded_resource_blob_unsupported"),
            (
                EmbeddedResourceContentBlock(
                    type="resource",
                    resource=TextResourceContents(uri="", text="provided text"),
                ),
                "embedded_resource_uri_empty",
            ),
            (
                EmbeddedResourceContentBlock(
                    type="resource",
                    resource=TextResourceContents(uri="memory://empty", text=" \t"),
                ),
                "embedded_resource_text_empty",
            ),
            (
                EmbeddedResourceContentBlock(
                    type="resource",
                    resource=TextResourceContents(
                        uri="memory://oversized",
                        text="x" * (MAX_EMBEDDED_TEXT_RESOURCE_BYTES + 1),
                    ),
                ),
                "embedded_resource_text_too_large",
            ),
        )
        for resource, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(RequestError) as error:
                convert_prompt_content([resource])
            self.assertEqual(error.exception.data["reason"], reason)

        valid = EmbeddedResourceContentBlock(
            type="resource",
            resource=TextResourceContents(uri="memory://provided", text="text"),
        )
        with self.assertRaises(RequestError) as count_error:
            convert_prompt_content([valid] * (MAX_EMBEDDED_TEXT_RESOURCES + 1))
        self.assertEqual(count_error.exception.data["reason"], "too_many_embedded_text_resources")

        with (
            patch.object(acp_module, "MAX_EMBEDDED_TEXT_RESOURCE_BYTES", 4),
            patch.object(acp_module, "MAX_EMBEDDED_TEXT_TOTAL_BYTES", 3),
            self.assertRaises(RequestError) as total_size_error,
        ):
            small = EmbeddedResourceContentBlock(
                type="resource",
                resource=TextResourceContents(uri="memory://small", text="ab"),
            )
            convert_prompt_content([small, small])
        self.assertEqual(
            total_size_error.exception.data["reason"],
            "embedded_text_resources_too_large",
        )

    def test_unsupported_and_oversized_content_is_rejected(self) -> None:
        with self.assertRaises(RequestError) as audio_error:
            convert_prompt_content(
                [
                    AudioContentBlock(
                        type="audio",
                        data="AA==",
                        mime_type="audio/wav",
                    )
                ]
            )
        self.assertEqual(audio_error.exception.data["reason"], "unsupported_prompt_content")

        with self.assertRaises(RequestError) as text_error:
            convert_prompt_content(
                [TextContentBlock(type="text", text="x" * (MAX_TEXT_BLOCK_BYTES + 1))]
            )
        self.assertEqual(text_error.exception.data["reason"], "text_block_too_large")

        links = [
            ResourceContentBlock(
                type="resource_link",
                uri=f"https://example.invalid/{index}",
                name=str(index),
            )
            for index in range(MAX_RESOURCE_LINKS + 1)
        ]
        with self.assertRaises(RequestError) as links_error:
            convert_prompt_content(links)
        self.assertEqual(links_error.exception.data["reason"], "too_many_resource_links")

    def test_prompt_block_and_total_limits_are_enforced(self) -> None:
        cases = (
            ([], "prompt_empty"),
            (
                [TextContentBlock(type="text", text="x")] * (MAX_TEXT_BLOCKS + 1),
                "too_many_text_blocks",
            ),
            (
                [
                    ImageContentBlock(
                        type="image",
                        data="AA==",
                        mime_type="image/png",
                    )
                ]
                * (MAX_PROMPT_BLOCKS + 1),
                "too_many_prompt_blocks",
            ),
            (
                [
                    TextContentBlock(
                        type="text",
                        text="x" * MAX_TEXT_BLOCK_BYTES,
                    )
                ]
                * (MAX_PROMPT_BYTES // MAX_TEXT_BLOCK_BYTES + 1),
                "prompt_too_large",
            ),
        )
        for prompt, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(RequestError) as error:
                convert_prompt_content(prompt)
            self.assertEqual(error.exception.data["reason"], reason)

    def test_resource_field_annotation_and_serialized_limits_are_enforced(self) -> None:
        invalid_resources = (
            (
                ResourceContentBlock(
                    type="resource_link",
                    uri="https://example.invalid",
                    name="invalid size",
                    size=-1,
                ),
                "resource_size_invalid",
            ),
            (
                ResourceContentBlock.model_validate(
                    {
                        "type": "resource_link",
                        "uri": "https://example.invalid",
                        "name": "invalid priority",
                        "annotations": {"priority": float("nan")},
                    }
                ),
                "resource_annotation_priority_invalid",
            ),
            (
                ResourceContentBlock.model_validate(
                    {
                        "type": "resource_link",
                        "uri": "https://example.invalid",
                        "name": "too many audiences",
                        "annotations": {
                            "audience": [str(index) for index in range(MAX_ANNOTATION_AUDIENCE + 1)]
                        },
                    }
                ),
                "resource_annotations_too_large",
            ),
        )
        for resource, reason in invalid_resources:
            with self.subTest(reason=reason), self.assertRaises(RequestError) as error:
                convert_prompt_content([resource])
            self.assertEqual(error.exception.data["reason"], reason)

        oversized_links = [
            ResourceContentBlock(
                type="resource_link",
                uri=f"https://example.invalid/{index}",
                name=str(index),
                description="x" * 2_048,
            )
            for index in range(MAX_RESOURCE_LINKS)
        ]
        with self.assertRaises(RequestError) as serialized:
            convert_prompt_content(oversized_links)
        self.assertEqual(serialized.exception.data["reason"], "resource_links_too_large")

    def test_resource_standard_fields_and_controls_are_rendered_safely(self) -> None:
        converted = convert_prompt_content(
            [
                ResourceContentBlock.model_validate(
                    {
                        "type": "resource_link",
                        "uri": "https://example.invalid/a\u0001",
                        "name": "name",
                        "title": "title",
                        "description": "description",
                        "mimeType": "text/plain",
                        "size": 12,
                        "annotations": {
                            "audience": ["assistant"],
                            "lastModified": "2026-01-01",
                            "priority": 0.5,
                        },
                    }
                )
            ]
        )
        self.assertNotIn("\u0001", converted.content)
        self.assertIn("\ufffd", converted.content)
        self.assertIn('"mimeType":"text/plain"', converted.content)
        self.assertIn('"lastModified":"2026-01-01"', converted.content)
        self.assertIn('"size":12', converted.content)

    def test_inline_images_preserve_block_order_without_dereferencing_uri(self) -> None:
        encoded = base64.b64encode(b"image fixture").decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "not-read.png"
            target.write_bytes(b"CONTENT-MUST-NOT-BE-READ")
            converted = convert_prompt_content(
                [
                    TextContentBlock(type="text", text="before"),
                    ImageContentBlock(
                        type="image",
                        data=encoded,
                        mime_type="image/jpg",
                        uri=target.as_uri(),
                    ),
                    TextContentBlock(type="text", text="after"),
                ]
            )

        self.assertEqual(converted.content, "before\nafter")
        self.assertEqual(
            [part.to_dict() for part in converted.content_parts],
            [
                {"type": "text", "text": "before"},
                {"type": "image", "url": f"data:image/jpeg;base64,{encoded}"},
                {"type": "text", "text": "after"},
            ],
        )
        self.assertNotIn("CONTENT-MUST-NOT-BE-READ", repr(converted))

    def test_image_prompt_validation_and_limits_fail_closed(self) -> None:
        valid_image = ImageContentBlock(
            type="image",
            data=base64.b64encode(b"image").decode("ascii"),
            mime_type="image/png",
        )
        cases = (
            (
                ImageContentBlock(type="image", data="not base64", mime_type="image/png"),
                "image_data_invalid",
            ),
            (
                ImageContentBlock(type="image", data="AA==", mime_type="application/octet-stream"),
                "image_mime_type_unsupported",
            ),
        )
        for image, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(RequestError) as error:
                convert_prompt_content([image])
            self.assertEqual(error.exception.data["reason"], reason)

        with self.assertRaises(RequestError) as count_error:
            convert_prompt_content([valid_image] * (MAX_IMAGE_BLOCKS + 1))
        self.assertEqual(count_error.exception.data["reason"], "too_many_image_blocks")

        with self.assertRaises(RequestError) as empty_data_error:
            convert_prompt_content(
                [ImageContentBlock(type="image", data="", mime_type="image/png")]
            )
        self.assertEqual(empty_data_error.exception.data["reason"], "image_block_too_large")

        with patch.object(acp_module, "MAX_IMAGE_BLOCK_BYTES", 1):
            oversized = ImageContentBlock(
                type="image",
                data=base64.b64encode(b"ab").decode("ascii"),
                mime_type="image/png",
            )
            with self.assertRaises(RequestError) as size_error:
                convert_prompt_content([oversized])
        self.assertEqual(size_error.exception.data["reason"], "image_block_too_large")

        with (
            patch.object(acp_module, "MAX_IMAGE_BLOCK_BYTES", 4),
            patch.object(acp_module, "MAX_IMAGE_TOTAL_BYTES", 3),
            self.assertRaises(RequestError) as total_size_error,
        ):
            small_image = ImageContentBlock(
                type="image",
                data=base64.b64encode(b"ab").decode("ascii"),
                mime_type="image/png",
            )
            convert_prompt_content([small_image, small_image])
        self.assertEqual(total_size_error.exception.data["reason"], "images_too_large")

        with self.assertRaises(RequestError) as blank_text_error:
            convert_prompt_content([TextContentBlock(type="text", text=" \t")])
        self.assertEqual(blank_text_error.exception.data["reason"], "prompt_empty")

    def test_image_only_prompt_is_valid_structured_input(self) -> None:
        converted = convert_prompt_content(
            [
                ImageContentBlock(
                    type="image",
                    data=base64.b64encode(b"image").decode("ascii"),
                    mime_type="image/png",
                )
            ]
        )
        self.assertEqual(converted.content, "")
        self.assertEqual(len(converted.content_parts), 1)


class McpConfigurationTests(unittest.TestCase):
    @staticmethod
    def _server(
        *,
        name: str = "fixture",
        command: str = "fixture-command",
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
    ) -> McpServerStdio:
        return McpServerStdio(
            name=name,
            command=command,
            args=[] if args is None else args,
            env=[] if env is None else env,
        )

    def _assert_reason(
        self,
        servers: list[Any],
        reason: str,
        *,
        protected: frozenset[str] = frozenset(),
    ) -> None:
        with self.assertRaises(RequestError) as error:
            acp_module._mcp_server_configurations(
                servers,
                protected_environment_variables=protected,
            )
        self.assertEqual(error.exception.data["reason"], reason)

    def test_http_and_sse_configurations_are_validated_without_forwarding_meta(self) -> None:
        servers = [
            HttpMcpServer.model_validate(
                {
                    "name": "http",
                    "type": "http",
                    "url": "https://mcp.example.test/v1?tenant=one",
                    "headers": [
                        {"name": "Authorization", "value": "Bearer fixture-secret"},
                    ],
                    "_meta": {"must": "not-forward"},
                }
            ),
            SseMcpServer(
                name="sse",
                type="sse",
                url="http://127.0.0.1:8123/events",
                headers=[],
            ),
        ]

        configurations = acp_module._mcp_server_configurations(
            servers,
            protected_environment_variables=frozenset(),
        )

        self.assertEqual(configurations[0].transport, "http")
        self.assertEqual(configurations[0].url, "https://mcp.example.test/v1?tenant=one")
        self.assertEqual(
            configurations[0].headers,
            (("Authorization", "Bearer fixture-secret"),),
        )
        self.assertEqual(configurations[1].transport, "sse")
        self.assertNotIn("must", repr(configurations))

    def test_http_and_sse_reject_unsafe_urls_and_headers(self) -> None:
        def server(url: str, headers: list[dict[str, str]]) -> HttpMcpServer:
            return HttpMcpServer.model_validate(
                {"name": "http", "type": "http", "url": url, "headers": headers}
            )

        cases = (
            (server("ftp://mcp.example.test", []), "mcp_http_url_invalid"),
            (server("https://token@example.test/mcp", []), "mcp_http_url_invalid"),
            (server("https://mcp.example.test/mcp#fragment", []), "mcp_http_url_invalid"),
            (
                server("https://mcp.example.test", [{"name": "Host", "value": "override"}]),
                "mcp_http_header_reserved",
            ),
            (
                server(
                    "https://mcp.example.test",
                    [
                        {"name": "Authorization", "value": "one"},
                        {"name": "authorization", "value": "two"},
                    ],
                ),
                "mcp_http_header_name_invalid",
            ),
        )
        for configured, reason in cases:
            with self.subTest(reason=reason):
                self._assert_reason([configured], reason)

    def test_server_name_command_and_argument_limits(self) -> None:
        valid = self._server(args=["", "--stdio"])
        self.assertEqual(
            acp_module._mcp_server_configurations(
                [valid],
                protected_environment_variables=frozenset(),
            )[0].args,
            ("", "--stdio"),
        )
        cases = (
            (
                [self._server()] * (acp_module.MAX_MCP_SERVERS + 1),
                "too_many_mcp_servers",
            ),
            (
                [self._server(name="same"), self._server(name="SAME")],
                "mcp_server_name_duplicate",
            ),
            ([self._server(name="bad\nname")], "mcp_server_name_invalid"),
            ([self._server(command="bad\ncommand")], "mcp_server_command_invalid"),
            (
                [self._server(args=["x"] * (acp_module.MAX_MCP_ARGUMENTS + 1))],
                "too_many_mcp_server_arguments",
            ),
            (
                [self._server(args=["x" * (acp_module.MAX_MCP_ARGUMENT_BYTES + 1)])],
                "mcp_server_argument_invalid",
            ),
            (
                [
                    self._server(
                        args=["x" * acp_module.MAX_MCP_ARGUMENT_BYTES]
                        * acp_module.MAX_MCP_ARGUMENTS
                    )
                ],
                "mcp_server_arguments_too_large",
            ),
        )
        for servers, reason in cases:
            with self.subTest(reason=reason):
                self._assert_reason(servers, reason)

    def test_environment_and_aggregate_limits(self) -> None:
        cases = (
            (
                [
                    self._server(
                        env=[
                            EnvVariable(name=f"VALUE_{index}", value="x")
                            for index in range(acp_module.MAX_MCP_ENVIRONMENT_VARIABLES + 1)
                        ]
                    )
                ],
                "too_many_mcp_environment_variables",
                frozenset(),
            ),
            (
                [
                    self._server(
                        env=[
                            EnvVariable(name="VALUE", value="one"),
                            EnvVariable(name="value", value="two"),
                        ]
                    )
                ],
                "mcp_environment_name_invalid",
                frozenset(),
            ),
            (
                [self._server(env=[EnvVariable(name="BAD-NAME", value="x")])],
                "mcp_environment_name_invalid",
                frozenset(),
            ),
            (
                [self._server(env=[EnvVariable(name="SECRET", value="x")])],
                "mcp_environment_protected",
                frozenset({"secret"}),
            ),
            (
                [
                    self._server(
                        env=[
                            EnvVariable(
                                name="VALUE",
                                value="x" * (acp_module.MAX_MCP_ENVIRONMENT_VALUE_BYTES + 1),
                            )
                        ]
                    )
                ],
                "mcp_environment_value_invalid",
                frozenset(),
            ),
            (
                [
                    self._server(
                        env=[
                            EnvVariable(
                                name=f"VALUE_{index}",
                                value="x" * acp_module.MAX_MCP_ENVIRONMENT_VALUE_BYTES,
                            )
                            for index in range(5)
                        ]
                    )
                ],
                "mcp_environment_too_large",
                frozenset(),
            ),
        )
        for servers, reason, protected in cases:
            with self.subTest(reason=reason):
                self._assert_reason(servers, reason, protected=protected)

        with patch.object(acp_module, "MAX_MCP_CONFIGURATION_BYTES", 1):
            self._assert_reason(
                [self._server()],
                "mcp_configuration_too_large",
            )


class AcpAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_artifact_extension_lists_and_reads_bounded_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, artifact_service = await initialized_artifact_agent(Path(directory))

            listed = await agent.ext_method(
                ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION,
                {"sessionId": "acp-artifacts", "limit": 1},
            )
            read = await agent.ext_method(
                ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION,
                {
                    "sessionId": "acp-artifacts",
                    "artifactId": "a" * 32,
                    "maxBytes": 128,
                },
            )

        self.assertEqual(
            listed,
            {
                "artifacts": [
                    {
                        "artifactId": "a" * 32,
                        "byteCount": 64,
                        "truncated": True,
                        "eventSequence": 7,
                    }
                ]
            },
        )
        self.assertEqual(
            read,
            {
                "artifactId": "a" * 32,
                "content": "redacted output\n",
                "readTruncated": True,
            },
        )
        self.assertEqual(artifact_service.list_requests[0].limit, 1)
        self.assertEqual(artifact_service.read_requests[0].max_bytes, 128)

    async def test_private_artifact_extension_is_session_scoped_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, application, _ = await initialized_artifact_agent(Path(directory))
            other = SessionSummary(
                id="other-internal",
                cwd=str(Path(directory)),
                provider="fixture",
                model="fixture-model",
                created_at=datetime(2026, 7, 2, tzinfo=UTC),
                updated_at=datetime(2026, 7, 2, tzinfo=UTC),
                title="Other session",
            )
            application.store.summaries.append(other)
            application.store.session_ids.add(other.id)
            await application.store.bind_session_alias("acp-v1", "acp-other", other.id)

            with self.assertRaises(RequestError) as cross_session:
                await agent.ext_method(
                    ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION,
                    {"sessionId": "acp-other", "artifactId": "a" * 32},
                )
            with self.assertRaises(RequestError) as invalid:
                await agent.ext_method(
                    ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION,
                    {"sessionId": "acp-artifacts", "artifactId": "not-an-id"},
                )
            with self.assertRaises(RequestError) as unknown:
                await agent.ext_method("other/private/method", {})

        self.assertEqual(cross_session.exception.code, -32602)
        self.assertEqual(cross_session.exception.data, {"reason": "artifact_not_found"})
        self.assertEqual(invalid.exception.data, {"reason": "artifact_id_invalid"})
        self.assertEqual(unknown.exception.code, -32601)

    async def test_prompt_uses_application_turn_service(self) -> None:
        runner = RunnerFixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [runner])
            session = await agent.new_session(str(root))
            response = await agent.prompt(
                session.session_id,
                [TextContentBlock(type="text", text="answer")],
            )

        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(application.session_service.bound_runners, [runner])

    async def test_initialize_declares_session_lifecycle_and_saves_client_details(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            agent.on_connect(cast(Client, client))
            capabilities = ClientCapabilities(terminal=True)
            info = Implementation(name="fixture-client", version="2.0")

            response = await agent.initialize(1, capabilities, info)

        self.assertIs(agent.client_capabilities, capabilities)
        self.assertIs(agent.client_info, info)
        self.assertEqual(response.protocol_version, 1)
        self.assertEqual(
            response.agent_capabilities.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
                exclude_unset=True,
            ),
            {
                "loadSession": True,
                "mcpCapabilities": {"http": True, "sse": True},
                "sessionCapabilities": {
                    "list": {},
                    "delete": {},
                    "fork": {},
                    "resume": {},
                    "close": {},
                },
            },
        )

    async def test_initialize_negotiates_v1_and_rejects_duplicate_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = ApplicationFixture(Path(directory), [])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            response = await agent.initialize(99)
            with self.assertRaises(RequestError) as duplicate:
                await agent.initialize(1)

        self.assertEqual(response.protocol_version, 1)
        self.assertEqual(duplicate.exception.data["reason"], "already_initialized")

    async def test_client_filesystem_capabilities_are_bound_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [RunnerFixture()])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            agent.on_connect(cast(Client, client))
            await agent.initialize(
                1,
                ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=True, write_text_file=True)
                ),
            )

            created = await agent.new_session(str(root))
            client_file_system = application.client_file_systems[0]
            self.assertIsNotNone(client_file_system)
            assert client_file_system is not None
            self.assertTrue(client_file_system.supports_read)
            self.assertTrue(client_file_system.supports_write)

            target = root / "remote.txt"
            self.assertEqual(
                await client_file_system.read_text_file(target, line=3, limit=4),
                "client file contents",
            )
            await client_file_system.write_text_file(target, "updated")

        self.assertEqual(
            client.read_text_file_requests,
            [(created.session_id, str(target), 3, 4)],
        )
        self.assertEqual(
            client.write_text_file_requests,
            [(created.session_id, str(target), "updated")],
        )

    async def test_client_filesystem_fails_closed_for_capabilities_and_client_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(
                root, [RunnerFixture(), RunnerFixture(), RunnerFixture()]
            )
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            agent.on_connect(cast(Client, client))
            await agent.initialize(
                1,
                ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=True, write_text_file=True)
                ),
            )
            created = await agent.new_session(str(root))
            delegated = application.client_file_systems[0]
            assert delegated is not None
            target = root / "remote.txt"

            client.read_text_file_error = RuntimeError("untrusted client detail")
            with self.assertRaisesRegex(ToolError, "ACP client text-file read failed"):
                await delegated.read_text_file(target)
            client.read_text_file_error = None
            client.read_text_file_content = "x" * (acp_module.MAX_CLIENT_FILE_BYTES + 1)
            with self.assertRaisesRegex(ToolError, "response exceeds the size limit"):
                await delegated.read_text_file(target)
            client.read_text_file_content = "ok"

            with self.assertRaisesRegex(ToolError, "write exceeds the size limit"):
                await delegated.write_text_file(
                    target,
                    "x" * (acp_module.MAX_CLIENT_FILE_BYTES + 1),
                )
            client.write_text_file_error = RuntimeError("untrusted client detail")
            with self.assertRaisesRegex(ToolError, "ACP client text-file write failed"):
                await delegated.write_text_file(target, "ok")
            client.write_text_file_error = None

            await agent.close_session(created.session_id)

            read_only_agent = NeuroCodeAcpAgent(_acp_service(application))
            read_only_agent.on_connect(cast(Client, client))
            await read_only_agent.initialize(
                1,
                ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=True, write_text_file=False)
                ),
            )
            read_only_created = await read_only_agent.new_session(str(root))
            read_only = application.client_file_systems[1]
            assert read_only is not None
            with self.assertRaisesRegex(ToolError, "does not support text-file writes"):
                await read_only.write_text_file(target, "blocked")
            await read_only_agent.close_session(read_only_created.session_id)

            write_only_agent = NeuroCodeAcpAgent(_acp_service(application))
            write_only_agent.on_connect(cast(Client, client))
            await write_only_agent.initialize(
                1,
                ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=False, write_text_file=True)
                ),
            )
            write_only_created = await write_only_agent.new_session(str(root))
            write_only = application.client_file_systems[2]
            assert write_only is not None
            with self.assertRaisesRegex(ToolError, "does not support text-file reads"):
                await write_only.read_text_file(target)
            await write_only_agent.close_session(write_only_created.session_id)

    async def test_client_terminal_capability_is_bound_and_releases_foreground_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [RunnerFixture()])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            agent.on_connect(cast(Client, client))
            await agent.initialize(1, ClientCapabilities(terminal=True))

            created = await agent.new_session(str(root))
            terminal = application.client_terminals[0]
            self.assertIsNotNone(terminal)
            assert terminal is not None
            result = await terminal.run(
                "git",
                ("status", "--short"),
                cwd=root,
                output_byte_limit=128,
                timeout_seconds=1,
            )

        self.assertEqual(result.output, "client terminal output")
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.signal)
        self.assertFalse(result.truncated)
        self.assertEqual(
            client.create_terminal_requests,
            [(created.session_id, "git", ["status", "--short"], str(root), 128)],
        )
        self.assertEqual(client.terminal_envs, [None])
        self.assertEqual(client.terminal_wait_requests, [(created.session_id, "client-terminal")])
        self.assertEqual(client.terminal_output_requests, [(created.session_id, "client-terminal")])
        self.assertEqual(client.terminal_kill_requests, [])
        self.assertEqual(
            client.terminal_release_requests, [(created.session_id, "client-terminal")]
        )

    async def test_client_terminal_fails_closed_for_capabilities_and_client_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [RunnerFixture(), RunnerFixture()])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            agent.on_connect(cast(Client, client))
            await agent.initialize(1)
            created = await agent.new_session(str(root))
            self.assertIsNone(application.client_terminals[0])
            await agent.close_session(created.session_id)

            terminal_agent = NeuroCodeAcpAgent(_acp_service(application))
            terminal_agent.on_connect(cast(Client, client))
            await terminal_agent.initialize(1, ClientCapabilities(terminal=True))
            terminal_created = await terminal_agent.new_session(str(root))
            terminal = application.client_terminals[1]
            assert terminal is not None

            client.create_terminal_error = RuntimeError("untrusted client detail")
            with self.assertRaisesRegex(ToolError, "terminal creation failed"):
                await terminal.run("git", (), cwd=root, output_byte_limit=128, timeout_seconds=1)
            client.create_terminal_error = None

            client.terminal_output_error = RuntimeError("untrusted client detail")
            with self.assertRaisesRegex(ToolError, "terminal output failed"):
                await terminal.run("git", (), cwd=root, output_byte_limit=128, timeout_seconds=1)
            client.terminal_output_error = None

            client.terminal_output_response = TerminalOutputResponse(
                output="x" * 129,
                truncated=False,
                exit_status=TerminalExitStatus(exit_code=0),
            )
            with self.assertRaisesRegex(ToolError, "response exceeds the output limit"):
                await terminal.run("git", (), cwd=root, output_byte_limit=128, timeout_seconds=1)
            client.terminal_output_response = TerminalOutputResponse(
                output="ok",
                truncated=False,
                exit_status=TerminalExitStatus(exit_code=0),
            )

            client.terminal_wait = WaitForTerminalExitResponse()
            with self.assertRaisesRegex(ToolError, "returned no exit status"):
                await terminal.run("git", (), cwd=root, output_byte_limit=128, timeout_seconds=1)
            client.terminal_wait = WaitForTerminalExitResponse(exit_code=0)

            client.terminal_wait_error = RuntimeError("untrusted client detail")
            with self.assertRaisesRegex(ToolError, "terminal wait failed"):
                await terminal.run("git", (), cwd=root, output_byte_limit=128, timeout_seconds=1)
            client.terminal_wait_error = None

            client.terminal_wait_started = asyncio.Event()
            client.terminal_wait_gate = asyncio.Event()
            with self.assertRaisesRegex(ToolError, "timed out after"):
                await terminal.run(
                    "git", (), cwd=root, output_byte_limit=128, timeout_seconds=0.001
                )

            client.terminal_wait_started = asyncio.Event()
            pending = asyncio.create_task(
                terminal.run("git", (), cwd=root, output_byte_limit=128, timeout_seconds=1)
            )
            await client.terminal_wait_started.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            client.terminal_wait_gate = None
            await terminal_agent.close_session(terminal_created.session_id)

        self.assertGreaterEqual(len(client.terminal_kill_requests), 3)
        self.assertGreaterEqual(len(client.terminal_release_requests), 6)

    async def test_client_background_terminal_is_session_bound_and_releases_on_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [RunnerFixture()])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            client.terminal_wait_gate = asyncio.Event()
            agent.on_connect(cast(Client, client))
            await agent.initialize(1, ClientCapabilities(terminal=True))
            created = await agent.new_session(str(root))
            terminal = application.client_terminals[0]
            assert terminal is not None

            started = await terminal.start_exec(
                "git",
                ("status", "--short"),
                cwd=root,
                output_byte_limit=128,
                timeout_seconds=10,
            )
            await client.terminal_wait_started.wait()
            self.assertEqual(started.status, BackgroundTaskStatus.RUNNING)
            self.assertTrue(started.task_id.startswith("terminal-task-"))
            self.assertEqual(
                client.create_terminal_requests,
                [(created.session_id, "git", ["status", "--short"], str(root), 128)],
            )

            running = await terminal.get(started.task_id)
            assert running is not None
            self.assertEqual(running.status, BackgroundTaskStatus.RUNNING)
            self.assertEqual(running.output, "client terminal output")
            client.terminal_wait_gate.set()
            completed = await terminal.wait(
                (started.task_id,),
                mode=BackgroundTaskWaitMode.WAIT_ALL,
                timeout_seconds=1,
            )
            self.assertFalse(completed.timed_out)
            self.assertEqual(completed.snapshots[0].status, BackgroundTaskStatus.COMPLETED)
            self.assertEqual(completed.snapshots[0].output, "client terminal output")
            await agent.close_session(created.session_id)

        self.assertEqual(client.terminal_kill_requests, [])
        self.assertEqual(
            client.terminal_release_requests, [(created.session_id, "client-terminal")]
        )

    async def test_client_background_terminal_is_killed_and_released_when_session_closes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [RunnerFixture()])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            client.terminal_wait_gate = asyncio.Event()
            agent.on_connect(cast(Client, client))
            await agent.initialize(1, ClientCapabilities(terminal=True))
            created = await agent.new_session(str(root))
            terminal = application.client_terminals[0]
            assert terminal is not None

            started = await terminal.start_exec(
                "git",
                ("status",),
                cwd=root,
                output_byte_limit=128,
            )
            await client.terminal_wait_started.wait()
            await agent.close_session(created.session_id)

        self.assertTrue(started.task_id.startswith("terminal-task-"))
        self.assertGreaterEqual(
            client.terminal_kill_requests.count((created.session_id, "client-terminal")),
            1,
        )
        self.assertEqual(
            client.terminal_release_requests, [(created.session_id, "client-terminal")]
        )

    async def test_client_background_terminal_times_out_and_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [RunnerFixture()])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            client = AcpClientFixture()
            client.terminal_wait_gate = asyncio.Event()
            agent.on_connect(cast(Client, client))
            await agent.initialize(1, ClientCapabilities(terminal=True))
            created = await agent.new_session(str(root))
            terminal = application.client_terminals[0]
            assert terminal is not None

            started = await terminal.start_exec(
                "git",
                ("status",),
                cwd=root,
                output_byte_limit=128,
                timeout_seconds=0.001,
            )
            await client.terminal_wait_started.wait()
            completed = await terminal.wait(
                (started.task_id,),
                mode=BackgroundTaskWaitMode.WAIT_ALL,
                timeout_seconds=1,
            )
            await agent.close_session(created.session_id)

        self.assertEqual(completed.snapshots[0].status, BackgroundTaskStatus.TIMED_OUT)
        self.assertIn((created.session_id, "client-terminal"), client.terminal_kill_requests)
        self.assertEqual(
            client.terminal_release_requests, [(created.session_id, "client-terminal")]
        )

    async def test_requests_require_initialize_and_active_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [])
            agent = NeuroCodeAcpAgent(_acp_service(application))
            with self.assertRaises(RequestError) as not_initialized:
                await agent.new_session(str(root))
            self.assertEqual(not_initialized.exception.data["reason"], "not_initialized")

            await agent.initialize(1)
            with self.assertRaises(RequestError) as unknown:
                await agent.prompt(
                    "unknown\u0001session",
                    [TextContentBlock(type="text", text="hello")],
                )
            self.assertEqual(unknown.exception.data["reason"], "session_not_active")
            self.assertTrue(unknown.exception.data["sessionId"].startswith("id-"))

            with self.assertRaises(RequestError) as creation:
                await agent.new_session(str(root))
            self.assertEqual(creation.exception.data["reason"], "session_creation_failed")

    async def test_image_prompt_reaches_the_binding_as_structured_history(self) -> None:
        encoded = base64.b64encode(b"image fixture").decode("ascii")
        runner = RunnerFixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(root, [runner])
            session = await agent.new_session(str(root))
            response = await agent.prompt(
                session.session_id,
                [
                    TextContentBlock(type="text", text="inspect"),
                    ImageContentBlock(type="image", data=encoded, mime_type="image/png"),
                ],
            )

        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(runner.prompts[0][0], "inspect")
        self.assertEqual(
            [part.to_dict() for part in runner.prompts[0][1]],
            [
                {"type": "text", "text": "inspect"},
                {"type": "image", "url": f"data:image/png;base64,{encoded}"},
            ],
        )

    async def test_embedded_text_resource_reaches_the_binding_as_labeled_text(self) -> None:
        runner = RunnerFixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(root, [runner])
            session = await agent.new_session(str(root))
            response = await agent.prompt(
                session.session_id,
                [
                    TextContentBlock(type="text", text="inspect"),
                    EmbeddedResourceContentBlock(
                        type="resource",
                        resource=TextResourceContents(
                            uri="memory://review-notes",
                            mime_type="text/markdown",
                            text="## Notes\nReview this change.",
                        ),
                    ),
                ],
            )

        resource_text = (
            '<embedded_resource>{"mimeType":"text/markdown",'
            '"uri":"memory://review-notes"}</embedded_resource>\n'
            "## Notes\nReview this change."
        )
        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(runner.prompts[0][0], f"inspect\n{resource_text}")
        self.assertEqual(
            [part.to_dict() for part in runner.prompts[0][1]],
            [
                {"type": "text", "text": "inspect"},
                {"type": "text", "text": resource_text},
            ],
        )

    async def test_session_new_validates_workspace_and_keeps_stable_acp_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RunnerFixture()
            agent, application, _ = await initialized_agent(root, [runner])

            with self.assertRaises(RequestError) as relative:
                await agent.new_session("relative")
            self.assertEqual(relative.exception.data["reason"], "cwd_not_absolute")

            with self.assertRaises(RequestError) as mismatch:
                await agent.new_session(str(root.parent))
            self.assertEqual(mismatch.exception.data["reason"], "cwd_workspace_mismatch")

            with self.assertRaises(RequestError) as directories:
                await agent.new_session(str(root), additional_directories=[str(root)])
            self.assertEqual(
                directories.exception.data["reason"],
                "additional_directory_overlaps_workspace",
            )

            with self.assertRaises(RequestError) as mcp:
                await agent.new_session(str(root), mcp_servers=[cast(Any, object())])
            self.assertEqual(mcp.exception.data["reason"], "mcp_transport_unsupported")

            additional = root.parent / f"{root.name}-additional"
            additional.mkdir()
            created = await agent.new_session(
                str(root),
                additional_directories=[str(additional)],
                mcp_servers=[],
            )
            acp_id = created.session_id
            response = await agent.prompt(
                acp_id,
                [TextContentBlock(type="text", text="hello")],
            )
            persisted_mapping = await application.store.resolve_session_alias(
                "acp-v1",
                acp_id,
            )

        self.assertTrue(acp_id.startswith("acp-"))
        self.assertNotEqual(acp_id, runner.session_id)
        self.assertEqual(application.additional_workspace_roots, [(additional.resolve(),)])
        self.assertEqual(persisted_mapping, runner.session_id)
        self.assertEqual(response.stop_reason, "end_turn")

    async def test_additional_directories_have_bounded_and_sandboxed_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extra = root.parent / f"{root.name}-extra"
            extra.mkdir()
            agent, _, _ = await initialized_agent(root, [RunnerFixture()])

            with self.assertRaises(RequestError) as relative:
                await agent.new_session(str(root), additional_directories=["relative"])
            self.assertEqual(relative.exception.data["reason"], "additional_directory_not_absolute")

            with self.assertRaises(RequestError) as missing:
                await agent.new_session(
                    str(root),
                    additional_directories=[str(extra / "missing")],
                )
            self.assertEqual(missing.exception.data["reason"], "additional_directory_invalid")

            with self.assertRaises(RequestError) as too_many:
                await agent.new_session(
                    str(root),
                    additional_directories=[str(extra)] * 5,
                )
            self.assertEqual(too_many.exception.data["reason"], "additional_directories_too_many")

            for profile in (
                SandboxProfile.WORKSPACE,
                SandboxProfile.READ_ONLY,
                SandboxProfile.STRICT,
            ):
                application = ApplicationFixture(root, [])
                application.config = replace(application.config, sandbox_profile=profile)
                sandboxed_agent = NeuroCodeAcpAgent(_acp_service(application))
                await sandboxed_agent.initialize(1)
                with self.subTest(profile=profile), self.assertRaises(RequestError) as sandboxed:
                    await sandboxed_agent.new_session(
                        str(root),
                        additional_directories=[str(extra)],
                    )
                self.assertEqual(
                    sandboxed.exception.data["reason"],
                    "additional_directories_sandbox_unsupported",
                )

    async def test_stdio_mcp_is_bounded_session_owned_and_available_to_new_and_load(
        self,
    ) -> None:
        first_collection = McpCollectionFixture()
        second_collection = McpCollectionFixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(
                root,
                [RunnerFixture(), RunnerFixture(session_id="persisted-id")],
            )
            await application.store.bind_session_alias(
                "acp-v1",
                "acp-durable",
                "persisted-id",
            )
            server = McpServerStdio.model_validate(
                {
                    "name": "fixture",
                    "command": "fixture-command",
                    "args": ["--stdio"],
                    "env": [{"name": "MCP_TOKEN", "value": "fixture-secret"}],
                    "_meta": {"must": "be ignored"},
                }
            )
            open_mcp = AsyncMock(
                side_effect=(first_collection, second_collection),
            )
            with patch(
                "neuro_code.bootstrap.entrypoints.McpStdioToolCollection.open",
                new=open_mcp,
            ):
                created = await agent.new_session(str(root), mcp_servers=[server])
                await agent.close_session(created.session_id)
                await agent.load_session(
                    str(root),
                    "acp-durable",
                    mcp_servers=[server],
                )
                await agent.close_session("acp-durable")

            configurations = open_mcp.await_args_list[0].args[0]
            self.assertEqual(configurations[0].name, "fixture")
            self.assertEqual(configurations[0].command, "fixture-command")
            self.assertEqual(configurations[0].args, ("--stdio",))
            self.assertEqual(configurations[0].env, (("MCP_TOKEN", "fixture-secret"),))
            self.assertNotIn("must", repr(configurations))
            self.assertEqual(
                application.additional_tool_names,
                [("remote_echo",), ("remote_echo",)],
            )
            self.assertEqual(first_collection.close_calls, 1)
            self.assertEqual(second_collection.close_calls, 1)

            protected_server = McpServerStdio(
                name="protected",
                command="fixture-command",
                args=[],
                env=[EnvVariable(name="FIXTURE_KEY", value="must-not-override")],
            )
            with self.assertRaises(RequestError) as protected:
                await agent.new_session(
                    str(root),
                    mcp_servers=[protected_server],
                )
            self.assertEqual(
                protected.exception.data["reason"],
                "mcp_environment_protected",
            )

    async def test_mcp_factory_is_lazy_until_a_session_requests_stdio_tools(self) -> None:
        collection = McpCollectionFixture()
        server = McpServerStdio(name="fixture", command="fixture-command", args=[], env=[])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(root, [RunnerFixture(), RunnerFixture()])
            open_mcp = AsyncMock(return_value=collection)
            with patch(
                "neuro_code.bootstrap.entrypoints.McpStdioToolCollection.open",
                new=open_mcp,
            ):
                created_without_mcp = await agent.new_session(str(root))
                open_mcp.assert_not_awaited()
                await agent.close_session(created_without_mcp.session_id)

                created_with_mcp = await agent.new_session(str(root), mcp_servers=[server])
                open_mcp.assert_awaited_once()
                await agent.close_session(created_with_mcp.session_id)

        self.assertEqual(collection.close_calls, 1)

    async def test_http_mcp_is_session_owned_and_opened_by_the_remote_adapter(self) -> None:
        collection = McpCollectionFixture()
        server = HttpMcpServer(
            name="remote",
            type="http",
            url="https://mcp.example.test/mcp",
            headers=[{"name": "Authorization", "value": "Bearer fixture-secret"}],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [RunnerFixture()])
            open_mcp = AsyncMock(return_value=collection)
            with patch(
                "neuro_code.bootstrap.entrypoints.McpHttpToolCollection.open",
                new=open_mcp,
            ):
                created = await agent.new_session(str(root), mcp_servers=[server])
                await agent.close_session(created.session_id)

        configuration = open_mcp.await_args.args[0][0]
        self.assertEqual(configuration.name, "remote")
        self.assertEqual(configuration.url, "https://mcp.example.test/mcp")
        self.assertEqual(
            configuration.headers,
            (("Authorization", "Bearer fixture-secret"),),
        )
        self.assertEqual(configuration.transport, "http")
        self.assertEqual(application.additional_tool_names, [("remote_echo",)])
        self.assertEqual(collection.close_calls, 1)

    async def test_failed_session_creation_cleans_binding_then_mcp_in_reverse_order(self) -> None:
        class NeverPublishAgent(NeuroCodeAcpAgent):
            async def _publish_session(self, session: Any) -> bool:
                del session
                return False

        cleanup_events: list[str] = []
        collection = McpCollectionFixture(cleanup_events)
        server = McpServerStdio(name="fixture", command="fixture-command", args=[], env=[])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [RunnerFixture()])
            application.cleanup_events = cleanup_events
            agent = NeverPublishAgent(_acp_service(application))
            await agent.initialize(1)
            with (
                patch(
                    "neuro_code.bootstrap.entrypoints.McpStdioToolCollection.open",
                    new=AsyncMock(return_value=collection),
                ),
                self.assertRaises(RequestError) as error,
            ):
                await agent.new_session(str(root), mcp_servers=[server])

        self.assertEqual(error.exception.data["reason"], "connection_closing")
        self.assertEqual(cleanup_events, ["binding", "mcp"])

    async def test_cancel_keeps_session_mcp_context_until_explicit_close(self) -> None:
        collection = McpCollectionFixture()
        server = McpServerStdio(name="fixture", command="fixture-command", args=[], env=[])
        runner = RunnerFixture(block=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(root, [runner])
            with patch(
                "neuro_code.bootstrap.entrypoints.McpStdioToolCollection.open",
                new=AsyncMock(return_value=collection),
            ):
                created = await agent.new_session(str(root), mcp_servers=[server])
                prompt = asyncio.create_task(
                    agent.prompt(
                        created.session_id,
                        [TextContentBlock(type="text", text="wait")],
                    )
                )
                await runner.wait_started()
                await agent.cancel(created.session_id)
                response = await prompt
                self.assertEqual(collection.close_calls, 0)
                await agent.close_session(created.session_id)

        self.assertEqual(response.stop_reason, "cancelled")
        self.assertEqual(collection.close_calls, 1)

    async def test_session_load_replays_bounded_visible_history_and_resumes(self) -> None:
        embedded_resource = (
            '<embedded_resource>{"mimeType":"text/plain",'
            '"uri":"memory://previous-notes"}</embedded_resource>\n'
            "previous resource text"
        )
        history = (
            Message(Role.SYSTEM, "hidden system instructions"),
            Message(
                Role.USER,
                f"previous question\n{embedded_resource}",
                content_parts=(
                    ContentPart.from_text("previous question"),
                    ContentPart.from_text(embedded_resource),
                    ContentPart.from_image("data:image/png;base64,cHJpdmF0ZS1pbWFnZQ=="),
                ),
            ),
            PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "reasoning-1",
                    "encrypted_content": "hidden preserved reasoning",
                },
            ),
            Message(
                Role.ASSISTANT,
                "previous answer",
                reasoning_content="hidden assistant reasoning",
                tool_calls=(
                    ToolCall(
                        "call-read",
                        "read_file",
                        {"path": "safe.txt", "secret": "sk-secretvalue"},
                    ),
                ),
            ),
            Message(
                Role.TOOL,
                "token=sk-secretvalue",
                name="read_file",
                tool_call_id="call-read",
            ),
            Message(
                Role.ASSISTANT,
                tool_calls=(ToolCall("call-pending", "bash", {"command": "pwd"}),),
            ),
        )
        first_runner = RunnerFixture(session_id="persisted-id", items=history)
        second_runner = RunnerFixture(session_id="persisted-id", items=history)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            additional = root.parent / f"{root.name}-additional"
            additional.mkdir()
            agent, application, client = await initialized_agent(
                root,
                [first_runner, second_runner],
            )
            await application.store.bind_session_alias(
                "acp-v1",
                "acp-durable",
                "persisted-id",
            )

            loaded = await agent.load_session(
                str(root),
                "acp-durable",
                mcp_servers=[],
                additional_directories=[str(additional)],
            )
            self.assertIsNotNone(loaded)
            replay = [update for _, update in client.updates]
            self.assertEqual(
                [update.session_update for update in replay],
                [
                    "user_message_chunk",
                    "agent_message_chunk",
                    "tool_call",
                    "tool_call_update",
                    "tool_call",
                    "tool_call_update",
                ],
            )
            self.assertIsInstance(replay[0], UserMessageChunk)
            self.assertEqual(
                replay[0].content.text,
                f"previous question\n{embedded_resource}\n"
                "[image content preserved in session; binary replay is unavailable]",
            )
            self.assertEqual(replay[1].content.text, "previous answer")
            self.assertEqual([replay[2].status, replay[3].status], ["pending", "completed"])
            self.assertEqual([replay[4].status, replay[5].status], ["pending", "failed"])
            self.assertEqual(replay[2].locations[0].path, "safe.txt")
            self.assertNotIn("secretvalue", repr(replay))
            self.assertNotIn("cHJpdmF0ZS1pbWFnZQ==", repr(replay))
            self.assertNotIn("hidden", repr(replay))
            self.assertIsNone(replay[2].raw_input)
            self.assertIsNone(replay[3].raw_output)
            self.assertTrue(all(session_id == "acp-durable" for session_id, _ in client.updates))

            prompted = await agent.prompt(
                "acp-durable",
                [TextContentBlock(type="text", text="continue")],
            )
            self.assertEqual(prompted.stop_reason, "end_turn")
            await agent.close_session("acp-durable")
            client.updates.clear()
            await agent.load_session(
                str(root),
                "acp-durable",
                mcp_servers=[],
                additional_directories=[str(additional)],
            )
            await agent.close_session("acp-durable")

            self.assertEqual(application.resume_ids, ["persisted-id", "persisted-id"])
            self.assertTrue(all(terminal is not None for terminal in application.client_terminals))
        self.assertEqual(
            application.additional_workspace_roots,
            [(additional.resolve(),), (additional.resolve(),)],
        )
        self.assertEqual([scope.shutdown_calls for scope in application.background_scopes], [1, 1])

    async def test_session_load_validates_inputs_identity_and_active_state(self) -> None:
        loaded_runner = RunnerFixture(session_id="persisted-id")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [loaded_runner])

            cases = (
                (
                    lambda: agent.load_session("relative", "missing", mcp_servers=[]),
                    "cwd_not_absolute",
                ),
                (
                    lambda: agent.load_session(
                        str(root),
                        "missing",
                        mcp_servers=[],
                        additional_directories=[str(root)],
                    ),
                    "additional_directory_overlaps_workspace",
                ),
                (
                    lambda: agent.load_session(
                        str(root),
                        "missing",
                        mcp_servers=[cast(Any, object())],
                    ),
                    "mcp_transport_unsupported",
                ),
                (
                    lambda: agent.load_session(str(root), "bad\nid", mcp_servers=[]),
                    "session_id_invalid",
                ),
                (
                    lambda: agent.load_session(str(root), "界" * 200, mcp_servers=[]),
                    "session_id_too_large",
                ),
                (
                    lambda: agent.load_session(str(root), "missing", mcp_servers=[]),
                    "session_not_found",
                ),
            )
            for operation, reason in cases:
                with self.subTest(reason=reason), self.assertRaises(RequestError) as error:
                    await operation()
                self.assertEqual(error.exception.data["reason"], reason)

            await application.store.bind_session_alias(
                "acp-v1",
                "acp-durable",
                "persisted-id",
            )
            application.resume_error = ConfigurationError(
                "session does not belong to the application workspace"
            )
            with self.assertRaises(RequestError) as mismatch:
                await agent.load_session(str(root), "acp-durable", mcp_servers=[])
            self.assertEqual(mismatch.exception.data["reason"], "session_workspace_mismatch")
            application.resume_error = None

            await agent.load_session(str(root), "acp-durable", mcp_servers=[])
            with self.assertRaises(RequestError) as active:
                await agent.load_session(str(root), "acp-durable", mcp_servers=[])
            self.assertEqual(active.exception.data["reason"], "session_already_active")
            await agent.close_session("acp-durable")

    async def test_session_load_history_limit_fails_before_replay_and_cleans_scope(self) -> None:
        oversized = tuple(Message(Role.USER, f"history-{index}") for index in range(2_001))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, client = await initialized_agent(
                root,
                [RunnerFixture(session_id="persisted-id", items=oversized)],
            )
            await application.store.bind_session_alias(
                "acp-v1",
                "acp-durable",
                "persisted-id",
            )
            with self.assertRaises(RequestError) as error:
                await agent.load_session(str(root), "acp-durable", mcp_servers=[])

        self.assertEqual(error.exception.data["reason"], "session_history_too_large")
        self.assertEqual(client.updates, [])
        self.assertEqual(application.background_scopes[0].shutdown_calls, 1)

    async def test_session_resume_restores_context_without_history_replay(self) -> None:
        history = (
            Message(Role.USER, "previous question"),
            Message(Role.ASSISTANT, "previous answer"),
        )
        runner = RunnerFixture(session_id="persisted-id", items=history)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            additional = root.parent / f"{root.name}-additional"
            additional.mkdir()
            agent, application, client = await initialized_agent(root, [runner])
            await application.store.bind_session_alias(
                "acp-v1",
                "acp-durable",
                "persisted-id",
            )

            resumed = await agent.resume_session(
                "acp-durable",
                str(root),
                mcp_servers=[],
                additional_directories=[str(additional)],
            )

            self.assertEqual(resumed.model_dump(exclude_none=True), {})
            self.assertEqual(client.updates, [])
            self.assertEqual(application.resume_ids, ["persisted-id"])
            self.assertEqual(application.additional_workspace_roots, [(additional.resolve(),)])
            self.assertIsNotNone(application.client_terminals[0])
            prompted = await agent.prompt(
                "acp-durable",
                [TextContentBlock(type="text", text="continue")],
            )
            self.assertEqual(prompted.stop_reason, "end_turn")
            await agent.close_session("acp-durable")

    async def test_session_list_is_workspace_scoped_paginated_and_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [])
            summaries = [
                SessionSummary(
                    id=f"internal-{index}",
                    cwd=str(root),
                    provider="fixture",
                    model="fixture-model",
                    created_at=datetime(2026, 7, index, tzinfo=UTC),
                    updated_at=datetime(2026, 7, index, tzinfo=UTC),
                    title=f"Session {index}",
                )
                for index in (3, 2, 1)
            ]
            summaries.extend(
                (
                    SessionSummary(
                        id="other-workspace",
                        cwd=str(root.parent),
                        provider="fixture",
                        model="fixture-model",
                        created_at=datetime(2026, 7, 4, tzinfo=UTC),
                        updated_at=datetime(2026, 7, 4, tzinfo=UTC),
                        title="Must stay hidden",
                    ),
                    SessionSummary(
                        id="relative-workspace",
                        cwd="relative",
                        provider="fixture",
                        model="fixture-model",
                        created_at=datetime(2026, 7, 5, tzinfo=UTC),
                        updated_at=datetime(2026, 7, 5, tzinfo=UTC),
                        title="Invalid metadata",
                    ),
                )
            )
            application.store.summaries.extend(summaries)
            application.store.session_ids.update(summary.id for summary in summaries)
            await application.store.bind_session_alias(
                "acp-v1",
                "acp-existing",
                "internal-3",
            )

            with patch("neuro_code.acp.ACP_SESSION_LIST_PAGE_SIZE", 2):
                first = await agent.list_sessions()
                self.assertIsNotNone(first.next_cursor)
                second = await agent.list_sessions(
                    str(root),
                    cursor=first.next_cursor,
                )
                repeated = await agent.list_sessions(str(root))

            self.assertEqual(
                [session.title for session in first.sessions],
                ["Session 3", "Session 2"],
            )
            self.assertEqual(first.sessions[0].session_id, "acp-existing")
            self.assertEqual([session.title for session in second.sessions], ["Session 1"])
            self.assertIsNone(second.next_cursor)
            self.assertEqual(
                [session.session_id for session in repeated.sessions],
                [session.session_id for session in first.sessions],
            )
            self.assertEqual(application.resume_ids, [])
            self.assertEqual(application.background_scopes, [])
            self.assertTrue(
                all(session.cwd == str(root) for session in (*first.sessions, *second.sessions))
            )
            self.assertTrue(
                all(
                    session.additional_directories is None and session.field_meta is None
                    for session in (*first.sessions, *second.sessions)
                )
            )
            for listed in (*first.sessions, *second.sessions):
                self.assertIn(
                    await application.store.resolve_session_alias(
                        "acp-v1",
                        listed.session_id,
                    ),
                    {"internal-1", "internal-2", "internal-3"},
                )

            with self.assertRaises(RequestError) as relative:
                await agent.list_sessions("relative")
            self.assertEqual(relative.exception.data["reason"], "cwd_not_absolute")
            with self.assertRaises(RequestError) as mismatch:
                await agent.list_sessions(str(root.parent))
            self.assertEqual(mismatch.exception.data["reason"], "cwd_workspace_mismatch")
            with self.assertRaises(RequestError) as invalid_cursor:
                await agent.list_sessions(cursor="unknown-cursor")
            self.assertEqual(invalid_cursor.exception.data["reason"], "cursor_invalid")

    async def test_session_delete_removes_listed_state_and_closes_unpersisted_active_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [RunnerFixture()])
            summary = SessionSummary(
                id="persisted-id",
                cwd=str(root),
                provider="fixture",
                model="fixture-model",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                title="Delete me",
            )
            application.store.summaries.append(summary)
            application.store.session_ids.add(summary.id)
            await application.store.bind_session_alias(
                "acp-v1",
                "acp-durable",
                summary.id,
            )

            deleted = await agent.delete_session("acp-durable")

            self.assertEqual(deleted.model_dump(exclude_none=True), {})
            self.assertEqual(
                application.session_service.delete_requests,
                [DeleteSessionRequest("persisted-id")],
            )
            self.assertEqual(
                application.session_service.summary_requests,
                [GetSessionSummaryRequest("persisted-id")],
            )
            self.assertEqual(application.store.deleted_session_ids, ["persisted-id"])
            with self.assertRaises(RequestError) as missing:
                await agent.delete_session("acp-durable")
            self.assertEqual(missing.exception.data["reason"], "session_not_found")

            active = await agent.new_session(str(root))
            await agent.delete_session(active.session_id)
            self.assertEqual(application.background_scopes[0].shutdown_calls, 1)
            with self.assertRaises(RequestError) as inactive:
                await agent.prompt(
                    active.session_id,
                    [TextContentBlock(type="text", text="closed")],
                )
            self.assertEqual(inactive.exception.data["reason"], "session_not_active")

    async def test_session_delete_hides_other_workspace_as_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [])
            summary = SessionSummary(
                id="other-id",
                cwd=str(root.parent),
                provider="fixture",
                model="fixture-model",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
            application.store.summaries.append(summary)
            application.store.session_ids.add(summary.id)
            await application.store.bind_session_alias("acp-v1", "acp-other", summary.id)

            with self.assertRaises(RequestError) as hidden:
                await agent.delete_session("acp-other")

            self.assertEqual(hidden.exception.data["reason"], "session_not_found")
            self.assertEqual(application.store.deleted_session_ids, [])

    async def test_session_fork_creates_independent_active_context_without_replay(self) -> None:
        history = (
            Message(Role.USER, "source question"),
            Message(Role.ASSISTANT, "source answer"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            additional = root.parent / f"{root.name}-additional"
            additional.mkdir()
            agent, application, client = await initialized_agent(
                root,
                [RunnerFixture(session_id="forked-1", items=history)],
            )
            summary = SessionSummary(
                id="source-id",
                cwd=str(root),
                provider="fixture",
                model="fixture-model",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                title="Source title",
            )
            application.store.summaries.append(summary)
            application.store.session_ids.add(summary.id)
            await application.store.bind_session_alias("acp-v1", "acp-source", summary.id)

            forked = await agent.fork_session(
                "acp-source",
                str(root),
                mcp_servers=[],
                additional_directories=[str(additional)],
            )

            self.assertNotEqual(forked.session_id, "acp-source")
            self.assertEqual(
                application.store.forked_session_ids,
                [("source-id", "forked-1")],
            )
            self.assertEqual(
                application.session_service.fork_requests,
                [ForkSessionRequest("source-id")],
            )
            self.assertEqual(
                await application.store.resolve_session_alias(
                    "acp-v1",
                    forked.session_id,
                ),
                "forked-1",
            )
            self.assertEqual(application.resume_ids, ["forked-1"])
            self.assertEqual(application.additional_workspace_roots, [(additional.resolve(),)])
            self.assertIsNotNone(application.client_terminals[0])
            self.assertEqual(client.updates, [])
            prompted = await agent.prompt(
                forked.session_id,
                [TextContentBlock(type="text", text="fork continues")],
            )
            self.assertEqual(prompted.stop_reason, "end_turn")
            self.assertIn("source-id", application.store.session_ids)
            await agent.close_session(forked.session_id)

    async def test_session_fork_rolls_back_persisted_copy_when_binding_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(
                root,
                [RunnerFixture(session_id="wrong-fork-id")],
            )
            summary = SessionSummary(
                id="source-id",
                cwd=str(root),
                provider="fixture",
                model="fixture-model",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
            application.store.summaries.append(summary)
            application.store.session_ids.add(summary.id)
            await application.store.bind_session_alias("acp-v1", "acp-source", summary.id)

            with self.assertRaises(RequestError) as mismatch:
                await agent.fork_session("acp-source", str(root), mcp_servers=[])

            self.assertEqual(mismatch.exception.data["reason"], "session_identity_mismatch")
            self.assertEqual(application.store.deleted_session_ids, ["forked-1"])
            self.assertEqual(application.background_scopes[0].shutdown_calls, 1)

    async def test_event_mapping_has_stable_message_id_and_bounded_tool_fields(self) -> None:
        events = (
            AgentEvent.create(1, AgentEventKind.TEXT_DELTA, {"text": "one"}),
            AgentEvent.create(2, AgentEventKind.REASONING_DELTA, {"text": "hidden"}),
            AgentEvent.create(3, AgentEventKind.TEXT_DELTA, {"text": "two"}),
            AgentEvent.create(
                4,
                AgentEventKind.TOOL_REQUESTED,
                {
                    "id": "tool-1",
                    "name": "bash",
                    "arguments": {"command": "api_key=sk-secretvalue"},
                },
            ),
            AgentEvent.create(
                5,
                AgentEventKind.TOOL_STARTED,
                {"id": "tool-1", "name": "bash"},
            ),
            AgentEvent.create(
                6,
                AgentEventKind.TOOL_COMPLETED,
                {
                    "id": "tool-1",
                    "name": "bash",
                    "content": "token=sk-secretvalue",
                    "metadata": {"unbounded": "not-forwarded"},
                },
            ),
            AgentEvent.create(
                7,
                AgentEventKind.TURN_COMPLETED,
                {"stop_reason": "stop"},
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, client = await initialized_agent(root, [RunnerFixture(events=events)])
            session = await agent.new_session(str(root))
            response = await agent.prompt(
                session.session_id,
                [TextContentBlock(type="text", text="run")],
            )

        chunks = [update for _, update in client.updates if isinstance(update, AgentMessageChunk)]
        self.assertEqual([chunk.content.text for chunk in chunks], ["one", "two"])
        self.assertEqual(len({chunk.message_id for chunk in chunks}), 1)
        self.assertNotIn("hidden", repr(client.updates))

        tools = [
            update
            for _, update in client.updates
            if isinstance(update, (ToolCallStart, ToolCallProgress))
        ]
        self.assertEqual([tool.status for tool in tools], ["pending", "in_progress", "completed"])
        self.assertIsNone(tools[0].raw_input)
        self.assertIsNone(tools[-1].raw_output)
        self.assertNotIn("secretvalue", repr(tools))
        self.assertNotIn("not-forwarded", repr(tools))
        self.assertEqual(response.stop_reason, "end_turn")

    async def test_usage_synthesized_tool_start_and_stop_reason_mapping(self) -> None:
        events = (
            AgentEvent.create(
                1,
                AgentEventKind.CONTEXT_USAGE_UPDATED,
                {"used_tokens": 123},
            ),
            AgentEvent.create(
                2,
                AgentEventKind.TOOL_FAILED,
                {
                    "id": "missing-request",
                    "name": "read_file",
                    "content": "",
                },
            ),
            AgentEvent.create(
                3,
                AgentEventKind.TEXT_DELTA,
                {"text": "\u0001" + ("界" * 30_000)},
            ),
            AgentEvent.create(
                4,
                AgentEventKind.TURN_COMPLETED,
                {"stop_reason": "length"},
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, client = await initialized_agent(root, [RunnerFixture(events=events)])
            session = await agent.new_session(str(root))
            response = await agent.prompt(
                session.session_id,
                [TextContentBlock(type="text", text="run")],
            )

        usage = [
            update for _, update in client.updates if update.__class__.__name__ == "UsageUpdate"
        ]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].used, 123)
        tools = [
            update
            for _, update in client.updates
            if isinstance(update, (ToolCallStart, ToolCallProgress))
        ]
        self.assertEqual([tool.status for tool in tools], ["pending", "failed"])
        chunks = [update for _, update in client.updates if isinstance(update, AgentMessageChunk)]
        self.assertIn("\ufffd", chunks[0].content.text)
        self.assertLessEqual(len(chunks[0].content.text.encode("utf-8")), 64 * 1024)
        self.assertEqual(response.stop_reason, "max_tokens")

    async def test_refusal_and_error_stop_reasons_are_deterministic(self) -> None:
        refusal = RunnerFixture(
            events=(
                AgentEvent.create(
                    1,
                    AgentEventKind.TURN_COMPLETED,
                    {"stop_reason": "refusal"},
                ),
            )
        )
        max_steps = RunnerFixture(
            failure=ProviderError("agent exceeded the maximum of 2 model steps")
        )
        provider_failure = RunnerFixture(failure=ProviderError("provider unavailable"))
        prompt_failure = RunnerFixture(failure=RuntimeError("unexpected"))
        forwarded = RequestError.invalid_params({"reason": "sink_failure"})
        request_failure = RunnerFixture(failure=forwarded)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(
                root,
                [refusal, max_steps, provider_failure, prompt_failure, request_failure],
            )
            sessions = [await agent.new_session(str(root)) for _ in range(5)]
            refused = await agent.prompt(
                sessions[0].session_id,
                [TextContentBlock(type="text", text="refuse")],
            )
            limited = await agent.prompt(
                sessions[1].session_id,
                [TextContentBlock(type="text", text="limit")],
            )
            with self.assertRaises(RequestError) as provider:
                await agent.prompt(
                    sessions[2].session_id,
                    [TextContentBlock(type="text", text="provider")],
                )
            with self.assertRaises(RequestError) as generic:
                await agent.prompt(
                    sessions[3].session_id,
                    [TextContentBlock(type="text", text="generic")],
                )
            with self.assertRaises(RequestError) as direct:
                await agent.prompt(
                    sessions[4].session_id,
                    [TextContentBlock(type="text", text="request")],
                )

        self.assertEqual(refused.stop_reason, "refusal")
        self.assertEqual(limited.stop_reason, "max_turn_requests")
        self.assertEqual(provider.exception.data["reason"], "provider_failure")
        self.assertEqual(generic.exception.data["reason"], "prompt_failure")
        self.assertIs(direct.exception, forwarded)

    async def test_typed_execution_outcomes_take_priority_over_legacy_error_mapping(self) -> None:
        model_step_limited = RunnerFixture(
            outcome=AgentExecutionOutcome(
                AgentExecutionStatus.BUDGET_LIMITED,
                SupervisorReasonCode.MODEL_STEP_LIMIT,
                finalized=True,
                recoverable=True,
            )
        )
        stuck = RunnerFixture(
            outcome=AgentExecutionOutcome(
                AgentExecutionStatus.STUCK,
                SupervisorReasonCode.PERIODIC_CYCLE,
                finalized=True,
                recoverable=True,
            )
        )
        token_limited = RunnerFixture(
            outcome=AgentExecutionOutcome(
                AgentExecutionStatus.BUDGET_LIMITED,
                SupervisorReasonCode.OUTPUT_TOKEN_BUDGET,
                finalized=True,
                recoverable=True,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(
                root,
                [model_step_limited, stuck, token_limited],
            )
            sessions = [await agent.new_session(str(root)) for _ in range(3)]
            limited = await agent.prompt(
                sessions[0].session_id,
                [TextContentBlock(type="text", text="limit")],
            )
            stuck_response = await agent.prompt(
                sessions[1].session_id,
                [TextContentBlock(type="text", text="stuck")],
            )
            token_response = await agent.prompt(
                sessions[2].session_id,
                [TextContentBlock(type="text", text="tokens")],
            )

        self.assertEqual(limited.stop_reason, "max_turn_requests")
        self.assertEqual(stuck_response.stop_reason, "end_turn")
        self.assertEqual(token_response.stop_reason, "max_tokens")
        self.assertEqual(limited.field_meta["neuro_code.execution_status"], "budget_limited")
        self.assertEqual(limited.field_meta["neuro_code.execution_reason"], "model_step_limit")
        self.assertTrue(limited.field_meta["neuro_code.finalized"])
        self.assertTrue(limited.field_meta["neuro_code.recoverable"])
        self.assertEqual(stuck_response.field_meta["neuro_code.execution_status"], "stuck")
        self.assertNotIn("snapshot", repr(limited.field_meta))
        self.assertNotIn("digest", repr(limited.field_meta))

    async def test_non_budget_execution_outcome_keeps_the_existing_stop_reason(self) -> None:
        completed = RunnerFixture(
            outcome=AgentExecutionOutcome(
                AgentExecutionStatus.COMPLETED,
                None,
                finalized=False,
                recoverable=False,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(root, [completed])
            session = await agent.new_session(str(root))
            response = await agent.prompt(
                session.session_id,
                [TextContentBlock(type="text", text="complete")],
            )

        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(response.field_meta["neuro_code.execution_status"], "completed")
        self.assertEqual(response.field_meta["neuro_code.execution_reason"], "none")

    async def test_permission_approval_and_denial_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approved_runner = RunnerFixture(request_approval=True)
            denied_runner = RunnerFixture(request_approval=True)
            agent, _, client = await initialized_agent(
                root,
                [approved_runner, denied_runner],
            )
            approved_session = await agent.new_session(str(root))
            await agent.prompt(
                approved_session.session_id,
                [TextContentBlock(type="text", text="approve")],
            )
            client.permission_response = RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled")
            )
            denied_session = await agent.new_session(str(root))
            await agent.prompt(
                denied_session.session_id,
                [TextContentBlock(type="text", text="deny")],
            )

        self.assertTrue(approved_runner.approvals[0].allowed)
        self.assertFalse(denied_runner.approvals[0].allowed)
        self.assertEqual(len(client.permission_requests), 2)
        _, pending, options = client.permission_requests[0]
        self.assertEqual(pending.status, "pending")
        self.assertEqual(
            [option.kind for option in options],
            ["allow_once", "allow_always", "reject_once"],
        )

    async def test_session_approval_cache_unscoped_and_client_failure(self) -> None:
        cached = RunnerFixture(request_approval=True)
        unscoped = RunnerFixture(request_approval=True, approval_scope=None)
        failed = RunnerFixture(request_approval=True)
        unknown = RunnerFixture(request_approval=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, client = await initialized_agent(
                root,
                [cached, unscoped, failed, unknown],
            )
            client.permission_response = RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id="allow_session")
            )
            cached_session = await agent.new_session(str(root))
            await agent.prompt(
                cached_session.session_id,
                [TextContentBlock(type="text", text="first")],
            )
            await agent.prompt(
                cached_session.session_id,
                [TextContentBlock(type="text", text="same")],
            )
            unscoped_session = await agent.new_session(str(root))
            await agent.prompt(
                unscoped_session.session_id,
                [TextContentBlock(type="text", text="unscoped")],
            )

            client.permission_error = RuntimeError("client disconnected")
            failed_session = await agent.new_session(str(root))
            await agent.prompt(
                failed_session.session_id,
                [TextContentBlock(type="text", text="failure")],
            )
            client.permission_error = None
            client.permission_response = RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id="unknown-option")
            )
            unknown_session = await agent.new_session(str(root))
            await agent.prompt(
                unknown_session.session_id,
                [TextContentBlock(type="text", text="unknown")],
            )

        self.assertEqual(len(client.permission_requests), 4)
        self.assertEqual(cached.approvals[0].kind.value, "allow_session")
        self.assertEqual(cached.approvals[1].kind.value, "allow_session")
        self.assertEqual(unscoped.approvals[0].kind.value, "allow_once")
        self.assertFalse(failed.approvals[0].allowed)
        self.assertFalse(unknown.approvals[0].allowed)

    async def test_cancel_is_silent_and_same_session_can_run_again(self) -> None:
        runner = RunnerFixture(block=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(root, [runner])
            session = await agent.new_session(str(root))
            task = asyncio.create_task(
                agent.prompt(
                    session.session_id,
                    [TextContentBlock(type="text", text="wait")],
                )
            )
            await runner.wait_started()
            self.assertIsNone(await agent.cancel("unknown-session"))
            self.assertIsNone(await agent.cancel(session.session_id))
            response = await task
            runner.release()
            second = await agent.prompt(
                session.session_id,
                [TextContentBlock(type="text", text="again")],
            )

        self.assertEqual(response.stop_reason, "cancelled")
        self.assertEqual(second.stop_reason, "end_turn")

    async def test_wrapped_provider_cancellation_still_returns_cancelled(self) -> None:
        runner = RunnerFixture(block=True, wrap_cancellation=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = await initialized_agent(root, [runner])
            session = await agent.new_session(str(root))
            task = asyncio.create_task(
                agent.prompt(
                    session.session_id,
                    [TextContentBlock(type="text", text="wait")],
                )
            )
            await runner.wait_started()
            await agent.cancel(session.session_id)
            response = await task

        self.assertEqual(response.stop_reason, "cancelled")

    async def test_concurrent_prompt_close_and_cleanup_are_isolated(self) -> None:
        first = RunnerFixture(block=True)
        second = RunnerFixture(block=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [first, second])
            first_session = await agent.new_session(str(root))
            second_session = await agent.new_session(str(root))
            first_task = asyncio.create_task(
                agent.prompt(
                    first_session.session_id,
                    [TextContentBlock(type="text", text="first")],
                )
            )
            second_task = asyncio.create_task(
                agent.prompt(
                    second_session.session_id,
                    [TextContentBlock(type="text", text="second")],
                )
            )
            await first.wait_started()
            await second.wait_started()

            with self.assertRaises(RequestError) as busy:
                await agent.prompt(
                    first_session.session_id,
                    [TextContentBlock(type="text", text="concurrent")],
                )
            self.assertEqual(busy.exception.data["reason"], "prompt_already_active")

            await agent.close_session(first_session.session_id)
            first_response = await first_task
            self.assertFalse(second_task.done())
            self.assertEqual(application.background_scopes[0].shutdown_calls, 1)

            with self.assertRaises(RequestError):
                await agent.close_session(first_session.session_id)
            second.release()
            second_response = await second_task
            await agent.shutdown()

        self.assertEqual(first_response.stop_reason, "cancelled")
        self.assertEqual(second_response.stop_reason, "end_turn")
        self.assertEqual(application.background_scopes[1].shutdown_calls, 1)

    async def test_sdk_router_exposes_stable_session_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, application, _ = await initialized_agent(root, [])
            summary = SessionSummary(
                id="persisted-id",
                cwd=str(root),
                provider="fixture",
                model="fixture-model",
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
            application.store.summaries.append(summary)
            application.store.session_ids.add(summary.id)
            await application.store.bind_session_alias(
                "acp-v1",
                "acp-durable",
                summary.id,
            )

            result = await acp_module._build_acp_router(agent)(
                "session/delete",
                {"sessionId": "acp-durable"},
                False,
            )

        self.assertEqual(result, {})
        self.assertEqual(application.store.deleted_session_ids, ["persisted-id"])

    async def test_sdk_router_dispatches_private_artifact_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, _ = await initialized_artifact_agent(Path(directory))

            result = await acp_module._build_acp_router(agent)(
                "_" + ACP_TOOL_OUTPUT_ARTIFACT_EXTENSION,
                {"sessionId": "acp-artifacts", "limit": 1},
                False,
            )

        self.assertEqual(result["artifacts"][0]["artifactId"], "a" * 32)

    async def test_serve_uses_official_sdk_streams_and_always_closes_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = ApplicationFixture(Path(directory), [])
            connection = AsyncMock()
            with (
                patch(
                    "neuro_code.acp.stdio_streams",
                    new=AsyncMock(return_value=(object(), object())),
                ) as streams,
                patch(
                    "neuro_code.acp._AcpSdkConnection",
                    return_value=connection,
                ) as connection_type,
            ):
                await serve_acp(_acp_service(application))

        streams.assert_awaited_once_with(limit=ACP_STDIO_BUFFER_LIMIT_BYTES)
        connection_type.assert_called_once()
        connection.listen.assert_awaited_once_with()
        connection.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
