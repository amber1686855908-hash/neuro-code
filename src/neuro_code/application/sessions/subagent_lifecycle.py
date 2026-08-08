"""Explicit lifecycle commands for linked child subagent sessions.

定义已关联子代理会话的显式生命周期命令.

This boundary validates the parent-owned relationship before delegating the
existing session lifecycle operation.  It never starts a model turn, replays
tools, or exposes child transcript content.
该边界在委托现有会话生命周期操作前校验父会话拥有的关系.它不会启动模型回合、重放工具或暴露子会话正文.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.sessions.lifecycle import (
    DeleteSessionRequest,
    ForkSessionRequest,
    SessionLifecycleController,
)
from neuro_code.application.sessions.subagent_queries import (
    GetSubagentRelationshipRequest,
    SubagentRelationshipAction,
)
from neuro_code.domain.session_tasks import SessionTaskKind
from neuro_code.shared.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class SubagentRelationshipActionRequest:
    """Validated request for one explicit child-session lifecycle action.

    用于一次明确子会话生命周期操作的经过验证的请求.
    """

    parent_session_id: str
    parent_task_id: str
    action: SubagentRelationshipAction

    def __post_init__(self) -> None:
        GetSubagentRelationshipRequest(self.parent_session_id, self.parent_task_id)
        if not isinstance(self.action, SubagentRelationshipAction):
            raise ValueError("subagent relationship action must be canonical")


@dataclass(frozen=True, slots=True)
class SubagentRelationshipActionResult:
    """Bounded result of one relationship action.

    The result returns only lifecycle identifiers.  A forked session is not
    automatically opened or linked as a new child task.
    结果只返回生命周期标识.分叉出的会话不会自动打开,也不会被登记为新的子任务.
    """

    parent_session_id: str
    parent_task_id: str
    child_session_id: str
    action: SubagentRelationshipAction
    forked_session_id: str | None = None

    def __post_init__(self) -> None:
        GetSubagentRelationshipRequest(self.parent_session_id, self.parent_task_id)
        if not isinstance(self.child_session_id, str):
            raise ValueError("child_session_id must be a safe identifier")
        GetSubagentRelationshipRequest(self.parent_session_id, self.child_session_id)
        if self.forked_session_id is not None:
            GetSubagentRelationshipRequest(self.parent_session_id, self.forked_session_id)
        if not isinstance(self.action, SubagentRelationshipAction):
            raise ValueError("subagent relationship action must be canonical")
        if self.action is SubagentRelationshipAction.FORK:
            if self.forked_session_id is None:
                raise ValueError("fork action must return a forked session ID")
        elif self.forked_session_id is not None:
            raise ValueError("only fork action may return a forked session ID")


class SubagentRelationshipLifecycleController(Protocol):
    """Minimal owner consumed by explicit inbound relationship controls.

    表示显式入站关系控制使用的最小 owner 契约.
    """

    async def execute(
        self,
        request: SubagentRelationshipActionRequest,
    ) -> SubagentRelationshipActionResult: ...


class SubagentRelationshipLifecycleService:
    """Validate ownership and delegate one child lifecycle operation.

    The relationship is checked before each command.  This is deliberately a
    narrow application boundary; cross-process locking and provider binding
    remain owned by the existing session lifecycle and inbound adapters.
    校验关系归属并委托一次子会话生命周期操作.
    该边界保持精简;跨进程锁和 Provider 绑定仍由现有会话生命周期与入站适配器负责.
    """

    __slots__ = ("_lifecycle", "_store")

    def __init__(
        self,
        store: SessionStore,
        lifecycle: SessionLifecycleController,
    ) -> None:
        self._store = store
        self._lifecycle = lifecycle

    async def execute(
        self,
        request: SubagentRelationshipActionRequest,
    ) -> SubagentRelationshipActionResult:
        """Run one explicitly requested, terminal-child lifecycle action.

        执行一次调用方明确请求的终态子会话生命周期操作.
        """

        if not isinstance(request, SubagentRelationshipActionRequest):
            raise ValueError("subagent relationship action request must be canonical")
        link = await self._store.load_subagent_link(
            request.parent_session_id,
            request.parent_task_id,
        )
        if link is None:
            raise ConfigurationError("subagent relationship does not exist")
        task = await self._store.get_session_task(
            request.parent_session_id,
            request.parent_task_id,
        )
        if task is None or task.kind is not SessionTaskKind.SUBAGENT:
            raise ConfigurationError("subagent relationship parent task is invalid")
        if task.status.active:
            raise ConfigurationError("subagent relationship is still active")
        await self._store.get_session(link.child_session_id)

        if request.action is SubagentRelationshipAction.RESUME:
            return SubagentRelationshipActionResult(
                parent_session_id=link.parent_session_id,
                parent_task_id=link.parent_task_id,
                child_session_id=link.child_session_id,
                action=request.action,
            )
        if request.action is SubagentRelationshipAction.FORK:
            forked_session_id = await self._lifecycle.fork_session(
                ForkSessionRequest(link.child_session_id)
            )
            return SubagentRelationshipActionResult(
                parent_session_id=link.parent_session_id,
                parent_task_id=link.parent_task_id,
                child_session_id=link.child_session_id,
                action=request.action,
                forked_session_id=forked_session_id,
            )

        await self._lifecycle.delete_session(DeleteSessionRequest(link.child_session_id))
        return SubagentRelationshipActionResult(
            parent_session_id=link.parent_session_id,
            parent_task_id=link.parent_task_id,
            child_session_id=link.child_session_id,
            action=request.action,
        )


__all__ = [
    "SubagentRelationshipAction",
    "SubagentRelationshipActionRequest",
    "SubagentRelationshipActionResult",
    "SubagentRelationshipLifecycleController",
    "SubagentRelationshipLifecycleService",
]
