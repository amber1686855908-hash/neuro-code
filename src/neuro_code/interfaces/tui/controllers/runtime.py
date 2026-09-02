from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import monotonic

from rich.table import Table
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.tui.commands import SlashCompletion, slash_completions
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.state import (
    _COMMAND_HINT_LIMIT,
    _LOADING_ANIMATION_TICK_SECONDS,
    ToolActivityGroupState,
    ToolFeedbackState,
    TranscriptEntry,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    ACCENT_WARNING,
    TEXT_DIM,
    TEXT_DISABLED,
    TEXT_EMPHASIS,
    TEXT_MUTED,
    TEXT_SECONDARY,
    WAITING_STYLE,
    loading_style,
)
from neuro_code.interfaces.tui.tool_activity import (
    ToolDisclosureLevel,
)
from neuro_code.interfaces.tui.widgets import PromptInput


class RuntimeControllerMixin(TuiAppControllerMixin):
    def _start_model_loading(self) -> None:
        self._model_loading = True
        self._loading_animation.reset()
        self._loading_animation_elapsed = 0.0
        self._turn_activity_started_at = monotonic()
        self._turn_activity_kind = "thinking"
        self._turn_activity_tool_name = None
        self._turn_activity_tool_started_at = None
        self._refresh_turn_activity()

    def _stop_model_loading(self) -> None:
        self._model_loading = False
        self._loading_animation_elapsed = 0.0
        self._turn_activity_started_at = None
        self._turn_activity_kind = "thinking"
        self._turn_activity_tool_name = None
        self._turn_activity_tool_started_at = None
        activity = self._main_screen_query_optional("#turn-activity", Static)
        if activity is not None:
            activity.update("")
            activity.display = False

    def _advance_model_loading_animation(self) -> None:
        if self._model_loading:
            self._loading_animation_elapsed += _LOADING_ANIMATION_TICK_SECONDS
            if self._loading_animation_elapsed + 1e-9 >= self._loading_animation.delay_seconds:
                self._loading_animation_elapsed = 0.0
                self._loading_animation.advance()
                self._refresh_turn_activity()

    def _refresh_running_tool_elapsed(self) -> None:
        if self._main_screen_query_optional("#transcript", VerticalScroll) is None:
            return
        groups: dict[int, ToolActivityGroupState] = {}
        ungrouped: list[ToolFeedbackState] = []
        for state in tuple(self._tool_feedback_by_entry.values()):
            if state.phase != "running":
                continue
            group = self._tool_activity_group_by_entry.get(state.entry_index)
            if group is None:
                ungrouped.append(state)
            else:
                groups[id(group)] = group
        for state in ungrouped:
            self._refresh_tool_feedback(state)
        for group in groups.values():
            if group.disclosure is ToolDisclosureLevel.SUMMARY:
                self._refresh_tool_activity_group(group)
        self._refresh_turn_activity()

    def _refresh_turn_activity(self) -> None:
        if not self._model_loading:
            return
        activity = self._main_screen_query_optional("#turn-activity", Static)
        if activity is None:
            return

        if self._turn_activity_kind == "tool":
            key = (
                "turn.activity.waiting_tool"
                if self._turn_activity_tool_name in {"wait_tasks", "task_output", "wait_for_tasks"}
                else "turn.activity.running_tool"
            )
            label = ui_text(
                self._language,
                key,
                tool=self._turn_activity_tool_name or ui_text(self._language, "value.unknown"),
            )
            started_at = self._turn_activity_tool_started_at
        else:
            label = ui_text(self._language, f"turn.activity.{self._turn_activity_kind}")
            started_at = self._turn_activity_started_at

        elapsed = (
            self._event_duration({"duration_seconds": max(0.0, monotonic() - started_at)})
            if started_at is not None
            else "—"
        )
        rendered = self._loading_wave()
        rendered.append("  ")
        rendered.append(label, style=TEXT_SECONDARY)
        rendered.append(f"  ·  {elapsed:>7}", style=TEXT_DIM)
        activity.update(rendered)
        activity.display = True

    def _apply_language_to_chrome(self) -> None:
        self.sub_title = ui_text(self._language, "subtitle")
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        prompt.placeholder = ui_text(self._language, "prompt.placeholder")
        prompt.refresh()
        self._refresh_command_hints(prompt.value)
        self._refresh_runtime_bar()

    def _slash_completions(self, value: str) -> tuple[SlashCompletion, ...]:
        provider_names = (
            tuple(option.name for option in self._provider_controller.profiles if option.selectable)
            if self._provider_controller is not None
            else ()
        )
        return slash_completions(value, provider_names=provider_names)

    def _refresh_command_hints(self, value: str) -> None:
        widget = self._main_screen_query_one("#command-hints", Static)
        completions = self._slash_completions(value)
        if not completions:
            widget.update("")
            widget.display = False
            return

        hints = Text()
        hints.append(ui_text(self._language, "command_hint.tab"), style=f"bold {TEXT_EMPHASIS}")
        hints.append("  ", style=TEXT_DISABLED)
        for index, completion in enumerate(completions[:_COMMAND_HINT_LIMIT]):
            if index:
                hints.append("  ·  ", style=TEXT_DISABLED)
            hints.append(completion.display, style=TEXT_SECONDARY)
        if len(completions) > _COMMAND_HINT_LIMIT:
            hints.append("  ·  …", style=TEXT_MUTED)
        widget.update(hints)
        widget.display = True

    def _context_percentage(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return self._context_token_usage()
        percentage = self._context_used_tokens / window * 100
        rendered = "<0.1%" if 0 < percentage < 0.1 else f"{percentage:.1f}%"
        return f"~{rendered}" if self._context_usage_estimated else rendered

    def _context_color(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return TEXT_SECONDARY
        ratio = self._context_used_tokens / window
        if ratio >= 0.8:
            return ACCENT_WARNING
        return TEXT_SECONDARY

    def _context_token_usage(self) -> str:
        tokens = self._context_used_tokens
        if tokens >= 1_000_000:
            rendered = f"{tokens / 1_000_000:.1f}M"
        elif tokens >= 1_000:
            rendered = f"{tokens / 1_000:.1f}k"
        else:
            rendered = f"{tokens:,}"
        approximation = "≈" if self._context_usage_estimated else ""
        return f"{approximation}{rendered} tok"

    def _context_usage_summary(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return self._context_token_usage()
        approximation = "≈" if self._context_usage_estimated else ""
        return (
            f"{self._context_percentage()} "
            f"({approximation}{self._context_used_tokens:,}/{window:,})"
        )

    def _loading_wave(self) -> Text:
        symbols = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")
        wave = Text()
        for level in self._loading_animation.levels():
            safe_level = max(0, min(7, level))
            wave.append(symbols[safe_level], style=loading_style(safe_level))
        return wave

    def _render_model_loading(self) -> Text:
        loading = self._loading_wave()
        loading.append("  ")
        key = "turn.finalizing" if self._finalizing else "turn.waiting"
        loading.append(ui_text(self._language, key), style=WAITING_STYLE)
        loading.append("  ·  ↓", style=TEXT_DIM)
        loading.append(self._context_token_usage(), style=self._context_color())
        return loading

    def _refresh_runtime_bar(self) -> None:
        model = Text(self._model_name, style=TEXT_EMPHASIS, overflow="ellipsis", no_wrap=True)
        requested = self._reasoning_effort
        effective = self._effective_reasoning_effort
        effort = Text()
        effort.append(" · ", style=TEXT_DIM)
        effort.append(
            requested.value,
            style=TEXT_MUTED if requested is ReasoningEffort.ULTRACODE else TEXT_SECONDARY,
        )
        if effective is not requested:
            effort.append(" → ", style=TEXT_DIM)
            effort.append(
                effective.value,
                style=TEXT_SECONDARY,
            )
        mode = Text()
        mode.append(" · ", style=TEXT_DIM)
        mode.append(self._interaction_mode.value, style=TEXT_SECONDARY)

        primary = Table.grid(expand=True, padding=(0, 0))
        primary.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        primary.add_column(width=len(effort.plain), no_wrap=True)
        primary.add_column(width=len(mode.plain), no_wrap=True)
        primary.add_row(model, effort, mode)
        primary_widget = self._main_screen_query_one("#runtime-primary", Static)
        primary_widget.update(primary)
        mode_help = ui_text(
            self._language,
            (
                "runtime.mode_help_auto_unrestricted"
                if self._interaction_mode is InteractionMode.AUTO and self._auto_mode_unrestricted
                else f"runtime.mode_help.{self._interaction_mode.value}"
            ),
        )
        primary_widget.tooltip = (
            f"{self._provider_name}/{self._model_name}\n"
            f"{ui_text(self._language, 'runtime.effort_help')}\n{mode_help}"
        )

        context = Text()
        context.append("ctx ", style=TEXT_MUTED)
        context.append(self._context_percentage(), style=self._context_color())
        workspace = Text(self._display_cwd(), style=TEXT_MUTED, overflow="ellipsis", no_wrap=True)
        secondary = Table.grid(expand=True, padding=(0, 0))
        secondary.add_column(width=len(context.plain), no_wrap=True)
        secondary.add_column(ratio=1, justify="right", overflow="ellipsis", no_wrap=True)
        secondary.add_row(context, workspace)
        secondary_widget = self._main_screen_query_one("#runtime-secondary", Static)
        secondary_widget.update(secondary)
        context_help = (
            self._context_token_usage()
            if self._context_window_tokens is None
            else ui_text(
                self._language,
                (
                    "runtime.context_help_estimated"
                    if self._context_usage_estimated
                    else "runtime.context_help_reported"
                ),
                used=f"{self._context_used_tokens:,}",
                window=f"{self._context_window_tokens:,}",
            )
        )
        secondary_widget.tooltip = f"{context_help}\n{self._cwd}"

    def _display_cwd(self) -> str:
        try:
            relative = self._cwd.resolve().relative_to(Path.home().resolve())
        except (OSError, RuntimeError, ValueError):
            return str(self._cwd)
        return "~" if str(relative) == "." else f"~/{relative}"

    def _reasoning_effort_summary(self) -> str:
        requested = self._reasoning_effort
        effective = self._effective_reasoning_effort
        summary = f"{requested.glyph} {requested.value}"
        if effective is not requested:
            summary += f" → {effective.glyph} {effective.value}"
        return summary

    def _interaction_mode_summary(self) -> str:
        summary = f"{self._interaction_mode.glyph} {self._interaction_mode.value}"
        if self._interaction_mode is InteractionMode.AUTO and not self._auto_mode_unrestricted:
            summary += f" ({ui_text(self._language, 'mode.limited')})"
        return summary

    def _refresh_localized_interface(self) -> None:
        self._apply_language_to_chrome()
        for index, (entry, widget) in enumerate(
            zip(self._entries, self._entry_widgets, strict=True)
        ):
            if entry.category == "plan" and self._plan is not None:
                rendered_plan = self._render_plan(self._plan, self._plan_comments)
                self._entries[index] = TranscriptEntry("plan", rendered_plan.plain)
                widget.update(rendered_plan)
                continue
            tool_state = self._tool_feedback_by_entry.get(index)
            if tool_state is not None:
                group = self._tool_activity_group_by_entry.get(index)
                content = (
                    self._tool_activity_text(group)
                    if group is not None and group.entry_index == index
                    else self._tool_summary_line(tool_state)
                )
                self._entries[index] = TranscriptEntry("tool", content)
                continue
            if entry.ui_key is not None:
                content = ui_text(
                    self._language,
                    entry.ui_key,
                    **dict(entry.ui_values),
                )
                entry = replace(entry, text=content)
                self._entries[index] = entry
            widget.update(
                self._render_entry(
                    entry.category,
                    entry.text,
                    ui_key=entry.ui_key,
                    ui_values=entry.ui_values,
                )
            )
        for group in self._tool_activity_groups:
            self._refresh_tool_activity_group(group)
        if self._pending_assistant is not None and self._assistant_parts:
            self._pending_assistant.update(
                self._render_entry("assistant", "".join(self._assistant_parts))
            )
