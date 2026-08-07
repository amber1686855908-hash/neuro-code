"""Typed application owner for durable session lifecycle commands.

This module owns only the small, storage-backed lifecycle boundary shared by
the runtime, CLI, and ACP adapters.  Conversation locks, binding replacement,
workspace visibility, and protocol cleanup remain with their existing owners.

定义持久化会话生命周期命令的类型化应用 owner.
本模块只负责 Runtime、CLI 和 ACP 共享的精简存储边界;会话锁、绑定替换、工作区可见性
与协议清理仍由现有 owner 负责.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary


@dataclass(frozen=True, slots=True)
class StartSessionRequest:
    """Validated input for creating a new durable session.

    用于创建新的持久化会话的已验证输入.
    """

    cwd: str
    provider: str
    model: str
    context_affinity: str | None = None
    sandbox_profile: SandboxProfile = SandboxProfile.OFF

    def __post_init__(self) -> None:
        for field_name in ("cwd", "provider", "model"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.context_affinity is not None and (
            not isinstance(self.context_affinity, str) or not self.context_affinity.strip()
        ):
            raise ValueError("context_affinity must be non-empty when provided")
        if not isinstance(self.sandbox_profile, SandboxProfile):
            raise ValueError("sandbox_profile must be canonical")


@dataclass(frozen=True, slots=True)
class ForkSessionRequest:
    """Validated input for creating an independent session copy.

    用于创建独立会话副本的已验证输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


@dataclass(frozen=True, slots=True)
class DeleteSessionRequest:
    """Validated input for deleting one persisted session.

    用于删除一个已持久化会话的已验证输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


@dataclass(frozen=True, slots=True)
class ImportSessionRequest:
    """Validated input for importing one complete session snapshot.

    The CLI/import adapter parses the source; this boundary only validates the
    canonical snapshot before delegating its durable write.

    用于导入完整会话快照的已验证输入.
    CLI/导入适配器负责解析源数据,此边界只校验规范快照并委托持久化写入.
    """

    snapshot: SessionSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, SessionSnapshot):
            raise ValueError("import session snapshot must be canonical")


@dataclass(frozen=True, slots=True)
class RenameSessionRequest:
    """Validated input for changing a persisted session title.

    用于修改已持久化会话标题的已验证输入.
    """

    session_id: str
    title: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("session title must not be empty")


class SessionLifecycleController(Protocol):
    """Minimal lifecycle owner consumed by cross-interface adapters.

    表示跨接口适配器使用的最小生命周期 owner 契约.
    """

    async def start_session(self, request: StartSessionRequest) -> SessionSummary: ...

    async def import_session(self, request: ImportSessionRequest) -> str: ...

    async def fork_session(self, request: ForkSessionRequest) -> str: ...

    async def delete_session(self, request: DeleteSessionRequest) -> None: ...

    async def rename_session(self, request: RenameSessionRequest) -> SessionSummary: ...


class SessionLifecycleService:
    """Apply durable session lifecycle commands through ``SessionStore``.

    The service does not acquire conversation locks or perform workspace and
    protocol checks; callers retain those policies around this narrow port.

    通过 `SessionStore` 执行持久化会话生命周期命令.
    本服务不获取会话锁,也不执行工作区或协议检查;这些策略仍由调用方围绕该精简端口负责.
    """

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def start_session(self, request: StartSessionRequest) -> SessionSummary:
        """Create a session and return its canonical summary.

        创建会话并返回其规范摘要.
        """

        if not isinstance(request, StartSessionRequest):
            raise ValueError("start session request must be canonical")
        session_id = await self._store.create_session(
            request.cwd,
            request.provider,
            request.model,
            request.context_affinity,
            request.sandbox_profile,
        )
        return await self._store.get_session(session_id)

    async def import_session(self, request: ImportSessionRequest) -> str:
        """Persist one canonical session snapshot.

        持久化一个规范会话快照.
        """

        if not isinstance(request, ImportSessionRequest):
            raise ValueError("import session request must be canonical")
        return await self._store.import_session(request.snapshot)

    async def fork_session(self, request: ForkSessionRequest) -> str:
        """Create an independent durable copy of one session.

        创建一个会话的独立持久化副本.
        """

        if not isinstance(request, ForkSessionRequest):
            raise ValueError("fork session request must be canonical")
        return await self._store.fork_session(request.session_id)

    async def delete_session(self, request: DeleteSessionRequest) -> None:
        """Delete one persisted session.

        删除一个已持久化会话.
        """

        if not isinstance(request, DeleteSessionRequest):
            raise ValueError("delete session request must be canonical")
        await self._store.delete_session(request.session_id)

    async def rename_session(self, request: RenameSessionRequest) -> SessionSummary:
        """Update one persisted session title.

        更新一个已持久化会话的标题.
        """

        if not isinstance(request, RenameSessionRequest):
            raise ValueError("rename session request must be canonical")
        return await self._store.update_session_title(request.session_id, request.title)


__all__ = [
    "DeleteSessionRequest",
    "ForkSessionRequest",
    "ImportSessionRequest",
    "RenameSessionRequest",
    "SessionLifecycleController",
    "SessionLifecycleService",
    "StartSessionRequest",
]
