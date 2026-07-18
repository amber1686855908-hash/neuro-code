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
from neuro_code.domain.events import AgentEvent, AgentEventKind
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
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary
from neuro_code.domain.tools import ToolDefinition, ToolResult

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
    "Role",
    "SessionItem",
    "SessionSnapshot",
    "SessionSummary",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
]
