"""Provider-neutral ports for the read-only Language Server Protocol slice.

LSP is deliberately an application capability rather than a model-provider
feature.  The port contains only bounded configuration, request, and status
values; framing, process ownership, URI parsing, and server quirks remain in
the infrastructure adapter.

只读 LSP 纵向切片的 Provider 无关应用端口.

LSP 刻意作为应用能力存在,而不是模型 Provider 功能.本端口只包含有界的配置、
请求和状态值;分帧、进程所有权、URI 解析和 server 差异由基础设施适配器拥有.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from neuro_code.application.ports.workspace import FilesystemAccessTarget
from neuro_code.shared.errors import ToolError

MAX_LSP_PROFILE_NAME_BYTES = 128
MAX_LSP_LANGUAGE_BYTES = 128
MAX_LSP_COMMAND_PARTS = 32
MAX_LSP_COMMAND_BYTES = 4 * 1024
MAX_LSP_EXTENSIONS = 64
MAX_LSP_ROOT_MARKERS = 32
MAX_LSP_QUERY_BYTES = 4 * 1024
MAX_LSP_RESULT_ITEMS = 200


class LspFailureKind(StrEnum):
    """Bounded facts describing a failure at the LSP application boundary."""

    NOT_CONFIGURED = "not_configured"
    PROFILE_NOT_FOUND = "profile_not_found"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    STARTUP_TIMEOUT = "startup_timeout"
    INITIALIZATION_TIMEOUT = "initialization_timeout"
    REQUEST_TIMEOUT = "request_timeout"
    SHUTDOWN_TIMEOUT = "shutdown_timeout"
    SERVER_CRASH = "server_crash"
    PROTOCOL_ERROR = "protocol_error"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_URI = "invalid_uri"
    SECURITY_FILTERED = "security_filtered"
    DOCUMENT_ERROR = "document_error"
    CONFIGURATION_ERROR = "configuration_error"
    UNKNOWN = "unknown"


class LspFailurePhase(StrEnum):
    CONFIGURATION = "configuration"
    STARTUP = "startup"
    INITIALIZATION = "initialization"
    DOCUMENT_SYNC = "document_sync"
    REQUEST = "request"
    NOTIFICATION = "notification"
    SHUTDOWN = "shutdown"
    PROJECTION = "projection"


class LspError(ToolError):
    """An expected LSP failure that remains inside the tool boundary."""

    def __init__(
        self,
        message: str,
        *,
        kind: LspFailureKind,
        phase: LspFailurePhase,
        retryable: bool = False,
    ) -> None:
        self.kind = kind
        self.phase = phase
        self.retryable = retryable
        super().__init__(message[:1_000])


class LspOperation(StrEnum):
    """Read-only semantic operations exposed to the model."""

    DEFINITION = "definition"
    REFERENCES = "references"
    HOVER = "hover"
    DOCUMENT_SYMBOLS = "document_symbols"
    WORKSPACE_SYMBOLS = "workspace_symbols"
    DIAGNOSTICS = "diagnostics"
    STATUS = "status"
    RESTART = "restart"


@dataclass(frozen=True, slots=True)
class LanguageServerProfile:
    """One explicit argv-safe language-server configuration.

    The executable is never installed or inferred from a language name.  A
    profile is eligible only when its configured command is available at
    execution time.
    """

    name: str
    language: str
    command: tuple[str, ...]
    extensions: tuple[str, ...] = ()
    root_markers: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("language-server profile name must be non-empty")
        if len(self.name.encode("utf-8")) > MAX_LSP_PROFILE_NAME_BYTES:
            raise ValueError("language-server profile name is too long")
        if not isinstance(self.language, str) or not self.language.strip():
            raise ValueError("language-server profile language must be non-empty")
        if len(self.language.encode("utf-8")) > MAX_LSP_LANGUAGE_BYTES:
            raise ValueError("language-server profile language is too long")
        if (
            not isinstance(self.command, tuple)
            or not 1 <= len(self.command) <= MAX_LSP_COMMAND_PARTS
        ):
            raise ValueError("language-server profile command must contain 1-32 argv parts")
        if any(not isinstance(part, str) or not part or "\x00" in part for part in self.command):
            raise ValueError("language-server profile command contains an invalid argv part")
        if len("\x00".join(self.command).encode("utf-8")) > MAX_LSP_COMMAND_BYTES:
            raise ValueError("language-server profile command is too long")
        try:
            raw_extensions = tuple(self.extensions)
            raw_markers = tuple(self.root_markers)
            variables = dict(self.environment)
        except (TypeError, ValueError) as error:
            raise ValueError("language-server profile collections are invalid") from error
        if len(raw_extensions) > MAX_LSP_EXTENSIONS or any(
            not isinstance(extension, str) or not extension.startswith(".") or "\x00" in extension
            for extension in raw_extensions
        ):
            raise ValueError("language-server profile extensions are invalid")
        if len(raw_markers) > MAX_LSP_ROOT_MARKERS or any(
            not isinstance(marker, str)
            or not marker
            or marker in {".", ".."}
            or "/" in marker
            or "\\" in marker
            or "\x00" in marker
            for marker in raw_markers
        ):
            raise ValueError("language-server profile root markers are invalid")
        extensions = tuple(extension.casefold() for extension in raw_extensions)
        markers = raw_markers
        for key, value in variables.items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\x00" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise ValueError("language-server profile environment is invalid")
        if not isinstance(self.enabled, bool):
            raise TypeError("language-server profile enabled must be boolean")
        object.__setattr__(self, "extensions", extensions)
        object.__setattr__(self, "root_markers", markers)
        object.__setattr__(self, "environment", MappingProxyType(variables))


@dataclass(frozen=True, slots=True)
class LspRequest:
    """A validated model-facing semantic request."""

    operation: LspOperation
    path: Path | None = None
    line: int | None = None
    column: int | None = None
    query: str | None = None
    profile: str | None = None
    max_results: int = MAX_LSP_RESULT_ITEMS

    def __post_init__(self) -> None:
        if not isinstance(self.operation, LspOperation):
            raise TypeError("LSP operation must be canonical")
        if self.path is not None and (
            not isinstance(self.path, Path) or not self.path.is_absolute()
        ):
            raise ValueError("LSP path must be an absolute pathlib.Path")
        for name in ("line", "column"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"LSP {name} must be a positive integer")
        if self.query is not None:
            if not isinstance(self.query, str) or not self.query.strip():
                raise ValueError("LSP query must be non-empty when provided")
            if len(self.query.encode("utf-8")) > MAX_LSP_QUERY_BYTES:
                raise ValueError("LSP query is too long")
        if self.profile is not None and (
            not isinstance(self.profile, str) or not self.profile.strip()
        ):
            raise ValueError("LSP profile must be non-empty when provided")
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or not 1 <= self.max_results <= MAX_LSP_RESULT_ITEMS
        ):
            raise ValueError("LSP max_results is outside the bounded range")


@dataclass(frozen=True, slots=True)
class LspOperationResult:
    """Bounded, provider-neutral projection returned by the LSP service."""

    operation: LspOperation
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.operation, LspOperation):
            raise TypeError("LSP result operation must be canonical")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class LspStatus:
    """Safe operational status for one workspace/profile route."""

    workspace_root: Path
    profile: str | None
    language: str | None
    state: str
    capabilities: tuple[str, ...] = ()
    last_error: str | None = None
    restart_count: int = 0
    omitted_results: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_root, Path) or not self.workspace_root.is_absolute():
            raise ValueError("LSP status workspace root must be absolute")
        if not isinstance(self.state, str) or not self.state:
            raise ValueError("LSP status state must be non-empty")
        if isinstance(self.restart_count, bool) or not isinstance(self.restart_count, int):
            raise TypeError("LSP status restart count must be an integer")
        if self.restart_count < 0:
            raise ValueError("LSP status restart count must be non-negative")
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise TypeError("LSP status last error must be text")
        if self.omitted_results < 0:
            raise ValueError("LSP status omitted result count must be non-negative")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


class LspResultVisibilityPolicy(Protocol):
    """Permission projection used only to filter returned cross-file URIs."""

    def decide_targets(
        self,
        tool_name: str,
        targets: tuple[FilesystemAccessTarget, ...],
        *,
        side_effecting: bool,
    ) -> object: ...


class LanguageServerService(Protocol):
    """Application-facing lifecycle and semantic-query boundary."""

    async def execute(
        self,
        request: LspRequest,
        *,
        visibility_policy: LspResultVisibilityPolicy | None = None,
    ) -> LspOperationResult: ...

    async def close(self) -> None: ...


__all__ = [
    "MAX_LSP_COMMAND_BYTES",
    "MAX_LSP_COMMAND_PARTS",
    "MAX_LSP_EXTENSIONS",
    "MAX_LSP_LANGUAGE_BYTES",
    "MAX_LSP_PROFILE_NAME_BYTES",
    "MAX_LSP_QUERY_BYTES",
    "MAX_LSP_RESULT_ITEMS",
    "MAX_LSP_ROOT_MARKERS",
    "LanguageServerProfile",
    "LanguageServerService",
    "LspOperation",
    "LspOperationResult",
    "LspRequest",
    "LspResultVisibilityPolicy",
    "LspStatus",
]
