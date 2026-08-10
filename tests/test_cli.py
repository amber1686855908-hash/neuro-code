from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from neuro_code.application.execution_policy import ExecutionProfile
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionCommandResult,
    ContextCompactionCommandStatus,
)
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.provider_settings import ManagedProviderProfile
from neuro_code.application.providers import ChangeProviderRequest, ProviderChangeService
from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.sessions import (
    ResumeSessionRequest,
    SessionSelectionService,
    SessionTurnService,
)
from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipAction,
    SubagentRelationshipActionRequest,
    SubagentRelationshipActionResult,
)
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.tools import SessionToolOutputArtifactApplicationService
from neuro_code.application.workflows import (
    PlanExecutionService,
    PlanSchedulingService,
    QueuedPlanExecutionService,
    ReadOnlySubagentApplicationService,
    RunSubagentRequest,
    SubagentResultProjection,
)
from neuro_code.application.workflows.subagent import MAX_SUBAGENT_STEPS
from neuro_code.bootstrap.entrypoints import main
from neuro_code.cli import (
    _application_settings,
    _execution_control_mode,
    _normalize_rule,
    _run_agent,
    _run_subagent,
    _run_subagent_lifecycle,
    build_parser,
    run,
)
from neuro_code.configuration.app import AppConfig
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import Message, Role, ToolCall
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SessionExecutionRecord,
    SupervisorReasonCode,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.session_tasks import SessionTaskStatus
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.providers.provider_catalog import HttpProviderCatalog
from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore
from neuro_code.interfaces.cli.serialization import (
    serialize_context_compaction_result,
    serialize_execution_outcome,
    serialize_execution_record,
    serialize_subagent_relationship_action,
    serialize_subagent_result,
)
from neuro_code.shared.errors import ConfigurationError, ProviderError
from neuro_code.shared.ui_language import UiLanguage


class CliProvider:
    provider_name = "cli-fixture"
    model_name = "fixture-model"

    def __init__(self) -> None:
        self.contexts: list[ModelContext] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tools, tool_policy
        self.contexts.append(context)
        yield ModelTextDelta("fixture response")
        yield ModelCompleted("stop", 2, 3)


class FinalizingCliProvider:
    provider_name = "cli-finalizer-fixture"
    model_name = "fixture-model"

    def __init__(self, *, normal_tool_calls: int, fail_finalizer: bool = False) -> None:
        self._normal_tool_calls = normal_tool_calls
        self._fail_finalizer = fail_finalizer
        self._normal_calls = 0
        self.tool_policies: list[ModelToolPolicy] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        self.tool_policies.append(tool_policy)
        if tool_policy is ModelToolPolicy.ALLOWED:
            self._normal_calls += 1
            if self._normal_calls > self._normal_tool_calls:
                raise AssertionError("unexpected ordinary model request")
            yield ModelToolCall(
                ToolCall(
                    f"list-{self._normal_calls}",
                    "list_dir",
                    {"path": "."},
                )
            )
            yield ModelCompleted("tool_calls")
            return
        if self._fail_finalizer:
            raise ProviderError("finalizer provider failed")
        yield ModelTextDelta("finalized fixture response")
        yield ModelCompleted("stop")


