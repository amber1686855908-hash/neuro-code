from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from acp.exceptions import RequestError
from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    ImageContentBlock,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    ResourceContentBlock,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)

from neuro_code.acp import (
    ACP_STDIO_BUFFER_LIMIT_BYTES,
    MAX_ANNOTATION_AUDIENCE,
    MAX_PROMPT_BLOCKS,
    MAX_PROMPT_BYTES,
    MAX_RESOURCE_LINKS,
    MAX_TEXT_BLOCK_BYTES,
    MAX_TEXT_BLOCKS,
    NeuroCodeAcpAgent,
    convert_prompt_content,
    serve_acp,
)
from neuro_code.application import ApplicationComposition
from neuro_code.config import AppConfig, ProviderProfile
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import Message, Role, SessionItem
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelEvent
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ProviderError
from neuro_code.permissions import PermissionApproval, PermissionRequest
from neuro_code.ports.approval import PermissionApprover
from neuro_code.ports.model import ModelProvider
from neuro_code.runtime.agent import AgentRunResult, EventSink
from neuro_code.runtime.profile_conversation import ConversationBinding


class AcpClientFixture:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.permission_requests: list[tuple[str, ToolCallUpdate, list[PermissionOption]]] = []
        self.permission_response = RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id="allow_once")
        )
        self.permission_error: Exception | None = None

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
    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


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
    ) -> None:
        self._session_id: str | None = None
        self._events = tuple(events)
        self._block = block
        self._request_approval = request_approval
        self._approval_scope = approval_scope
        self._failure = failure
        self._wrap_cancellation = wrap_cancellation
        self._approver: PermissionApprover | None = None
        self._started = asyncio.Event()
        self._release = asyncio.Event()
        self.approvals: list[PermissionApproval] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def items(self) -> tuple[SessionItem, ...]:
        return ()

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
    ) -> AgentRunResult:
        self._session_id = "internal-session"
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
        return AgentRunResult(
            self._session_id,
            prompt,
            (Message(Role.USER, prompt),),
            (Message(Role.USER, prompt),),
            tuple(events),
            1,
        )

    def attach_approver(self, approver: PermissionApprover | None) -> None:
        self._approver = approver

    async def wait_started(self) -> None:
        await self._started.wait()

    def release(self) -> None:
        self._release.set()


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

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None = None,
        **_kwargs: Any,
    ) -> ConversationBinding:
        runner = self._runners.pop(0)
        runner.attach_approver(approver)
        background = BackgroundTasksFixture()
        self.background_scopes.append(background)
        return ConversationBinding(
            runner,
            cast(ModelProvider, ProviderFixture()),
            cast(Any, background),
        )


async def initialized_agent(
    root: Path,
    runners: Sequence[RunnerFixture],
) -> tuple[NeuroCodeAcpAgent, ApplicationFixture, AcpClientFixture]:
    application = ApplicationFixture(root, runners)
    agent = NeuroCodeAcpAgent(cast(ApplicationComposition, application))
    client = AcpClientFixture()
    agent.on_connect(cast(Client, client))
    await agent.initialize(
        1,
        ClientCapabilities(terminal=True),
        Implementation(name="fixture-client", version="1.0"),
    )
    return agent, application, client


class PromptContentTests(unittest.TestCase):
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
        self.assertLess(converted.index("before"), converted.index("resource_link"))
        self.assertLess(converted.index("resource_link"), converted.index("after"))
        self.assertNotIn("resource-secret", converted)
        self.assertNotIn("annotation-secret", converted)
        self.assertIn('"audience":["assistant"]', converted)

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
        self.assertIn(target.as_uri(), converted)
        self.assertNotIn("CONTENT-MUST-NOT-BE-READ", converted)

    def test_unsupported_and_oversized_content_is_rejected(self) -> None:
        with self.assertRaises(RequestError) as image_error:
            convert_prompt_content(
                [
                    ImageContentBlock(
                        type="image",
                        data="AA==",
                        mime_type="image/png",
                    )
                ]
            )
        self.assertEqual(image_error.exception.data["reason"], "unsupported_prompt_content")

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
        self.assertNotIn("\u0001", converted)
        self.assertIn("\ufffd", converted)
        self.assertIn('"mimeType":"text/plain"', converted)
        self.assertIn('"lastModified":"2026-01-01"', converted)
        self.assertIn('"size":12', converted)


class AcpAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_declares_only_close_and_saves_client_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [])
            agent = NeuroCodeAcpAgent(cast(ApplicationComposition, application))
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
            {"sessionCapabilities": {"close": {}}},
        )

    async def test_initialize_negotiates_v1_and_rejects_duplicate_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = ApplicationFixture(Path(directory), [])
            agent = NeuroCodeAcpAgent(cast(ApplicationComposition, application))
            response = await agent.initialize(99)
            with self.assertRaises(RequestError) as duplicate:
                await agent.initialize(1)

        self.assertEqual(response.protocol_version, 1)
        self.assertEqual(duplicate.exception.data["reason"], "already_initialized")

    async def test_requests_require_initialize_and_active_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = ApplicationFixture(root, [])
            agent = NeuroCodeAcpAgent(cast(ApplicationComposition, application))
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

    async def test_session_new_validates_workspace_and_keeps_stable_acp_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = RunnerFixture()
            agent, _, _ = await initialized_agent(root, [runner])

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
                "additional_directories_unsupported",
            )

            with self.assertRaises(RequestError) as mcp:
                await agent.new_session(str(root), mcp_servers=[cast(Any, object())])
            self.assertEqual(mcp.exception.data["reason"], "mcp_servers_unsupported")

            created = await agent.new_session(
                str(root),
                additional_directories=[],
                mcp_servers=[],
            )
            acp_id = created.session_id
            response = await agent.prompt(
                acp_id,
                [TextContentBlock(type="text", text="hello")],
            )

        self.assertTrue(acp_id.startswith("acp-"))
        self.assertNotEqual(acp_id, runner.session_id)
        self.assertEqual(response.stop_reason, "end_turn")

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

    async def test_serve_uses_official_sdk_settings_and_always_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = ApplicationFixture(Path(directory), [])
            with patch("neuro_code.acp.run_agent", new_callable=AsyncMock) as run:
                await serve_acp(cast(ApplicationComposition, application))

        self.assertTrue(run.await_args.kwargs["use_unstable_protocol"])
        self.assertEqual(
            run.await_args.kwargs["stdio_buffer_limit_bytes"],
            ACP_STDIO_BUFFER_LIMIT_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
