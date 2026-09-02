from __future__ import annotations

from collections.abc import Mapping
from time import monotonic

from rich.text import Text
from textual.containers import VerticalScroll

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.state import (
    ToolActivityGroupState,
    ToolFeedbackState,
    TranscriptEntry,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    TOOL_DETAIL_STYLE,
)
from neuro_code.interfaces.tui.tool_activity import (
    ToolDisclosureLevel,
)
from neuro_code.interfaces.tui.widgets import ToolFeedbackMessage


class ToolActivityEventsMixin(TuiAppControllerMixin):
    def on_tool_feedback_message_advance_requested(
        self,
        event: ToolFeedbackMessage.AdvanceRequested,
    ) -> None:
        state = self._tool_feedback_by_entry.get(event.card.entry_index)
        if state is None:
            return
        group = self._tool_activity_group_by_entry.get(state.entry_index)
        if group is None:
            return
        if group.disclosure is ToolDisclosureLevel.SUMMARY:
            group.disclosure = ToolDisclosureLevel.PEEK
            group.selected_tool_index = min(group.selected_tool_index, len(group.tools) - 1)
            self._refresh_tool_activity_group(group)
            event.card.focus()
            return
        self._open_tool_inspector(group)

    def on_tool_feedback_message_toggle_peek_requested(
        self,
        event: ToolFeedbackMessage.TogglePeekRequested,
    ) -> None:
        state = self._tool_feedback_by_entry.get(event.card.entry_index)
        if state is None:
            return
        group = self._tool_activity_group_by_entry.get(state.entry_index)
        if group is None:
            return
        group.disclosure = (
            ToolDisclosureLevel.SUMMARY
            if group.disclosure is ToolDisclosureLevel.PEEK
            else ToolDisclosureLevel.PEEK
        )
        self._refresh_tool_activity_group(group)
        event.card.focus()

    def on_tool_feedback_message_collapse_requested(
        self,
        event: ToolFeedbackMessage.CollapseRequested,
    ) -> None:
        state = self._tool_feedback_by_entry.get(event.card.entry_index)
        if state is None:
            return
        group = self._tool_activity_group_by_entry.get(state.entry_index)
        if group is None or group.disclosure is ToolDisclosureLevel.SUMMARY:
            return
        group.disclosure = ToolDisclosureLevel.SUMMARY
        self._refresh_tool_activity_group(group)
        event.card.focus()

    def on_tool_feedback_message_selection_requested(
        self,
        event: ToolFeedbackMessage.SelectionRequested,
    ) -> None:
        state = self._tool_feedback_by_entry.get(event.card.entry_index)
        if state is None:
            return
        group = self._tool_activity_group_by_entry.get(state.entry_index)
        if (
            group is None
            or group.disclosure is not ToolDisclosureLevel.PEEK
            or len(group.tools) < 2
        ):
            return
        group.selected_tool_index = max(
            0,
            min(group.selected_tool_index + event.delta, len(group.tools) - 1),
        )
        self._refresh_tool_activity_group(group)
        event.card.focus()

    def _handle_tool_feedback_event(self, event: AgentEvent) -> None:
        hosted = event.kind in {
            AgentEventKind.BACKEND_TOOL_STARTED,
            AgentEventKind.BACKEND_TOOL_COMPLETED,
        }
        starts_card = event.kind in {
            AgentEventKind.BACKEND_TOOL_STARTED,
            AgentEventKind.TOOL_REQUESTED,
        }
        state = (
            self._start_tool_feedback(event, hosted=hosted)
            if starts_card
            else self._find_or_start_tool_feedback(event, hosted=hosted)
        )
        data = event.data
        if event.kind is AgentEventKind.BACKEND_TOOL_STARTED:
            state.phase = "running"
            if state.started_at is None:
                state.started_at = monotonic()
            self._activate_tool_activity(state)
        elif event.kind is AgentEventKind.BACKEND_TOOL_COMPLETED:
            state.phase = "completed"
            state.duration = self._event_duration(data)
            state.duration_seconds = self._event_duration_seconds(data)
            state.started_at = None
            self._finish_tool_activity(state)
        elif event.kind is AgentEventKind.TOOL_PERMISSION:
            state.permission_effect = self._optional_text(data.get("effect"))
            state.permission_reason = self._optional_text(data.get("reason"))
            if state.permission_effect == "deny":
                state.phase = "permission_denied"
            elif state.permission_effect == "ask":
                state.phase = "approval_required"
            else:
                state.phase = "permitted"
        elif event.kind is AgentEventKind.TOOL_APPROVAL_REQUESTED:
            state.phase = "awaiting_approval"
        elif event.kind is AgentEventKind.TOOL_APPROVAL_RESOLVED:
            state.approval_effect = self._optional_text(data.get("effect"))
            state.approval_outcome = self._optional_text(data.get("outcome"))
            state.approval_reason = self._optional_text(data.get("reason"))
            state.phase = (
                "approval_denied" if state.approval_effect == "deny" else "approval_resolved"
            )
        elif event.kind is AgentEventKind.TOOL_STARTED:
            state.phase = "running"
            if state.started_at is None:
                state.started_at = monotonic()
            self._activate_tool_activity(state)
        elif event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}:
            state.phase = "failed" if event.kind is AgentEventKind.TOOL_FAILED else "completed"
            state.duration = self._event_duration(data)
            state.duration_seconds = self._event_duration_seconds(data)
            state.started_at = None
            state.content = self._optional_text(data.get("content"), allow_empty=True)
            state.is_error = (
                event.kind is AgentEventKind.TOOL_FAILED or data.get("is_error") is True
            )
            raw_metadata = data.get("metadata")
            state.metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else None
            state.artifact_id = self._artifact_id_from_metadata(raw_metadata)
            state.artifact_content = None
            state.artifact_stored_truncated = (
                isinstance(raw_metadata, Mapping)
                and raw_metadata.get("output_artifact_truncated") is True
            )
            state.artifact_read_truncated = False
            state.artifact_loading = False
            state.artifact_unavailable = False
            raw_changes = data.get("workspace_changes")
            state.workspace_changes = (
                dict(raw_changes) if isinstance(raw_changes, Mapping) else None
            )
            self._finish_tool_activity(state)
        self._refresh_tool_feedback(state)

    def _activate_tool_activity(self, state: ToolFeedbackState) -> None:
        self._turn_activity_kind = "tool"
        self._turn_activity_tool_name = state.name
        self._turn_activity_tool_started_at = state.started_at
        self._refresh_turn_activity()

    def _finish_tool_activity(self, state: ToolFeedbackState) -> None:
        if self._turn_activity_kind != "tool" or self._turn_activity_tool_name != state.name:
            return
        self._turn_activity_kind = "continuing"
        self._turn_activity_tool_name = None
        self._turn_activity_tool_started_at = None
        self._refresh_turn_activity()

    def _start_tool_feedback(
        self,
        event: AgentEvent,
        *,
        hosted: bool,
    ) -> ToolFeedbackState:
        data = event.data
        call_id = self._tool_event_id(event)
        raw_arguments = data.get("arguments")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else {}
        state = ToolFeedbackState(
            call_id=call_id,
            name=self._field(data, "name"),
            arguments=arguments,
            entry_index=len(self._entries),
            hosted=hosted,
        )
        group = self._active_tool_activity_group
        if group is None or group.tools[-1].entry_index != len(self._entries) - 1:
            group = ToolActivityGroupState()
            self._tool_activity_groups.append(group)
            self._active_tool_activity_group = group
        group.tools.append(state)
        self._tool_feedback_by_call[(hosted, call_id)] = state
        self._tool_feedback_by_entry[state.entry_index] = state
        self._tool_activity_group_by_entry[state.entry_index] = group
        content = self._tool_summary_line(state)
        self._write_entry("tool", content, tool_state=state)
        self._refresh_tool_activity_group(group)
        return state

    def _find_or_start_tool_feedback(
        self,
        event: AgentEvent,
        *,
        hosted: bool,
    ) -> ToolFeedbackState:
        raw_id = event.data.get("id")
        if isinstance(raw_id, str) and raw_id:
            state = self._tool_feedback_by_call.get((hosted, raw_id))
            if state is not None:
                return state
        name = self._optional_text(event.data.get("name"))
        candidates = (
            state
            for state in self._tool_feedback_by_entry.values()
            if state.hosted is hosted
            and (name is None or state.name == name)
            and state.phase not in {"completed", "failed", "permission_denied", "approval_denied"}
        )
        latest = max(candidates, key=lambda state: state.entry_index, default=None)
        return latest if latest is not None else self._start_tool_feedback(event, hosted=hosted)

    @staticmethod
    def _tool_event_id(event: AgentEvent) -> str:
        raw_id = event.data.get("id")
        return raw_id if isinstance(raw_id, str) and raw_id else f"event-{event.sequence}"

    @staticmethod
    def _optional_text(value: object, *, allow_empty: bool = False) -> str | None:
        if not isinstance(value, str):
            return None
        if value or allow_empty:
            return value
        return None

    @staticmethod
    def _artifact_id_from_metadata(metadata: object) -> str | None:
        if not isinstance(metadata, Mapping):
            return None
        value = metadata.get("output_artifact_id")
        if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 128:
            return None
        return value

    def _refresh_tool_feedback(self, state: ToolFeedbackState) -> None:
        if state.entry_index >= len(self._entries):
            return
        group = self._tool_activity_group_by_entry.get(state.entry_index)
        if group is None:
            body = Text(self._tool_summary_line(state), style=TOOL_DETAIL_STYLE)
            self._entries[state.entry_index] = TranscriptEntry("tool", body.plain)
            widget = self._entry_widgets[state.entry_index]
            widget.update(self._render_tool_feedback(state, body=body))
            if isinstance(widget, ToolFeedbackMessage):
                self._configure_tool_feedback_widget(widget, state)
            self._refresh_active_tool_inspector(state)
            return
        self._refresh_tool_activity_group(group)

    def _refresh_tool_activity_group(self, group: ToolActivityGroupState) -> None:
        if not group.tools or group.entry_index >= len(self._entry_widgets):
            return
        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        leader_index = group.entry_index
        transcript_summary = self._tool_activity_text(group)
        for state in group.tools:
            if state.entry_index >= len(self._entry_widgets):
                continue
            self._entries[state.entry_index] = TranscriptEntry(
                "tool",
                (
                    transcript_summary
                    if state.entry_index == leader_index
                    else self._tool_summary_line(state)
                ),
            )
            widget = self._entry_widgets[state.entry_index]
            if not isinstance(widget, ToolFeedbackMessage):
                continue
            is_leader = state.entry_index == leader_index
            widget.display = is_leader
            widget.can_focus = is_leader
            if is_leader:
                widget.update(self._render_tool_activity_group(group))
                self._configure_tool_feedback_widget(widget, state)
            else:
                widget.set_class(False, "tool-interactive")
                widget.set_class(False, "tool-peek")
        if follow:
            transcript.scroll_end(animate=False)
        self._refresh_active_tool_inspector_group(group)

    def _configure_tool_feedback_widget(
        self,
        widget: ToolFeedbackMessage,
        state: ToolFeedbackState,
    ) -> None:
        group = self._tool_activity_group_by_entry.get(state.entry_index)
        is_leader = group is None or group.entry_index == state.entry_index
        available = is_leader and group is not None and bool(group.tools)
        peek_active = group is not None and group.disclosure is ToolDisclosureLevel.PEEK
        widget.can_focus = available
        widget.peek_active = peek_active
        widget.tool_count = len(group.tools) if group is not None else 1
        widget.set_class(available, "tool-interactive")
        widget.set_class(available and peek_active, "tool-peek")
        widget.tooltip = (
            ui_text(
                self._language,
                ("tool.peek.tooltip.close" if peek_active else "tool.peek.tooltip.open"),
            )
            if available
            else None
        )
