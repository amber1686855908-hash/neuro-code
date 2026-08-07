from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.runtime.finalization import (
    AgentFinalizer,
    FinalizationAttempt,
    FinalizationEvidence,
    FinalizationResult,
    FinalizationStatus,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import Message, Role, ToolCall
from neuro_code.domain.execution import SupervisorReasonCode
from neuro_code.domain.tools import ToolDefinition
from neuro_code.shared.errors import ProviderError


@dataclass(frozen=True, slots=True)
class RequestRecord:
    tool_policy: ModelToolPolicy
    tools: tuple[ToolDefinition, ...]
    finalizer_instructions: tuple[str, ...]
    rejected_calls: tuple[tuple[str, str], ...]
    rejected_results: tuple[tuple[str, str], ...]


class ScriptedFinalizerProvider:
    provider_name = "scripted-finalizer"
    model_name = "fixture-model"
    context_affinity = "fixture-v1"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent | BaseException]]) -> None:
        self._scripts = list(scripts)
        self.requests: list[RequestRecord] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        instructions = tuple(
            message.content
            for message in context.messages
            if message.role is Role.SYSTEM and message.content.startswith("You are producing")
        )
        rejected_calls = tuple(
            (call.id, call.name)
            for message in context.messages
            if message.role is Role.ASSISTANT
            for call in message.tool_calls
            if call.id.startswith("finalizer-rejected-")
        )
        rejected_results = tuple(
            (message.tool_call_id or "", message.name or "")
            for message in context.messages
            if message.role is Role.TOOL
            and (message.tool_call_id or "").startswith("finalizer-rejected-")
        )
        self.requests.append(
            RequestRecord(
                tool_policy,
                tuple(tools),
                instructions,
                rejected_calls,
                rejected_results,
            )
        )
        script = self._scripts.pop(0)
        for event in script:
            if isinstance(event, BaseException):
                raise event
            yield event


def context() -> ModelContext:
    return ModelContext((Message(Role.SYSTEM, "system"), Message(Role.USER, "help me")))


def evidence(**overrides: object) -> FinalizationEvidence:
    values: dict[str, object] = {
        "trigger": SupervisorReasonCode.MODEL_CALL_RESERVE,
        "completed_items": ("Reviewed the available evidence.",),
        "verification": ("No verification was run.",),
    }
    values.update(overrides)
    return FinalizationEvidence(**values)  # type: ignore[arg-type]


