from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from rich.console import RenderableType
from rich.text import Text
from textual.containers import VerticalScroll

from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import Message, Role, SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.screens import TranscriptCopyScreen
from neuro_code.interfaces.tui.state import (
    _ERROR_MARK,
    _RESTORED_MESSAGE_LIMIT,
    ToolFeedbackState,
    TranscriptEntry,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    ACCENT_SUCCESS,
    ASSISTANT_TEXT_STYLE,
    EFFORT_STYLES,
    ERROR_DETAIL_STYLE,
    ERROR_LABEL_STYLE,
    ERROR_TEXT_STYLE,
    MODE_STYLES,
    MONO_SYNTAX_THEME,
    RECOVERABLE_LABEL_STYLE,
    RECOVERABLE_TEXT_STYLE,
    STATUS_LABEL_STYLE,
    STATUS_TEXT_STYLE,
    SYSTEM_LABEL_STYLE,
    SYSTEM_TEXT_STYLE,
    TEXT_BODY,
    TEXT_DIM,
    TEXT_EMPHASIS,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TOOL_DETAIL_STYLE,
    TOOL_LABEL_STYLE,
    TOOL_TEXT_STYLE,
    USER_TEXT_STYLE,
)
from neuro_code.interfaces.tui.widgets import (
    AssistantMarkdown,
    AssistantMessage,
    ConversationMessage,
    ToolFeedbackMessage,
)


def _markdown_code_theme() -> str:
    """Contain Rich Markdown's narrow annotation without changing the runtime theme.

    Markdown forwards the value to Syntax, whose runtime API accepts a
    PygmentsSyntaxTheme. Its public annotation is limited to a named string
    theme, so this local cast preserves the custom theme object.

    通过局部类型辅助函数容纳 Rich Markdown 的窄类型注解,不改变运行时主题.
    """

    return cast(str, MONO_SYNTAX_THEME)


