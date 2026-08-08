"""Narrow application contracts required by the ACP protocol adapter.

定义 ACP 协议适配器所需的精简应用契约."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.tools import Tool
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.subagent_queries import SubagentRelationshipAction
from neuro_code.application.workflows.subagent import (
    MAX_SUBAGENT_PROMPT_BYTES,
    MAX_SUBAGENT_STEPS,
)

MAX_MCP_SERVERS = 8
MAX_ADDITIONAL_DIRECTORIES = 4
MAX_ADDITIONAL_DIRECTORY_BYTES = 4 * 1024
MAX_ACP_ARTIFACT_QUERY_SESSION_ID_BYTES = 512
MAX_ACP_ARTIFACT_ID_BYTES = 64
MAX_ACP_ARTIFACT_QUERY_LIMIT = 100
MAX_ACP_ARTIFACT_QUERY_READ_BYTES = 256 * 1024
MAX_ACP_SUBAGENT_PROMPT_BYTES = MAX_SUBAGENT_PROMPT_BYTES
MAX_ACP_SUBAGENT_STEPS = MAX_SUBAGENT_STEPS
MAX_ACP_SUBAGENT_TASK_ID_BYTES = 512
_ARTIFACT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class AcpToolOutputArtifactQueryError(ValueError):
    """Stable validation failure for the private artifact query extension.

    私有 artifact 查询扩展使用的稳定输入校验失败.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AcpReadOnlySubagentQueryError(ValueError):
    """Stable validation failure for the private read-only subagent extension.

    私有只读子代理扩展使用的稳定输入校验失败.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AcpSubagentLifecycleQueryError(ValueError):
    """Stable validation failure for the private child-lifecycle extension.

    私有子会话生命周期扩展使用的稳定输入校验失败.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AcpReadOnlySubagentQuery:
    """Bounded external ACP request for one explicit read-only child run.

    一个明确只读子代理运行使用的有界 ACP 外部请求.
    """

    session_id: str
    prompt: str
    max_steps: int = 8

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_id, str)
            or not self.session_id
            or "\x00" in self.session_id
            or any(ord(character) < 32 or ord(character) == 127 for character in self.session_id)
            or len(self.session_id.encode("utf-8")) > MAX_ACP_ARTIFACT_QUERY_SESSION_ID_BYTES
        ):
            raise AcpReadOnlySubagentQueryError("session_id_invalid")
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or "\x00" in self.prompt
            or len(self.prompt.encode("utf-8")) > MAX_ACP_SUBAGENT_PROMPT_BYTES
            or any(ord(character) < 32 and character not in "\n\t\r" for character in self.prompt)
        ):
            raise AcpReadOnlySubagentQueryError("prompt_invalid")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or not 1 <= self.max_steps <= MAX_ACP_SUBAGENT_STEPS
        ):
            raise AcpReadOnlySubagentQueryError("max_steps_invalid")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AcpReadOnlySubagentQuery:
        """Parse a bounded extension payload without retaining raw fields.

        解析有界扩展载荷,且不保留原始字段.
        """

        if not isinstance(payload, Mapping):
            raise AcpReadOnlySubagentQueryError("subagent_query_invalid")
        allowed = {"sessionId", "prompt", "maxSteps"}
        if any(key not in allowed for key in payload):
            raise AcpReadOnlySubagentQueryError("subagent_query_field_unsupported")
        session_id = payload.get("sessionId")
        prompt = payload.get("prompt")
        max_steps = payload.get("maxSteps", 8)
        if not isinstance(session_id, str):
            raise AcpReadOnlySubagentQueryError("session_id_invalid")
        if not isinstance(prompt, str):
            raise AcpReadOnlySubagentQueryError("prompt_invalid")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int):
            raise AcpReadOnlySubagentQueryError("max_steps_invalid")
        return cls(session_id=session_id, prompt=prompt, max_steps=max_steps)


