from __future__ import annotations

from neuro_code.domain.conversation.context import estimate_context_tokens
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.screens import SessionSelectionScreen
from neuro_code.interfaces.tui.text import ui_text


class SessionControllerMixin(TuiAppControllerMixin):
    async def action_select_session(self) -> None:
        await self._select_session(None)

    async def _select_session(
        self,
        requested: str | None,
        *,
        query: str | None = None,
    ) -> None:
        controller = self._session_selection_owner()
        if controller is None:
            self._write_ui_entry("error", "session.resume_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.resume_running")
            return
        if requested is not None:
            await self._apply_session_selection(requested)
            return
        try:
            options = await controller.list_sessions(query)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not options:
            if query is None:
                self._write_ui_entry("status", "session.none")
            else:
                self._write_ui_entry(
                    "status",
                    "session.none_matching",
                    query=query,
                )
            return
        self.push_screen(
            SessionSelectionScreen(
                options,
                query=query,
                language=self._language,
                search_callback=controller.list_sessions,
            ),
            self._session_selected,
        )

    async def _rename_session(self, title: str) -> None:
        controller = self._session_selection_owner()
        if controller is None:
            self._write_ui_entry("error", "session.rename_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.rename_running")
            return
        try:
            summary = await controller.rename_session(title)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_ui_entry(
            "status",
            "session.renamed",
            session_id=summary.id,
            title=summary.title,
        )

    async def _session_selected(self, session_id: str | None) -> None:
        if session_id is not None:
            await self._apply_session_selection(session_id)

    async def _apply_session_selection(self, session_id: str) -> None:
        controller = self._session_selection_owner()
        assert controller is not None
        try:
            result = await controller.select_session(session_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._provider_name = result.provider_name
        self._model_name = result.model_name
        if self._plan_controller is not None:
            self._plan = self._plan_controller.plan
        self._context_window_tokens = result.context_window_tokens
        if result.changed:
            self._context_used_tokens = estimate_context_tokens(result.items)
            self._context_usage_estimated = True
        self._refresh_runtime_bar()
        if not result.changed:
            self._write_ui_entry(
                "status",
                "session.already_open",
                session_id=result.session_id,
            )
            await self._announce_recovery_state()
            return

        self._queued_interjections.clear()
        self._reset_background_task_tracking()
        await self._ensure_background_wake_state()
        self._replace_transcript(result.items)
        self._execution_record = self._session_execution_record()
        profile_note = (
            ui_text(
                self._language,
                "session.profile",
                profile=result.profile_name,
            )
            if result.source_profile_match
            else ui_text(
                self._language,
                "session.profile_unavailable",
                profile=result.profile_name,
                source=result.source_provider,
            )
        )
        previous_note = (
            ui_text(
                self._language,
                "session.previous_saved",
                session_id=result.previous_session_id,
            )
            if result.previous_session_id is not None
            else ""
        )
        self._write_ui_entry(
            "system",
            "session.resumed",
            session_id=result.session_id,
            profile_note=profile_note,
            provider=result.provider_name,
            model=result.model_name,
            previous=previous_note,
            stopped=self._stopped_task_note(result.stopped_background_tasks),
        )
        self._write_recoverable_resume_notice(self._execution_record)
        await self._announce_recovery_state()
