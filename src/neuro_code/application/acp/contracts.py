"""Narrow application contracts required by the ACP protocol adapter."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.tools import Tool
from neuro_code.application.runtime.profile_conversation import ConversationBinding

MAX_MCP_SERVERS = 8
MAX_ADDITIONAL_DIRECTORIES = 4
MAX_ADDITIONAL_DIRECTORY_BYTES = 4 * 1024


class AcpWorkspaceValidationError(ValueError):
    """Stable workspace-validation failure reported to an ACP client."""

    def __init__(self, reason: str, details: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class AcpMcpToolError(RuntimeError):
    """Stable failure raised while opening ACP session-owned MCP tools."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AcpResumeUnavailableError(RuntimeError):
    """The persisted session cannot safely be resumed by this ACP process."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AcpMcpServerConfig:
    """Validated stdio MCP server input independent of its concrete adapter."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AcpSessionMetadata:
    """Read-only process metadata ACP needs for protocol mapping."""

    workspace: Path
    protected_environment_variables: frozenset[str]
    context_window_tokens: int | None


@dataclass(frozen=True, slots=True)
class AcpBinding:
    """A binding and the metadata the protocol must render for it."""

    binding: ConversationBinding
    context_window_tokens: int | None


class AcpPreparedSession(Protocol):
    """A safely selected persisted-session configuration, not the full config."""

    @property
    def context_window_tokens(self) -> int | None: ...

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None,
        additional_tools: Sequence[Tool],
        additional_workspace_roots: Sequence[Path],
    ) -> ConversationBinding: ...


class AcpBindingFactory(Protocol):
    """Create ACP conversation bindings without exposing a composition root."""

    async def create_binding(
        self,
        *,
        approver: PermissionApprover | None,
        additional_tools: Sequence[Tool],
        additional_workspace_roots: Sequence[Path],
    ) -> AcpBinding: ...

    async def prepare_session_resume(self, session_id: str) -> AcpPreparedSession: ...


class AcpMcpTools(Protocol):
    """One session-owned MCP tool context with deterministic shutdown."""

    @property
    def tools(self) -> Sequence[Tool]: ...

    async def close(self) -> None: ...


class AcpMcpToolFactory(Protocol):
    """Open a concrete MCP context only after ACP creates a session."""

    async def open(
        self,
        configurations: Sequence[AcpMcpServerConfig],
        *,
        cwd: Path,
        explicit_redactions: Sequence[str],
    ) -> AcpMcpTools: ...


class AcpWorkspaceValidator(Protocol):
    """Concrete workspace identity validation selected by bootstrap."""

    async def validate(
        self,
        cwd: str,
        additional_directories: Sequence[str],
    ) -> tuple[Path, ...]: ...

    def matches(self, cwd: Path) -> bool: ...
