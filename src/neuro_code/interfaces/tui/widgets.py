"""Textual widgets owned by the TUI interface.

TUI 界面拥有的 Textual 组件.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import RenderResult
from textual.binding import Binding, BindingType
from textual.message import Message as TextualMessage
from textual.widgets import Button, Static, TextArea

from neuro_code.interfaces.tui.state import (
    _PROMPT_MARK,
    _PROMPT_MAX_VISIBLE_LINES,
    _SUCCESS_MARK,
)
from neuro_code.interfaces.tui.theme import (
    ACCENT_CODE,
    TEXT_DISABLED,
    TEXT_PLACEHOLDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TOOL_COMPLETE_STYLE,
)


class MenuOptionButton(Button):
    """Sparse modal row with independent focus and selected-state signals.

    使用独立焦点与已选择信号的克制模态列表行。
    """

    def __init__(
        self,
        primary: str,
        *,
        secondary: str = "",
        selected: bool = False,
        muted: bool = False,
        primary_width: int | None = None,
        secondary_justify: Literal["left", "right"] = "right",
        id: str | None = None,
        disabled: bool = False,
    ) -> None:
        accessible_label = " · ".join(part for part in (primary, secondary) if part)
        super().__init__(accessible_label, id=id, disabled=disabled)
        self._primary = primary
        self._secondary = secondary
        self._selected = selected
        self._muted = muted
        self._primary_width = primary_width
        self._secondary_justify = secondary_justify

    def render(self) -> RenderResult:
        primary_style = TEXT_DISABLED if self.disabled or self._muted else TEXT_PRIMARY
        secondary_style = TEXT_DISABLED if self.disabled or self._muted else TEXT_SECONDARY
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=1, no_wrap=True)
        if self._primary_width is None:
            table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        else:
            table.add_column(width=self._primary_width, overflow="ellipsis", no_wrap=True)
        table.add_column(
            ratio=1,
            justify=self._secondary_justify,
            overflow="ellipsis",
            no_wrap=True,
        )
        table.add_column(width=1, no_wrap=True)
        table.add_row(
            Text(_PROMPT_MARK if self.has_focus else " ", style=ACCENT_CODE),
            Text(self._primary, style=primary_style),
            Text(self._secondary, style=secondary_style),
            Text(_SUCCESS_MARK if self._selected else " ", style=TOOL_COMPLETE_STYLE),
        )
        return table


class AssistantMarkdown(Markdown):
    """Safe model Markdown whose string form remains useful in diagnostics.

    安全的模型 Markdown,其字符串形式仍适合诊断."""

    def __str__(self) -> str:
        return self.markup


class ConversationMessage(Static):
    """One stable message node in the scrollable conversation.

    可滚动会话中的一个稳定消息节点."""

    def __init__(
        self,
        category: str,
        rendered: RenderableType,
        *,
        pending: bool = False,
    ) -> None:
        classes = f"conversation-message message-{category}"
        if pending:
            classes += " message-pending"
        super().__init__(rendered, markup=False, classes=classes)
        self.category = category

    def set_pending(self, pending: bool) -> None:
        self.set_class(pending, "message-pending")


class AssistantMessage(ConversationMessage):
    """Assistant Markdown with an explicit route to selectable source text.

    带有明确可选择原文入口的助手 Markdown.
    """

    class CopyRequested(TextualMessage):
        """Ask the owning app to show this reply in the selection view.

        请求所属应用在选择视图中显示此回复.
        """

        def __init__(self, message: AssistantMessage) -> None:
            self.message = message
            super().__init__()

    def __init__(
        self,
        rendered: RenderableType,
        *,
        content: str = "",
        pending: bool = False,
        copy_hint: str | None = None,
    ) -> None:
        super().__init__("assistant", rendered, pending=pending)
        self.content = content
        self.tooltip = copy_hint

    def set_content(self, content: str) -> None:
        self.content = content

    async def _on_click(self, event: events.Click) -> None:
        if event.chain < 2 or not self.content:
            return
        event.stop()
        self.post_message(self.CopyRequested(self))


class PromptInput(TextArea):
    """Bounded multi-line prompt editor with explicit submit semantics.

    带有明确提交语义且高度有界的多行提示编辑器.

    Terminal bracketed paste is preserved as real document lines. ``Enter``
    submits the complete prompt, while ``Shift+Enter`` (or ``Ctrl+J``) inserts a
    newline. Common editor selection remains local to the prompt.

    终端 bracketed paste 会保留为真实文档行.``Enter`` 提交完整提示,
    ``Shift+Enter`` (或 ``Ctrl+J``) 插入换行,常用编辑选择操作保持在提示框内.
    """

    @dataclass
    class Submitted(TextualMessage):
        """Prompt submission carrying the complete multi-line value.

        携带完整多行内容的提示提交消息.
        """

        input: PromptInput
        value: str

        @property
        def control(self) -> PromptInput:
            return self.input

    def __init__(
        self,
        *,
        placeholder: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(soft_wrap=True, tab_behavior="focus", id=id)
        self.placeholder = placeholder

    @property
    def value(self) -> str:
        """Compatibility alias used by the existing prompt lifecycle.

        供现有提示生命周期使用的兼容别名.
        """

        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value.replace("\r\n", "\n").replace("\r", "\n"))

    @property
    def cursor_position(self) -> int:
        row, column = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + column

    @cursor_position.setter
    def cursor_position(self, position: int) -> None:
        bounded = max(0, min(position, len(self.text)))
        prefix = self.text[:bounded]
        row = prefix.count("\n")
        column = len(prefix.rsplit("\n", maxsplit=1)[-1])
        self.move_cursor((row, column))

    def get_line(self, line_index: int) -> Text:
        if line_index == 0 and not self.text and self.placeholder:
            return Text(self.placeholder, style=TEXT_PLACEHOLDER, end="")
        return super().get_line(line_index)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default().stop()
            self.post_message(self.Submitted(self, self.text))
            return
        if event.key in {"shift+enter", "ctrl+j"}:
            event.prevent_default().stop()
            result = self.replace("\n", *self.selection, maintain_selection_offset=False)
            self.move_cursor(result.end_location)
            return
        if event.key == "ctrl+a":
            event.prevent_default().stop()
            self.action_select_all()
            return
        await super()._on_key(event)

    async def _on_paste(self, event: events.Paste) -> None:
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        if text:
            result = self.replace(text, *self.selection, maintain_selection_offset=False)
            self.move_cursor(result.end_location)
        event.prevent_default().stop()

    def sync_content_height(self) -> None:
        """Fit short prompts and scroll longer prompts without moving the layout.

        短提示自动适配高度,长提示在固定上限内滚动,不改变整体布局.
        """

        visible_lines = max(1, min(self.wrapped_document.height, _PROMPT_MAX_VISIBLE_LINES))
        self.styles.height = visible_lines
        if self.parent is not None:
            self.parent.styles.height = visible_lines + 2

    def _on_resize(self) -> None:
        super()._on_resize()
        self.call_after_refresh(self.sync_content_height)


class ToolFeedbackMessage(ConversationMessage, can_focus=True):
    """A stable Tool Activity card with a bounded selection viewport.

    带有有界选择 viewport 的稳定 Tool Activity 卡片."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "advance_disclosure", "Inspect", show=False),
        Binding("space", "toggle_peek", "Toggle peek", show=False),
        Binding("escape", "collapse_peek", "Summary", priority=True, show=False),
        Binding("up", "select_previous_tool", "Previous tool", show=False),
        Binding("down", "select_next_tool", "Next tool", show=False),
    ]

    class AdvanceRequested(TextualMessage):
        """Advance Summary to Peek, or Peek to Inspector."""

        def __init__(self, card: ToolFeedbackMessage) -> None:
            self.card = card
            super().__init__()

    class TogglePeekRequested(TextualMessage):
        """Toggle only the Conversation-local Summary/Peek state."""

        def __init__(self, card: ToolFeedbackMessage) -> None:
            self.card = card
            super().__init__()

    class CollapseRequested(TextualMessage):
        """Return a Peek viewport to its stable Summary."""

        def __init__(self, card: ToolFeedbackMessage) -> None:
            self.card = card
            super().__init__()

    class SelectionRequested(TextualMessage):
        """Move the selected tool within a multi-tool Peek viewport."""

        def __init__(self, card: ToolFeedbackMessage, delta: int) -> None:
            self.card = card
            self.delta = delta
            super().__init__()

    def __init__(self, rendered: RenderableType, *, entry_index: int) -> None:
        super().__init__("tool", rendered)
        self.entry_index = entry_index
        self.peek_active = False
        self.tool_count = 1

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        message = (
            self.TogglePeekRequested(self) if self.peek_active else self.AdvanceRequested(self)
        )
        self.post_message(message)

    def action_advance_disclosure(self) -> None:
        self.post_message(self.AdvanceRequested(self))

    def action_toggle_peek(self) -> None:
        self.post_message(self.TogglePeekRequested(self))

    def action_collapse_peek(self) -> None:
        if self.peek_active:
            self.post_message(self.CollapseRequested(self))

    def action_select_previous_tool(self) -> None:
        if self.peek_active and self.tool_count > 1:
            self.post_message(self.SelectionRequested(self, -1))

    def action_select_next_tool(self) -> None:
        if self.peek_active and self.tool_count > 1:
            self.post_message(self.SelectionRequested(self, 1))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        del parameters
        if action in {"select_previous_tool", "select_next_tool"}:
            return self.peek_active and self.tool_count > 1
        if action == "collapse_peek":
            return self.peek_active
        return True


__all__ = [
    "AssistantMarkdown",
    "AssistantMessage",
    "ConversationMessage",
    "MenuOptionButton",
    "PromptInput",
    "ToolFeedbackMessage",
]
