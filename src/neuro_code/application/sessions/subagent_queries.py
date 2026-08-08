"""Typed read-only projections for parent/child subagent relationships.

定义父子子代理关系的类型化只读投影.

This boundary reports which explicit lifecycle operations are currently safe
to offer to an inbound interface.  It never executes resume, fork, or delete,
and it never loads child messages, events, prompts, tool arguments, or output.
该边界只报告入站接口当前可以提供哪些明确生命周期操作. 它不会执行恢复、分叉或删除,
也不会加载子会话消息、事件、提示词、工具参数或输出.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.session_tasks import (
    MAX_SUBAGENT_LINK_ID_BYTES,
    SessionTaskKind,
    SessionTaskStatus,
    SubagentLink,
)
from neuro_code.shared.errors import ConfigurationError

MAX_SUBAGENT_RELATIONSHIP_LIMIT = 100


class SubagentRelationshipAction(StrEnum):
    """An explicit lifecycle action an interface may offer to a caller.

    表示入站接口可以提供给调用方的明确生命周期动作.

    Values are capability labels only; this read-only query never performs the
    corresponding mutation or starts a model turn.
    这些值只是能力标签;本只读查询不会执行相应变更,也不会启动模型回合.
    """

    RESUME = "resume"
    FORK = "fork"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class ListSubagentRelationshipsRequest:
    """Validated input for a bounded parent-child relationship query.

    用于有界查询父子关系的经过验证的输入.
    """

    parent_session_id: str
    limit: int = 50

    def __post_init__(self) -> None:
        _validate_identifier(self.parent_session_id, field_name="parent_session_id")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_SUBAGENT_RELATIONSHIP_LIMIT
        ):
            raise ValueError(
                "subagent relationship limit must be between 1 and "
                f"{MAX_SUBAGENT_RELATIONSHIP_LIMIT}"
            )


@dataclass(frozen=True, slots=True)
class GetSubagentRelationshipRequest:
    """Validated input for one parent-task relationship lookup.

    用于查询一个父任务关系的经过验证的输入.
    """

    parent_session_id: str
    parent_task_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.parent_session_id, field_name="parent_session_id")
        _validate_identifier(self.parent_task_id, field_name="parent_task_id")


@dataclass(frozen=True, slots=True)
class SubagentRelationshipProjection:
    """Safe metadata projection for one linked child session.

    The projection contains identifiers, task status, provider/model labels,
    timestamps, and capability labels only.  It deliberately excludes the
    child working directory, title, transcript, prompt, tools, output, and
    credentials.

    一个已关联子会话的安全元数据投影.
    投影只包含标识符、任务状态、Provider/模型标签、时间戳和能力标签,
    有意排除子会话工作区、标题、transcript、提示词、工具、输出和凭据.
    """

    parent_session_id: str
    parent_task_id: str
    child_session_id: str
    task_status: SessionTaskStatus
    created_at: datetime
    child_provider: str
    child_model: str
    child_updated_at: datetime
    available_actions: tuple[SubagentRelationshipAction, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.parent_session_id, field_name="parent_session_id")
        _validate_identifier(self.parent_task_id, field_name="parent_task_id")
        _validate_identifier(self.child_session_id, field_name="child_session_id")
        if not isinstance(self.task_status, SessionTaskStatus):
            raise ValueError("subagent relationship task status must be canonical")
        for field_name, value in (
            ("created_at", self.created_at),
            ("child_updated_at", self.child_updated_at),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if not isinstance(self.child_provider, str) or not self.child_provider.strip():
            raise ValueError("child_provider must not be empty")
        if not isinstance(self.child_model, str) or not self.child_model.strip():
            raise ValueError("child_model must not be empty")
        actions = tuple(self.available_actions)
        if len(set(actions)) != len(actions) or not all(
            isinstance(action, SubagentRelationshipAction) for action in actions
        ):
            raise ValueError("subagent relationship actions must be unique and canonical")
        expected_actions = _available_actions(self.task_status)
        if actions != expected_actions:
            raise ValueError("subagent relationship actions do not match task status")
        object.__setattr__(self, "available_actions", actions)


class SubagentRelationshipQueryController(Protocol):
    """Minimal read-only owner consumed by inbound relationship views.

    表示入站关系视图使用的最小只读 owner 契约.
    """

    async def list_subagent_relationships(
        self,
        request: ListSubagentRelationshipsRequest,
    ) -> tuple[SubagentRelationshipProjection, ...]: ...

    async def get_subagent_relationship(
        self,
        request: GetSubagentRelationshipRequest,
    ) -> SubagentRelationshipProjection | None: ...


class SubagentRelationshipQueryService:
    """Build bounded parent-child projections through the storage port.

    通过存储端口构建有界父子关系投影.

    Resume, fork, and delete remain owned by their existing lifecycle
    services.  This service only reads enough metadata to tell an interface
    which actions are safe to present.
    恢复、分叉和删除仍由现有生命周期服务负责. 本服务只读取足够的元数据,
    用于告知接口哪些动作可以安全展示.
    """

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def list_subagent_relationships(
        self,
        request: ListSubagentRelationshipsRequest,
    ) -> tuple[SubagentRelationshipProjection, ...]:
        """List newest bounded child relationships without side effects.

        无副作用地列出最新的有界子会话关系.
        """

        if not isinstance(request, ListSubagentRelationshipsRequest):
            raise ValueError("list subagent relationships request must be canonical")
        links = await self._store.list_subagent_links(
            request.parent_session_id,
            limit=request.limit,
        )
        projections: list[SubagentRelationshipProjection] = []
        for link in links:
            projections.append(await self._project(link))
        return tuple(projections)

    async def get_subagent_relationship(
        self,
        request: GetSubagentRelationshipRequest,
    ) -> SubagentRelationshipProjection | None:
        """Load one child relationship without starting or replaying it.

        在不启动或重放子会话的情况下加载一个子关系.
        """

        if not isinstance(request, GetSubagentRelationshipRequest):
            raise ValueError("get subagent relationship request must be canonical")
        link = await self._store.load_subagent_link(
            request.parent_session_id,
            request.parent_task_id,
        )
        if link is None:
            return None
        return await self._project(link)

    async def _project(self, link: SubagentLink) -> SubagentRelationshipProjection:
        if not isinstance(link, SubagentLink):
            raise ConfigurationError("subagent relationship storage returned an invalid link")
        task = await self._store.get_session_task(link.parent_session_id, link.parent_task_id)
        if task is None or task.kind is not SessionTaskKind.SUBAGENT:
            raise ConfigurationError("subagent relationship parent task is invalid")
        child = await self._store.get_session(link.child_session_id)
        return SubagentRelationshipProjection(
            parent_session_id=link.parent_session_id,
            parent_task_id=link.parent_task_id,
            child_session_id=link.child_session_id,
            task_status=task.status,
            created_at=link.created_at,
            child_provider=child.provider,
            child_model=child.model,
            child_updated_at=child.updated_at,
            available_actions=_available_actions(task.status),
        )


def _available_actions(status: SessionTaskStatus) -> tuple[SubagentRelationshipAction, ...]:
    if status.active:
        return ()
    return (
        SubagentRelationshipAction.RESUME,
        SubagentRelationshipAction.FORK,
        SubagentRelationshipAction.DELETE,
    )


def _validate_identifier(value: object, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_SUBAGENT_LINK_ID_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")


__all__ = [
    "MAX_SUBAGENT_RELATIONSHIP_LIMIT",
    "GetSubagentRelationshipRequest",
    "ListSubagentRelationshipsRequest",
    "SubagentRelationshipAction",
    "SubagentRelationshipProjection",
    "SubagentRelationshipQueryController",
    "SubagentRelationshipQueryService",
]
