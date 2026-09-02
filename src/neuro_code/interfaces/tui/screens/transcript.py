"""Transcript selection screen.

会话记录选择屏幕.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, cast

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, TextArea

from neuro_code.interfaces.tui.clipboard import ClipboardWriteResult
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.shared.ui_language import UiLanguage


class _ClipboardOwner(Protocol):
    def copy_text_to_clipboard(self, text: str) -> ClipboardWriteResult: ...


class TranscriptCopyScreen(ModalScreen[None]):
    """Selectable, read-only projection of the visible transcript.

    当前可见会话记录的可选择只读投影.

    Textual owns terminal mouse reporting while the full-screen app is active,
    so native terminal drag-selection is not portable. This screen provides a
    real text selection model and uses the app's native clipboard adapter before
    falling back to Textual's terminal clipboard path, without exposing hidden
    Runtime or tool state.

    全屏应用运行时由 Textual 管理终端鼠标上报,原生终端拖选无法跨平台保证.此界面
    提供真实文本选择模型,会先使用应用的原生剪贴板适配器,再回退到 Textual 的终端
    剪贴板路径,不会暴露隐藏的 Runtime 或工具状态.
    """

    CSS = """
    TranscriptCopyScreen {
        align: center middle;
        background: $background 80%;
    }

    #transcript-copy-dialog {
        width: 92%;
        max-width: 116;
        height: 88%;
        padding: $space-2 $space-3;
        background: $surface;
        border: solid $border;
    }

    #transcript-copy-title {
        height: 1;
        color: $text-primary;
        text-style: bold;
    }

    #transcript-copy-help,
    #transcript-copy-status {
        height: 1;
        color: $text-secondary;
    }

    #transcript-copy-text {
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

    #transcript-copy-text:focus {
        border: none;
        border-top: solid $border-focus;
        border-bottom: solid $border-focus;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", priority=True, show=False),
        Binding("ctrl+a", "select_all", "Select all", priority=True, show=False),
        Binding("ctrl+c", "copy_selection", "Copy", priority=True, show=False),
        Binding("ctrl+shift+c", "copy_selection", "Copy", priority=True, show=False),
    ]

    def __init__(self, content: str, *, language: UiLanguage) -> None:
        super().__init__()
        self._content = content
        self._language = language

    def compose(self) -> ComposeResult:
        with Vertical(id="transcript-copy-dialog", classes="modal-dialog modal-l"):
            yield Label(
                ui_text(self._language, "transcript_copy.title"),
                id="transcript-copy-title",
            )
            yield Label(
                ui_text(self._language, "transcript_copy.help"),
                id="transcript-copy-help",
            )
            yield TextArea(
                self._content,
                read_only=True,
                soft_wrap=True,
                id="transcript-copy-text",
            )
            yield Label("", id="transcript-copy-status")

    def on_mount(self) -> None:
        self.query_one("#transcript-copy-text", TextArea).focus()

    def action_select_all(self) -> None:
        self.query_one("#transcript-copy-text", TextArea).select_all()

    def action_copy_selection(self) -> None:
        editor = self.query_one("#transcript-copy-text", TextArea)
        selected = editor.selected_text
        status = self.query_one("#transcript-copy-status", Label)
        if not selected:
            status.update(ui_text(self._language, "transcript_copy.select_first"))
            return
        app = cast(_ClipboardOwner, self.app)
        result = app.copy_text_to_clipboard(selected)
        if result.native_copied:
            status.update(
                ui_text(
                    self._language,
                    "transcript_copy.copied",
                    characters=len(selected),
                )
            )
            return
        status.update(ui_text(self._language, "transcript_copy.clipboard_unavailable"))

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["TranscriptCopyScreen"]
