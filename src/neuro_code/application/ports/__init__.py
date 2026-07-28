"""Canonical application ports implemented by infrastructure adapters."""

from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.background_tasks import (
    BackgroundTaskManager,
    BackgroundTaskSupervisor,
)
from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.provider_catalog import ProviderCatalog
from neuro_code.application.ports.sandbox import ShellLaunch, ShellSandbox
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.terminal import (
    InteractiveTerminalManager,
    InteractiveTerminalSession,
)
from neuro_code.application.ports.tools import Tool, ToolCollection, ToolContext
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
    "BackgroundTaskManager",
    "BackgroundTaskSupervisor",
    "HttpClientPolicy",
    "InteractiveTerminalManager",
    "InteractiveTerminalSession",
    "ModelProvider",
    "PermissionApprover",
    "ProviderCatalog",
    "SessionStore",
    "ShellLaunch",
    "ShellSandbox",
    "Tool",
    "ToolCollection",
    "ToolContext",
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
