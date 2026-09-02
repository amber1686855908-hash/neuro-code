from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.containers import VerticalScroll

from neuro_code.application.workflows.plan_scheduling import (
    SchedulePlanRequest,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.plans import PlanComment, PlanStepStatus, SessionPlan
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.state import (
    _PROMPT_MARK,
    _SUCCESS_MARK,
    ToolFeedbackState,
    TranscriptEntry,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    ACCENT_CODE,
    ACCENT_SUCCESS,
    TEXT_BODY,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from neuro_code.interfaces.tui.widgets import ConversationMessage, ToolFeedbackMessage


class PlanControllerMixin(TuiAppControllerMixin):
    async def _execute_plan(self) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        plan = controller.plan
        self._plan = plan
        if plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        await self._apply_interaction_mode(InteractionMode.ACCEPT_EDITS)
        if self._interaction_mode is not InteractionMode.ACCEPT_EDITS:
            return
        self._write_ui_entry("user", "plan.execution_user")
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
            self._run_plan_execution(),
            name="agent-plan-execution",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _schedule_plan(self) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        if controller.plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        try:
            service = self._plan_scheduling_service
            if service is not None:
                task = await service.schedule_plan(SchedulePlanRequest())
            else:
                task = await controller.schedule_plan()
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_ui_entry("status", "plan.scheduled", task_id=task.task_id)

    async def _add_plan_comment(self, arguments: str) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.comment_unavailable")
            return
        plan = controller.plan
        self._plan = plan
        if plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        raw_index, separator, content = arguments.strip().partition(" ")
        if not separator or not raw_index or not content.strip():
            self._write_ui_entry("error", "plan.comment_usage")
            return
        try:
            step_index = int(raw_index)
        except ValueError:
            self._write_ui_entry("error", "plan.comment_step_invalid", index=raw_index)
            return
        if not 1 <= step_index <= len(plan.steps):
            self._write_ui_entry("error", "plan.comment_step_invalid", index=raw_index)
            return
        try:
            await controller.add_plan_comment(step_index, content)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_ui_entry("status", "plan.comment_added", index=step_index)
        await self._show_plan()

    async def _show_plan(self) -> None:
        controller = self._plan_controller
        plan = controller.plan if controller is not None else self._plan
        self._plan = plan
        if plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        comments: tuple[PlanComment, ...] = ()
        if controller is not None:
            try:
                comments = await controller.list_plan_comments()
            except Exception as error:
                self._write_entry("error", f"{type(error).__name__}: {error}")
                return
        self._plan_comments = comments
        self._upsert_plan_entry(plan, comments)

    def _render_plan(self, plan: SessionPlan, comments: Sequence[PlanComment] = ()) -> Text:
        body = Text(overflow="fold")
        body.append(
            ui_text(self._language, "plan.heading"),
            style=f"bold {TEXT_PRIMARY}",
        )
        if plan.explanation is not None:
            body.append("\n")
            body.append(
                ui_text(self._language, "plan.purpose", explanation=plan.explanation),
                style=TEXT_SECONDARY,
            )
        for index, step in enumerate(plan.steps, start=1):
            marker, marker_style = {
                PlanStepStatus.COMPLETED: (_SUCCESS_MARK, ACCENT_SUCCESS),
                PlanStepStatus.IN_PROGRESS: (_PROMPT_MARK, ACCENT_CODE),
                PlanStepStatus.PENDING: ("□", TEXT_SECONDARY),
            }[step.status]
            body.append("\n")
            body.append(f"{marker} ", style=marker_style)
            body.append(step.step, style=TEXT_BODY)
            for comment in comments:
                if comment.step_index == index:
                    body.append("\n  · ", style=TEXT_MUTED)
                    body.append(comment.content, style=TEXT_SECONDARY)
        return body

    def _upsert_plan_entry(
        self,
        plan: SessionPlan,
        comments: Sequence[PlanComment] = (),
    ) -> None:
        self._active_tool_activity_group = None
        rendered = self._render_plan(plan, comments)
        index = self._plan_entry_index
        if index is not None and 0 <= index < len(self._entries):
            self._entries[index] = TranscriptEntry("plan", rendered.plain)
            widget = self._entry_widgets[index]
            widget.update(rendered)
            transcript = self._main_screen_query_one("#transcript", VerticalScroll)
            self._move_plan_entry_to_latest_position(index, widget, transcript)
            if transcript.is_vertical_scroll_end:
                transcript.scroll_end(animate=False)
            return

        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        widget = ConversationMessage("plan", rendered)
        pending = self._pending_assistant
        if pending is not None and pending.parent is transcript:
            transcript.mount(widget, before=pending)
        else:
            transcript.mount(widget)
        self._entries.append(TranscriptEntry("plan", rendered.plain))
        self._entry_widgets.append(widget)
        self._plan_entry_index = len(self._entries) - 1
        if follow:
            transcript.scroll_end(animate=False)

    def _move_plan_entry_to_latest_position(
        self,
        index: int,
        widget: ConversationMessage,
        transcript: VerticalScroll,
    ) -> None:
        """Keep one Plan node adjacent to the update that most recently changed it.

        保留一个计划节点,并让它紧邻最近一次更新计划的操作.
        """

        if index != len(self._entries) - 1:
            plan_entry = self._entries.pop(index)
            plan_widget = self._entry_widgets.pop(index)
            self._entries.append(plan_entry)
            self._entry_widgets.append(plan_widget)
            remapped_tool_feedback: dict[int, ToolFeedbackState] = {}
            for entry_index, state in self._tool_feedback_by_entry.items():
                remapped_index = entry_index - 1 if entry_index > index else entry_index
                state.entry_index = remapped_index
                remapped_tool_feedback[remapped_index] = state
                remapped_widget = self._entry_widgets[remapped_index]
                if isinstance(remapped_widget, ToolFeedbackMessage):
                    remapped_widget.entry_index = remapped_index
            self._tool_feedback_by_entry = remapped_tool_feedback
            self._rebuild_tool_activity_indexes()
            self._plan_entry_index = len(self._entries) - 1

        pending = self._pending_assistant
        if pending is not None and pending.parent is transcript:
            transcript.move_child(widget, before=pending)
            return
        children = tuple(transcript.children)
        if children and children[-1] is not widget:
            transcript.move_child(widget, after=children[-1])