@dataclass(frozen=True, slots=True)
class AcpSubagentLifecycleQuery:
    """Bounded external ACP request for one child relationship action.

    一次子会话关系生命周期操作使用的有界 ACP 外部请求.
    """

    session_id: str
    task_id: str
    action: SubagentRelationshipAction

    def __post_init__(self) -> None:
        for field_name, value, limit in (
            ("session_id", self.session_id, MAX_ACP_ARTIFACT_QUERY_SESSION_ID_BYTES),
            ("task_id", self.task_id, MAX_ACP_SUBAGENT_TASK_ID_BYTES),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\x00" in value
                or len(value.encode("utf-8")) > limit
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise AcpSubagentLifecycleQueryError(f"{field_name}_invalid")
        if not isinstance(self.action, SubagentRelationshipAction):
            raise AcpSubagentLifecycleQueryError("action_invalid")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AcpSubagentLifecycleQuery:
        """Parse a strict lifecycle payload without retaining raw fields.

        严格解析生命周期载荷,且不保留原始字段.
        """

        if not isinstance(payload, Mapping):
            raise AcpSubagentLifecycleQueryError("lifecycle_query_invalid")
        allowed = {"sessionId", "taskId", "action"}
        if any(key not in allowed for key in payload):
            raise AcpSubagentLifecycleQueryError("lifecycle_query_field_unsupported")
        session_id = payload.get("sessionId")
        task_id = payload.get("taskId")
        action = payload.get("action")
        if not isinstance(session_id, str):
            raise AcpSubagentLifecycleQueryError("session_id_invalid")
        if not isinstance(task_id, str):
            raise AcpSubagentLifecycleQueryError("task_id_invalid")
        if not isinstance(action, str):
            raise AcpSubagentLifecycleQueryError("action_invalid")
        try:
            canonical_action = SubagentRelationshipAction(action)
        except ValueError:
            raise AcpSubagentLifecycleQueryError("action_invalid") from None
        return cls(session_id=session_id, task_id=task_id, action=canonical_action)


@dataclass(frozen=True, slots=True)
class AcpToolOutputArtifactQuery:
    """Typed payload for listing or reading session-owned output artifacts.

    列出或读取会话拥有的输出 artifact 的类型化载荷.

    ``artifact_id`` selects read mode.  The protocol adapter is responsible
    for mapping the external ACP session ID to an internal session before the
    application service is called.

    ``artifact_id`` 选择读取模式.协议适配器负责在调用应用服务前将 ACP 外部会话
    ID 映射到内部会话.
    """

    session_id: str
    artifact_id: str | None = None
    limit: int = MAX_ACP_ARTIFACT_QUERY_LIMIT
    max_bytes: int = MAX_ACP_ARTIFACT_QUERY_READ_BYTES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_id, str)
            or not self.session_id
            or "\x00" in self.session_id
            or any(ord(character) < 32 or ord(character) == 127 for character in self.session_id)
            or len(self.session_id.encode("utf-8")) > MAX_ACP_ARTIFACT_QUERY_SESSION_ID_BYTES
        ):
            raise AcpToolOutputArtifactQueryError("session_id_invalid")
        if self.artifact_id is not None and (
            not isinstance(self.artifact_id, str)
            or len(self.artifact_id.encode("utf-8")) > MAX_ACP_ARTIFACT_ID_BYTES
            or _ARTIFACT_ID_PATTERN.fullmatch(self.artifact_id) is None
        ):
            raise AcpToolOutputArtifactQueryError("artifact_id_invalid")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_ACP_ARTIFACT_QUERY_LIMIT
        ):
            raise AcpToolOutputArtifactQueryError("artifact_limit_invalid")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or not 1 <= self.max_bytes <= MAX_ACP_ARTIFACT_QUERY_READ_BYTES
        ):
            raise AcpToolOutputArtifactQueryError("artifact_max_bytes_invalid")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AcpToolOutputArtifactQuery:
        """Parse one bounded extension payload without retaining raw fields.

        解析一个有界扩展载荷,且不保留原始字段.
        """

        if not isinstance(payload, Mapping):
            raise AcpToolOutputArtifactQueryError("artifact_query_invalid")
        allowed = {"sessionId", "artifactId", "limit", "maxBytes"}
        if any(key not in allowed for key in payload):
            raise AcpToolOutputArtifactQueryError("artifact_query_field_unsupported")
        raw_session_id = payload.get("sessionId")
        if not isinstance(raw_session_id, str):
            raise AcpToolOutputArtifactQueryError("session_id_invalid")
        raw_artifact_id = payload.get("artifactId")
        if raw_artifact_id is not None and not isinstance(raw_artifact_id, str):
            raise AcpToolOutputArtifactQueryError("artifact_id_invalid")
        if raw_artifact_id is None and "maxBytes" in payload:
            raise AcpToolOutputArtifactQueryError("max_bytes_requires_artifact_id")
        if raw_artifact_id is not None and "limit" in payload:
            raise AcpToolOutputArtifactQueryError("limit_only_applies_to_artifact_list")
        raw_limit = payload.get("limit", MAX_ACP_ARTIFACT_QUERY_LIMIT)
        raw_max_bytes = payload.get("maxBytes", MAX_ACP_ARTIFACT_QUERY_READ_BYTES)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise AcpToolOutputArtifactQueryError("artifact_limit_invalid")
        if isinstance(raw_max_bytes, bool) or not isinstance(raw_max_bytes, int):
            raise AcpToolOutputArtifactQueryError("artifact_max_bytes_invalid")
        return cls(
            session_id=raw_session_id,
            artifact_id=raw_artifact_id,
            limit=raw_limit,
            max_bytes=raw_max_bytes,
        )


