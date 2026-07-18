"""Provider-independent domain contracts."""

from neuro_code.domain.background_tasks import (
    MAX_BACKGROUND_TASK_WAIT_IDS,
    BackgroundTaskKillOutcome,
    BackgroundTaskKillResult,
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWaitMode,
    BackgroundTaskWaitResult,
)
from neuro_code.domain.context_usage import estimate_context_tokens, estimate_text_tokens
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.interaction_mode import InteractionMode, interaction_mode_guidance
from neuro_code.domain.messages import (
    ContentPart,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    ToolCall,
)
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.reasoning import ReasoningEffort, reasoning_guidance
from neuro_code.domain.session_search import SessionSearchHit, SessionSearchPage
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.domain.ui_preferences import UiLanguage

__all__ = [
    "MAX_BACKGROUND_TASK_WAIT_IDS",
    "AgentEvent",
    "AgentEventKind",
    "BackgroundTaskKillOutcome",
    "BackgroundTaskKillResult",
    "BackgroundTaskSnapshot",
    "BackgroundTaskStatus",
    "BackgroundTaskWaitMode",
    "BackgroundTaskWaitResult",
    "ContentPart",
    "ContentPartKind",
    "ContextItemKind",
    "InteractionMode",
    "Message",
    "ModelBackendToolCompleted",
    "ModelBackendToolStarted",
    "ModelCompleted",
    "ModelContext",
    "ModelEvent",
    "ModelProviderAttemptFailed",
    "ModelProviderSelected",
    "ModelReasoningDelta",
    "ModelTextDelta",
    "ModelToolCall",
    "PreservedContextItem",
    "ReasoningEffort",
    "Role",
    "SessionItem",
    "SessionSearchHit",
    "SessionSearchPage",
    "SessionSnapshot",
    "SessionSummary",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "UiLanguage",
    "estimate_context_tokens",
    "estimate_text_tokens",
    "interaction_mode_guidance",
    "reasoning_guidance",
]
