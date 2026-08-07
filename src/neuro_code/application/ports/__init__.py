"""Canonical application ports implemented by infrastructure adapters.

定义由基础设施适配器实现的规范应用端口."""

from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.background_tasks import (
    BackgroundTaskManager,
    BackgroundTaskSupervisor,
)
from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal, ClientTerminalResult
from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
    ProviderSettingsStore,
)
from neuro_code.application.ports.sandbox import ShellLaunch, ShellSandbox
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.terminal import (
    InteractiveTerminalManager,
    InteractiveTerminalSession,
)
from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
    MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
    TOOL_OUTPUT_ARTIFACT_PRUNE_GRACE_SECONDS,
    Tool,
    ToolCollection,
    ToolContext,
    ToolOutputArtifact,
    ToolOutputArtifactGarbageCollector,
    ToolOutputArtifactPruneResult,
    ToolOutputArtifactRead,
    ToolOutputArtifactReader,
    ToolOutputArtifactStore,
)
from neuro_code.application.ports.ui_preferences import UiPreferencesStore
from neuro_code.application.ports.workspace import WorkspaceIdentity, WorkspacePathResolver
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeEventPayload,
    WorkspaceChangeFileEventPayload,
    WorkspaceChangeHiddenFileEventPayload,
    WorkspaceChangeHiddenReason,
    WorkspaceChangeObserver,
    WorkspaceChangeReport,
    WorkspaceChangeStatus,
    WorkspaceChangeVisibleFileEventPayload,
    WorkspaceFileChange,
)

__all__ = [
    "MAX_TOOL_OUTPUT_ARTIFACT_BYTES",
    "MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES",
    "TOOL_OUTPUT_ARTIFACT_PRUNE_GRACE_SECONDS",
    "BackgroundTaskManager",
    "BackgroundTaskSupervisor",
    "ClientFileSystem",
    "ClientTerminal",
    "ClientTerminalResult",
    "HttpClientPolicy",
    "InteractiveTerminalManager",
    "InteractiveTerminalSession",
    "ManagedProviderProfile",
    "ManagedProviderSettings",
    "ManagedProxyPolicy",
    "ModelProvider",
    "ModelToolPolicy",
    "PermissionApprover",
    "ProviderCatalog",
    "ProviderCatalogError",
    "ProviderCatalogResult",
    "ProviderConnectionSpec",
    "ProviderSettingsStore",
    "SessionStore",
    "ShellLaunch",
    "ShellSandbox",
    "Tool",
    "ToolCollection",
    "ToolContext",
    "ToolOutputArtifact",
    "ToolOutputArtifactGarbageCollector",
    "ToolOutputArtifactPruneResult",
    "ToolOutputArtifactRead",
    "ToolOutputArtifactReader",
    "ToolOutputArtifactStore",
    "UiPreferencesStore",
    "WorkspaceChangeCheckpoint",
    "WorkspaceChangeEventPayload",
    "WorkspaceChangeFileEventPayload",
    "WorkspaceChangeHiddenFileEventPayload",
    "WorkspaceChangeHiddenReason",
    "WorkspaceChangeObserver",
    "WorkspaceChangeReport",
    "WorkspaceChangeStatus",
    "WorkspaceChangeVisibleFileEventPayload",
    "WorkspaceFileChange",
    "WorkspaceIdentity",
    "WorkspacePathResolver",
]