class BackgroundTaskSupervisorFixture:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def open_scope(self) -> BackgroundTaskSupervisorFixture:
        return self

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class CliApplicationRunnerFixture:
    session_id = "session-fixture"

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def run(
        self,
        prompt: str,
        *,
        sink=None,
        content_parts=(),
        cancellation_policy=None,
        turn_source=None,
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
        event = AgentEvent.create(1, AgentEventKind.TEXT_DELTA, {"text": "fixture response"})
        if sink is not None:
            await sink(event)
        return AgentRunResult("session-fixture", "fixture response", (), (), (event,), 1)


class CliApplicationSessionFixture:
    def __init__(self, runner: CliApplicationRunnerFixture, operations: list[str]) -> None:
        self.runner = runner
        self.operations = operations
        self.resume_requests: list[ResumeSessionRequest] = []
        self.bound_runners: list[object] = []

    async def prepare_resume(self, request: ResumeSessionRequest) -> None:
        self.operations.append("prepare_resume")
        self.resume_requests.append(request)

    def bind_runner(self, runner: CliApplicationRunnerFixture) -> SessionTurnService:
        self.operations.append("bind_runner")
        self.bound_runners.append(runner)
        return SessionTurnService(runner)


class CliApplicationFixture:
    def __init__(self) -> None:
        self.runner = CliApplicationRunnerFixture()
        self.operations: list[str] = []
        self.session_service = CliApplicationSessionFixture(self.runner, self.operations)
        self.created_resume_ids: list[str | None] = []
        self.close_calls = 0

    async def create_binding(
        self,
        *,
        resume_id: str | None = None,
        user_interaction: object | None = None,
    ) -> SimpleNamespace:
        del user_interaction
        self.operations.append("create_binding")
        self.created_resume_ids.append(resume_id)
        return SimpleNamespace(runner=self.runner)

    async def close(self) -> None:
        self.close_calls += 1


class CliSubagentServiceFixture:
    def __init__(self, projection: SubagentResultProjection) -> None:
        self.projection = projection
        self.requests: list[RunSubagentRequest] = []

    async def run_subagent(self, request: RunSubagentRequest) -> SubagentResultProjection:
        self.requests.append(request)
        return self.projection


class CliSubagentApplicationFixture:
    def __init__(self, projection: SubagentResultProjection) -> None:
        self.subagent_service = CliSubagentServiceFixture(projection)
        self.resume_checks: list[str] = []
        self.close_calls = 0

    async def config_for_session_resume(self, session_id: str) -> None:
        self.resume_checks.append(session_id)

    def create_read_only_subagent_application_service(
        self,
    ) -> ReadOnlySubagentApplicationService:
        return cast(ReadOnlySubagentApplicationService, self.subagent_service)

    async def close(self) -> None:
        self.close_calls += 1


class CliSubagentServicesFixture:
    def __init__(self, application: CliSubagentApplicationFixture) -> None:
        self.application = application

    async def open_application(
        self, settings: ApplicationSettings
    ) -> CliSubagentApplicationFixture:
        del settings
        return self.application


class CliSubagentLifecycleServiceFixture:
    def __init__(self) -> None:
        self.requests: list[SubagentRelationshipActionRequest] = []

    async def execute(
        self,
        request: SubagentRelationshipActionRequest,
    ) -> SubagentRelationshipActionResult:
        self.requests.append(request)
        return SubagentRelationshipActionResult(
            parent_session_id=request.parent_session_id,
            parent_task_id=request.parent_task_id,
            child_session_id="child-session",
            action=request.action,
            forked_session_id=(
                "forked-session" if request.action is SubagentRelationshipAction.FORK else None
            ),
        )


class CliSubagentLifecycleApplicationFixture:
    def __init__(self) -> None:
        self.lifecycle = CliSubagentLifecycleServiceFixture()
        self.resume_checks: list[str] = []
        self.close_calls = 0

    async def config_for_session_resume(self, session_id: str) -> None:
        self.resume_checks.append(session_id)

    def create_subagent_relationship_lifecycle_service(
        self,
    ) -> CliSubagentLifecycleServiceFixture:
        return self.lifecycle

    async def close(self) -> None:
        self.close_calls += 1


class CliSubagentLifecycleServicesFixture:
    def __init__(self, application: CliSubagentLifecycleApplicationFixture) -> None:
        self.application = application

    async def open_application(
        self,
        settings: ApplicationSettings,
    ) -> CliSubagentLifecycleApplicationFixture:
        del settings
        return self.application


class CliServicesFixture:
    def __init__(self, application: CliApplicationFixture) -> None:
        self.application = application

    async def open_application(self, settings: ApplicationSettings) -> CliApplicationFixture:
        del settings
        return self.application


class CliTests(unittest.TestCase):
    @staticmethod
    def _write_provider_config(state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "config.toml").write_text(
            """
[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
""",
            encoding="utf-8",
        )

    @classmethod
    def _run_finalizing_agent(
        cls,
        root: Path,
        provider: FinalizingCliProvider,
        *arguments: str,
    ) -> tuple[int, str, str]:
        state = root / "state"
        cls._write_provider_config(state)
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ),
            patch(
                "neuro_code.bootstrap.composition.create_routed_provider",
                return_value=provider,
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            exit_code = main(arguments)
        return exit_code, output.getvalue(), errors.getvalue()

    def test_cli_execution_control_defaults_to_finalize_terminal(self) -> None:
        settings = _application_settings(build_parser().parse_args(("-p", "answer")))

        self.assertIs(settings.execution_control_mode, ExecutionControlMode.FINALIZE_TERMINAL)
        self.assertIs(settings.execution_profile, ExecutionProfile.NORMAL)
        self.assertEqual(settings.max_steps, 48)

    def test_cli_execution_profile_is_shared_by_agent_tui_and_acp(self) -> None:
        parser = build_parser()
        settings = tuple(
            _application_settings(parser.parse_args(arguments))
            for arguments in (
                ("agent", "-p", "answer", "--execution-profile", "deep"),
                ("code", "--execution-profile", "deep"),
                ("acp", "--execution-profile", "deep"),
            )
        )

        self.assertTrue(all(item.execution_profile is ExecutionProfile.DEEP for item in settings))
        self.assertTrue(all(item.execution_budget.max_model_calls == 96 for item in settings))
        self.assertTrue(all(item.execution_budget.max_tool_rounds == 96 for item in settings))
        self.assertTrue(all(item.execution_budget.max_tool_calls == 384 for item in settings))

    def test_cli_max_steps_compatibility_override_scales_complete_budget(self) -> None:
        settings = _application_settings(
            build_parser().parse_args(
                ("agent", "-p", "answer", "--execution-profile", "deep", "--max-steps", "60")
            )
        )

        self.assertEqual(settings.max_steps, 60)
        self.assertEqual(settings.execution_budget.max_model_calls, 60)
        self.assertEqual(settings.execution_budget.max_tool_rounds, 60)
        self.assertEqual(settings.execution_budget.max_tool_calls, 240)

    def test_subagent_parser_requires_parent_and_bounds_steps(self) -> None:
        args = build_parser().parse_args(
            ("subagent", "inspect repository", "--parent-session", "parent-session")
        )

        self.assertEqual(args.parent_session, "parent-session")
        self.assertEqual(args.max_steps, 8)
        self.assertFalse(args.json)
        with self.assertRaises(SystemExit) as error:
            build_parser().parse_args(
                (
                    "subagent",
                    "inspect repository",
                    "--parent-session",
                    "parent-session",
                    "--max-steps",
                    str(MAX_SUBAGENT_STEPS + 1),
                )
            )
        self.assertEqual(error.exception.code, 2)

    def test_subagents_lifecycle_parser_requires_parent_and_action(self) -> None:
        args = build_parser().parse_args(
            (
                "subagents",
                "resume",
                "subagent-task",
                "--parent-session",
                "parent-session",
            )
        )

        self.assertEqual(args.action, "resume")
        self.assertEqual(args.task_id, "subagent-task")
        self.assertEqual(args.parent_session, "parent-session")
        self.assertFalse(args.json)
        with self.assertRaises(SystemExit) as error:
            build_parser().parse_args(
                (
                    "subagents",
                    "resume",
                    "subagent-task",
                )
            )
        self.assertEqual(error.exception.code, 2)

    def test_subagents_lifecycle_plain_output_is_bounded_and_does_not_run_model(self) -> None:
        application = CliSubagentLifecycleApplicationFixture()
        args = build_parser().parse_args(
            (
                "subagents",
                "resume",
                "subagent-task",
                "--parent-session",
                "parent-session",
            )
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = asyncio.run(
                _run_subagent_lifecycle(
                    args,
                    CliSubagentLifecycleServicesFixture(application),
                )
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "Child session child-session is ready to resume.\n")
        self.assertEqual(application.resume_checks, ["parent-session"])
        self.assertEqual(
            application.lifecycle.requests,
            [
                SubagentRelationshipActionRequest(
                    "parent-session",
                    "subagent-task",
                    SubagentRelationshipAction.RESUME,
                )
            ],
        )
        self.assertEqual(application.close_calls, 1)

    def test_subagents_lifecycle_json_output_uses_bounded_serializer(self) -> None:
        application = CliSubagentLifecycleApplicationFixture()
        args = build_parser().parse_args(
            (
                "subagents",
                "fork",
                "subagent-task",
                "--parent-session",
                "parent-session",
                "--json",
            )
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = asyncio.run(
                _run_subagent_lifecycle(
                    args,
                    CliSubagentLifecycleServicesFixture(application),
                )
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "parent_session_id": "parent-session",
                "parent_task_id": "subagent-task",
                "child_session_id": "child-session",
                "action": "fork",
                "forked_session_id": "forked-session",
            },
        )
        self.assertNotIn("prompt", output.getvalue())
        self.assertEqual(
            serialize_subagent_relationship_action(
                SubagentRelationshipActionResult(
                    "parent-session",
                    "subagent-task",
                    "child-session",
                    SubagentRelationshipAction.FORK,
                    "forked-session",
                )
            ),
            json.loads(output.getvalue()),
        )

    def test_explicit_subagent_plain_output_is_only_the_safe_response(self) -> None:
        projection = SubagentResultProjection(
            parent_session_id="parent-session",
            task_id="subagent-task",
            child_session_id="child-session",
            status=SessionTaskStatus.COMPLETED,
            response="read-only repository answer",
            steps=2,
            truncated=False,
        )
        application = CliSubagentApplicationFixture(projection)
        args = build_parser().parse_args(
            ("subagent", "inspect repository", "--parent-session", "parent-session")
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = asyncio.run(_run_subagent(args, CliSubagentServicesFixture(application)))

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "read-only repository answer\n")
        self.assertEqual(application.resume_checks, ["parent-session"])
        self.assertEqual(
            application.subagent_service.requests,
            [RunSubagentRequest("parent-session", "inspect repository", max_steps=8)],
        )
        self.assertEqual(application.close_calls, 1)

    def test_explicit_subagent_json_output_is_bounded_and_typed(self) -> None:
        projection = SubagentResultProjection(
            parent_session_id="parent-session",
            task_id="subagent-task",
            child_session_id="child-session",
            status=SessionTaskStatus.COMPLETED,
            response="bounded answer",
            steps=3,
            truncated=True,
            outcome=AgentExecutionOutcome(
                AgentExecutionStatus.BUDGET_LIMITED,
                SupervisorReasonCode.MODEL_STEP_LIMIT,
                finalized=True,
                recoverable=True,
            ),
        )
        application = CliSubagentApplicationFixture(projection)
        args = build_parser().parse_args(
            (
                "subagent",
                "inspect repository",
                "--parent-session",
                "parent-session",
                "--max-steps",
                "3",
                "--json",
            )
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = asyncio.run(_run_subagent(args, CliSubagentServicesFixture(application)))

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["parent_session_id"], "parent-session")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["steps"], 3)
        self.assertEqual(
            payload["outcome"],
            {
                "status": "budget_limited",
                "reason": "model_step_limit",
                "finalized": True,
                "recoverable": True,
            },
        )
        self.assertNotIn("prompt", payload)
        self.assertNotIn("messages", payload)
        self.assertNotIn("tool_arguments", payload)

    def test_subagent_command_dispatches_through_cli_run(self) -> None:
        projection = SubagentResultProjection(
            parent_session_id="parent-session",
            task_id="subagent-task",
            child_session_id="child-session",
            status=SessionTaskStatus.COMPLETED,
            response="dispatched response",
            steps=1,
            truncated=False,
        )
        application = CliSubagentApplicationFixture(projection)
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                (
                    "subagent",
                    "inspect repository",
                    "--parent-session",
                    "parent-session",
                ),
                services=CliSubagentServicesFixture(application),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "dispatched response\n")
        self.assertEqual(application.resume_checks, ["parent-session"])
        self.assertEqual(application.close_calls, 1)

    def test_subagent_serializer_omits_child_internal_state(self) -> None:
        projection = SubagentResultProjection(
            parent_session_id="parent-session",
            task_id="subagent-task",
            child_session_id="child-session",
            status=SessionTaskStatus.FAILED,
            response="safe failure summary",
            steps=1,
            truncated=False,
        )

        serialized = serialize_subagent_result(projection)

        self.assertEqual(serialized["status"], "failed")
        self.assertNotIn("events", serialized)
        self.assertNotIn("items", serialized)
        self.assertNotIn("arguments", serialized)

    def test_cli_serialization_helpers_keep_bounded_execution_projection(self) -> None:
        outcome = AgentExecutionOutcome(
            status=AgentExecutionStatus.BUDGET_LIMITED,
            reason_code=SupervisorReasonCode.MODEL_STEP_LIMIT,
            finalized=True,
            recoverable=True,
        )
        self.assertEqual(
            serialize_execution_outcome(outcome),
            {
                "status": "budget_limited",
                "reason": "model_step_limit",
                "finalized": True,
                "recoverable": True,
            },
        )
        self.assertIsNone(serialize_execution_outcome(None))

        record = SessionExecutionRecord(
            outcome=outcome,
            event_sequence=3,
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        serialized_record = serialize_execution_record(record)
        self.assertIsNotNone(serialized_record)
        assert serialized_record is not None
        self.assertEqual(serialized_record["completed_at"], "2026-01-01T00:00:00+00:00")
        self.assertNotIn("event_sequence", serialized_record)

    def test_cli_serializes_compaction_result_without_internal_context(self) -> None:
        result = ContextCompactionCommandResult(
            status=ContextCompactionCommandStatus.COMPLETED,
            triggered=True,
            compaction_id="compaction-1",
            source_item_count=12,
            candidate_item_count=8,
            summary_tokens=24,
            summary_truncated=False,
        )

        serialized = serialize_context_compaction_result(result)

        self.assertEqual(serialized["status"], "completed")
        self.assertEqual(serialized["compaction_id"], "compaction-1")
        self.assertNotIn("summary", serialized)
        self.assertNotIn("source_fingerprint", serialized)
        self.assertNotIn("prompt", serialized)

    def test_cli_accepts_observe_only_execution_control_for_agent_acp_and_tui(self) -> None:
        parser = build_parser()
        agent_settings = _application_settings(
            parser.parse_args(("agent", "-p", "answer", "--execution-control", "observe-only"))
        )
        acp_settings = _application_settings(
            parser.parse_args(("acp", "--execution-control", "observe-only"))
        )
        tui_settings = _application_settings(
            parser.parse_args(("code", "--execution-control", "observe-only"))
        )

        self.assertIs(agent_settings.execution_control_mode, ExecutionControlMode.OBSERVE_ONLY)
        self.assertIs(acp_settings.execution_control_mode, ExecutionControlMode.OBSERVE_ONLY)
        self.assertIs(tui_settings.execution_control_mode, ExecutionControlMode.OBSERVE_ONLY)

    def test_cli_rejects_unknown_execution_control(self) -> None:
        with self.assertRaises(SystemExit) as error:
            build_parser().parse_args(("agent", "-p", "answer", "--execution-control", "unsafe"))

        self.assertEqual(error.exception.code, 2)

    def test_headless_agent_uses_application_turn_service_for_new_session(self) -> None:
        application = CliApplicationFixture()
        services = CliServicesFixture(application)
        args = build_parser().parse_args(("-p", "answer"))
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = asyncio.run(_run_agent(args, services))

        self.assertEqual(exit_code, 0)
        self.assertEqual(application.operations, ["create_binding", "bind_runner"])
        self.assertEqual(application.created_resume_ids, [None])
        self.assertEqual(application.session_service.bound_runners, [application.runner])
        self.assertEqual(application.runner.calls[0][0], "answer")
        self.assertEqual(output.getvalue(), "fixture response\n")
        self.assertEqual(application.close_calls, 1)

    def test_headless_agent_preflights_resume_before_binding_runner(self) -> None:
        application = CliApplicationFixture()
        application.runner.session_id = "session-resume"
        services = CliServicesFixture(application)
        args = build_parser().parse_args(("-p", "continue", "--resume", "session-resume"))
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = asyncio.run(_run_agent(args, services))

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            application.operations,
            ["prepare_resume", "create_binding", "bind_runner"],
        )
        self.assertEqual(
            application.session_service.resume_requests,
            [ResumeSessionRequest("session-resume")],
        )
        self.assertEqual(application.created_resume_ids, ["session-resume"])
        self.assertEqual(application.session_service.bound_runners, [application.runner])
        self.assertEqual(application.runner.calls[0][0], "continue")
        self.assertEqual(output.getvalue(), "fixture response\n")
        self.assertEqual(application.close_calls, 1)

    def test_execution_control_conversion_fails_closed_outside_argparse(self) -> None:
        for value in ("unsafe", object()):
            with (
                self.subTest(value_type=type(value).__name__),
                self.assertRaisesRegex(
                    ConfigurationError,
                    "execution control selection",
                ),
            ):
                _execution_control_mode(value)

    def test_tui_launch_receives_execution_control_setting(self) -> None:
        launch = AsyncMock(return_value=0)
        with patch("neuro_code.bootstrap.entrypoints.BootstrapCliServices.run_tui", launch):
            exit_code = main(("code", "--execution-control", "observe-only"))

        self.assertEqual(exit_code, 0)
        settings = launch.await_args.args[1]
        self.assertIs(settings.execution_control_mode, ExecutionControlMode.OBSERVE_ONLY)

    def test_plain_output_prints_a_finalized_response_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, errors = self._run_finalizing_agent(
                Path(directory),
                FinalizingCliProvider(normal_tool_calls=1),
                "-p",
                "summarize",
                "--max-steps",
                "1",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(output.count("finalized fixture response"), 1)

    def test_json_output_contains_budget_limited_outcome_without_internal_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, errors = self._run_finalizing_agent(
                Path(directory),
                FinalizingCliProvider(normal_tool_calls=1),
                "-p",
                "summarize",
                "--max-steps",
                "1",
                "--output-format",
                "json",
            )

        payload = json.loads(output)
        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(
            payload["outcome"],
            {
                "status": "budget_limited",
                "reason": "model_step_limit",
                "finalized": True,
                "recoverable": True,
            },
        )
        self.assertNotIn("snapshot", repr(payload))
        self.assertNotIn("digest", repr(payload))

    def test_json_output_contains_stuck_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, errors = self._run_finalizing_agent(
                Path(directory),
                FinalizingCliProvider(normal_tool_calls=4),
                "-p",
                "repeat",
                "--max-steps",
                "4",
                "--output-format",
                "json",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output)["outcome"]["status"], "stuck")

    def test_jsonl_preserves_event_protocol_and_terminal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, errors = self._run_finalizing_agent(
                Path(directory),
                FinalizingCliProvider(normal_tool_calls=1),
                "-p",
                "summarize",
                "--max-steps",
                "1",
                "--output-format",
                "jsonl",
            )

        records = [json.loads(line) for line in output.splitlines()]
        text_records = [record for record in records if record["kind"] == "text_delta"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(records[-1]["kind"], "turn_completed")
        self.assertEqual(records[-1]["data"]["execution_status"], "budget_limited")
        self.assertEqual(
            [record["data"]["text"] for record in text_records], ["finalized fixture response"]
        )

    def test_observe_only_preserves_the_legacy_max_step_provider_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, errors = self._run_finalizing_agent(
                Path(directory),
                FinalizingCliProvider(normal_tool_calls=1),
                "-p",
                "summarize",
                "--max-steps",
                "1",
                "--execution-control",
                "observe-only",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(output, "")
        self.assertIn("agent exceeded the maximum of 1 model steps", errors)

    def test_finalizer_provider_error_keeps_a_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, errors = self._run_finalizing_agent(
                Path(directory),
                FinalizingCliProvider(normal_tool_calls=1, fail_finalizer=True),
                "-p",
                "summarize",
                "--max-steps",
                "1",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(output, "")
        self.assertIn("finalizer provider failed", errors)

    def test_native_bash_permission_patterns_are_normalized(self) -> None:
        self.assertEqual(_normalize_rule("Bash"), "bash:*")
        self.assertEqual(_normalize_rule("Bash(*)"), "bash:*")
        self.assertEqual(_normalize_rule("Bash(git:*)"), "bash:git*")
        self.assertEqual(_normalize_rule("Bash(git status)"), "bash:git status")

    def test_headless_effort_flag_reaches_the_model_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)
            provider = CliProvider()
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch("neuro_code.bootstrap.composition.enforce_configured_sandbox"),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider", return_value=provider
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    (
                        "-p",
                        "answer directly",
                        "--cwd",
                        str(root),
                        "--effort",
                        "low",
                    )
                )

            self.assertEqual(exit_code, 0)
            self.assertIs(provider.contexts[0].reasoning_effort, ReasoningEffort.LOW)
            system = next(
                message for message in provider.contexts[0].messages if message.role is Role.SYSTEM
            )
            self.assertIn("low review depth", system.content)

    def test_version_json_is_machine_readable(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(("version", "--json"))
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload, {"name": "neuro-code", "version": "0.1.0.dev0"})

    def test_inspect_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_provider_config(root / "state")
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(root / "state"),
                        "FIXTURE_KEY": "never-print-this",
                    },
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(("inspect", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertNotIn("never-print-this", output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["provider"]["credential_configured"])
            self.assertEqual(payload["sandbox"], {"profile": "off", "source": "default"})

    def test_run_sandbox_flag_is_enforced_before_runtime_composition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)
            output = io.StringIO()
            shell_sandbox = object()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch("neuro_code.bootstrap.composition.enforce_configured_sandbox") as enforce,
                patch(
                    "neuro_code.bootstrap.composition.create_shell_sandbox",
                    return_value=shell_sandbox,
                ) as create_sandbox,
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=CliProvider(),
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    (
                        "-p",
                        "hello",
                        "--cwd",
                        str(root),
                        "--sandbox",
                        "workspace",
                    )
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("fixture response", output.getvalue())
            enforce.assert_called_once()
            self.assertIs(enforce.call_args.args[0], SandboxProfile.WORKSPACE)
            self.assertEqual(enforce.call_args.args[1], root.resolve())
            self.assertEqual(enforce.call_args.args[2], state.resolve())
            self.assertIn("--sandbox", enforce.call_args.args[3])
            create_sandbox.assert_called_once_with(
                SandboxProfile.WORKSPACE,
                root.resolve(),
                state.resolve(),
            )

    def test_resume_restores_saved_sandbox_and_rejects_explicit_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)
            store = SqliteSessionStore(state / "sessions.db")
            asyncio.run(store.initialize())
            session_id = asyncio.run(
                store.create_session(
                    str(root),
                    "cli-fixture",
                    "fixture-model",
                    sandbox_profile=SandboxProfile.WORKSPACE,
                )
            )
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(state),
                "FIXTURE_KEY": "fixture-key",
            }
            output = io.StringIO()
            shell_sandbox = object()
            with (
                patch.dict("os.environ", environment, clear=True),
                patch("neuro_code.bootstrap.composition.enforce_configured_sandbox") as enforce,
                patch(
                    "neuro_code.bootstrap.composition.create_shell_sandbox",
                    return_value=shell_sandbox,
                ) as create_sandbox,
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=CliProvider(),
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    (
                        "-p",
                        "resume safely",
                        "--cwd",
                        str(root),
                        "--resume",
                        session_id,
                    )
                )

            self.assertEqual(exit_code, 0)
            enforce.assert_called_once()
            self.assertIs(enforce.call_args.args[0], SandboxProfile.WORKSPACE)
            create_sandbox.assert_called_once_with(
                SandboxProfile.WORKSPACE,
                root.resolve(),
                state.resolve(),
            )

            error_output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                patch(
                    "neuro_code.bootstrap.composition.enforce_configured_sandbox"
                ) as conflicting_enforce,
                redirect_stderr(error_output),
            ):
                conflict_code = main(
                    (
                        "-p",
                        "do not weaken or strengthen silently",
                        "--cwd",
                        str(root),
                        "--resume",
                        session_id,
                        "--sandbox",
                        "strict",
                    )
                )

            self.assertEqual(conflict_code, 2)
            self.assertIn("created with 'workspace'", error_output.getvalue())
            conflicting_enforce.assert_not_called()

    def test_plain_inspect_version_and_completion_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arguments, expected in (
                (("version",), "neuro-code"),
                (("inspect", "--cwd", str(root)), "provider: (not configured)"),
                (("completions", "bash"), "complete -F"),
                (("completions", "zsh"), "#compdef"),
                (("completions", "fish"), "complete -c"),
                (("completions", "powershell"), "Register-ArgumentCompleter"),
            ):
                output = io.StringIO()
                with (
                    patch.dict(
                        "os.environ",
                        {"HOME": str(root), "NEURO_CODE_HOME": str(root / "state")},
                        clear=True,
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = main(arguments)
                self.assertEqual(exit_code, 0)
                self.assertIn(expected, output.getvalue())
                if arguments[0] == "completions":
                    self.assertIn("subagent", output.getvalue())

    def test_headless_plain_json_and_jsonl_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for output_format in ("plain", "json", "jsonl"):
                self._write_provider_config(root / output_format)
                output = io.StringIO()
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "NEURO_CODE_HOME": str(root / output_format),
                            "HOME": str(root),
                            "FIXTURE_KEY": "fixture-key",
                        },
                        clear=True,
                    ),
                    patch(
                        "neuro_code.bootstrap.composition.create_routed_provider",
                        return_value=CliProvider(),
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = main(
                        (
                            "-p",
                            "hello",
                            "--cwd",
                            str(root),
                            "--output-format",
                            output_format,
                        )
                    )
                self.assertEqual(exit_code, 0)
                self.assertIn("fixture response", output.getvalue())
                if output_format == "json":
                    payload = json.loads(output.getvalue())
                    self.assertEqual(payload["steps"], 1)
                    self.assertIsNone(payload["outcome"])
                if output_format == "jsonl":
                    records = [json.loads(line) for line in output.getvalue().splitlines()]
                    self.assertEqual(records[-1]["kind"], "turn_completed")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process cleanup assertion")
    def test_headless_exit_terminates_a_managed_background_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)
            pid_file = root / "cli-background.pid"
            code = (
                "import os,pathlib,time;"
                "pathlib.Path('cli-background.pid').write_text(str(os.getpid()));"
                "time.sleep(60)"
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

            class BackgroundProvider(CliProvider):
                calls = 0

                async def stream(
                    self,
                    context: ModelContext,
                    tools: Sequence[ToolDefinition],
                ) -> AsyncIterator[ModelEvent]:
                    del context
                    self.calls += 1
                    names = {tool.name for tool in tools}
                    if self.calls == 1:
                        assert {"bash", "task_output", "wait_tasks", "kill_task"} <= names
                        yield ModelToolCall(
                            ToolCall(
                                "background-cli",
                                "bash",
                                {"command": command, "is_background": True},
                            )
                        )
                        yield ModelCompleted("tool_calls")
                        return
                    for _ in range(100):
                        if pid_file.exists():
                            break
                        await asyncio.sleep(0.01)
                    assert pid_file.exists()
                    yield ModelTextDelta("background fixture started")
                    yield ModelCompleted("stop")

            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=BackgroundProvider(),
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    (
                        "-p",
                        "start a background fixture",
                        "--cwd",
                        str(root),
                        "--always-approve",
                    )
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("background fixture started", output.getvalue())
            pid = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(100):
                if not self._process_running(pid):
                    break
                time.sleep(0.01)
            self.assertFalse(self._process_running(pid))

    @staticmethod
    def _process_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        stat = Path(f"/proc/{pid}/stat")
        if stat.is_file():
            try:
                return stat.read_text(encoding="utf-8").split()[2] != "Z"
            except (FileNotFoundError, ProcessLookupError):
                return False
        return True

    def test_agent_subcommand_requires_a_prompt(self) -> None:
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            exit_code = main(("agent",))
        self.assertEqual(exit_code, 2)
        self.assertIn("agent subcommand requires", errors.getvalue())

    def test_no_subcommand_launches_the_tui(self) -> None:
        launch = AsyncMock(return_value=0)
        with patch("neuro_code.bootstrap.entrypoints.BootstrapCliServices.run_tui", launch):
            exit_code = main(())
        self.assertEqual(exit_code, 0)
        launch.assert_awaited_once()

    def test_code_alias_launches_the_tui(self) -> None:
        launch = AsyncMock(return_value=0)
        with patch("neuro_code.bootstrap.entrypoints.BootstrapCliServices.run_tui", launch):
            exit_code = main(("code",))
        self.assertEqual(exit_code, 0)
        launch.assert_awaited_once()

    def test_unconfigured_tui_launches_first_run_provider_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            captured: dict[str, object] = {}

            class SetupFixture:
                def __init__(self, **kwargs: object) -> None:
                    captured.update(kwargs)

                async def run_async(self) -> bool:
                    captured["ran"] = True
                    return False

            with (
                patch.dict(
                    "os.environ",
                    {"HOME": str(root), "NEURO_CODE_HOME": str(state)},
                    clear=True,
                ),
                patch("neuro_code.tui.ProviderSetupApp", SetupFixture),
            ):
                exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            self.assertTrue(captured["ran"])
            self.assertIn("provider_settings_store", captured)

    def test_tui_recovers_invalid_managed_proxy_inside_provider_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            store = JsonProviderSettingsStore(state)
            asyncio.run(
                store.save_profile(
                    ManagedProviderProfile(
                        name="deepseek",
                        protocol="openai-chat",
                        model="deepseek-v4-flash",
                        base_url="https://api.deepseek.com",
                        api_key="stored-secret",
                    )
                )
            )
            captured: dict[str, object] = {}

            class SetupFixture:
                def __init__(self, **kwargs: object) -> None:
                    captured.update(kwargs)

                async def run_async(self) -> bool:
                    captured["ran"] = True
                    return False

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "ALL_PROXY": "socks://127.0.0.1:7890",
                    },
                    clear=True,
                ),
                patch("neuro_code.tui.ProviderSetupApp", SetupFixture),
            ):
                exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            self.assertTrue(captured["ran"])
            self.assertFalse(captured["first_run"])
            self.assertEqual(captured["initial_profile"], "deepseek")
            self.assertIn("ALL_PROXY", str(captured["initial_error"]))

    def test_tui_composition_uses_the_selected_provider_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_provider_config(root / "state")
            (root / "state" / "ui-preferences.json").write_text(
                json.dumps({"version": 1, "language": "zh-CN"}),
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            class TuiFixture:
                return_code: int | None = None

                def __init__(
                    self,
                    runner: object,
                    *,
                    turn_service: object,
                    approval_controller: object,
                    provider_controller: object,
                    reasoning_controller: object,
                    interaction_mode_controller: object,
                    session_controller: object,
                    session_selection_service: object,
                    task_controller: object,
                    session_task_controller: object,
                    plan_controller: object,
                    plan_execution_service: object,
                    plan_scheduling_service: object,
                    queued_plan_execution_service: object,
                    ui_preferences: object,
                    provider_settings_store: object,
                    provider_catalog: object,
                    managed_provider_settings: object,
                    tool_output_artifact_service: object,
                    read_only_subagent_service: object,
                    subagent_relationship_query: object,
                    subagent_relationship_lifecycle: object,
                    language: UiLanguage,
                    initial_items: object,
                    provider_name: str,
                    model_name: str,
                    cwd: Path,
                    user_interaction: object,
                ) -> None:
                    captured.update(
                        runner=runner,
                        turn_service=turn_service,
                        approval_controller=approval_controller,
                        provider_controller=provider_controller,
                        reasoning_controller=reasoning_controller,
                        interaction_mode_controller=interaction_mode_controller,
                        session_controller=session_controller,
                        session_selection_service=session_selection_service,
                        task_controller=task_controller,
                        session_task_controller=session_task_controller,
                        plan_controller=plan_controller,
                        plan_execution_service=plan_execution_service,
                        plan_scheduling_service=plan_scheduling_service,
                        queued_plan_execution_service=queued_plan_execution_service,
                        ui_preferences=ui_preferences,
                        provider_settings_store=provider_settings_store,
                        provider_catalog=provider_catalog,
                        managed_provider_settings=managed_provider_settings,
                        tool_output_artifact_service=tool_output_artifact_service,
                        read_only_subagent_service=read_only_subagent_service,
                        subagent_relationship_query=subagent_relationship_query,
                        subagent_relationship_lifecycle=subagent_relationship_lifecycle,
                        language=language,
                        initial_items=initial_items,
                        provider_name=provider_name,
                        model_name=model_name,
                        cwd=cwd,
                    )

                async def run_async(self) -> None:
                    captured["ran"] = True

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(root / "state"),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=CliProvider(),
                ),
                patch("neuro_code.tui.NeuroCodeApp", TuiFixture),
            ):
                exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["provider_name"], "cli-fixture")
            self.assertEqual(captured["model_name"], "fixture-model")
            self.assertEqual(captured["cwd"], root.resolve())
            self.assertIsInstance(captured["provider_controller"], ProviderChangeService)
            self.assertIsInstance(captured["session_selection_service"], SessionSelectionService)
            self.assertIsInstance(captured["plan_execution_service"], PlanExecutionService)
            self.assertIsInstance(captured["plan_scheduling_service"], PlanSchedulingService)
            self.assertIsInstance(
                captured["queued_plan_execution_service"], QueuedPlanExecutionService
            )
            self.assertIs(captured["runner"], captured["session_controller"])
            self.assertIs(captured["runner"], captured["task_controller"])
            self.assertIs(captured["runner"], captured["session_task_controller"])
            self.assertIs(captured["runner"], captured["plan_controller"])
            self.assertIs(captured["runner"], captured["reasoning_controller"])
            self.assertIs(captured["runner"], captured["interaction_mode_controller"])
            self.assertIsInstance(captured["turn_service"], SessionTurnService)
            self.assertEqual(captured["initial_items"], ())
            self.assertEqual(captured["language"], UiLanguage.SIMPLIFIED_CHINESE)
            self.assertIsInstance(captured["provider_catalog"], HttpProviderCatalog)
            self.assertIsInstance(
                captured["tool_output_artifact_service"],
                SessionToolOutputArtifactApplicationService,
            )
            self.assertTrue(captured["ran"])

    def test_tui_propagates_textual_return_code_and_shuts_down_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)
            supervisor = BackgroundTaskSupervisorFixture()

            class TuiFixture:
                return_code: int | None = 7

                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                async def run_async(self) -> None:
                    pass

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch(
                    "neuro_code.bootstrap.composition.LocalBackgroundTaskManager",
                    return_value=supervisor,
                ),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=CliProvider(),
                ),
                patch("neuro_code.tui.NeuroCodeApp", TuiFixture),
            ):
                exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 7)
            self.assertEqual(supervisor.shutdown_calls, 1)

    def test_tui_launch_exception_still_shuts_down_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)
            supervisor = BackgroundTaskSupervisorFixture()

            class TuiFixture:
                return_code: int | None = None

                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                async def run_async(self) -> None:
                    raise RuntimeError("fixture TUI failure")

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch(
                    "neuro_code.bootstrap.composition.LocalBackgroundTaskManager",
                    return_value=supervisor,
                ),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=CliProvider(),
                ),
                patch("neuro_code.tui.NeuroCodeApp", TuiFixture),
                self.assertRaisesRegex(RuntimeError, "fixture TUI failure"),
            ):
                main(("--cwd", str(root)))

            self.assertEqual(supervisor.shutdown_calls, 1)

    def test_tui_session_search_is_scoped_to_the_workspace_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            other_workspace = root / "other"
            other_workspace.mkdir()
            workspace_alias = workspace / ".." / "workspace"
            self._write_provider_config(state)

            async def seed_sessions() -> tuple[str, str]:
                store = SqliteSessionStore(state / "sessions.db")
                await store.initialize()
                local_id = await store.create_session(
                    str(workspace_alias),
                    "cli-fixture",
                    "fixture-model",
                )
                await store.save_messages(
                    local_id,
                    [Message(Role.USER, "workspace scope marker")],
                )
                other_id = await store.create_session(
                    str(other_workspace),
                    "cli-fixture",
                    "fixture-model",
                )
                await store.save_messages(
                    other_id,
                    [Message(Role.USER, "workspace scope marker")],
                )
                return local_id, other_id

            local_id, other_id = asyncio.run(seed_sessions())
            captured: dict[str, object] = {}

            class TuiFixture:
                return_code: int | None = None

                def __init__(
                    self,
                    runner: object,
                    *,
                    turn_service: object,
                    approval_controller: object,
                    provider_controller: object,
                    reasoning_controller: object,
                    interaction_mode_controller: object,
                    session_controller: object,
                    session_selection_service: object,
                    task_controller: object,
                    session_task_controller: object,
                    plan_controller: object,
                    plan_execution_service: object,
                    plan_scheduling_service: object,
                    queued_plan_execution_service: object,
                    ui_preferences: object,
                    provider_settings_store: object,
                    provider_catalog: object,
                    managed_provider_settings: object,
                    tool_output_artifact_service: object,
                    read_only_subagent_service: object,
                    subagent_relationship_query: object,
                    subagent_relationship_lifecycle: object,
                    language: UiLanguage,
                    initial_items: object,
                    provider_name: str,
                    model_name: str,
                    cwd: Path,
                    user_interaction: object,
                ) -> None:
                    del (
                        runner,
                        turn_service,
                        approval_controller,
                        provider_controller,
                        reasoning_controller,
                        interaction_mode_controller,
                        session_selection_service,
                        task_controller,
                        session_task_controller,
                        plan_controller,
                        plan_execution_service,
                        plan_scheduling_service,
                        queued_plan_execution_service,
                        ui_preferences,
                        provider_settings_store,
                        provider_catalog,
                        managed_provider_settings,
                        tool_output_artifact_service,
                        read_only_subagent_service,
                        subagent_relationship_query,
                        subagent_relationship_lifecycle,
                        language,
                        initial_items,
                        provider_name,
                        model_name,
                        cwd,
                    )
                    self.session_controller = session_controller

                async def run_async(self) -> None:
                    options = await self.session_controller.list_sessions("scope marker")
                    captured["session_ids"] = [option.session_id for option in options]

            with (
                patch.dict(
                    "os.environ",
                    {
                        "NEURO_CODE_HOME": str(state),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=CliProvider(),
                ),
                patch("neuro_code.tui.NeuroCodeApp", TuiFixture),
            ):
                exit_code = main(("--cwd", str(workspace)))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["session_ids"], [local_id])
            self.assertNotIn(other_id, captured["session_ids"])

    def test_tui_profile_controller_recomposes_a_fresh_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "first"

