"""Typed application seam for interactive session selection.

定义交互式会话选择的类型化应用接缝.

The profile conversation controller remains the owner of session binding
replacement, locking, workspace checks, and resume lifecycle. This module
only exposes the bounded inbound operations needed by interfaces.

profile 会话控制器仍负责会话绑定替换、锁、工作区检查和恢复生命周期.
本模块只暴露入站接口所需的有界操作.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from neuro_code.application.sessions.contracts import (
    SessionOption,
    SessionSelectionResult,
)
from neuro_code.application.sessions.recovery import TurnRecoveryInspection
from neuro_code.domain.sessions import SessionSummary

if TYPE_CHECKING:
    from neuro_code.application.runtime.agent_loop import AgentRunResult, EventSink


class SessionSelectionController(Protocol):
    """Minimal owner contract consumed by the session-selection facade.

    表示会话选择门面使用的最小所有者契约.
    """

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]: ...

    async def select_session(self, session_id: str) -> SessionSelectionResult: ...

    async def rename_session(self, title: str) -> SessionSummary: ...

    async def inspect_recovery(self) -> tuple[TurnRecoveryInspection, ...]: ...

    async def abandon_recovery(
        self,
        turn_id: str,
        *,
        reason: str = "explicit_user_resolution",
    ) -> TurnRecoveryInspection: ...

    async def retry_recovery(
        self,
        turn_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...


class SessionSelectionService:
    """Expose interactive session selection without owning its lifecycle.

    暴露交互式会话选择,但不拥有会话生命周期.

    The facade deliberately keeps the controller's argument and exception
    semantics intact. It is an inbound application boundary, not a second
    implementation of resume or rename behavior.

    该门面有意保持控制器的参数和异常语义.它是入站应用边界,不是恢复或重命名行为的第二份实现.
    """

    __slots__ = ("_controller",)

    def __init__(self, controller: SessionSelectionController) -> None:
        self._controller = controller

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]:
        """Delegate bounded listing/search to the existing owner.

        将有界列表/搜索委托给现有所有者.
        """

        return await self._controller.list_sessions(query)

    async def select_session(self, session_id: str) -> SessionSelectionResult:
        """Delegate session selection while preserving resume errors.

        委托会话选择,同时保留恢复错误语义.
        """

        return await self._controller.select_session(session_id)

    async def rename_session(self, title: str) -> SessionSummary:
        """Delegate the active-session rename operation.

        委托当前会话的重命名操作.
        """

        return await self._controller.rename_session(title)

    async def inspect_recovery(self) -> tuple[TurnRecoveryInspection, ...]:
        return await self._controller.inspect_recovery()

    async def abandon_recovery(
        self,
        turn_id: str,
        *,
        reason: str = "explicit_user_resolution",
    ) -> TurnRecoveryInspection:
        return await self._controller.abandon_recovery(turn_id, reason=reason)

    async def retry_recovery(
        self,
        turn_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult:
        return await self._controller.retry_recovery(turn_id, sink=sink)


__all__ = ["SessionSelectionController", "SessionSelectionService"]
