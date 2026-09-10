from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    ContextCompactionPlanner,
    ContextCompactionPolicy,
    ProviderContextWindow,
)
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeGate,
    ContextCompactionSafePoint,
    ContextPreflightStatus,
    assess_context_preflight,
    build_automatic_context_compaction_runtime_request,
)
from neuro_code.application.memory.compaction_service import ContextCompactionApplicationService
from neuro_code.application.memory.compaction_trigger import ContextCompactionTriggerService
from neuro_code.application.permissions.policy import PermissionManager
from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import AgentEventKind, ModelCompleted, ModelEvent
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.conversation.request import ModelRequestSnapshot
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from tests.fakes import EmptyWorkspaceChangeObserver


class _EmptyToolCollection:
    def get(self, name: str) -> Tool | None:
        del name
        return None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return ()


class _ScriptedProvider(ModelProvider):
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = "profile-v1:fixture"

    def __init__(self, events: Sequence[ModelEvent]) -> None:
        self._events = tuple(events)
        self.calls: list[tuple[ModelContext, tuple[ToolDefinition, ...]]] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        self.calls.append((context, tuple(tools)))
        for event in self._events:
            yield event


class _SequencedProvider(ModelProvider):
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = "profile-v1:fixture"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent]]) -> None:
        self._scripts = [tuple(script) for script in scripts]
        self.calls: list[tuple[ModelContext, tuple[ToolDefinition, ...]]] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        if not self._scripts:
            raise AssertionError("unexpected provider call")
        self.calls.append((context, tuple(tools)))
        for event in self._scripts.pop(0):
            yield event


class _StaticToolCollection:
    def __init__(self, definitions: Sequence[ToolDefinition]) -> None:
        self._definitions = tuple(definitions)

    def get(self, name: str) -> Tool | None:
        del name
        return None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions


class ContextPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ModelContext((Message(Role.USER, "inspect the repository"),))
        self.window = ProviderContextWindow("fixture", "fixture-model", 10_000)

    def test_known_safe_request_accounts_for_output_reserve_and_margin(self) -> None:
        result = assess_context_preflight(
            context=self.context,
            tools=(),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=self.window,
            max_output_tokens=256,
        )

        self.assertIs(result.status, ContextPreflightStatus.SAFE)
        self.assertEqual(result.reserved_output_tokens, 256)
        self.assertGreaterEqual(result.safety_margin_tokens or 0, 128)
        self.assertEqual(
            result.estimated_remaining_tokens,
            self.window.capacity_tokens - (result.estimated_total_tokens or 0),
        )

    def test_tool_definitions_contribute_to_request_estimate(self) -> None:
        small = assess_context_preflight(
            context=self.context,
            tools=(ToolDefinition("inspect", "short", {"type": "object"}),),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=self.window,
            max_output_tokens=256,
        )
        large = assess_context_preflight(
            context=self.context,
            tools=(
                ToolDefinition(
                    "inspect",
                    "x" * 20_000,
                    {"type": "object", "description": "y" * 20_000},
                ),
            ),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=self.window,
            max_output_tokens=256,
        )

        self.assertGreater(large.tool_tokens, small.tool_tokens)
        self.assertGreater(large.estimated_input_tokens, small.estimated_input_tokens)

    def test_output_reserve_can_make_request_unsafe(self) -> None:
        result = assess_context_preflight(
            context=self.context,
            tools=(),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=ProviderContextWindow("fixture", "fixture-model", 512),
            max_output_tokens=512,
        )

        self.assertIs(result.status, ContextPreflightStatus.BLOCKED)
        self.assertGreaterEqual(result.irreducible_tokens or 0, 512)
        blocked = assess_context_preflight(
            context=self.context,
            tools=(),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=ProviderContextWindow("fixture", "fixture-model", 512),
            max_output_tokens=512,
            compaction_attempted=True,
        )
        self.assertIs(blocked.status, ContextPreflightStatus.BLOCKED)

    def test_large_tool_schema_is_blocked_before_compaction(self) -> None:
        result = assess_context_preflight(
            context=self.context,
            tools=(
                ToolDefinition(
                    "inspect",
                    "x" * 40_000,
                    {"type": "object", "description": "y" * 40_000},
                ),
            ),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=ProviderContextWindow("fixture", "fixture-model", 2_000),
            max_output_tokens=128,
        )

        self.assertIs(result.status, ContextPreflightStatus.BLOCKED)
        self.assertGreaterEqual(result.irreducible_tokens or 0, 2_000)

    def test_large_history_keeps_compaction_actionable_when_immutable_cost_fits(self) -> None:
        result = assess_context_preflight(
            context=ModelContext((Message(Role.USER, "history " + "x" * 20_000),)),
            tools=(),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=ProviderContextWindow("fixture", "fixture-model", 2_000),
            max_output_tokens=128,
        )

        self.assertIs(result.status, ContextPreflightStatus.COMPACTION_REQUIRED)
        self.assertLess(result.irreducible_tokens or 0, result.capacity_tokens or 0)
        self.assertGreater(result.context_tokens or 0, result.irreducible_tokens or 0)

    def test_unknown_capacity_does_not_invent_numeric_limit(self) -> None:
        result = assess_context_preflight(
            context=self.context,
            tools=(),
            provider="fixture",
            model="fixture-model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=None,
            max_output_tokens=256,
        )

        self.assertIs(result.status, ContextPreflightStatus.UNKNOWN)
        self.assertIsNone(result.capacity_tokens)
        self.assertIsNone(result.estimated_total_tokens)
        self.assertIsNone(result.estimated_remaining_tokens)
        self.assertIsNone(result.safety_margin_tokens)

    def test_margin_is_deterministic(self) -> None:
        kwargs = {
            "context": self.context,
            "tools": (),
            "provider": "fixture",
            "model": "fixture-model",
            "context_affinity": None,
            "reasoning_effort": ReasoningEffort.HIGH,
            "provider_window": self.window,
            "max_output_tokens": 256,
        }
        first = assess_context_preflight(**kwargs)
        second = assess_context_preflight(**kwargs)
        self.assertEqual(first, second)


class ContextPreflightRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_override_must_match_active_provider_window(self) -> None:
        provider = _ScriptedProvider(())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            service = ContextCompactionTriggerService(
                ContextCompactionApplicationService(store, provider),
            )
            active_window = ProviderContextWindow("fixture", "fixture-model", 1_000)
            stale_window = ProviderContextWindow("other", "other-model", 2_000)
            same_capacity_other_window = ProviderContextWindow("other", "other-model", 1_000)
            identityless_usage = CompactionContextUsage(
                used_tokens=10,
                capacity_tokens=1_000,
                estimated=True,
            )

            with self.assertRaisesRegex(ValueError, "usage_override capacity"):
                build_automatic_context_compaction_runtime_request(
                    service,
                    source_context=ModelContext((Message(Role.USER, "source"),)),
                    usage_context=ModelContext((Message(Role.USER, "usage"),)),
                    boundary=ContextCompactionRuntimeBoundary(
                        ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                        0,
                    ),
                    provider_window=active_window,
                    usage_override=CompactionContextUsage.from_provider_window(
                        10,
                        stale_window,
                        estimated=True,
                    ),
                )
            with self.assertRaisesRegex(ValueError, "usage_override provider_window"):
                build_automatic_context_compaction_runtime_request(
                    service,
                    source_context=ModelContext((Message(Role.USER, "source"),)),
                    usage_context=ModelContext((Message(Role.USER, "usage"),)),
                    boundary=ContextCompactionRuntimeBoundary(
                        ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                        0,
                    ),
                    provider_window=active_window,
                    usage_override=identityless_usage,
                )

            active_window = ProviderContextWindow(
                "fixture",
                "fixture-model",
                1_000,
                "profile-v1:fixture",
            )
            identity_variants = (
                ProviderContextWindow("other", "fixture-model", 1_000, "profile-v1:fixture"),
                ProviderContextWindow("fixture", "other-model", 1_000, "profile-v1:fixture"),
                ProviderContextWindow("fixture", "fixture-model", 1_000, "profile-v1:other"),
            )
            for variant in identity_variants:
                with (
                    self.subTest(variant=variant),
                    self.assertRaisesRegex(ValueError, "usage_override provider_window"),
                ):
                    build_automatic_context_compaction_runtime_request(
                        service,
                        source_context=ModelContext((Message(Role.USER, "source"),)),
                        usage_context=ModelContext((Message(Role.USER, "usage"),)),
                        boundary=ContextCompactionRuntimeBoundary(
                            ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                            0,
                        ),
                        provider_window=active_window,
                        usage_override=CompactionContextUsage.from_provider_window(
                            10,
                            variant,
                            estimated=True,
                        ),
                    )
            exact_usage = CompactionContextUsage.from_provider_window(
                10,
                active_window,
                estimated=True,
            )
            request = build_automatic_context_compaction_runtime_request(
                service,
                source_context=ModelContext((Message(Role.USER, "source"),)),
                usage_context=ModelContext((Message(Role.USER, "usage"),)),
                boundary=ContextCompactionRuntimeBoundary(
                    ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                    0,
                ),
                provider_window=active_window,
                usage_override=exact_usage,
            )
            self.assertIs(request.trigger.usage, exact_usage)
            with self.assertRaisesRegex(ValueError, "usage_override provider_window"):
                build_automatic_context_compaction_runtime_request(
                    service,
                    source_context=ModelContext((Message(Role.USER, "source"),)),
                    usage_context=ModelContext((Message(Role.USER, "usage"),)),
                    boundary=ContextCompactionRuntimeBoundary(
                        ContextCompactionSafePoint.BEFORE_MODEL_REQUEST,
                        0,
                    ),
                    provider_window=active_window,
                    usage_override=CompactionContextUsage.from_provider_window(
                        10,
                        same_capacity_other_window,
                        estimated=True,
                    ),
                )

    async def test_known_safe_request_calls_provider_once(self) -> None:
        provider = _ScriptedProvider((ModelCompleted("stop", response_text="answer"),))
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=_EmptyToolCollection(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                provider_context_window=ProviderContextWindow(
                    "fixture",
                    "fixture-model",
                    100_000,
                ),
                provider_max_output_tokens=256,
            )
            result = await runtime.run("hello")

        self.assertEqual(len(provider.calls), 1)
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(len(preflights), 1)
        self.assertEqual(preflights[0].data["status"], ContextPreflightStatus.SAFE.value)
        self.assertEqual(result.response, "answer")

    async def test_blocked_request_stops_before_provider_and_finalizer(self) -> None:
        provider = _ScriptedProvider(())
        finalizer_calls = 0

        def finalizer_factory(*args: object) -> object:
            nonlocal finalizer_calls
            del args
            finalizer_calls += 1
            raise AssertionError("a blocked preflight must not construct a finalizer")

        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=_EmptyToolCollection(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                finalizer_factory=finalizer_factory,
                provider_context_window=ProviderContextWindow("fixture", "fixture-model", 128),
                provider_max_output_tokens=128,
            )
            result = await runtime.run("hello")

        self.assertEqual(len(provider.calls), 0)
        self.assertEqual(finalizer_calls, 0)
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(preflights[-1].data["status"], ContextPreflightStatus.BLOCKED.value)
        self.assertNotIn(
            AgentEventKind.MODEL_REQUEST_STARTED, [event.kind for event in result.events]
        )
        self.assertNotIn(
            AgentEventKind.MODEL_OUTPUT_STARTED, [event.kind for event in result.events]
        )

    async def test_irreducible_block_stops_before_real_compaction(self) -> None:
        provider = _ScriptedProvider(())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "fixture-model")
            gate = ContextCompactionRuntimeGate(
                ContextCompactionTriggerService(
                    ContextCompactionApplicationService(store, provider),
                )
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=_EmptyToolCollection(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                compaction_runtime_gate=gate,
                provider_context_window=ProviderContextWindow(
                    "fixture",
                    "fixture-model",
                    512,
                ),
                provider_max_output_tokens=512,
            )

            result = await runtime.run("hello", session_id=session_id)
            compaction_items = await store.load_compaction_items(session_id)

        self.assertEqual(len(provider.calls), 0)
        self.assertEqual(len(compaction_items), 0)
        self.assertEqual(
            result.response.splitlines()[0],
            "I could not produce a reliable final summary from the available evidence.",
        )
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(len(preflights), 1)
        self.assertEqual(preflights[0].data["status"], ContextPreflightStatus.BLOCKED.value)

    async def test_unknown_capacity_keeps_existing_provider_path(self) -> None:
        provider = _ScriptedProvider((ModelCompleted("stop", response_text="answer"),))
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=_EmptyToolCollection(),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(Path(directory)),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                provider_max_output_tokens=256,
            )
            result = await runtime.run("hello")

        self.assertEqual(len(provider.calls), 1)
        preflight = next(
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        )
        self.assertEqual(preflight.data["status"], ContextPreflightStatus.UNKNOWN.value)
        self.assertIsNone(preflight.data["capacity_tokens"])

    async def test_first_request_compacts_once_and_rebuilds_the_request(self) -> None:
        provider = _SequencedProvider(
            (
                (ModelCompleted("stop", response_text="bounded history summary"),),
                (ModelCompleted("stop", response_text="final answer"),),
            )
        )
        definitions = (
            ToolDefinition(
                "inspect",
                "Inspect the workspace.",
                {"type": "object", "additionalProperties": False},
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SqliteSessionStore(root / "sessions.db")
            await store.initialize()
            session_id = await store.create_session(str(root), "fixture", "fixture-model")
            gate = ContextCompactionRuntimeGate(
                ContextCompactionTriggerService(
                    ContextCompactionApplicationService(store, provider),
                    planner=ContextCompactionPlanner(
                        ContextCompactionPolicy(minimum_recent_items=1, max_summary_tokens=64)
                    ),
                )
            )
            history = (
                Message(Role.SYSTEM, "Use the repository context."),
                *(Message(Role.USER, f"history-{index}: " + "x" * 1_500) for index in range(8)),
            )
            runtime = AgentRuntime(
                provider=provider,
                tools=_StaticToolCollection(definitions),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(),
                tool_context=ToolContext(root),
                session_store=store,
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                compaction_runtime_gate=gate,
                provider_context_window=ProviderContextWindow("fixture", "fixture-model", 2_000),
                provider_max_output_tokens=128,
            )

            result = await runtime.run(
                "answer from the compacted context",
                session_id=session_id,
                initial_items=history,
            )
            compaction_count = len(await store.load_compaction_items(session_id))

        self.assertEqual(result.response, "final answer")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0][1], ())
        self.assertEqual(provider.calls[1][1], definitions)
        self.assertEqual(compaction_count, 1)
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(
            [event.data["status"] for event in preflights],
            [
                ContextPreflightStatus.COMPACTION_REQUIRED.value,
                ContextPreflightStatus.SAFE.value,
            ],
        )
        snapshot_event = next(
            event for event in result.events if event.kind is AgentEventKind.MODEL_REQUEST_SNAPSHOT
        )
        rebuilt_snapshot = ModelRequestSnapshot.build(
            context=provider.calls[1][0],
            tools=provider.calls[1][1],
            provider=provider.provider_name,
            model=provider.model_name,
            context_affinity=provider.context_affinity,
            step=1,
            reasoning_effort=ReasoningEffort.HIGH,
        )
        self.assertEqual(
            snapshot_event.data["request_fingerprint"], rebuilt_snapshot.request_fingerprint
        )


if __name__ == "__main__":
    unittest.main()
