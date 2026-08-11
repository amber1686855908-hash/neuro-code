"""Canonical tool execution ports.

定义规范的工具执行端口."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from neuro_code.application.ports.background_tasks import BackgroundTaskManager
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.instructions import InstructionContextTracker
from neuro_code.application.ports.sandbox import LocalProcessSandbox
from neuro_code.application.ports.skills import SkillContextTracker
from neuro_code.application.ports.user_interaction import (
    InteractionEventSink,
    UserInteractionPort,
)
from neuro_code.application.ports.workspace_changes import WorkspaceChangeJournal
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult

MAX_TOOL_OUTPUT_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES = 256 * 1024
TOOL_OUTPUT_ARTIFACT_PRUNE_GRACE_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class ToolOutputArtifact:
    """Describe one bounded, redacted full-output artifact.

    描述一个有界且已脱敏的完整输出文件.
    """

    artifact_id: str
    relative_path: str
    byte_count: int
    truncated: bool

    def __post_init__(self) -> None:
        if not self.artifact_id or "\x00" in self.artifact_id:
            raise ValueError("tool output artifact ID must be non-empty")
        if not self.relative_path or self.relative_path.startswith(("/", "\\")):
            raise ValueError("tool output artifact path must be relative")
        if "\x00" in self.relative_path or ".." in Path(self.relative_path).parts:
            raise ValueError("tool output artifact path must not escape its root")
        if self.byte_count < 0:
            raise ValueError("tool output artifact byte count must not be negative")


class ToolOutputArtifactStore(Protocol):
    """Persist bounded tool output outside the model-visible conversation.

    将有界工具输出持久化到模型不可见的会话状态目录之外.
    """

    async def save(
        self,
        *,
        tool_name: str,
        content: bytes,
        content_truncated: bool = False,
    ) -> ToolOutputArtifact: ...


@dataclass(frozen=True, slots=True)
class ToolOutputArtifactPruneResult:
    """Bounded result of an explicit, conservative artifact sweep.

    显式且保守 artifact 清理的有界结果.
    """

    deleted_count: int
    preserved_count: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.deleted_count, bool)
            or not isinstance(self.deleted_count, int)
            or self.deleted_count < 0
        ):
            raise ValueError("deleted artifact count must be non-negative")
        if (
            isinstance(self.preserved_count, bool)
            or not isinstance(self.preserved_count, int)
            or self.preserved_count < 0
        ):
            raise ValueError("preserved artifact count must be non-negative")


class ToolOutputArtifactGarbageCollector(Protocol):
    """Delete only old, unreferenced artifact files after a complete scan.

    在完成引用扫描后,只删除过期且未被引用的 artifact 文件.
    """

    async def prune_unreferenced(
        self,
        keep_artifact_ids: Collection[str],
        *,
        min_age_seconds: float = TOOL_OUTPUT_ARTIFACT_PRUNE_GRACE_SECONDS,
    ) -> ToolOutputArtifactPruneResult: ...


@dataclass(frozen=True, slots=True)
class ToolOutputArtifactRead:
    """A bounded, redacted text projection of one output artifact.

    一个输出 artifact 的有界、已脱敏文本投影.
    """

    artifact: ToolOutputArtifact
    content: str
    read_truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ToolOutputArtifact):
            raise ValueError("tool output artifact read handle must be canonical")
        if not isinstance(self.content, str):
            raise ValueError("tool output artifact read content must be text")
        if not isinstance(self.read_truncated, bool):
            raise ValueError("tool output artifact read truncation must be boolean")


class ToolOutputArtifactReader(Protocol):
    """Read a bounded artifact through an opaque, validated handle.

    通过不透明且经过校验的句柄读取有界 artifact.
    """

    async def read(
        self,
        artifact: ToolOutputArtifact,
        *,
        max_bytes: int = MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
    ) -> ToolOutputArtifactRead: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    cwd: Path
    additional_workspace_roots: tuple[Path, ...] = ()
    output_byte_limit: int = 200_000
    command_timeout_seconds: float = 120.0
    termination_grace_seconds: float = 1.0
    sandbox_profile: SandboxProfile = SandboxProfile.OFF
    local_process_sandbox: LocalProcessSandbox | None = None
    protected_environment_variables: frozenset[str] = frozenset()
    redaction_values: tuple[str, ...] = field(default=(), repr=False)
    background_tasks: BackgroundTaskManager | None = None
    instruction_tracker: InstructionContextTracker | None = None
    skill_tracker: SkillContextTracker | None = None
    client_file_system: ClientFileSystem | None = None
    client_terminal: ClientTerminal | None = None
    output_artifact_store: ToolOutputArtifactStore | None = None
    workspace_change_journal: WorkspaceChangeJournal | None = None
    user_interaction: UserInteractionPort | None = None
    interaction_event_sink: InteractionEventSink | None = field(default=None, repr=False)


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    @property
    def side_effecting(self) -> bool: ...

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult: ...


@runtime_checkable
class InteractionControlTool(Protocol):
    """Typed marker for a tool that controls user-interaction flow."""

    interaction_control: str


class ToolCollection(Protocol):
    """Resolve tools and expose their ordered model definitions.

    解析工具并提供按稳定顺序排列的模型工具定义."""

    def get(self, name: str) -> Tool | None: ...

    def definitions(self) -> tuple[ToolDefinition, ...]: ...


__all__ = [
    "MAX_TOOL_OUTPUT_ARTIFACT_BYTES",
    "MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES",
    "TOOL_OUTPUT_ARTIFACT_PRUNE_GRACE_SECONDS",
    "InteractionControlTool",
    "Tool",
    "ToolCollection",
    "ToolContext",
    "ToolOutputArtifact",
    "ToolOutputArtifactGarbageCollector",
    "ToolOutputArtifactPruneResult",
    "ToolOutputArtifactRead",
    "ToolOutputArtifactReader",
    "ToolOutputArtifactStore",
]
