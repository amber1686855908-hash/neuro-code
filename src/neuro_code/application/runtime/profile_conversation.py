"""Compatibility facade for the canonical application session controller.

提供应用会话控制器的兼容门面,并转发到规范实现."""

from neuro_code.application.sessions.binding import (
    ConversationBinding,
)
from neuro_code.application.sessions.binding import (
    ConversationRunner as _ConversationRunner,
)
from neuro_code.application.sessions.contracts import (
    InteractionModeSelectionResult,
    ReasoningEffortSelectionResult,
    SessionOption,
    SessionSelectionResult,
)
from neuro_code.application.sessions.profile_conversation import (
    ProfileConversationController,
    ProviderOption,
    ProviderSelectionResult,
)

ConversationRunner = _ConversationRunner
__all__ = [
    "ConversationBinding",
    "InteractionModeSelectionResult",
    "ProfileConversationController",
    "ProviderOption",
    "ProviderSelectionResult",
    "ReasoningEffortSelectionResult",
    "SessionOption",
    "SessionSelectionResult",
]
