"""Monochrome visual tokens for the Textual terminal interface.

This module deliberately contains presentation-only values.  It must not own
application state, controller decisions, or terminal interaction behavior.

Textual 终端界面使用的单色视觉令牌.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pygments.style import Style as PygmentsStyle
from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    String,
    Text,
    Whitespace,
    _TokenType,
)
from rich.syntax import PygmentsSyntaxTheme
from rich.theme import Theme as RichTheme
from textual.theme import Theme

BG_0 = "#0B0B0B"
BG_1 = "#121212"
BG_2 = "#1C1C1C"
BORDER = "#303030"
FG_DIM = "#505050"
FG_MUTED = "#6A6A6A"
FG_SECONDARY = "#929292"
FG_EMPHASIS = "#BDBDBD"
FG_BODY = "#D8D8D8"
FG_PRIMARY = "#EEEEEE"

# One restrained cool accent is reserved for focus, interaction, links, and
# inline technical emphasis. Object types such as paths, tools, and models do
# not receive an accent merely because of their type.
ACCENT = "#789A9E"
SUCCESS = "#839A7D"
WARNING = "#AC9669"
ERROR = "#A77A7A"

# Compatibility aliases keep render call sites explicit while all values still
# originate from the compact semantic token set above.
BACKGROUND = BG_0
SURFACE = BG_1
SURFACE_HOVER = BG_2
SURFACE_SELECTED = BG_2
BORDER_DIM = BORDER
BORDER_FOCUS = ACCENT
TEXT_DIM = FG_DIM
TEXT_PLACEHOLDER = FG_DIM
TEXT_DISABLED = FG_DIM
TEXT_MUTED = FG_MUTED
TEXT_SECONDARY = FG_SECONDARY
TEXT_EMPHASIS = FG_EMPHASIS
TEXT_BODY = FG_BODY
TEXT_PRIMARY = FG_PRIMARY
BRAND_TEXT = FG_PRIMARY

# Semantic accents stay deliberately muted so the terminal remains primarily
# monochrome while structured technical information remains scannable.
ACCENT_CODE = ACCENT
ACCENT_LINK = ACCENT
ACCENT_NUMBER = FG_EMPHASIS
ACCENT_SUCCESS = SUCCESS
ACCENT_WARNING = WARNING
ACCENT_ERROR = ERROR
SYNTAX_OPERATOR = "#9A9A9A"

MONO_COLORS = (
    BACKGROUND,
    SURFACE,
    SURFACE_HOVER,
    SURFACE_SELECTED,
    BORDER_DIM,
    BORDER,
    TEXT_DIM,
    TEXT_PLACEHOLDER,
    TEXT_DISABLED,
    TEXT_MUTED,
    TEXT_SECONDARY,
    TEXT_EMPHASIS,
    TEXT_BODY,
    TEXT_PRIMARY,
    BRAND_TEXT,
)

TEXTUAL_THEME = Theme(
    name="neuro-code-mono",
    primary=BORDER_FOCUS,
    secondary=TEXT_SECONDARY,
    accent=TEXT_EMPHASIS,
    warning=WARNING,
    error=ERROR,
    success=SUCCESS,
    foreground=TEXT_PRIMARY,
    background=BACKGROUND,
    surface=SURFACE,
    panel=SURFACE,
    boost=SURFACE_HOVER,
    luminosity_spread=0.08,
    text_alpha=0.96,
    variables={
        "border": BORDER,
        "border-dim": BORDER_DIM,
        "border-focus": BORDER_FOCUS,
        "bg-0": BG_0,
        "bg-1": BG_1,
        "bg-2": BG_2,
        "fg-primary": FG_PRIMARY,
        "fg-secondary": FG_SECONDARY,
        "fg-muted": FG_MUTED,
        "space-0": "0",
        "space-1": "1",
        "space-2": "2",
        "space-3": "3",
        "space-4": "4",
        "space-6": "6",
        "space-8": "8",
        "surface-hover": SURFACE_HOVER,
        "surface-selected": SURFACE_SELECTED,
        "text-primary": TEXT_PRIMARY,
        "text-body": TEXT_BODY,
        "text-secondary": TEXT_SECONDARY,
        "text-muted": TEXT_MUTED,
        "text-dim": TEXT_DIM,
        "text-placeholder": TEXT_PLACEHOLDER,
        "text-disabled": TEXT_DISABLED,
        "text-emphasis": TEXT_EMPHASIS,
        "brand-text": BRAND_TEXT,
        "block-cursor-background": BORDER_FOCUS,
        "block-cursor-foreground": BACKGROUND,
        "block-hover-background": SURFACE_HOVER,
        "button-color-foreground": BACKGROUND,
        "button-focus-text-style": "bold",
        "footer-background": BACKGROUND,
        "footer-description-background": BACKGROUND,
        "footer-description-foreground": TEXT_MUTED,
        "footer-item-background": BACKGROUND,
        "footer-key-background": BACKGROUND,
        "footer-key-foreground": TEXT_EMPHASIS,
        "input-cursor-background": TEXT_PRIMARY,
        "input-cursor-foreground": BACKGROUND,
        "input-selection-background": f"{BORDER} 55%",
        "scrollbar": BORDER,
        "scrollbar-active": TEXT_SECONDARY,
        "scrollbar-background": BACKGROUND,
        "scrollbar-hover": TEXT_MUTED,
    },
)

MARKDOWN_THEME = RichTheme(
    {
        "markdown.paragraph": TEXT_BODY,
        "markdown.text": TEXT_BODY,
        "markdown.em": f"italic {TEXT_EMPHASIS}",
        "markdown.strong": f"bold {TEXT_PRIMARY}",
        "markdown.code": f"bold {ACCENT_CODE} on {SURFACE}",
        "markdown.code_block": f"{TEXT_BODY} on {SURFACE}",
        "markdown.block_quote": f"italic {TEXT_SECONDARY}",
        "markdown.list": TEXT_BODY,
        "markdown.item": TEXT_BODY,
        "markdown.item.bullet": f"bold {TEXT_EMPHASIS}",
        "markdown.item.number": f"bold {TEXT_EMPHASIS}",
        "markdown.hr": BORDER,
        "markdown.h1": f"bold {BRAND_TEXT}",
        "markdown.h2": f"bold {TEXT_EMPHASIS}",
        "markdown.h3": f"bold {TEXT_PRIMARY}",
        "markdown.h4": f"bold {TEXT_BODY}",
        "markdown.h5": f"bold {TEXT_EMPHASIS}",
        "markdown.h6": f"bold {TEXT_SECONDARY}",
        "markdown.link": f"underline {ACCENT_LINK}",
        "markdown.link_url": f"underline {ACCENT_LINK}",
        "markdown.table.border": BORDER,
        "markdown.table.header": f"bold {TEXT_EMPHASIS}",
        "markdown.kbd": f"bold {TEXT_EMPHASIS} on {SURFACE_SELECTED}",
    }
)


class _MonochromePygmentsStyle(PygmentsStyle):
    """Pygments token styles for fenced Markdown code blocks.

    用于 Markdown 围栏代码块的 Pygments 令牌样式."""

    background_color: ClassVar[str] = SURFACE
    styles: ClassVar[Mapping[_TokenType, str]] = {
        Text: TEXT_BODY,
        Whitespace: TEXT_BODY,
        Comment: f"italic {TEXT_MUTED}",
        Keyword: f"bold {ACCENT_LINK}",
        Keyword.Type: f"bold {ACCENT_CODE}",
        Operator: SYNTAX_OPERATOR,
        Operator.Word: f"bold {SYNTAX_OPERATOR}",
        Name: TEXT_BODY,
        Name.Builtin: ACCENT_CODE,
        Name.Function: f"bold {ACCENT_CODE}",
        Name.Class: f"bold {ACCENT_CODE}",
        Name.Decorator: ACCENT_LINK,
        String: ACCENT_SUCCESS,
        Number: TEXT_EMPHASIS,
        Generic.Deleted: ACCENT_ERROR,
        Generic.Inserted: f"bold {ACCENT_SUCCESS}",
        Generic.Heading: f"bold {TEXT_PRIMARY}",
        Generic.Subheading: f"bold {ACCENT_CODE}",
        Error: f"bold {ACCENT_ERROR}",
    }


MONO_SYNTAX_THEME = PygmentsSyntaxTheme(_MonochromePygmentsStyle)

EFFORT_STYLES = {
    "low": TEXT_EMPHASIS,
    "medium": TEXT_EMPHASIS,
    "high": TEXT_EMPHASIS,
    "xhigh": TEXT_EMPHASIS,
    "ultracode": TEXT_EMPHASIS,
}

MODE_STYLES = {
    "normal": TEXT_EMPHASIS,
    "accept-edits": TEXT_EMPHASIS,
    "plan": TEXT_EMPHASIS,
    "auto": TEXT_EMPHASIS,
}

USER_TEXT_STYLE = TEXT_PRIMARY
ASSISTANT_TEXT_STYLE = TEXT_BODY
SYSTEM_LABEL_STYLE = f"bold {TEXT_EMPHASIS}"
SYSTEM_TEXT_STYLE = TEXT_BODY
STATUS_LABEL_STYLE = f"bold {TEXT_SECONDARY}"
STATUS_TEXT_STYLE = TEXT_SECONDARY
RECOVERABLE_LABEL_STYLE = f"bold {TEXT_PRIMARY} on {BORDER_DIM}"
RECOVERABLE_TEXT_STYLE = f"bold {TEXT_EMPHASIS}"
TOOL_LABEL_STYLE = f"bold {TEXT_EMPHASIS}"
TOOL_TEXT_STYLE = TEXT_BODY
TOOL_TITLE_STYLE = f"bold {TEXT_EMPHASIS}"
TOOL_ACTIVE_STYLE = TEXT_SECONDARY
TOOL_COMPLETE_STYLE = f"bold {ACCENT_SUCCESS}"
TOOL_META_STYLE = TEXT_SECONDARY
TOOL_DETAIL_STYLE = TEXT_BODY
TOOL_GUIDE_STYLE = TEXT_MUTED
ERROR_LABEL_STYLE = f"bold {ACCENT_ERROR} on {BORDER_DIM}"
ERROR_TEXT_STYLE = f"bold {ACCENT_ERROR}"
ERROR_DETAIL_STYLE = ACCENT_ERROR
WAITING_STYLE = TEXT_SECONDARY

DIFF_HUNK_STYLE = f"bold {TEXT_EMPHASIS} on {SURFACE_SELECTED}"
DIFF_FILE_STYLE = f"bold {TEXT_EMPHASIS}"
DIFF_ADDITION_STYLE = f"{ACCENT_SUCCESS} on {SURFACE_SELECTED}"
DIFF_DELETION_STYLE = f"{ACCENT_ERROR} on {SURFACE_HOVER}"
DIFF_CONTEXT_STYLE = TEXT_BODY
DIFF_SUMMARY_ADDITION_STYLE = f"bold {ACCENT_SUCCESS} on {SURFACE_SELECTED}"
DIFF_SUMMARY_DELETION_STYLE = f"bold {ACCENT_ERROR} on {SURFACE_HOVER}"

LOADING_LEVEL_STYLES = (
    TEXT_DIM,
    TEXT_DISABLED,
    TEXT_MUTED,
    TEXT_SECONDARY,
    TEXT_EMPHASIS,
    TEXT_EMPHASIS,
    TEXT_EMPHASIS,
    TEXT_EMPHASIS,
)

CONNECTION_STATUS_STYLES = {
    "success": TOOL_COMPLETE_STYLE,
    "warning": f"bold {ACCENT_WARNING}",
    "error": ERROR_TEXT_STYLE,
}


def loading_style(level: int) -> str:
    """Return a bounded monochrome style for one loading-wave column.

    返回一个用于加载波列的有界单色样式."""

    safe_level = max(0, min(len(LOADING_LEVEL_STYLES) - 1, level))
    style = LOADING_LEVEL_STYLES[safe_level]
    return f"bold {style}" if safe_level == len(LOADING_LEVEL_STYLES) - 1 else style


__all__ = [
    "ACCENT",
    "ACCENT_CODE",
    "ACCENT_ERROR",
    "ACCENT_LINK",
    "ACCENT_NUMBER",
    "ACCENT_SUCCESS",
    "ACCENT_WARNING",
    "ASSISTANT_TEXT_STYLE",
    "BACKGROUND",
    "BG_0",
    "BG_1",
    "BG_2",
    "BORDER",
    "BORDER_DIM",
    "BORDER_FOCUS",
    "BRAND_TEXT",
    "CONNECTION_STATUS_STYLES",
    "DIFF_ADDITION_STYLE",
    "DIFF_CONTEXT_STYLE",
    "DIFF_DELETION_STYLE",
    "DIFF_FILE_STYLE",
    "DIFF_HUNK_STYLE",
    "DIFF_SUMMARY_ADDITION_STYLE",
    "DIFF_SUMMARY_DELETION_STYLE",
    "EFFORT_STYLES",
    "ERROR",
    "ERROR_DETAIL_STYLE",
    "ERROR_LABEL_STYLE",
    "ERROR_TEXT_STYLE",
    "LOADING_LEVEL_STYLES",
    "MARKDOWN_THEME",
    "MODE_STYLES",
    "MONO_COLORS",
    "MONO_SYNTAX_THEME",
    "RECOVERABLE_LABEL_STYLE",
    "RECOVERABLE_TEXT_STYLE",
    "STATUS_LABEL_STYLE",
    "STATUS_TEXT_STYLE",
    "SUCCESS",
    "SURFACE",
    "SURFACE_HOVER",
    "SURFACE_SELECTED",
    "SYNTAX_OPERATOR",
    "SYSTEM_LABEL_STYLE",
    "SYSTEM_TEXT_STYLE",
    "TEXTUAL_THEME",
    "TEXT_BODY",
    "TEXT_DIM",
    "TEXT_DISABLED",
    "TEXT_EMPHASIS",
    "TEXT_MUTED",
    "TEXT_PLACEHOLDER",
    "TEXT_PRIMARY",
    "TEXT_SECONDARY",
    "TOOL_ACTIVE_STYLE",
    "TOOL_COMPLETE_STYLE",
    "TOOL_DETAIL_STYLE",
    "TOOL_GUIDE_STYLE",
    "TOOL_LABEL_STYLE",
    "TOOL_META_STYLE",
    "TOOL_TEXT_STYLE",
    "TOOL_TITLE_STYLE",
    "USER_TEXT_STYLE",
    "WAITING_STYLE",
    "WARNING",
    "loading_style",
]
