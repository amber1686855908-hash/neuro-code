"""Attached-terminal controls for the TUI inbound adapter.

The controller projects the current binding's application terminal port into a
single bounded panel.  It owns only selection, cursors, and presentation
state; process lifecycle, permissions, workspace validation, and output
retention remain application/infrastructure responsibilities.

TUI 附加终端控制器.本模块只拥有选择、游标和展示状态,进程生命周期、权限、工作区校验及
输出保留仍由应用层和基础设施层负责.
"""

from __future__ import annotations

from typing import cast

from neuro_code.application.ports.terminal import (
    InteractiveTerminalManager,
    InteractiveTerminalSession,
)
from neuro_code.domain.terminal.models import (
    MAX_TERMINAL_DIMENSION,
    MAX_TERMINAL_READ_BYTES,
    MAX_TERMINAL_WRITE_BYTES,
    TerminalSize,
)
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.widgets import (
    AttachedTerminalPanel,
    PromptInput,
)

_TERMINAL_OUTPUT_POLL_SECONDS = 0.25
_TERMINAL_PANEL_READ_BYTES = 64 * 1024
_TERMINAL_DISPLAY_BYTES = 64 * 1024
_TERMINAL_MAX_INPUT_BYTES = MAX_TERMINAL_WRITE_BYTES