class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalizer_uses_disabled_tool_policy(self) -> None:
        provider = ScriptedFinalizerProvider(((ModelTextDelta("done"), ModelCompleted("stop")),))

        result = await AgentFinalizer(provider).finalize(context(), evidence())

        self.assertIs(result.status, FinalizationStatus.COMPLETED)
        self.assertEqual(
            [request.tool_policy for request in provider.requests], [ModelToolPolicy.DISABLED]
        )
        self.assertEqual(provider.requests[0].tools, ())

    async def test_finalizer_buffers_text_and_prefers_completion_response_text(self) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (
                    ModelTextDelta("streamed "),
                    ModelTextDelta("text"),
                    ModelCompleted("stop", response_text="canonical"),
                ),
            )
        )

        result = await AgentFinalizer(provider).finalize(context(), evidence())

        self.assertEqual(result.response, "canonical")
        self.assertEqual(result.attempts[0].buffered_text_length, len("canonical"))
        self.assertEqual(len(provider.requests), 1)

    async def test_finalizer_returns_completed_after_one_attempt_and_counts_tokens(self) -> None:
        provider = ScriptedFinalizerProvider(
            ((ModelTextDelta("done"), ModelCompleted("stop", 11, 7)),)
        )

        result = await AgentFinalizer(provider).finalize(context(), evidence())

        self.assertTrue(result.completed)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual((result.total_input_tokens, result.total_output_tokens), (11, 7))

    async def test_finalizer_retries_after_illegal_tool_call_without_execution(self) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (
                    ModelToolCall(ToolCall("remote", "read_file", {"path": "secret.txt"})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("safe answer"), ModelCompleted("stop")),
            )
        )

        result = await AgentFinalizer(provider).finalize(context(), evidence())

        self.assertIs(result.status, FinalizationStatus.COMPLETED)
        self.assertEqual(result.illegal_tool_calls, 1)
        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(
            all(request.tool_policy is ModelToolPolicy.DISABLED for request in provider.requests)
        )
        self.assertEqual(provider.requests[1].rejected_calls, provider.requests[1].rejected_results)

    async def test_finalizer_pairs_every_rejected_tool_call_and_discards_partial_text(self) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (
                    ModelTextDelta("discard this"),
                    ModelToolCall(ToolCall("one", "read_file", {"api_key": "plain-secret"})),
                    ModelToolCall(ToolCall("two", "bash", {"command": "echo plain-secret"})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("accepted"), ModelCompleted("stop")),
            )
        )

        result = await AgentFinalizer(provider, redaction_values=("plain-secret",)).finalize(
            context(), evidence()
        )

        self.assertEqual(result.response, "accepted")
        self.assertEqual(result.illegal_tool_calls, 2)
        self.assertNotIn("discard this", result.response)
        self.assertEqual(len(provider.requests[1].rejected_calls), 2)
        self.assertEqual(provider.requests[1].rejected_calls, provider.requests[1].rejected_results)
        self.assertNotIn("plain-secret", repr(result))

    async def test_illegal_tool_arguments_are_not_retained_in_trace_or_retry_context(self) -> None:
        secret = "nonstandard-finalizer-secret"
        provider = ScriptedFinalizerProvider(
            (
                (
                    ModelToolCall(ToolCall("remote", "read_file", {"credential": secret})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("safe"), ModelCompleted("stop")),
            )
        )

        result = await AgentFinalizer(provider, redaction_values=(secret,)).finalize(
            context(), evidence()
        )

        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, repr(result.attempts))
        self.assertNotIn(secret, repr(provider.requests[1]))

    async def test_finalizer_stops_after_bounded_illegal_tool_retries(self) -> None:
        illegal = (ModelToolCall(ToolCall("remote", "search", {})), ModelCompleted("tool_calls"))
        provider = ScriptedFinalizerProvider((illegal, illegal))

        result = await AgentFinalizer(provider, max_attempts=2).finalize(context(), evidence())

        self.assertIs(result.status, FinalizationStatus.TOOL_CALL_REJECTED)
        self.assertFalse(result.completed)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(result.illegal_tool_calls, 2)
        self.assertIn("could not produce a reliable final summary", result.response)

    async def test_finalizer_retries_empty_response_then_returns_empty_response_when_bounded(
        self,
    ) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (ModelCompleted("stop", response_text=""),),
                (ModelTextDelta("after retry"), ModelCompleted("stop")),
            )
        )

        completed = await AgentFinalizer(provider).finalize(context(), evidence())

        self.assertIs(completed.status, FinalizationStatus.COMPLETED)
        self.assertEqual(len(completed.attempts), 2)
        empty_provider = ScriptedFinalizerProvider(
            (
                (ModelCompleted("stop", response_text=""),),
                (ModelCompleted("stop", response_text=" "),),
            )
        )
        empty = await AgentFinalizer(empty_provider).finalize(context(), evidence())
        self.assertIs(empty.status, FinalizationStatus.EMPTY_RESPONSE)
        self.assertEqual(len(empty_provider.requests), 2)
        self.assertIn("could not produce a final summary", empty.response)

    async def test_provider_error_after_rejected_attempt_is_propagated(self) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (ModelToolCall(ToolCall("remote", "read_file", {})), ModelCompleted("tool_calls")),
                (ProviderError("provider down"),),
            )
        )

        with self.assertRaisesRegex(ProviderError, "provider down"):
            await AgentFinalizer(provider).finalize(context(), evidence())

    async def test_provider_error_is_not_converted_to_a_finalization_result(self) -> None:
        provider = ScriptedFinalizerProvider(((ProviderError("initial provider error"),),))

        with self.assertRaisesRegex(ProviderError, "initial provider error"):
            await AgentFinalizer(provider).finalize(context(), evidence())

    async def test_cancellation_is_not_swallowed(self) -> None:
        provider = ScriptedFinalizerProvider(((asyncio.CancelledError(),),))

        with self.assertRaises(asyncio.CancelledError):
            await AgentFinalizer(provider).finalize(context(), evidence())

    async def test_original_context_and_retry_state_are_not_modified_or_shared(self) -> None:
        original = context()
        provider = ScriptedFinalizerProvider(
            (
                (ModelToolCall(ToolCall("remote", "read_file", {})), ModelCompleted("tool_calls")),
                (ModelTextDelta("first"), ModelCompleted("stop")),
                (ModelTextDelta("second"), ModelCompleted("stop")),
            )
        )
        finalizer = AgentFinalizer(provider)

        first = await finalizer.finalize(original, evidence())
        second = await finalizer.finalize(original, evidence())

        self.assertEqual(original, context())
        self.assertEqual(first.response, "first")
        self.assertEqual(second.response, "second")
        self.assertEqual(provider.requests[1].rejected_calls, provider.requests[1].rejected_results)
        self.assertEqual(provider.requests[2].rejected_calls, ())

    async def test_finalization_does_not_make_disabled_policy_sticky_for_provider(self) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (ModelTextDelta("final"), ModelCompleted("stop")),
                (ModelTextDelta("ordinary"), ModelCompleted("stop")),
            )
        )

        await AgentFinalizer(provider).finalize(context(), evidence())
        ordinary_events = [
            event
            async for event in provider.stream(
                context(),
                (),
                tool_policy=ModelToolPolicy.ALLOWED,
            )
        ]

        self.assertEqual(len(ordinary_events), 2)
        self.assertEqual(
            [request.tool_policy for request in provider.requests],
            [ModelToolPolicy.DISABLED, ModelToolPolicy.ALLOWED],
        )

    async def test_evidence_is_redacted_and_bounded_and_prompt_forbids_unverified_claims(
        self,
    ) -> None:
        secret = "unmistakable-secret"
        provider = ScriptedFinalizerProvider(((ModelTextDelta("done"), ModelCompleted("stop")),))
        long_item = "x" * 600
        finalizer = AgentFinalizer(provider, redaction_values=(secret,))

        await finalizer.finalize(
            context(),
            evidence(
                completed_items=(secret, long_item, "one", "two", "three", "four"),
                unverified_items=("verification did not run",),
            ),
        )

        prompt = provider.requests[0].finalizer_instructions[0]
        self.assertNotIn(secret, prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertNotIn("x" * 500, prompt)
        self.assertIn("Do not claim an edit or validation happened", prompt)
        self.assertNotIn("Supervisor", prompt)
        self.assertNotIn("digest", prompt)

    async def test_unknown_token_usage_remains_unknown_and_attempt_history_is_bounded(self) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (ModelCompleted("stop", input_tokens=None, output_tokens=3, response_text=""),),
                (ModelTextDelta("done"), ModelCompleted("stop", 4, None)),
            )
        )

        result = await AgentFinalizer(provider, max_attempts=2).finalize(context(), evidence())

        self.assertIsNone(result.total_input_tokens)
        self.assertIsNone(result.total_output_tokens)
        self.assertEqual(len(result.attempts), 2)

    def test_evidence_is_bounded_and_typed_before_prompt_construction(self) -> None:
        item = "x" * 600
        bounded = FinalizationEvidence(
            SupervisorReasonCode.NO_PROGRESS,
            completed_items=(item, "one", "two", "three", "four"),
        )

        self.assertEqual(len(bounded.completed_items), 4)
        self.assertLess(len(bounded.completed_items[0]), len(item))
        with self.assertRaisesRegex(TypeError, "trigger"):
            FinalizationEvidence("not-a-reason")  # type: ignore[arg-type]

    async def test_finalizer_rejects_invalid_constructor_and_missing_completion(self) -> None:
        provider = ScriptedFinalizerProvider(((),))
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_attempts"):
                AgentFinalizer(provider, max_attempts=value)
        with self.assertRaisesRegex(TypeError, "redaction_values"):
            AgentFinalizer(provider, redaction_values=("safe", 1))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ProviderError, "without a completion"):
            await AgentFinalizer(provider).finalize(context(), evidence())

    async def test_finalizer_requires_completion_after_buffered_text(self) -> None:
        provider = ScriptedFinalizerProvider(((ModelTextDelta("partial answer"),),))

        with self.assertRaisesRegex(ProviderError, "without a completion"):
            await AgentFinalizer(provider).finalize(context(), evidence())

        self.assertEqual(len(provider.requests), 1)

    async def test_finalizer_totals_known_usage_without_claiming_unknown_usage(self) -> None:
        provider = ScriptedFinalizerProvider(
            (
                (ModelCompleted("stop", 2, 3, response_text=""),),
                (ModelTextDelta("accepted"), ModelCompleted("stop", None, 5)),
            )
        )

        result = await AgentFinalizer(provider).finalize(context(), evidence())

        self.assertIs(result.status, FinalizationStatus.COMPLETED)
        self.assertIsNone(result.total_input_tokens)
        self.assertEqual(result.total_output_tokens, 8)
        self.assertEqual(
            tuple((attempt.input_tokens, attempt.output_tokens) for attempt in result.attempts),
            ((2, 3), (None, 5)),
        )

    def test_finalization_values_bound_every_evidence_category_and_reject_incoherent_results(
        self,
    ) -> None:
        long_item = "x" * 600
        values = (long_item, "one", "two", "three", "four", "")
        bounded = FinalizationEvidence(
            SupervisorReasonCode.NO_PROGRESS,
            completed_items=values,
            workspace_changes=values,
            verification=values,
            unverified_items=values,
            blocker=long_item,
            uncertainty=values,
        )
        for items in (
            bounded.completed_items,
            bounded.workspace_changes,
            bounded.verification,
            bounded.unverified_items,
            bounded.uncertainty,
        ):
            self.assertEqual(len(items), 4)
            self.assertTrue(all(len(item) <= 400 for item in items))
        assert bounded.blocker is not None
        self.assertLessEqual(len(bounded.blocker), 400)
        with self.assertRaisesRegex(TypeError, "workspace_changes"):
            FinalizationEvidence(
                SupervisorReasonCode.NO_PROGRESS,
                workspace_changes=("safe", 1),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "completed attempt"):
            FinalizationAttempt(1, True, None, None, None, 0, 0)
        attempt = FinalizationAttempt(1, True, "stop", 1, 1, 0, 4)
        with self.assertRaisesRegex(ValueError, "must match attempt history"):
            FinalizationResult(
                FinalizationStatus.COMPLETED,
                "done",
                (attempt,),
                1,
                1,
                1,
                True,
                "stop",
            )
