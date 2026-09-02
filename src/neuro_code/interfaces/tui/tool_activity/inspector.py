"""Independent large Tool Inspector screen.

独立的大尺寸 Tool Inspector 界面。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, TextArea

from neuro_code.interfaces.tui.clipboard import ClipboardWriteResult
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.tool_activity.models import (
    ToolInspectorPresentation,
    ToolInspectorTab,
)
from neuro_code.shared.ui_language import UiLanguage


class ToolInspectorScreen(ModalScreen[None]):
    """Scrollable Output/Input/Meta documents for one selected tool call."""

    CSS = """
    ToolInspectorScreen {
        align: center middle;
        background: $background 80%;
    }

    #tool-inspector-dialog {
        width: 92%;
        max-width: 116;
        height: 90%;
        padding: $space-2 $space-3;
        background: $surface;
        border: solid $border;
    }

    #tool-inspector-title {
        height: 1;
        color: $text-primary;
        text-style: bold;
    }

    #tool-inspector-subtitle,
    #tool-inspector-help,
    #tool-inspector-copy-status {
        height: 1;
        color: $text-secondary;
    }

    #tool-inspector-tabs {
        width: 100%;
        height: 3;
        margin-top: $space-1;
        border-bottom: solid $border;
    }

    #tool-inspector-tabs Button {
        width: auto;
        min-width: 12;
        height: 3;
        margin-right: $space-1;
        padding: $space-0 $space-1;
        background: $surface;
        color: $text-secondary;
        border: none;
    }

    #tool-inspector-tabs Button:hover,
    #tool-inspector-tabs Button:focus {
        background: $surface-hover;
        color: $text-primary;
        border: none;
    }

    #tool-inspector-tabs Button.active {
        background: $surface-selected;
        color: $text-primary;
        text-style: bold;
        border-bottom: solid $accent;
    }

    #tool-inspector-notice {
        display: none;
        width: 100%;
        height: auto;
        max-height: 3;
        padding: $space-0 $space-1;
        color: $warning;
        background: $background;
    }

    #tool-inspector-notice.visible {
        display: block;
    }

    #tool-inspector-text {
        width: 100%;
        height: 1fr;
        margin: $space-1 $space-0;
        padding: $space-1;
        border: none;
        border-top: solid $border;
        border-bottom: solid $border;
        background: $background;
        color: $text-body;
    }

    #tool-inspector-text:focus {
        border: none;
        border-top: solid $border-focus;
        border-bottom: solid $border-focus;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", priority=True, show=False),
        Binding("1", "show_output", "Output", priority=True, show=False),
        Binding("2", "show_input", "Input", priority=True, show=False),
        Binding("3", "show_meta", "Meta", priority=True, show=False),
        Binding("ctrl+a", "select_all", "Select all", priority=True, show=False),
        Binding("ctrl+c", "copy_current", "Copy", priority=True, show=False),
        Binding("ctrl+shift+c", "copy_current", "Copy", priority=True, show=False),
    ]

    def __init__(
        self,
        presentation: ToolInspectorPresentation,
        *,
        language: UiLanguage,
        copy_text: Callable[[str], ClipboardWriteResult],
    ) -> None:
        super().__init__()
        self.presentation = presentation
        self._language = language
        self._copy_text = copy_text
        self._tab = ToolInspectorTab.OUTPUT

    @property
    def current_tab(self) -> ToolInspectorTab:
        return self._tab

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-inspector-dialog", classes="modal-dialog modal-l"):
            yield Label(self.presentation.title, id="tool-inspector-title")
            yield Label(self.presentation.subtitle, id="tool-inspector-subtitle")
            with Horizontal(id="tool-inspector-tabs"):
                yield Button(
                    ui_text(self._language, "tool.inspector.tab.output"),
                    id="tool-inspector-tab-output",
                    classes="active",
                )
                yield Button(
                    ui_text(self._language, "tool.inspector.tab.input"),
                    id="tool-inspector-tab-input",
                )
                yield Button(
                    ui_text(self._language, "tool.inspector.tab.meta"),
                    id="tool-inspector-tab-meta",
                )
            yield Label("", id="tool-inspector-notice")
            yield TextArea(
                self.presentation.output,
                read_only=True,
                soft_wrap=False,
                id="tool-inspector-text",
            )
            yield Label(ui_text(self._language, "tool.inspector.help"), id="tool-inspector-help")
            yield Label("", id="tool-inspector-copy-status")

    def on_mount(self) -> None:
        self._refresh_presentation()
        self.query_one("#tool-inspector-text", TextArea).focus()

    def update_presentation(self, presentation: ToolInspectorPresentation) -> None:
        if presentation == self.presentation:
            return
        self.presentation = presentation
        if self.is_mounted:
            self._refresh_presentation()

    def _refresh_presentation(self) -> None:
        self.query_one("#tool-inspector-title", Label).update(self.presentation.title)
        self.query_one("#tool-inspector-subtitle", Label).update(self.presentation.subtitle)
        editor = self.query_one("#tool-inspector-text", TextArea)
        document = self.presentation.document(self._tab)
        if editor.text != document:
            editor.load_text(document)
        notice = self.query_one("#tool-inspector-notice", Label)
        notice_text = (
            self.presentation.output_notice if self._tab is ToolInspectorTab.OUTPUT else ""
        )
        notice.update(notice_text)
        notice.set_class(bool(notice_text), "visible")
        for tab in ToolInspectorTab:
            button = self.query_one(f"#tool-inspector-tab-{tab.value}", Button)
            button.set_class(tab is self._tab, "active")

    def _show(self, tab: ToolInspectorTab) -> None:
        if tab is self._tab:
            return
        self._tab = tab
        self._refresh_presentation()
        self.query_one("#tool-inspector-text", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        prefix = "tool-inspector-tab-"
        button_id = event.button.id or ""
        if not button_id.startswith(prefix):
            return
        event.stop()
        self._show(ToolInspectorTab(button_id.removeprefix(prefix)))

    def action_show_output(self) -> None:
        self._show(ToolInspectorTab.OUTPUT)

    def action_show_input(self) -> None:
        self._show(ToolInspectorTab.INPUT)

    def action_show_meta(self) -> None:
        self._show(ToolInspectorTab.META)

    def action_select_all(self) -> None:
        self.query_one("#tool-inspector-text", TextArea).select_all()

    def action_copy_current(self) -> None:
        editor = self.query_one("#tool-inspector-text", TextArea)
        content = editor.selected_text or self.presentation.document(self._tab)
        status = self.query_one("#tool-inspector-copy-status", Label)
        result = self._copy_text(content)
        key = "tool.inspector.copied" if result.native_copied else "tool.inspector.copy_unavailable"
        status.update(ui_text(self._language, key, characters=len(content)))

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["ToolInspectorScreen"]
