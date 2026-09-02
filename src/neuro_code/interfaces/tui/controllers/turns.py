from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import monotonic

from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.application.workflows.plan_execution import (
    ExecutePlanRequest,
)
from neuro_code.application.workflows.session_task_execution import (
    RunSessionTaskRequest,
)
from neuro_code.domain.conversation.context import estimate_context_tokens, estimate_text_tokens
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.execution import (
    TurnCancellationPolicy,
)
from neuro_code.domain.plans import SessionPlan
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.execution import recoverable_terminal_status
from neuro_code.interfaces.tui.state import (
    _MAX_QUEUED_INTERJECTIONS,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.errors import ProviderError
from neuro_code.shared.redaction import redact_sensitive_text


class TurnControllerMixin(TuiAppControllerMixin):
    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        prompt = event.value.strip()
        event.input.value = ""
        if not prompt:
            return
        if self._pending_interaction_request_id is not None and self._user_interaction is not None:
            request_id = self._pending_interaction_request_id
            self._pending_interaction_request_id = None
            self._user_interaction.resolve(request_id, prompt)
            self._write_ui_entry("status", "interaction.submitted")
            return
        if prompt.startswith("/"):
            await self._dispatch_slash_command(prompt)
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            if not self._first_token_seen and self._pending_assistant is not None:
                if not self._queue_interjection(prompt):
                    event.input.value = prompt
                    event.input.cursor_position = len(prompt)
            else:
                self._write_ui_entry("error", "turn.running")
            return

        self._submit_prompt(prompt)

    def _submit_prompt(self, prompt: str) -> None:
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        self._active_prompt = prompt
        self._active_prompt_entry_index = len(self._entries)
        self._turn_pristine_rewound = False
        self._write_entry("user", prompt)
        self._context_used_tokens += 4 + estimate_text_tokens(prompt)
        self._context_usage_estimated = True
        self._refresh_runtime_bar()
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_prompt(prompt),
            name="agent-turn",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    def _queue_interjection(self, prompt: str) -> bool:
        if len(self._queued_interjections) >= _MAX_QUEUED_INTERJECTIONS:
            self._write_ui_entry("error", "turn.interjection_limit")
            return False
        self._queued_interjections.append(prompt)
        self._write_ui_entry("status", "turn.interjection_queued")
        return True

    def _start_next_interjection(self) -> None:
        if (
            self._turn_worker is not None and self._turn_worker.is_running
        ) or not self._queued_interjections:
            return
        self._submit_prompt(self._queued_interjections.popleft())

    def _restore_queued_interjections(self) -> None:
        """Return every unsent interjection to the draft without auto-submitting it.

        将所有未发送的插话放回草稿,不自动提交."""

        if not self._queued_interjections:
            return
        queued = tuple(self._queued_interjections)
        self._queued_interjections.clear()
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        prompt.value = "\n\n".join((*queued, prompt.value)) if prompt.value else "\n\n".join(queued)
        prompt.cursor_position = len(prompt.value)
        self._write_ui_entry("status", "turn.interjections_restored", count=len(queued))

    async def _restore_pristine_prompt(self) -> None:
        prompt_text = self._active_prompt
        if not prompt_text:
            return
        entry_index = self._active_prompt_entry_index
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        if not prompt.value:
            if entry_index is not None and 0 <= entry_index < len(self._entries):
                entry = self._entries[entry_index]
                if entry.category == "user" and entry.text == prompt_text:
                    await self._remove_transcript_entry(entry_index)
            prompt.value = prompt_text
            prompt.cursor_position = len(prompt_text)
            self._write_ui_entry("status", "turn.draft_restored")
            return
        self._write_ui_entry("status", "turn.draft_preserved")

    async def _run_prompt(self, prompt: str) -> None:
        turn_service = self._turn_service
        if turn_service is not None:
            request = RunTurnRequest(
                prompt,
                cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
                expected_session_id=self._runner.session_id,
            )
            await self._run_agent_turn(
                lambda: turn_service.run_turn(request, sink=self._handle_event)
            )
            return
        await self._run_agent_turn(
            lambda: self._runner.run(
                prompt,
                sink=self._handle_event,
                cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
            )
        )

    async def _run_background_wake(self) -> None:
        await self._run_agent_turn(
            lambda: self._runner.run_background_wake(sink=self._handle_event)
        )

    async def _run_plan_execution(self) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        service = self._plan_execution_service
        if service is not None:
            await self._run_agent_turn(
                lambda: service.execute_plan(
                    ExecutePlanRequest(),
                    sink=self._handle_event,
                )
            )
            return
        await self._run_agent_turn(lambda: controller.execute_plan(sink=self._handle_event))

    async def _run_queued_plan(self, task_id: str) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        service = self._queued_plan_execution_service
        if service is not None:
            await self._run_agent_turn(
                lambda: service.run_session_task(
                    RunSessionTaskRequest(task_id),
                    sink=self._handle_event,
                )
            )
            return
        await self._run_agent_turn(
            lambda: controller.run_session_task(task_id, sink=self._handle_event)
        )

    async def _run_agent_turn(
        self,
        run: Callable[[], Awaitable[AgentRunResult]],
    ) -> None:
        prompt_input = self._main_screen_query_one("#prompt", PromptInput)
        completed = False
        try:
            result = await run()
            if self._background_wake_active:
                await self._complete_background_wake()
            completed = True
            response = result.response or ui_text(self._language, "turn.no_response")
            if not self._turn_usage_reported:
                self._context_used_tokens = (
                    estimate_context_tokens(result.items)
                    if result.items
                    else self._context_used_tokens + 4 + estimate_text_tokens(response)
                )
                self._context_usage_estimated = True
                self._refresh_runtime_bar()
            self._finish_streamed_assistant_response(result, fallback=response)
            if self._terminal_execution_recoverable and self._terminal_execution_status is not None:
                self._write_ui_entry(
                    "recoverable",
                    f"turn.{self._terminal_execution_status}_recoverable",
                )
            elif self._turn_completion is not None:
                duration, steps = self._turn_completion
                self._write_ui_entry(
                    "status",
                    "turn.completed",
                    duration=duration,
                    steps=steps,
                )
        except asyncio.CancelledError:
            await self._discard_pending_assistant()
            if self._turn_pristine_rewound:
                await self._restore_pristine_prompt()
            self._restore_queued_interjections()
            self._write_ui_entry("status", "turn.cancelled")
            raise
        except Exception as error:
            await self._discard_pending_assistant()
            self._restore_queued_interjections()
            self._write_turn_failure(error)
        finally:
            self._pending_interaction_request_id = None
            if self._background_wake_active:
                self._background_wake_state = self._background_wake_state.abandon_wake(
                    failed_at=datetime.now(UTC)
                )
                self._background_wake_active = False
                self._background_wake_task_ids = ()
                await self._persist_background_wake_state()
            self._stop_model_loading()
            prompt_input.focus()
            if completed and self._queued_interjections:
                self.call_after_refresh(self._start_next_interjection)
            if completed or self._turn_pristine_rewound or self._active_prompt is not None:
                self._active_prompt = None
                self._active_prompt_entry_index = None

    def _write_turn_failure(self, error: Exception) -> None:
        """Render a failed turn without implying that its durable session was lost.

        将失败回合显示为可恢复状态,避免暗示其持久化会话已经丢失.
        """

        if isinstance(error, ProviderError):
            key = (
                "turn.provider_balance_recoverable"
                if self._provider_balance_is_insufficient(error)
                else "turn.provider_failure_recoverable"
            )
            self._write_ui_entry("recoverable", key)
            return
        detail = redact_sensitive_text(str(error))
        self._write_entry("error", f"{type(error).__name__}: {detail}")

    @staticmethod
    def _provider_balance_is_insufficient(error: ProviderError) -> bool:
        """Recognize the actionable payment failure without parsing provider payloads.

        识别可操作的付款失败,但不解析或暴露 Provider 原始载荷.
        """

        return error.failure.status_code == 402

    async def _handle_event(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind is AgentEventKind.USER_INPUT_REQUESTED:
            request_id = data.get("request_id")
            question = data.get("question")
            if isinstance(request_id, str) and isinstance(question, str):
                self._pending_interaction_request_id = request_id
                self._turn_activity_kind = "waiting_input"
                self._turn_activity_started_at = monotonic()
                self._refresh_turn_activity()
                options = data.get("options")
                lines = [question]
                if isinstance(options, Sequence) and not isinstance(options, str | bytes):
                    for index, option in enumerate(options, start=1):
                        if isinstance(option, Mapping) and isinstance(option.get("label"), str):
                            lines.append(f"{index}. {option['label']}")
                self._write_entry("status", "\n".join(lines))
        elif event.kind is AgentEventKind.USER_INPUT_RESOLVED:
            self._pending_interaction_request_id = None
            self._turn_activity_kind = "continuing"
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.MODEL_STEP_STARTED:
            self._seal_pending_assistant()
            self._turn_activity_kind = "model"
            self._turn_activity_tool_name = None
            self._turn_activity_tool_started_at = None
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.TEXT_DELTA:
            text = data.get("text")
            if isinstance(text, str):
                self._finalizing = False
                if text:
                    self._active_tool_activity_group = None
                    self._first_token_seen = True
                    self._turn_activity_kind = "responding"
                    self._turn_activity_tool_name = None
                    self._turn_activity_tool_started_at = None
                self._assistant_parts.append(text)
                self._update_pending_assistant("".join(self._assistant_parts))
                self._refresh_turn_activity()
        elif event.kind is AgentEventKind.FINALIZING_STARTED:
            self._finalizing = True
            self._turn_activity_kind = "finalizing"
            self._turn_activity_tool_name = None
            self._turn_activity_tool_started_at = None
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.ULTRACODE_DELEGATION_PROGRESS:
            decision = data.get("decision")
            state = data.get("state")
            if isinstance(decision, str) and isinstance(state, str):
                self._turn_activity_kind = "orchestrating"
                self._turn_activity_tool_name = None
                self._turn_activity_tool_started_at = None
                self._refresh_turn_activity()
                self._write_entry("status", f"Ultracode {decision} · {state}")
        elif event.kind is AgentEventKind.REASONING_DELTA:
            text = data.get("text")
            if isinstance(text, str) and text:
                self._first_token_seen = True
                self._turn_activity_kind = "reasoning"
                self._refresh_turn_activity()
        elif event.kind is AgentEventKind.MODEL_THINKING_COMPLETED:
            self._turn_activity_kind = "continuing"
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.CONTEXT_USAGE_UPDATED:
            used_tokens = data.get("used_tokens")
            if isinstance(used_tokens, int) and not isinstance(used_tokens, bool):
                self._context_used_tokens = max(0, used_tokens)
                self._context_usage_estimated = data.get("estimated") is not False
                self._turn_usage_reported = not self._context_usage_estimated
                self._refresh_runtime_bar()
        elif event.kind is AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER:
            raw_task_ids = data.get("task_ids")
            if (
                self._background_wake_active
                and isinstance(raw_task_ids, Sequence)
                and not isinstance(raw_task_ids, str | bytes)
            ):
                task_ids = tuple(task_id for task_id in raw_task_ids if isinstance(task_id, str))
                self._background_wake_task_ids = task_ids
        elif event.kind is AgentEventKind.PROVIDER_ATTEMPT_FAILED:
            provider = self._field(data, "provider")
            message = self._field(data, "message")
            self._write_ui_entry(
                "error",
                "provider.failed",
                provider=provider,
                message=message,
            )
        elif event.kind is AgentEventKind.PROVIDER_SELECTED:
            provider = self._field(data, "provider")
            model = self._field(data, "model")
            self._provider_name = provider
            self._model_name = model
            context_window_tokens = data.get("context_window_tokens")
            self._context_window_tokens = (
                context_window_tokens
                if isinstance(context_window_tokens, int)
                and not isinstance(context_window_tokens, bool)
                and context_window_tokens > 0
                else None
            )
            self._refresh_runtime_bar()
            key = (
                "provider.fallback_selected"
                if data.get("failover") is True
                else "provider.selected"
            )
            self._write_ui_entry("status", key, provider=provider, model=model)
        elif event.kind in {
            AgentEventKind.BACKEND_TOOL_STARTED,
            AgentEventKind.BACKEND_TOOL_COMPLETED,
            AgentEventKind.TOOL_REQUESTED,
            AgentEventKind.TOOL_PERMISSION,
            AgentEventKind.TOOL_APPROVAL_REQUESTED,
            AgentEventKind.TOOL_APPROVAL_RESOLVED,
            AgentEventKind.TOOL_STARTED,
            AgentEventKind.TOOL_COMPLETED,
            AgentEventKind.TOOL_FAILED,
        }:
            if event.kind in {
                AgentEventKind.BACKEND_TOOL_STARTED,
                AgentEventKind.TOOL_REQUESTED,
            }:
                self._seal_pending_assistant()
            self._handle_tool_feedback_event(event)
        elif event.kind is AgentEventKind.PLAN_UPDATED:
            try:
                self._plan = SessionPlan.from_dict(data)
            except ValueError:
                return
            self._plan_comments = ()
            self._upsert_plan_entry(self._plan)
        elif event.kind is AgentEventKind.PLAN_EXECUTION_REQUESTED:
            self._write_ui_entry("status", "plan.execution_requested")
        elif event.kind is AgentEventKind.TURN_COMPLETED:
            self._finalizing = False
            self._turn_activity_kind = "completed"
            self._refresh_turn_activity()
            self._turn_completion = (
                self._event_duration(data),
                self._positive_int(data.get("step"), fallback=1),
            )
            execution_status = recoverable_terminal_status(data)
            if execution_status is not None:
                self._terminal_execution_status = execution_status.value
                self._terminal_execution_recoverable = True
            else:
                self._terminal_execution_status = None
                self._terminal_execution_recoverable = False
        elif event.kind is AgentEventKind.TURN_FAILED:
            self._turn_activity_kind = "failed"
            self._refresh_turn_activity()
            self._turn_pristine_rewound = data.get("pristine_rewound") is True
