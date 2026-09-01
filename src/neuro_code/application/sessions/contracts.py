"""Typed projections shared by session application consumers.

定义由会话应用消费者共享的类型化投影.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox.models import SandboxProfile


@dataclass(frozen=True, slots=True)
class ReasoningEffortSelectionResult:
    """Report a requested reasoning-effort change.

    报告请求的推理强度变更.
    """

    requested: ReasoningEffort
    effective: ReasoningEffort
    changed: bool
    # True means the selected application strategy routes eligible user turns
    # through the workflow entry; it does not mean a downstream workflow is
    # currently running.
    workflow_orchestration_active: bool = False


@dataclass(frozen=True, slots=True)
class InteractionModeSelectionResult:
    """Report a requested interaction-mode change.

    报告请求的交互模式变更.
    """

    requested: InteractionMode
    changed: bool
    auto_unrestricted: bool = False
    safety_classifier_active: bool = False

    @property
    def limited_auto(self) -> bool:
        """Whether auto mode remains subject to the safe preview boundary.

        auto 模式是否仍受安全预览边界限制.
        """

        return self.requested is InteractionMode.AUTO and not self.auto_unrestricted


@dataclass(frozen=True, slots=True)
class SessionOption:
    """Bounded session choice shown to an inbound interface.

    提供给入站接口显示的有界会话选项.
    """

    session_id: str
    source_provider: str
    source_model: str
    updated_at: datetime
    resume_profile: str
    current: bool
    source_profile_match: bool
    selectable: bool
    sandbox_profile: SandboxProfile | None = None
    sandbox_profile_match: bool = True
    title: str | None = None
    matched_fields: tuple[str, ...] = ()
    snippet: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSelectionResult:
    """Safe result of selecting or resuming a session.

    选择或恢复会话的安全结果.
    """

    session_id: str
    source_provider: str
    source_model: str
    profile_name: str
    provider_name: str
    model_name: str
    previous_session_id: str | None
    changed: bool
    source_profile_match: bool
    items: tuple[SessionItem, ...]
    stopped_background_tasks: int = 0
    context_window_tokens: int | None = None


__all__ = [
    "InteractionModeSelectionResult",
    "ReasoningEffortSelectionResult",
    "SessionOption",
    "SessionSelectionResult",
]