class AttachedTerminalControllerMixin(TuiAppControllerMixin):
    """Control one selected attached terminal without touching chat state."""

    def _attached_terminal_manager(self) -> InteractiveTerminalManager | None:
        manager = getattr(self._runner, "interactive_terminals", None)
        if manager is None:
            return None
        if not all(hasattr(manager, name) for name in ("get_session", "list_sessions")):
            return None
        return cast(InteractiveTerminalManager, manager)

    def _attached_terminal_panel(self) -> AttachedTerminalPanel | None:
        return self._main_screen_query_optional(
            "#attached-terminal-panel",
            AttachedTerminalPanel,
        )

    def _attached_terminal_schedule_poll(self) -> None:
        self.run_worker(
            self._poll_attached_terminals(),
            name="attached-terminal-refresh",
            group="attached-terminals",
            exclusive=False,
            exit_on_error=False,
        )

    async def _poll_attached_terminals(self) -> None:
        if self._attached_terminal_polling:
            return
        panel = self._attached_terminal_panel()
        manager = self._attached_terminal_manager()
        if panel is None:
            return
        if manager is None:
            self._attached_terminal_session_ids = ()
            panel.display = False
            return

        self._attached_terminal_polling = True
        try:
            try:
                sessions = await manager.list_sessions()
            except Exception:
                self._attached_terminal_session_ids = ()
                panel.display = False
                return
            sessions = tuple(sessions[:8])
            session_ids = tuple(session.session_id for session in sessions)
            self._attached_terminal_session_ids = session_ids
            if self._attached_terminal_selected_id not in session_ids:
                self._attached_terminal_selected_id = session_ids[0] if session_ids else None
            self._attached_terminal_offsets = {
                session_id: offset
                for session_id, offset in self._attached_terminal_offsets.items()
                if session_id in session_ids
            }
            self._attached_terminal_output = {
                session_id: output
                for session_id, output in self._attached_terminal_output.items()
                if session_id in session_ids
            }

            session_lines: list[str] = []
            for session in sessions:
                session_lines.append(await self._attached_terminal_session_line(session))
            panel.update_sessions(
                "\n".join((ui_text(self._language, "terminal.title"), *session_lines))
                if session_lines
                else ui_text(self._language, "terminal.none")
            )
            panel.update_help(ui_text(self._language, "terminal.help"))
            panel.set_input_placeholder(ui_text(self._language, "terminal.input_placeholder"))
            if not sessions:
                panel.update_output(ui_text(self._language, "terminal.output_empty"))
                panel.display = False
                return

            selected_id = self._attached_terminal_selected_id
            selected = next(
                (session for session in sessions if session.session_id == selected_id),
                None,
            )
            if selected is None:
                panel.display = False
                return
            offset = self._attached_terminal_offsets.get(selected.session_id, 0)
            chunk = await selected.read(
                after_offset=offset,
                max_bytes=min(MAX_TERMINAL_READ_BYTES, _TERMINAL_PANEL_READ_BYTES),
                wait_seconds=0,
            )
            self._attached_terminal_offsets[selected.session_id] = chunk.next_offset
            decoded = self._attached_terminal_safe_text(chunk.data)
            if chunk.dropped_bytes:
                output = decoded
            else:
                output = self._attached_terminal_output.get(selected.session_id, "") + decoded
            self._attached_terminal_output[selected.session_id] = (
                self._attached_terminal_bounded_output(output)
            )
            panel.update_output(
                self._attached_terminal_output[selected.session_id]
                or ui_text(self._language, "terminal.output_empty")
            )
            panel.display = True
        except Exception:
            panel.display = bool(self._attached_terminal_session_ids)
        finally:
            self._attached_terminal_polling = False

    async def _attached_terminal_session_line(
        self,
        session: InteractiveTerminalSession,
    ) -> str:
        try:
            exit_code = await session.wait(timeout_seconds=0)
        except Exception:
            status = ui_text(self._language, "terminal.status_unknown")
        else:
            status = (
                ui_text(self._language, "terminal.exited", code=exit_code)
                if exit_code is not None
                else ui_text(self._language, "terminal.running")
            )
        marker = "▶ " if session.session_id == self._attached_terminal_selected_id else "  "
        return ui_text(
            self._language,
            "terminal.session",
            marker=marker,
            terminal_id=session.session_id,
            pid=session.process_id,
            status=status,
        )

    @staticmethod
    def _attached_terminal_safe_text(data: bytes) -> str:
        decoded = data.decode("utf-8", "replace")
        return "".join(
            character if character in {"\n", "\r", "\t"} or ord(character) >= 32 else "�"
            for character in decoded
        )

    @staticmethod
    def _attached_terminal_bounded_output(text: str) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= _TERMINAL_DISPLAY_BYTES:
            return text
        return encoded[-_TERMINAL_DISPLAY_BYTES:].decode("utf-8", "replace")

    def _attached_terminal_select_relative(self, delta: int) -> None:
        session_ids = self._attached_terminal_session_ids
        if not session_ids:
            self._attached_terminal_schedule_poll()
            return
        current = self._attached_terminal_selected_id
        index = session_ids.index(current) if current in session_ids else 0
        self._attached_terminal_selected_id = session_ids[(index + delta) % len(session_ids)]
        self._attached_terminal_focused = True
        panel = self._attached_terminal_panel()
        if panel is not None:
            panel.display = True
            panel.focus_input()
        self._attached_terminal_schedule_poll()

    def action_focus_attached_terminal(self) -> None:
        if self._attached_terminal_manager() is None:
            self._write_ui_entry("status", "terminal.unavailable")
            return
        if not self._attached_terminal_session_ids:
            self._attached_terminal_schedule_poll()
            return
        if self._attached_terminal_selected_id is None:
            self._attached_terminal_selected_id = self._attached_terminal_session_ids[0]
        self._attached_terminal_focused = True
        panel = self._attached_terminal_panel()
        if panel is not None:
            panel.display = True
            panel.focus_input()
        self._attached_terminal_schedule_poll()

    def action_attached_terminal_next(self) -> None:
        self._attached_terminal_select_relative(1)

    def action_attached_terminal_previous(self) -> None:
        self._attached_terminal_select_relative(-1)

    def action_attached_terminal_stop(self) -> None:
        self.run_worker(
            self._attached_terminal_stop_selected(),
            name="attached-terminal-stop",
            group="attached-terminals",
            exclusive=False,
            exit_on_error=False,
        )

    async def _attached_terminal_stop_selected(self) -> None:
        manager = self._attached_terminal_manager()
        terminal_id = self._attached_terminal_selected_id
        if manager is None or terminal_id is None:
            return
        session = await manager.get_session(terminal_id)
        if session is None:
            await self._poll_attached_terminals()
            return
        try:
            await session.close()
        except Exception as error:
            self._write_ui_entry(
                "error",
                "terminal.stop_failed",
                error=f"{type(error).__name__}: {error}",
            )
        else:
            self._write_ui_entry("status", "terminal.stopped", terminal_id=terminal_id)
        await self._poll_attached_terminals()

    def action_attached_terminal_resize(self) -> None:
        self.run_worker(
            self._attached_terminal_resize_selected(),
            name="attached-terminal-resize",
            group="attached-terminals",
            exclusive=False,
            exit_on_error=False,
        )

    async def _attached_terminal_resize_selected(self) -> None:
        manager = self._attached_terminal_manager()
        terminal_id = self._attached_terminal_selected_id
        if manager is None or terminal_id is None:
            return
        session = await manager.get_session(terminal_id)
        if session is None:
            await self._poll_attached_terminals()
            return
        size = TerminalSize(
            max(1, min(MAX_TERMINAL_DIMENSION, self.size.width - 8)),
            max(1, min(MAX_TERMINAL_DIMENSION, self.size.height // 3)),
        )
        try:
            await session.resize(size)
        except Exception as error:
            self._write_ui_entry(
                "error",
                "terminal.resize_failed",
                error=f"{type(error).__name__}: {error}",
            )
        else:
            self._write_ui_entry(
                "status",
                "terminal.resized",
                columns=size.columns,
                rows=size.rows,
            )
        await self._poll_attached_terminals()

    async def on_attached_terminal_panel_input_submitted(
        self,
        event: AttachedTerminalPanel.InputSubmitted,
    ) -> None:
        event.stop()
        if not self._attached_terminal_focused:
            return
        terminal_id = self._attached_terminal_selected_id
        manager = self._attached_terminal_manager()
        if manager is None or terminal_id is None:
            return
        data = event.value.encode("utf-8") + b"\n"
        if len(data) > _TERMINAL_MAX_INPUT_BYTES:
            self._write_ui_entry("error", "terminal.input_too_large")
            return
        session = await manager.get_session(terminal_id)
        if session is None:
            await self._poll_attached_terminals()
            return
        try:
            await session.write(data)
        except Exception as error:
            self._write_ui_entry(
                "error",
                "terminal.input_failed",
                error=f"{type(error).__name__}: {error}",
            )
        else:
            self._attached_terminal_schedule_poll()

    def on_attached_terminal_panel_focus_chat_requested(
        self,
        event: AttachedTerminalPanel.FocusChatRequested,
    ) -> None:
        event.stop()
        self._attached_terminal_focused = False
        panel = self._attached_terminal_panel()
        if panel is not None:
            panel.blur_input()
        self._main_screen_query_one("#prompt", PromptInput).focus()


__all__ = ["AttachedTerminalControllerMixin"]