class TranscriptControllerMixin(TuiAppControllerMixin):
    async def _remove_transcript_entry(self, index: int) -> None:
        if index < 0 or index >= len(self._entries):
            return
        widget = self._entry_widgets.pop(index)
        self._entries.pop(index)
        removed_state = self._tool_feedback_by_entry.pop(index, None)
        removed_group = self._tool_activity_group_by_entry.pop(index, None)
        if removed_state is not None:
            self._tool_feedback_by_call.pop(
                (removed_state.hosted, removed_state.call_id),
                None,
            )
            if removed_group is not None:
                removed_group.tools = [
                    tool for tool in removed_group.tools if tool is not removed_state
                ]
                if removed_group.tools:
                    removed_group.selected_tool_index = min(
                        removed_group.selected_tool_index,
                        len(removed_group.tools) - 1,
                    )
                if not removed_group.tools:
                    self._tool_activity_groups = [
                        group for group in self._tool_activity_groups if group is not removed_group
                    ]
                    if self._active_tool_activity_group is removed_group:
                        self._active_tool_activity_group = None
        if self._plan_entry_index == index:
            self._plan_entry_index = None
        elif self._plan_entry_index is not None and self._plan_entry_index > index:
            self._plan_entry_index -= 1
        shifted: dict[int, ToolFeedbackState] = {}
        for entry_index, state in self._tool_feedback_by_entry.items():
            if entry_index > index:
                state.entry_index -= 1
                shifted[entry_index - 1] = state
            else:
                shifted[entry_index] = state
        self._tool_feedback_by_entry = shifted
        if widget.parent is not None:
            await widget.remove()
        self._rebuild_tool_activity_indexes()

    def _rebuild_tool_activity_indexes(self) -> None:
        self._tool_activity_group_by_entry = {
            state.entry_index: group
            for group in self._tool_activity_groups
            for state in group.tools
        }
        for entry_index, _state in self._tool_feedback_by_entry.items():
            if entry_index >= len(self._entry_widgets):
                continue
            widget = self._entry_widgets[entry_index]
            if isinstance(widget, ToolFeedbackMessage):
                widget.entry_index = entry_index
        for group in self._tool_activity_groups:
            self._refresh_tool_activity_group(group)

    def on_assistant_message_copy_requested(
        self,
        event: AssistantMessage.CopyRequested,
    ) -> None:
        if isinstance(self.screen, TranscriptCopyScreen) or not event.message.content:
            return
        self.push_screen(
            TranscriptCopyScreen(
                event.message.content,
                language=self._language,
            )
        )

    def _replace_transcript(self, items: Sequence[SessionItem]) -> None:
        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        transcript.remove_children()
        self._entries.clear()
        self._entry_widgets.clear()
        self._tool_feedback_by_call.clear()
        self._tool_feedback_by_entry.clear()
        self._tool_activity_groups.clear()
        self._tool_activity_group_by_entry.clear()
        self._active_tool_activity_group = None
        self._plan_entry_index = None
        self._plan_comments = ()
        self._pending_assistant = None
        self._assistant_parts.clear()
        for item in items:
            if not isinstance(item, Message) or item.role is Role.SYSTEM:
                continue
            if item.role is Role.TOOL:
                self._write_ui_entry(
                    "tool",
                    "restore.result",
                    name=item.name or ui_text(self._language, "value.unknown"),
                )
                continue
            content = self._bounded_restored_text(item.model_content())
            if content:
                category = "user" if item.role is Role.USER else "assistant"
                self._write_entry(category, content)
            if item.role is Role.ASSISTANT and item.tool_calls:
                names = ", ".join(call.name for call in item.tool_calls)
                self._write_ui_entry("tool", "restore.request", names=names)
        if self._plan is not None:
            self._upsert_plan_entry(self._plan, self._plan_comments)

    def _bounded_restored_text(self, content: str) -> str:
        if len(content) <= _RESTORED_MESSAGE_LIMIT:
            return content
        return (
            f"{content[:_RESTORED_MESSAGE_LIMIT]}\n{ui_text(self._language, 'restore.truncated')}"
        )

    @staticmethod
    def _semantic_value_style(name: str, value: object) -> str | None:
        if name in {"provider", "model", "profile", "source"}:
            return f"bold {TEXT_EMPHASIS}"
        if name in {"name", "task_id", "session_id", "title"}:
            return f"bold {TEXT_EMPHASIS}"
        if name == "path":
            return TEXT_SECONDARY
        if name == "cwd":
            return TEXT_SECONDARY
        if name in {"effect", "outcome", "status"}:
            return f"bold {ACCENT_SUCCESS}"
        if name in {"duration", "steps", "step"}:
            return f"bold {TEXT_SECONDARY}"
        if name == "context":
            return f"bold {TEXT_SECONDARY}"
        if name in {"effort", "requested", "effective"}:
            try:
                effort = ReasoningEffort(str(value))
            except ValueError:
                return f"bold {TEXT_EMPHASIS}"
            return f"bold {EFFORT_STYLES[effort.value]}"
        if name == "mode":
            try:
                mode = InteractionMode(str(value))
            except ValueError:
                return f"bold {TEXT_EMPHASIS}"
            return f"bold {MODE_STYLES[mode.value]}"
        if name == "policy":
            return TEXT_SECONDARY
        if name in {"message", "reason", "error"}:
            return ERROR_TEXT_STYLE
        return None

    def _render_entry(
        self,
        category: str,
        content: str,
        *,
        ui_key: str | None = None,
        ui_values: tuple[tuple[str, object], ...] = (),
    ) -> RenderableType:
        if category == "user":
            return Text(content, style=USER_TEXT_STYLE, overflow="fold")
        if category == "assistant":
            return AssistantMarkdown(
                content,
                code_theme=_markdown_code_theme(),
                style=ASSISTANT_TEXT_STYLE,
                hyperlinks=False,
            )

        labels = {
            "error": (f"{_ERROR_MARK} {ui_text(self._language, 'label.error')}", ERROR_LABEL_STYLE),
            "recoverable": ("!", RECOVERABLE_LABEL_STYLE),
            "status": ("·", STATUS_LABEL_STYLE),
            "system": ("NEURO", SYSTEM_LABEL_STYLE),
            "tool": ("•", TOOL_LABEL_STYLE),
        }
        body_styles = {
            "error": ERROR_DETAIL_STYLE,
            "recoverable": RECOVERABLE_TEXT_STYLE,
            "status": STATUS_TEXT_STYLE,
            "system": SYSTEM_TEXT_STYLE,
            "tool": TOOL_TEXT_STYLE,
        }
        if category == "plan" and self._plan is not None:
            return self._render_plan(self._plan, self._plan_comments)
        label, label_style = labels.get(category, (category.title(), f"bold {TEXT_PRIMARY}"))
        body = Text(overflow="fold")
        body.append(label, style=label_style)
        body.append("  ", style=TEXT_DIM)
        content_start = len(body)
        body.append(content, style=body_styles.get(category, TEXT_BODY))
        for name, value in ui_values:
            style = self._semantic_value_style(name, value)
            rendered_value = str(value)
            if style is not None and rendered_value:
                offset = body.plain.find(rendered_value, content_start)
                while offset >= 0:
                    body.stylize(style, offset, offset + len(rendered_value))
                    offset = body.plain.find(rendered_value, offset + len(rendered_value))
        return body

    def _render_tool_feedback(
        self,
        state: ToolFeedbackState,
        *,
        body: Text | None = None,
    ) -> RenderableType:
        return (
            body
            if body is not None
            else Text(self._tool_summary_line(state), style=TOOL_DETAIL_STYLE)
        )

    def _write_ui_entry(self, category: str, key: str, **values: object) -> None:
        self._write_entry(
            category,
            ui_text(self._language, key, **values),
            ui_key=key,
            ui_values=tuple(values.items()),
        )

    def _write_entry(
        self,
        category: str,
        content: str,
        *,
        ui_key: str | None = None,
        ui_values: tuple[tuple[str, object], ...] = (),
        tool_state: ToolFeedbackState | None = None,
    ) -> None:
        if category != "tool" or tool_state is None:
            self._active_tool_activity_group = None
        entry = TranscriptEntry(category, content, ui_key, ui_values)
        if tool_state is not None:
            group = self._tool_activity_group_by_entry.get(tool_state.entry_index)
            is_group_leader = group is None or group.entry_index == tool_state.entry_index
            tool_widget = ToolFeedbackMessage(
                (
                    self._render_tool_activity_group(group)
                    if group is not None and is_group_leader
                    else self._render_tool_feedback(tool_state)
                ),
                entry_index=tool_state.entry_index,
            )
            tool_widget.display = is_group_leader
            self._configure_tool_feedback_widget(tool_widget, tool_state)
            widget: ConversationMessage = tool_widget
        elif category == "assistant":
            widget = AssistantMessage(
                self._render_entry(
                    category,
                    content,
                    ui_key=ui_key,
                    ui_values=ui_values,
                ),
                content=content,
                copy_hint=ui_text(self._language, "assistant.copy_hint"),
            )
        else:
            widget = ConversationMessage(
                category,
                self._render_entry(
                    category,
                    content,
                    ui_key=ui_key,
                    ui_values=ui_values,
                ),
            )
        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        pending = self._pending_assistant
        if pending is not None and pending.parent is transcript:
            transcript.mount(widget, before=pending)
        else:
            transcript.mount(widget)
        self._entries.append(entry)
        self._entry_widgets.append(widget)
        if follow:
            transcript.scroll_end(animate=False)

    def _begin_pending_assistant(self) -> None:
        if self._pending_assistant is not None:
            return
        self._start_model_loading()
        pending = AssistantMessage(
            Text(),
            content="",
            pending=True,
            copy_hint=ui_text(self._language, "assistant.copy_hint"),
        )
        # Keep a stable node for streamed assistant text, but render activity
        # only in the dedicated turn-activity row below the transcript.
        # 保留流式助手文本的稳定节点,但只在 transcript 下方的活动行渲染运行状态.
        pending.display = False
        self._pending_assistant = pending
        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        transcript.mount(pending)
        transcript.scroll_end(animate=False)

    def _update_pending_assistant(self, content: str) -> None:
        if self._pending_assistant is None:
            self._begin_pending_assistant()
        pending = self._pending_assistant
        assert pending is not None
        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        pending.set_pending(False)
        pending.display = True
        if isinstance(pending, AssistantMessage):
            pending.set_content(content)
        pending.update(self._render_entry("assistant", content))
        if follow:
            transcript.scroll_end(animate=False)

    def _seal_pending_assistant(self) -> bool:
        """Commit streamed text for the current model step without ending the turn.

        提交当前模型步骤的流式文本,但不结束本次回合.
        """

        content = "".join(self._assistant_parts)
        if not content:
            return False
        self._finish_pending_assistant(content, stop_loading=False)
        return True

    def _finish_streamed_assistant_response(
        self,
        result: AgentRunResult,
        *,
        fallback: str,
    ) -> None:
        """Finish only the active model-step response, never the aggregate turn text.

        只完成当前模型步骤的回复,绝不把整轮聚合文本重新显示一次.
        """

        if self._seal_pending_assistant():
            self._stop_model_loading()
            return
        if self._pending_assistant is None:
            self._stop_model_loading()
            return
        final_content = self._last_assistant_message_content(result.messages) or fallback
        self._finish_pending_assistant(final_content)

    @staticmethod
    def _last_assistant_message_content(messages: Sequence[Message]) -> str | None:
        for message in reversed(messages):
            if message.role is Role.ASSISTANT:
                content = message.model_content()
                if content:
                    return content
        return None

    def _finish_pending_assistant(self, content: str, *, stop_loading: bool = True) -> None:
        if self._pending_assistant is None:
            self._begin_pending_assistant()
        pending = self._pending_assistant
        assert pending is not None
        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        pending.set_pending(False)
        pending.display = True
        if isinstance(pending, AssistantMessage):
            pending.set_content(content)
        pending.update(self._render_entry("assistant", content))
        self._active_tool_activity_group = None
        self._entries.append(TranscriptEntry("assistant", content))
        self._entry_widgets.append(pending)
        self._pending_assistant = None
        self._assistant_parts.clear()
        if stop_loading:
            self._stop_model_loading()
        if follow:
            transcript.scroll_end(animate=False)

    async def _discard_pending_assistant(self) -> None:
        pending = self._pending_assistant
        self._pending_assistant = None
        self._assistant_parts.clear()
        self._stop_model_loading()
        if pending is not None and pending.parent is not None:
            await pending.remove()
