"""Shared typing surface for cohesive TUI controller mixins.

为内聚 TUI 控制器 mixin 提供共享的类型表面.

The concrete ``NeuroCodeApp`` owns the live Textual state.  Controller mixins
only contribute one reason-to-change worth of handlers and deliberately do
not create a second application object or service locator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from textual.widget import Widget

_WidgetType = TypeVar("_WidgetType", bound=Widget)

if TYPE_CHECKING:
    from collections import deque
    from collections.abc import Callable
    from pathlib import Path

    from rich.text import Text
    from textual.worker import Worker

    from neuro_code.application.ports.provider_catalog import ProviderCatalog
    from neuro_code.application.ports.provider_settings import (
        ManagedProviderSettings,
        ProviderSettingsStore,
    )
    from neuro_code.application.ports.ui_preferences import UiPreferencesStore
    from neuro_code.application.sessions.selection import (
        SessionSelectionService,
    )
    from neuro_code.application.sessions.subagent_lifecycle import (
        SubagentRelationshipLifecycleController,
    )
    from neuro_code.application.sessions.subagent_queries import (
        SubagentRelationshipQueryController,
    )
    from neuro_code.application.sessions.turns import SessionTurnService
    from neuro_code.application.tools.service import (
        SessionToolOutputArtifactApplicationService,
    )
    from neuro_code.application.workflows.plan_execution import (
        PlanExecutionService,
    )
    from neuro_code.application.workflows.plan_scheduling import (
        PlanSchedulingService,
    )
    from neuro_code.application.workflows.session_task_execution import (
        QueuedPlanExecutionService,
    )
    from neuro_code.application.workflows.subagent import (
        ReadOnlySubagentApplicationService,
    )
    from neuro_code.application.workflows.subagent_capabilities import (
        SubagentCapabilitySet,
    )
    from neuro_code.domain.background_tasks.models import (
        BackgroundTaskWakePolicy,
        BackgroundWakeLimits,
        BackgroundWakeState,
    )
    from neuro_code.domain.conversation.interaction_mode import InteractionMode
    from neuro_code.domain.conversation.messages import SessionItem
    from neuro_code.domain.conversation.reasoning import ReasoningEffort
    from neuro_code.domain.execution import SessionExecutionRecord
    from neuro_code.domain.plans import PlanComment, SessionPlan
    from neuro_code.interfaces.tui.clipboard import (
        ClipboardWriter,
        ClipboardWriteResult,
    )
    from neuro_code.interfaces.tui.contracts import (
        ApprovalController,
        ConversationRunner,
        InteractionModeController,
        PlanController,
        ProviderController,
        ReasoningController,
        SessionController,
        SessionTaskController,
        TaskController,
    )
    from neuro_code.interfaces.tui.interaction import TuiUserInteraction
    from neuro_code.interfaces.tui.state import (
        CollapsingPulseAnimation,
        ToolActivityGroupState,
        ToolFeedbackState,
        TranscriptEntry,
        _ActiveToolInspector,
    )
    from neuro_code.interfaces.tui.widgets import ConversationMessage
    from neuro_code.shared.ui_language import UiLanguage


class TuiAppControllerMixin:
    """Type-checking surface for methods mixed into ``NeuroCodeApp``."""

    # These declarations describe state owned by the concrete app.  They make
    # controller mixins statically composable without moving live state into a
    # second object or weakening the strict mypy configuration.
    if TYPE_CHECKING:
        _runner: ConversationRunner
        _user_interaction: TuiUserInteraction | None
        _turn_service: SessionTurnService | None
        _approval_controller: ApprovalController | None
        _provider_controller: ProviderController | None
        _reasoning_controller: ReasoningController | None
        _interaction_mode_controller: InteractionModeController | None
        _session_controller: SessionController | None
        _session_selection_service: SessionSelectionService | None
        _task_controller: TaskController | None
        _session_task_controller: SessionTaskController | None
        _plan_controller: PlanController | None
        _plan_execution_service: PlanExecutionService | None
        _plan_scheduling_service: PlanSchedulingService | None
        _queued_plan_execution_service: QueuedPlanExecutionService | None
        _ui_preferences: UiPreferencesStore | None
        _provider_settings_store: ProviderSettingsStore | None
        _provider_catalog: ProviderCatalog | None
        _managed_provider_settings: ManagedProviderSettings | None
        _socks_supported: bool
        _background_task_wake_policy_override: BackgroundTaskWakePolicy | None
        _background_task_wake_policy: BackgroundTaskWakePolicy
        _background_wake_limits: BackgroundWakeLimits
        _language: UiLanguage
        _initial_items: tuple[SessionItem, ...]
        _tool_output_artifact_service: SessionToolOutputArtifactApplicationService | None
        _read_only_subagent_service: ReadOnlySubagentApplicationService | None
        _subagent_parent_capability_provider: Callable[[], SubagentCapabilitySet] | None
        _subagent_relationship_query: SubagentRelationshipQueryController | None
        _subagent_relationship_lifecycle: SubagentRelationshipLifecycleController | None
        _execution_record: SessionExecutionRecord | None
        _provider_name: str
        _model_name: str
        _reasoning_effort: ReasoningEffort
        _effective_reasoning_effort: ReasoningEffort
        _interaction_mode: InteractionMode
        _auto_mode_unrestricted: bool
        _cwd: Path
        _context_window_tokens: int | None
        _context_used_tokens: int
        _context_usage_estimated: bool
        _plan: SessionPlan | None
        _plan_comments: tuple[PlanComment, ...]
        _plan_entry_index: int | None
        _entries: list[TranscriptEntry]
        _entry_widgets: list[ConversationMessage]
        _tool_feedback_by_call: dict[tuple[bool, str], ToolFeedbackState]
        _tool_feedback_by_entry: dict[int, ToolFeedbackState]
        _tool_activity_groups: list[ToolActivityGroupState]
        _tool_activity_group_by_entry: dict[int, ToolActivityGroupState]
        _active_tool_activity_group: ToolActivityGroupState | None
        _active_tool_inspector: _ActiveToolInspector | None
        _assistant_parts: list[str]
        _first_token_seen: bool
        _queued_interjections: deque[str]
        _active_prompt: str | None
        _active_prompt_entry_index: int | None
        _turn_pristine_rewound: bool
        _pending_assistant: ConversationMessage | None
        _reasoning_announced: bool
        _turn_completion: tuple[str, int] | None
        _terminal_execution_status: str | None
        _terminal_execution_recoverable: bool
        _finalizing: bool
        _turn_usage_reported: bool
        _turn_worker: Worker[None] | None
        _model_loading: bool
        _loading_animation: CollapsingPulseAnimation
        _loading_animation_elapsed: float
        _turn_activity_started_at: float | None
        _turn_activity_kind: str
        _turn_activity_tool_name: str | None
        _turn_activity_tool_started_at: float | None
        _announced_terminal_tasks: set[str]
        _pending_auto_wake_tasks: set[str]
        _background_wake_state: BackgroundWakeState
        _background_wake_state_loaded: bool
        _background_wake_active: bool
        _background_wake_task_ids: tuple[str, ...]
        _task_polling: bool
        _pending_interaction_request_id: str | None
        _clipboard_writer: ClipboardWriter
        _last_clipboard_write: ClipboardWriteResult

        def _main_screen_query_one(
            self,
            selector: str,
            expect_type: type[_WidgetType],
        ) -> _WidgetType: ...

        def _main_screen_query_optional(
            self,
            selector: str,
            expect_type: type[_WidgetType],
        ) -> _WidgetType | None: ...

        def _render_plan(
            self,
            plan: SessionPlan,
            comments: tuple[PlanComment, ...],
        ) -> Text: ...

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...


__all__ = ["TuiAppControllerMixin"]
