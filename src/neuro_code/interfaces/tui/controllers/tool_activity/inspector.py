from __future__ import annotations

import asyncio
import logging

from neuro_code.application.ports.tools import MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES
from neuro_code.application.tools.service import (
    ReadSessionToolOutputArtifactRequest,
)
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.state import (
    ToolActivityGroupState,
    ToolFeedbackState,
    _ActiveToolInspector,
)
from neuro_code.interfaces.tui.tool_activity import (
    ToolInspectorScreen,
)
from neuro_code.interfaces.tui.widgets import ToolFeedbackMessage

_LOGGER = logging.getLogger(__name__)


class ToolActivityInspectorMixin(TuiAppControllerMixin):
    def _open_tool_inspector(self, group: ToolActivityGroupState) -> None:
        state = group.selected_tool
        presentation = self._tool_inspector_presentation(state, group)
        inspector = ToolInspectorScreen(
            presentation,
            language=self._language,
            copy_text=self.copy_text_to_clipboard,
        )
        active = _ActiveToolInspector(state, group, inspector)
        self._active_tool_inspector = active

        def inspector_closed(_: None) -> None:
            self._on_tool_inspector_closed(active)

        self.push_screen(inspector, inspector_closed)
        self._maybe_load_active_tool_inspector_artifact(active)

    def _on_tool_inspector_closed(self, active: _ActiveToolInspector) -> None:
        if self._active_tool_inspector is active:
            self._active_tool_inspector = None
        group = active.group
        if not any(candidate is group for candidate in self._tool_activity_groups):
            return
        if not group.tools or group.entry_index >= len(self._entry_widgets):
            return
        widget = self._entry_widgets[group.entry_index]
        if isinstance(widget, ToolFeedbackMessage) and widget.display:
            widget.focus()

    def _maybe_load_active_tool_inspector_artifact(
        self,
        active: _ActiveToolInspector,
    ) -> None:
        state = active.state
        worker = active.artifact_worker
        can_load_artifact = (
            state.artifact_id is not None
            and state.artifact_content is None
            and not state.artifact_unavailable
            and not state.artifact_loading
            and self._tool_output_artifact_service is not None
            and self._runner.session_id is not None
            and (worker is None or not worker.is_running)
        )
        if not can_load_artifact:
            return
        active.artifact_worker = self.run_worker(
            self._load_tool_inspector_output(active),
            name=f"tool-inspector-output-{state.entry_index}",
            group="tool-inspector-output",
            exclusive=True,
            exit_on_error=False,
        )

    async def _load_tool_inspector_output(
        self,
        active: _ActiveToolInspector,
    ) -> None:
        await self._load_tool_artifact(active.state)
        if self._active_tool_inspector is active:
            active.screen.update_presentation(
                self._tool_inspector_presentation(active.state, active.group)
            )

    def _refresh_active_tool_inspector(self, state: ToolFeedbackState) -> None:
        active = self._active_tool_inspector
        if active is None or active.state is not state:
            return
        self._update_active_tool_inspector(active)

    def _refresh_active_tool_inspector_group(self, group: ToolActivityGroupState) -> None:
        active = self._active_tool_inspector
        if active is None or active.group is not group:
            return
        self._update_active_tool_inspector(active)

    def _update_active_tool_inspector(self, active: _ActiveToolInspector) -> None:
        active.screen.update_presentation(
            self._tool_inspector_presentation(active.state, active.group)
        )
        self._maybe_load_active_tool_inspector_artifact(active)

    async def _load_tool_artifact(self, state: ToolFeedbackState) -> None:
        service = self._tool_output_artifact_service
        session_id = self._runner.session_id
        artifact_id = state.artifact_id
        if (
            service is None
            or session_id is None
            or artifact_id is None
            or state.artifact_loading
            or state.artifact_content is not None
        ):
            return
        state.artifact_loading = True
        try:
            result = await service.read(
                ReadSessionToolOutputArtifactRequest(
                    session_id=session_id,
                    artifact_id=artifact_id,
                    max_bytes=MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
                )
            )
            if (
                self._runner.session_id != session_id
                or self._tool_feedback_by_entry.get(state.entry_index) is not state
            ):
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("tool output artifact is unavailable", exc_info=True)
            state.artifact_unavailable = True
        else:
            state.artifact_content = result.content
            state.artifact_stored_truncated = result.artifact.truncated
            state.artifact_read_truncated = result.read_truncated
            state.artifact_unavailable = False
        finally:
            state.artifact_loading = False