class AcpWorkspaceValidationError(ValueError):
    """Stable workspace-validation failure reported to an ACP client.

    表示报告给 ACP 客户端的稳定工作区验证失败."""

    def __init__(self, reason: str, details: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class AcpMcpToolError(RuntimeError):
    """Stable failure raised while opening ACP session-owned MCP tools.

    表示打开会话拥有的 ACP MCP 工具时抛出的稳定失败."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AcpResumeUnavailableError(RuntimeError):
    """The persisted session cannot safely be resumed by this ACP process.

    表示当前 ACP 进程无法安全恢复持久化会话."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AcpMcpStdioServerConfig:
    """Validated stdio MCP server input independent of its concrete adapter.

    表示与具体适配器无关且已验证的 stdio MCP 服务器输入."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AcpMcpHttpServerConfig:
    """Validated remote MCP server input for Streamable HTTP or legacy SSE.

    表示适用于 Streamable HTTP 或旧版 SSE 的已验证远程 MCP 服务器输入."""

    name: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    transport: Literal["http", "sse"] = "http"


type AcpMcpServerConfig = AcpMcpStdioServerConfig | AcpMcpHttpServerConfig


@dataclass(frozen=True, slots=True)
class AcpSessionMetadata:
    """Read-only process metadata ACP needs for protocol mapping.

    表示 ACP 进行协议映射所需的只读进程元数据."""

    workspace: Path
    protected_environment_variables: frozenset[str]
    context_window_tokens: int | None


@dataclass(frozen=True, slots=True)
class AcpBinding:
    """A binding and the metadata the protocol must render for it.

    表示一个绑定以及协议需要为它渲染的元数据."""

    binding: ConversationBinding
    context_window_tokens: int | None


class AcpPreparedSession(Protocol):
    """A safely selected persisted-session configuration, not the full config.

    表示安全选出的持久化会话配置,不包含完整配置."""

    @property
    def context_window_tokens(self) -> int | None: ...

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None,
        additional_tools: Sequence[Tool],
        additional_workspace_roots: Sequence[Path],
        client_file_system: ClientFileSystem | None,
        client_terminal: ClientTerminal | None,
    ) -> ConversationBinding: ...


class AcpBindingFactory(Protocol):
    """Create ACP conversation bindings without exposing a composition root.

    创建 ACP 会话绑定,但不暴露组合根."""

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None,
        additional_tools: Sequence[Tool],
        additional_workspace_roots: Sequence[Path],
        client_file_system: ClientFileSystem | None,
        client_terminal: ClientTerminal | None,
    ) -> AcpBinding: ...

    async def prepare_session_resume(self, session_id: str) -> AcpPreparedSession: ...


class AcpMcpTools(Protocol):
    """One session-owned MCP tool context with deterministic shutdown.

    表示一个由会话拥有并可确定性关闭的 MCP 工具上下文."""

    @property
    def tools(self) -> Sequence[Tool]: ...

    async def close(self) -> None: ...


class AcpMcpToolFactory(Protocol):
    """Open a concrete MCP context only after ACP creates a session.

    仅在 ACP 创建会话后打开具体的 MCP 上下文."""

    async def open(
        self,
        configurations: Sequence[AcpMcpServerConfig],
        *,
        cwd: Path,
        explicit_redactions: Sequence[str],
    ) -> AcpMcpTools: ...


class AcpWorkspaceValidator(Protocol):
    """Concrete workspace identity validation selected by bootstrap.

    表示由 bootstrap 选择的具体工作区身份验证逻辑."""

    async def validate(
        self,
        cwd: str,
        additional_directories: Sequence[str],
    ) -> tuple[Path, ...]: ...

    def matches(self, cwd: Path) -> bool: ...