[providers.first]
protocol = "openai-chat"
model = "first-model"
base_url = "https://first.invalid/v1"
api_key_env = "FIRST_KEY"

[providers.second]
protocol = "openai-chat"
model = "second-model"
base_url = "https://second.invalid/v1"
api_key_env = "SECOND_KEY"
""",
                encoding="utf-8",
            )
            selected: list[str] = []
            captured: dict[str, object] = {}

            def create(config: AppConfig, *, failover: bool) -> CliProvider:
                del failover
                selected.append(config.provider.name)
                provider = CliProvider()
                provider.provider_name = config.provider.name
                provider.model_name = config.provider.model
                return provider

            class TuiFixture:
                return_code: int | None = None

                def __init__(
                    self,
                    runner: object,
                    *,
                    turn_service: object,
                    approval_controller: object,
                    provider_controller: object,
                    reasoning_controller: object,
                    interaction_mode_controller: object,
                    session_controller: object,
                    session_selection_service: object,
                    task_controller: object,
                    session_task_controller: object,
                    plan_controller: object,
                    plan_execution_service: object,
                    plan_scheduling_service: object,
                    queued_plan_execution_service: object,
                    ui_preferences: object,
                    provider_settings_store: object,
                    provider_catalog: object,
                    managed_provider_settings: object,
                    tool_output_artifact_service: object,
                    read_only_subagent_service: object,
                    subagent_relationship_query: object,
                    subagent_relationship_lifecycle: object,
                    language: UiLanguage,
                    initial_items: object,
                    provider_name: str,
                    model_name: str,
                    cwd: Path,
                    user_interaction: object,
                ) -> None:
                    del (
                        approval_controller,
                        turn_service,
                        reasoning_controller,
                        interaction_mode_controller,
                        session_selection_service,
                        plan_controller,
                        plan_execution_service,
                        plan_scheduling_service,
                        queued_plan_execution_service,
                        ui_preferences,
                        provider_settings_store,
                        provider_catalog,
                        managed_provider_settings,
                        tool_output_artifact_service,
                        read_only_subagent_service,
                        subagent_relationship_query,
                        subagent_relationship_lifecycle,
                        language,
                        initial_items,
                        provider_name,
                        model_name,
                        cwd,
                    )
                    self.runner = runner
                    self.provider_controller = provider_controller
                    self.session_controller = session_controller
                    self.task_controller = task_controller
                    self.session_task_controller = session_task_controller

                async def run_async(self) -> None:
                    selection = await self.provider_controller.change_provider(
                        ChangeProviderRequest("second")
                    )
                    captured["selection"] = selection
                    captured["same_controller"] = self.runner is self.provider_controller
                    captured["same_session_controller"] = self.runner is self.session_controller
                    captured["same_task_controller"] = self.runner is self.task_controller
                    captured["same_session_task_controller"] = (
                        self.runner is self.session_task_controller
                    )

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIRST_KEY": "first-secret",
                        "SECOND_KEY": "second-secret",
                    },
                    clear=True,
                ),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider", side_effect=create
                ),
                patch("neuro_code.tui.NeuroCodeApp", TuiFixture),
            ):
                exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            self.assertEqual(selected, ["first", "second"])
            self.assertFalse(captured["same_controller"])
            self.assertTrue(captured["same_session_controller"])
            self.assertTrue(captured["same_task_controller"])
            self.assertTrue(captured["same_session_task_controller"])
            selection = captured["selection"]
            self.assertTrue(selection.changed)
            self.assertIsNone(selection.previous_session_id)

    def test_tui_session_controller_filters_workspace_and_resumes_saved_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            other = Path(directory) / "other-workspace"
            state = Path(directory) / "state"
            root.mkdir()
            other.mkdir()
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "first"

[providers.first]
protocol = "openai-chat"
model = "first-model"
base_url = "https://first.invalid/v1"
api_key_env = "FIRST_KEY"

[providers.second]
protocol = "openai-chat"
model = "second-model"
base_url = "https://second.invalid/v1"
api_key_env = "SECOND_KEY"
""",
                encoding="utf-8",
            )
            environment = {
                "HOME": str(Path(directory)),
                "NEURO_CODE_HOME": str(state),
                "FIRST_KEY": "first-secret",
                "SECOND_KEY": "second-secret",
            }
            created_profiles: list[str] = []

            def create(config: AppConfig, *, failover: bool) -> CliProvider:
                del failover
                created_profiles.append(config.provider.name)
                provider = CliProvider()
                provider.provider_name = config.provider.name
                provider.model_name = config.provider.model
                return provider

            def create_session(cwd: Path, profile: str) -> str:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main(
                        (
                            "-p",
                            f"prompt for {cwd.name}",
                            "--provider",
                            profile,
                            "--cwd",
                            str(cwd),
                            "--output-format",
                            "json",
                        )
                    )
                self.assertEqual(exit_code, 0)
                return str(json.loads(output.getvalue())["session_id"])

            captured: dict[str, object] = {}

            class TuiFixture:
                return_code: int | None = None

                def __init__(
                    self,
                    runner: object,
                    *,
                    turn_service: object,
                    approval_controller: object,
                    provider_controller: object,
                    reasoning_controller: object,
                    interaction_mode_controller: object,
                    session_controller: object,
                    session_selection_service: object,
                    task_controller: object,
                    session_task_controller: object,
                    plan_controller: object,
                    plan_execution_service: object,
                    plan_scheduling_service: object,
                    queued_plan_execution_service: object,
                    ui_preferences: object,
                    provider_settings_store: object,
                    provider_catalog: object,
                    managed_provider_settings: object,
                    tool_output_artifact_service: object,
                    read_only_subagent_service: object,
                    subagent_relationship_query: object,
                    subagent_relationship_lifecycle: object,
                    language: UiLanguage,
                    initial_items: object,
                    provider_name: str,
                    model_name: str,
                    cwd: Path,
                    user_interaction: object,
                ) -> None:
                    del (
                        approval_controller,
                        turn_service,
                        reasoning_controller,
                        interaction_mode_controller,
                        session_selection_service,
                        ui_preferences,
                        provider_settings_store,
                        provider_catalog,
                        managed_provider_settings,
                        tool_output_artifact_service,
                        read_only_subagent_service,
                        subagent_relationship_query,
                        subagent_relationship_lifecycle,
                        plan_controller,
                        plan_execution_service,
                        plan_scheduling_service,
                        queued_plan_execution_service,
                        session_task_controller,
                        language,
                        provider_name,
                        model_name,
                        cwd,
                    )
                    self.runner = runner
                    self.provider_controller = provider_controller
                    self.session_controller = session_controller
                    self.task_controller = task_controller
                    captured["initial_items"] = initial_items

                async def run_async(self) -> None:
                    options = await self.session_controller.list_sessions()
                    captured["session_ids"] = [option.session_id for option in options]
                    captured["selection"] = await self.session_controller.select_session(
                        captured["root_session"]
                    )
                    captured["renamed"] = await self.session_controller.rename_session(
                        "Renamed from TUI"
                    )
                    captured["same_controller"] = (
                        self.runner is self.session_controller is self.task_controller
                    )
                    captured["provider_service_is_distinct"] = (
                        self.runner is not self.provider_controller
                    )

            with (
                patch.dict("os.environ", environment, clear=True),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider", side_effect=create
                ),
            ):
                root_session = create_session(root, "second")
                other_session = create_session(other, "first")
                captured["root_session"] = root_session
                with patch("neuro_code.tui.NeuroCodeApp", TuiFixture):
                    exit_code = main(("--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured["session_ids"], [root_session])
            self.assertNotIn(other_session, captured["session_ids"])
            self.assertTrue(captured["same_controller"])
            self.assertTrue(captured["provider_service_is_distinct"])
            self.assertEqual(captured["initial_items"], ())
            selection = captured["selection"]
            self.assertEqual(selection.session_id, root_session)
            self.assertEqual(selection.profile_name, "second")
            self.assertTrue(selection.source_profile_match)
            self.assertEqual(captured["renamed"].title, "Renamed from TUI")
            self.assertGreaterEqual(len(selection.items), 2)
            self.assertEqual(created_profiles[-2:], ["first", "second"])

    def test_resume_list_and_export_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(root / "state"),
                "FIXTURE_KEY": "fixture-key",
            }
            self._write_provider_config(root / "state")

            def run(arguments: tuple[str, ...]) -> tuple[int, str]:
                output = io.StringIO()
                with (
                    patch.dict("os.environ", environment, clear=True),
                    patch(
                        "neuro_code.bootstrap.composition.create_routed_provider",
                        return_value=CliProvider(),
                    ),
                    redirect_stdout(output),
                ):
                    return main(arguments), output.getvalue()

            exit_code, first_output = run(
                ("-p", "first", "--cwd", str(root), "--output-format", "json")
            )
            self.assertEqual(exit_code, 0)
            session_id = json.loads(first_output)["session_id"]

            exit_code, second_output = run(
                (
                    "-p",
                    "second",
                    "--cwd",
                    str(root),
                    "--resume",
                    session_id,
                    "--output-format",
                    "json",
                )
            )
            self.assertEqual(exit_code, 0)
            resumed = json.loads(second_output)
            self.assertEqual(resumed["session_id"], session_id)
            self.assertGreater(resumed["events"][0]["sequence"], 1)

            async def save_recoverable_record() -> None:
                store = SqliteSessionStore(root / "state" / "sessions.db")
                await store.initialize()
                completed = AgentEvent.create(
                    await store.next_event_sequence(session_id),
                    AgentEventKind.TURN_COMPLETED,
                    {"step": 3},
                )
                await store.append_event(session_id, completed)
                await store.save_execution_record(
                    session_id,
                    SessionExecutionRecord(
                        outcome=AgentExecutionOutcome(
                            status=AgentExecutionStatus.BUDGET_LIMITED,
                            reason_code=SupervisorReasonCode.MODEL_STEP_LIMIT,
                            finalized=True,
                            recoverable=True,
                        ),
                        event_sequence=completed.sequence,
                        completed_at=datetime.now(UTC),
                    ),
                )

            asyncio.run(save_recoverable_record())

            exit_code, list_output = run(("sessions", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            listed = json.loads(list_output)[0]
            self.assertEqual(listed["id"], session_id)
            self.assertEqual(listed["sandbox_profile"], "off")
            self.assertEqual(listed["title"], "first")
            self.assertEqual(listed["last_execution"]["status"], "budget_limited")
            self.assertEqual(listed["last_execution"]["reason"], "model_step_limit")
            self.assertTrue(listed["last_execution"]["recoverable"])

            exit_code, search_output = run(
                (
                    "sessions",
                    "search",
                    "first second",
                    "--json",
                    "--include-content",
                    "--cwd",
                    str(root),
                )
            )
            self.assertEqual(exit_code, 0)
            search_page = json.loads(search_output)
            self.assertEqual(search_page["total_estimate"], 1)
            self.assertEqual(search_page["results"][0]["id"], session_id)
            self.assertIn("content", search_page["results"][0]["matched_fields"])
            self.assertIsNotNone(search_page["results"][0]["snippet"])
            self.assertEqual(
                search_page["results"][0]["last_execution"]["status"],
                "budget_limited",
            )
            self.assertEqual(
                search_page["results"][0]["last_execution"]["reason"],
                "model_step_limit",
            )
            self.assertTrue(search_page["results"][0]["last_execution"]["recoverable"])

            exit_code, rename_output = run(
                (
                    "sessions",
                    "rename",
                    session_id,
                    "Manual CLI title",
                    "--json",
                    "--cwd",
                    str(root),
                )
            )
            self.assertEqual(exit_code, 0)
            renamed = json.loads(rename_output)
            self.assertEqual(renamed["id"], session_id)
            self.assertEqual(renamed["title"], "Manual CLI title")

            exit_code, renamed_search_output = run(
                (
                    "sessions",
                    "search",
                    "manual CLI",
                    "--json",
                    "--cwd",
                    str(root),
                )
            )
            self.assertEqual(exit_code, 0)
            renamed_search = json.loads(renamed_search_output)
            self.assertEqual(renamed_search["results"][0]["id"], session_id)
            self.assertIn("title", renamed_search["results"][0]["matched_fields"])

            exit_code, markdown = run(("export", session_id, "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertIn("## User\n\nfirst", markdown)
            self.assertIn("## User\n\nsecond", markdown)

            export_path = root / "exports" / "session.json"
            exit_code, export_output = run(
                (
                    "export",
                    session_id,
                    "--cwd",
                    str(root),
                    "--format",
                    "json",
                    "--output",
                    str(export_path),
                )
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(export_output.strip(), str(export_path.resolve()))
            exported = json.loads(export_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["schema_version"], 4)
            self.assertEqual(exported["session"]["id"], session_id)
            self.assertEqual(exported["session"]["sandbox_profile"], "off")
            self.assertEqual(exported["session"]["title"], "Manual CLI title")
            self.assertEqual(exported["conversation_items"], exported["messages"])

    def test_sessions_artifacts_list_and_read_through_session_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)

            async def seed() -> tuple[str, str, Path]:
                store = SqliteSessionStore(state / "sessions.db")
                await store.initialize()
                session_id = await store.create_session(
                    str(root),
                    "cli-fixture",
                    "fixture-model",
                )
                artifact_store = FileToolOutputArtifactStore(state / "tool-output")
                artifact = await artifact_store.save(
                    tool_name="bash",
                    content=b"bounded output\n",
                    content_truncated=True,
                )
                orphan = await artifact_store.save(tool_name="bash", content=b"orphan")
                orphan_path = state / "tool-output" / Path(orphan.relative_path).name
                old_timestamp = os.stat(orphan_path).st_mtime - 7200
                os.utime(orphan_path, (old_timestamp, old_timestamp))
                event = AgentEvent.create(
                    await store.next_event_sequence(session_id),
                    AgentEventKind.TOOL_COMPLETED,
                    {
                        "metadata": {
                            "output_artifact_id": artifact.artifact_id,
                            "output_artifact_path": artifact.relative_path,
                            "output_artifact_bytes": artifact.byte_count,
                            "output_artifact_truncated": artifact.truncated,
                        }
                    },
                )
                await store.append_event(session_id, event)
                return session_id, artifact.artifact_id, orphan_path

            session_id, artifact_id, orphan_path = asyncio.run(seed())
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(state),
                "FIXTURE_KEY": "fixture-key",
            }
            output = io.StringIO()
            with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
                exit_code = main(
                    (
                        "sessions",
                        "artifacts",
                        session_id,
                        "--json",
                        "--cwd",
                        str(root),
                    )
                )
            self.assertEqual(exit_code, 0)
            listed = json.loads(output.getvalue())
            self.assertEqual(listed[0]["id"], artifact_id)
            self.assertEqual(listed[0]["bytes"], len(b"bounded output\n"))
            self.assertTrue(listed[0]["truncated"])
            self.assertNotIn("path", listed[0])

            output = io.StringIO()
            with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
                exit_code = main(
                    (
                        "sessions",
                        "artifacts",
                        session_id,
                        artifact_id,
                        "--json",
                        "--max-bytes",
                        "7",
                        "--cwd",
                        str(root),
                    )
                )
            self.assertEqual(exit_code, 0)
            read = json.loads(output.getvalue())
            self.assertEqual(read["id"], artifact_id)
            self.assertEqual(read["content"], "bounded")
            self.assertTrue(read["read_truncated"])

            output = io.StringIO()
            with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
                exit_code = main(
                    (
                        "sessions",
                        "artifacts",
                        "--prune",
                        "--json",
                        "--cwd",
                        str(root),
                    )
                )
            self.assertEqual(exit_code, 0)
            pruned = json.loads(output.getvalue())
            self.assertEqual(pruned["deleted"], 1)
            self.assertFalse(orphan_path.exists())

    def test_sessions_prune_is_restricted_to_artifact_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_provider_config(state)
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(state),
                "FIXTURE_KEY": "fixture-key",
            }
            with patch.dict("os.environ", environment, clear=True):
                output = io.StringIO()
                with redirect_stderr(output):
                    exit_code = main(("sessions", "--prune", "--cwd", str(root)))
            self.assertEqual(exit_code, 2)
            self.assertIn("--prune is only valid for sessions artifacts", output.getvalue())

    def test_import_rust_session_is_available_to_list_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rust-session"
            source.mkdir()
            (source / "summary.json").write_text(
                json.dumps(
                    {
                        "info": {"id": "rust-cli-id", "cwd": str(root)},
                        "created_at": "2026-07-01T10:20:30Z",
                        "updated_at": "2026-07-02T11:22:33Z",
                        "current_model_id": "xai-test-model",
                        "chat_format_version": 1,
                    }
                ),
                encoding="utf-8",
            )
            (source / "chat_history.jsonl").write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "user",
                                "content": [
                                    {"type": "text", "text": "legacy prompt"},
                                    {
                                        "type": "image",
                                        "url": "data:image/png;base64,fixture",
                                    },
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "type": "reasoning",
                                "id": "reasoning-cli",
                                "summary": [{"type": "summary_text", "text": "careful thought"}],
                            }
                        ),
                        json.dumps(
                            {
                                "type": "backend_tool_call",
                                "kind": {
                                    "tool_type": "web_search",
                                    "id": "web-cli",
                                    "action": {"type": "search", "query": "fixture query"},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "content": "legacy response",
                                "raw_output": [
                                    {
                                        "type": "reasoning",
                                        "id": "reasoning-recovered",
                                        "summary": [
                                            {
                                                "type": "summary_text",
                                                "text": "recovered thought",
                                            }
                                        ],
                                    },
                                    {
                                        "type": "web_search_call",
                                        "id": "web-cli",
                                        "status": "completed",
                                        "action": {
                                            "type": "search",
                                            "query": "duplicate query",
                                        },
                                    },
                                    {
                                        "type": "message",
                                        "id": "message-cli",
                                        "status": "completed",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                ],
                            }
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            source_before = {
                path.name: path.read_bytes()
                for path in (source / "summary.json", source / "chat_history.jsonl")
            }
            environment = {"HOME": str(root), "NEURO_CODE_HOME": str(root / "state")}
            self._write_provider_config(root / "state")

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(("import-session", str(source), "--json", "--cwd", str(root)))

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["session"]["id"], "rust-cli-id")
            self.assertEqual(payload["session"]["provider"], "upstream-rust-import")
            self.assertEqual(payload["imported_messages"], 2)
            self.assertEqual(payload["preserved_context_records"], 3)
            self.assertEqual(payload["recovered_context_records"], 1)
            self.assertEqual(payload["deduplicated_context_records"], 1)
            self.assertEqual(payload["invalid_embedded_records"], 0)
            self.assertEqual(payload["unsupported_embedded_records"], 0)
            self.assertEqual(payload["preserved_images"], 1)

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(("export", "rust-cli-id", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            markdown = output.getvalue()
            self.assertIn("legacy prompt", markdown)
            self.assertIn("image content preserved in session", markdown)
            self.assertIn("## Reasoning\n\ncareful thought", markdown)
            self.assertIn("## Reasoning\n\nrecovered thought", markdown)
            self.assertIn("legacy response", markdown)
            self.assertIn("## Backend tool call", markdown)
            self.assertIn("fixture query", markdown)

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                exit_code = main(("export", "rust-cli-id", "--format", "json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            exported = json.loads(output.getvalue())
            self.assertEqual(exported["schema_version"], 4)
            self.assertEqual(
                [item.get("type") for item in exported["conversation_items"]],
                [None, "reasoning", "backend_tool_call", "reasoning", None],
            )
            self.assertEqual(
                exported["conversation_items"][0]["content_parts"][1]["url"],
                "data:image/png;base64,fixture",
            )

            resume_provider = CliProvider()
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {**environment, "FIXTURE_KEY": "fixture-key"},
                    clear=True,
                ),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider",
                    return_value=resume_provider,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    (
                        "-p",
                        "continue imported session",
                        "--resume",
                        "rust-cli-id",
                        "--cwd",
                        str(root),
                    )
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(resume_provider.contexts), 1)
            resumed_context = resume_provider.contexts[0]
            self.assertEqual(resumed_context.source_provider, "upstream-rust-import")
            self.assertEqual(resumed_context.source_model, "xai-test-model")
            self.assertEqual(len(resumed_context.preserved_items), 3)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (source / "summary.json", source / "chat_history.jsonl")
                },
                source_before,
            )

    def test_provider_list_inspect_and_one_shot_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "config.toml").write_text(
                """
[routing]
default = "first"
fallbacks = ["second"]

[providers.first]
protocol = "openai-chat"
model = "first-model"
base_url = "https://first.invalid/v1"
api_key_env = "FIRST_KEY"

[providers.second]
protocol = "openai-chat"
model = "second-model"
base_url = "https://second.invalid/v1"
api_key_env = "SECOND_KEY"
""",
                encoding="utf-8",
            )
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(state),
                "FIRST_KEY": "first-secret",
                "SECOND_KEY": "second-secret",
            }

            output = io.StringIO()
            with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
                exit_code = main(("providers", "list", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            profiles = json.loads(output.getvalue())
            self.assertEqual([profile["name"] for profile in profiles], ["first", "second"])
            self.assertTrue(profiles[0]["default"])
            self.assertTrue(profiles[1]["fallback"])
            self.assertNotIn("first-secret", output.getvalue())

            output = io.StringIO()
            with patch.dict("os.environ", environment, clear=True), redirect_stdout(output):
                exit_code = main(("providers", "inspect", "second", "--json", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["model"], "second-model")

            selected: list[str] = []
            failover_values: list[bool] = []

            def create(config: AppConfig, *, failover: bool) -> CliProvider:
                selected.append(config.provider.name)
                failover_values.append(failover)
                return CliProvider()

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider", side_effect=create
                ),
                redirect_stdout(output),
            ):
                exit_code = main(("-p", "hello", "--provider", "second", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertEqual(selected, ["second"])
            self.assertEqual(failover_values, [True])

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                patch(
                    "neuro_code.bootstrap.composition.create_routed_provider", side_effect=create
                ),
                redirect_stdout(output),
            ):
                exit_code = main(("-p", "hello", "--no-failover", "--cwd", str(root)))
            self.assertEqual(exit_code, 0)
            self.assertEqual(failover_values, [True, False])


if __name__ == "__main__":
    unittest.main()
