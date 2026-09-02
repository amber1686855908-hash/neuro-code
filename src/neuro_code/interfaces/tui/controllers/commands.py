from __future__ import annotations

import asyncio

from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import TextArea

from neuro_code.application.workflows.subagent import (
    RunSubagentRequest,
)
from neuro_code.domain.background_tasks.models import (
    BackgroundTaskWakePolicy,
)
from neuro_code.domain.conversation.context import estimate_context_tokens
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    TurnRecoveryStatus,
)
from neuro_code.domain.session_tasks import SessionTaskStatus
from neuro_code.interfaces.tui.contracts import SessionController
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.screens import (
    BackgroundWakeSettingsScreen,
    LanguageSettingsScreen,
    NetworkProxySettingsScreen,
    PermissionApprovalScreen,
    ProviderSelectionScreen,
    ProviderSettingsScreen,
    ReasoningEffortScreen,
    SessionSelectionScreen,
    SettingsScreen,
    TranscriptCopyScreen,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.tool_activity import (
    ToolDisclosureLevel,
    ToolInspectorScreen,
)
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.errors import ConfigurationError


class CommandControllerMixin(TuiAppControllerMixin):
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, PromptInput) and event.text_area.screen is self.screen:
            event.text_area.sync_content_height()
            self._refresh_command_hints(event.text_area.value)

    def action_complete_slash_command(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_next()
            return
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        if not prompt.has_focus:
            self.screen.focus_next()
            return
        if not prompt.value.startswith("/"):
            self.screen.focus_next()
            return
        completions = self._slash_completions(prompt.value)
        if not completions:
            return
        completed = completions[0].value
        if completed == prompt.value:
            return
        prompt.value = completed
        prompt.cursor_position = len(completed)

    async def _dispatch_slash_command(self, raw: str) -> None:
        command, _, arguments = raw[1:].partition(" ")
        command = command.casefold()
        if command == "plan":
            description = arguments.strip()
            await self._apply_interaction_mode(InteractionMode.PLAN)
            if description and self._interaction_mode is InteractionMode.PLAN:
                self._submit_prompt(description)
            return
        if command in {"view-plan", "show-plan"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self._show_plan()
            return
        if command in {"comment-plan", "plan-comment"}:
            await self._add_plan_comment(arguments)
            return
        if command in {"execute-plan", "run-plan"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self._execute_plan()
            return
        if command in {"schedule-plan", "queue-plan"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self._schedule_plan()
            return
        if command == "mode":
            mode_value = arguments.strip()
            if not mode_value:
                self._write_ui_entry(
                    "system",
                    "mode.current",
                    mode=self._interaction_mode.value,
                    modes=", ".join(mode.value for mode in InteractionMode),
                )
                return
            try:
                mode = InteractionMode(mode_value.casefold())
            except ValueError:
                self._write_ui_entry(
                    "error",
                    "mode.invalid",
                    value=mode_value,
                    modes=", ".join(mode.value for mode in InteractionMode),
                )
                return
            await self._apply_interaction_mode(mode)
            return
        if command in {"effort", "reasoning"}:
            effort_value = arguments.strip()
            if not effort_value:
                await self._select_reasoning_effort(None)
                return
            try:
                effort = ReasoningEffort(effort_value.casefold())
            except ValueError:
                self._write_ui_entry(
                    "error",
                    "effort.invalid",
                    value=effort_value,
                    levels=", ".join(effort.value for effort in ReasoningEffort),
                )
                return
            await self._select_reasoning_effort(effort)
            return
        if command in {"model", "provider"}:
            await self._select_provider(arguments.strip() or None)
            return
        if command in {"resume", "sessions"}:
            requested_session = arguments.strip() or None
            if command == "sessions":
                await self._select_session(None, query=requested_session)
            else:
                await self._select_session(requested_session)
            return
        if command == "recover":
            await self._dispatch_recovery_command(arguments)
            return
        if command in {"rename", "title"}:
            await self._rename_session(arguments)
            return
        if command == "tasks":
            if arguments.strip():
                self._write_ui_entry("error", "command.tasks_arguments")
                return
            await self._show_tasks()
            return
        if command == "subagents":
            normalized_arguments = arguments.strip()
            if not normalized_arguments:
                await self._show_subagent_relationships()
            else:
                await self._run_subagent_relationship_action(normalized_arguments)
            return
        if command in {"auto-wake", "autowake"}:
            await self._apply_background_task_wake_policy(arguments)
            return
        if command == "view-task":
            await self._show_session_task(arguments.strip())
            return
        if command == "run-task":
            task_id = arguments.strip()
            if not task_id or " " in task_id:
                self._write_ui_entry("error", "tasks.run.usage")
                return
            await self._run_queued_task(task_id)
            return
        if command == "subagent":
            await self._run_read_only_subagent(arguments)
            return
        if command in {"setting", "settings"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self.action_open_settings()
            return
        if arguments.strip():
            self._write_ui_entry("error", "command.arguments", command=command)
            return
        if command in {"quit", "exit"}:
            self.exit()
        elif command == "cancel":
            self.action_cancel_turn()
        elif command == "clear":
            self.action_clear_transcript()
        elif command == "help":
            self._write_ui_entry("system", "command.help")
        elif command == "status":
            session_id = self._runner.session_id or ui_text(self._language, "command.not_created")
            profile = (
                ui_text(
                    self._language,
                    "command.profile",
                    profile=self._provider_controller.selected_profile,
                )
                if self._provider_controller is not None
                else ""
            )
            self._write_ui_entry(
                "system",
                "command.status",
                provider=self._provider_name,
                model=self._model_name,
                effort=self._reasoning_effort_summary(),
                context=self._context_usage_summary(),
                mode=self._interaction_mode_summary(),
                session=session_id,
                profile=profile,
                cwd=self._cwd,
            )
        elif command in {"compact", "context"}:
            await self._run_context_compaction()
        else:
            self._write_ui_entry("error", "command.unknown", command=command)

    async def _dispatch_recovery_command(self, arguments: str) -> None:
        tokens = arguments.split()
        if not tokens or tokens[0].casefold() == "inspect":
            if len(tokens) > 1:
                self._write_ui_entry("error", "recovery.usage")
                return
            await self._announce_recovery_state(verbose=True)
            return
        if len(tokens) != 2 or tokens[0].casefold() not in {"abandon", "retry"}:
            self._write_ui_entry("error", "recovery.usage")
            return
        owner = self._session_selection_owner()
        if owner is None:
            self._write_ui_entry("error", "recovery.unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        action, turn_id = tokens[0].casefold(), tokens[1]
        if action == "abandon":
            try:
                result = await owner.abandon_recovery(turn_id)
            except Exception as error:
                self._write_entry("error", f"{type(error).__name__}: {error}")
                return
            self._write_ui_entry(
                "status",
                "session.recovery.abandoned",
                turn_id=result.attempt.turn_id,
            )
            return

        self._assistant_parts.clear()
        self._first_token_seen = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_recovery_retry(owner, turn_id),
            name="agent-recovery-retry",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_recovery_retry(
        self,
        owner: SessionController,
        turn_id: str,
    ) -> None:
        await self._run_agent_turn(lambda: owner.retry_recovery(turn_id, sink=self._handle_event))

    async def _announce_recovery_state(self, *, verbose: bool = False) -> None:
        owner = self._session_selection_owner()
        if owner is None:
            return
        try:
            inspections = await owner.inspect_recovery()
        except Exception:
            return
        visible = tuple(
            inspection
            for inspection in inspections
            if verbose or inspection.attempt.resolution is None
        )
        if not visible:
            if verbose:
                self._write_ui_entry("status", "recovery.none")
            return
        for inspection in visible:
            attempt = inspection.attempt
            input_state = "exact" if attempt.input_reconstructable else "unavailable"
            if verbose:
                self._write_ui_entry(
                    "system",
                    "recovery.item",
                    turn_id=attempt.turn_id,
                    status=attempt.status.value,
                    stage=attempt.last_stage.value,
                    input_state=input_state,
                    reason=attempt.status_reason,
                    retry_available=str(attempt.retry_available).lower(),
                    abandon_available=str(attempt.abandon_available).lower(),
                )
                continue
            if attempt.status is TurnRecoveryStatus.SAFELY_RETRYABLE and attempt.retry_available:
                self._write_ui_entry(
                    "recoverable",
                    "session.recovery.safe",
                    turn_id=attempt.turn_id,
                )
            elif attempt.status is TurnRecoveryStatus.SAFELY_RETRYABLE:
                self._write_ui_entry(
                    "recoverable",
                    "session.recovery.retry_unavailable",
                    turn_id=attempt.turn_id,
                )
            elif attempt.status is TurnRecoveryStatus.INDETERMINATE:
                self._write_ui_entry(
                    "recoverable",
                    "session.recovery.indeterminate",
                    turn_id=attempt.turn_id,
                    stage=attempt.last_stage.value,
                )

    async def _run_context_compaction(self) -> None:
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        try:
            result = await self._runner.compact_now()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._write_ui_entry(
                "error",
                "context.compaction_failed",
                error=self._safe_tool_text(str(error)),
            )
            return
        self._context_used_tokens = estimate_context_tokens(self._runner.items)
        self._context_usage_estimated = True
        self._refresh_runtime_bar()
        self._write_ui_entry(
            "status",
            "context.compaction_result",
            status=result.status.value,
        )

    async def _run_read_only_subagent(self, raw_prompt: str) -> None:
        """Start one explicit, bounded read-only child without parent transcript reuse.

        启动一次明确且有界的只读子代理运行,不复用父会话 transcript.
        """

        prompt = raw_prompt.strip()
        if not prompt:
            self._write_ui_entry("error", "subagent.usage")
            return
        if self._read_only_subagent_service is None:
            self._write_ui_entry("error", "subagent.unavailable")
            return
        session_id = self._runner.session_id
        if session_id is None:
            self._write_ui_entry("error", "subagent.session_required")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        self._write_ui_entry("status", "subagent.started")
        self._turn_worker = self.run_worker(
            self._run_read_only_subagent_task(session_id, prompt),
            name="agent-read-only-subagent",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_read_only_subagent_task(self, session_id: str, prompt: str) -> None:
        service = self._read_only_subagent_service
        if service is None:
            return
        try:
            parent_capability_provider = self._subagent_parent_capability_provider
            if parent_capability_provider is None:
                raise ConfigurationError("active parent capability metadata is unavailable")
            parent_capabilities = parent_capability_provider()
            projection = await service.run_subagent(
                RunSubagentRequest(session_id, prompt),
                parent_capabilities=parent_capabilities,
            )
        except asyncio.CancelledError:
            self._write_ui_entry("status", "subagent.cancelled")
            raise
        except Exception as error:
            self._write_ui_entry(
                "error",
                "subagent.failed",
                error=self._safe_tool_text(str(error)),
            )
            return

        status_label = ui_text(
            self._language,
            f"tasks.status.{projection.status.value}",
        )
        if projection.status is SessionTaskStatus.COMPLETED:
            self._write_ui_entry(
                "status",
                "subagent.completed",
                steps=projection.steps,
            )
        else:
            self._write_ui_entry(
                "error",
                "subagent.finished",
                status=status_label,
                steps=projection.steps,
            )
        if projection.truncated:
            self._write_ui_entry("status", "subagent.truncated")
        if projection.response:
            self._write_entry("assistant", projection.response)

    async def _apply_background_task_wake_policy(self, arguments: str) -> None:
        value = arguments.strip().casefold()
        if not value:
            self._write_ui_entry(
                "system",
                "background_wake.current",
                policy=self._background_task_wake_policy_label(),
            )
            return
        policy_values = {
            "on": BackgroundTaskWakePolicy.ENABLED,
            "enabled": BackgroundTaskWakePolicy.ENABLED,
            "off": BackgroundTaskWakePolicy.DISABLED,
            "disabled": BackgroundTaskWakePolicy.DISABLED,
        }
        policy = policy_values.get(value)
        if policy is None:
            self._write_ui_entry(
                "error",
                "background_wake.invalid",
                value=value,
            )
            return
        if (
            policy is self._background_task_wake_policy
            and policy is self._background_task_wake_policy_override
        ):
            self._write_ui_entry(
                "status",
                "background_wake.already_selected",
                policy=self._background_task_wake_policy_label(),
            )
            return
        self._background_task_wake_policy_override = policy
        self._background_task_wake_policy = policy
        self._write_ui_entry(
            "status",
            "background_wake.changed",
            policy=self._background_task_wake_policy_label(),
        )
        if policy is BackgroundTaskWakePolicy.ENABLED:
            await self._poll_background_tasks()

    def _background_task_wake_policy_label(self) -> str:
        return ui_text(
            self._language,
            f"background_wake.policy.{self._background_task_wake_policy.value}",
        )

    def action_clear_transcript(self) -> None:
        transcript = self._main_screen_query_one("#transcript", VerticalScroll)
        transcript.remove_children(tuple(self._entry_widgets))
        self._entries.clear()
        self._entry_widgets.clear()
        self._tool_feedback_by_call.clear()
        self._tool_feedback_by_entry.clear()
        self._tool_activity_groups.clear()
        self._tool_activity_group_by_entry.clear()
        self._active_tool_activity_group = None
        self._plan_entry_index = None
        self._plan_comments = ()
        self._queued_interjections.clear()
        self._write_ui_entry("system", "transcript.cleared")

    def action_collapse_active_tool_peek(self) -> None:
        """Make Escape reliably restore Summary even after focus moved away."""

        if isinstance(self.screen, ModalScreen):
            return
        for group in self._tool_activity_groups:
            if group.disclosure is not ToolDisclosureLevel.PEEK:
                continue
            group.disclosure = ToolDisclosureLevel.SUMMARY
            self._refresh_tool_activity_group(group)

    def action_show_help(self) -> None:
        """Reveal the command reference on demand instead of reserving a footer row.

        按需显示命令参考,不再永久占用底部快捷键栏。
        """

        if isinstance(self.screen, ModalScreen):
            return
        self._write_ui_entry("system", "command.help")

    def action_copy_prompt(self) -> None:
        """Copy selected prompt text or open the transcript selection view.

        复制提示框选中文本;没有选区时打开会话记录选择界面.
        """

        if isinstance(self.screen, TranscriptCopyScreen):
            self.screen.action_copy_selection()
            return
        if isinstance(self.screen, ToolInspectorScreen):
            self.screen.action_copy_current()
            return
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        if prompt.has_focus and prompt.selected_text:
            prompt.action_copy()
            return
        self.action_copy_transcript()

    def action_copy_transcript(self) -> None:
        if isinstance(self.screen, TranscriptCopyScreen):
            return
        self.push_screen(
            TranscriptCopyScreen(
                self._copyable_transcript(),
                language=self._language,
            )
        )

    def _copyable_transcript(self) -> str:
        labels = {
            "assistant": "NEURO",
            "error": ui_text(self._language, "label.error"),
            "plan": ui_text(self._language, "plan.heading").rstrip(":\N{FULLWIDTH COLON}"),
            "recoverable": ui_text(self._language, "label.status"),
            "status": ui_text(self._language, "label.status"),
            "system": "SYSTEM",
            "tool": ui_text(self._language, "label.tool"),
            "user": "YOU",
        }
        sections: list[str] = []
        for index, entry in enumerate(self._entries):
            group = self._tool_activity_group_by_entry.get(index)
            if group is not None:
                if index == group.entry_index:
                    sections.append(self._tool_activity_text(group))
                continue
            sections.append(f"{labels.get(entry.category, entry.category.upper())}\n{entry.text}")
        if self._pending_assistant is not None and self._assistant_parts:
            sections.append(f"NEURO\n{''.join(self._assistant_parts)}")
        return "\n\n".join(sections) or ui_text(self._language, "transcript_copy.empty")

    def action_cancel_turn(self) -> None:
        if isinstance(self.screen, TranscriptCopyScreen):
            self.screen.action_copy_selection()
            return
        if isinstance(self.screen, ToolInspectorScreen):
            self.screen.action_copy_current()
            return
        if isinstance(self.screen, PermissionApprovalScreen):
            self.screen.action_deny()
            return
        if isinstance(self.screen, ProviderSelectionScreen):
            self.screen.action_cancel()
            return
        if isinstance(self.screen, ReasoningEffortScreen):
            self.screen.action_cancel()
            return
        if isinstance(self.screen, SessionSelectionScreen):
            self.screen.action_cancel()
            return
        if isinstance(
            self.screen,
            (
                SettingsScreen,
                LanguageSettingsScreen,
                NetworkProxySettingsScreen,
                BackgroundWakeSettingsScreen,
                ProviderSettingsScreen,
            ),
        ):
            self.screen.action_cancel()
            return
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        if prompt.has_focus and prompt.selected_text:
            prompt.action_copy()
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("status", "turn.cancel_requested")
            if (
                self._pending_interaction_request_id is not None
                and self._user_interaction is not None
            ):
                self._user_interaction.cancel(self._pending_interaction_request_id)
            self._turn_worker.cancel()
            return
        if prompt.value:
            prompt.value = ""
            self._write_ui_entry("status", "turn.draft_cleared")
        else:
            self._write_ui_entry("status", "turn.none_running")
