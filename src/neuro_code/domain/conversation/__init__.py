"""Canonical conversation and model-stream domain contracts.

定义规范的会话和模型流领域契约."""

from neuro_code.domain.conversation.compaction import (
    COMPACTION_SOURCE_FINGERPRINT_BYTES,
    MAX_DURABLE_COMPACTION_ID_BYTES,
    MAX_DURABLE_COMPACTION_SUMMARY_BYTES,
    DurableCompactionItem,
    compute_compaction_source_fingerprint,
)
from neuro_code.domain.conversation.context import (
    UPSTREAM_IMPORT_PROVIDER,
    ModelContext,
    estimate_context_tokens,
    estimate_text_tokens,
)
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelInputTokenSemantics,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
    ModelUsage,
)
from neuro_code.domain.conversation.interaction_mode import (
    InteractionMode,
    interaction_mode_guidance,
)
from neuro_code.domain.conversation.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPart,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    SyntheticReason,
    ToolCall,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort, reasoning_guidance

__all__ = [
    "COMPACTION_SOURCE_FINGERPRINT_BYTES",
    "IMAGE_MODEL_PLACEHOLDER",
    "MAX_DURABLE_COMPACTION_ID_BYTES",
    "MAX_DURABLE_COMPACTION_SUMMARY_BYTES",
    "UPSTREAM_IMPORT_PROVIDER",
    "AgentEvent",
    "AgentEventKind",
    "ContentPart",
    "ContentPartKind",
    "ContextItemKind",
    "DurableCompactionItem",
    "InteractionMode",
    "Message",
    "ModelBackendToolCompleted",
    "ModelBackendToolStarted",
    "ModelCompleted",
    "ModelContext",
    "ModelEvent",
    "ModelInputTokenSemantics",
    "ModelProviderAttemptFailed",
    "ModelProviderSelected",
    "ModelReasoningDelta",
    "ModelTextDelta",
    "ModelToolCall",
    "ModelUsage",
    "PreservedContextItem",
    "ReasoningEffort",
    "Role",
    "SessionItem",
    "SyntheticReason",
    "ToolCall",
    "compute_compaction_source_fingerprint",
    "estimate_context_tokens",
    "estimate_text_tokens",
    "interaction_mode_guidance",
    "reasoning_guidance",
]
