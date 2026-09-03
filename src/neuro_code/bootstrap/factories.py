"""Concrete infrastructure factories selected by bootstrap.

Bootstrap 默认 concrete factory 选择.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from neuro_code.application.ports.background_tasks import BackgroundTaskSupervisor
from neuro_code.application.ports.configuration import AppConfig
from neuro_code.application.ports.instructions import InstructionDiscovery
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.sandbox import LocalProcessSandbox
from neuro_code.application.ports.skills import SkillDiscovery
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.workspace_changes import WorkspaceChangeObserver
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.background_tasks import LocalBackgroundTaskManager
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.providers import create_routed_provider
from neuro_code.infrastructure.sandbox.linux_local_process import LinuxBubblewrapLocalProcessSandbox
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox
from neuro_code.infrastructure.sandbox.windows_native_local_process import (
    WindowsNativeLocalProcessSandbox,
)
from neuro_code.infrastructure.workspace.changes import FilesystemWorkspaceChangeObserver
from neuro_code.infrastructure.workspace.instructions import FilesystemInstructionDiscovery
from neuro_code.infrastructure.workspace.skills import FilesystemSkillDiscovery
from neuro_code.shared.errors import SandboxError

ProviderFactory = Callable[["AppConfig", bool], ModelProvider]
LocalProcessSandboxFactory = Callable[[SandboxProfile, Path, Path], LocalProcessSandbox]
SessionStoreFactory = Callable[[Path], SessionStore]
BackgroundSupervisorFactory = Callable[[], BackgroundTaskSupervisor]
InstructionDiscoveryFactory = Callable[[], InstructionDiscovery]
SkillDiscoveryFactory = Callable[[], SkillDiscovery]
WorkspaceChangeObserverFactory = Callable[[], WorkspaceChangeObserver]


def _default_provider_factory(config: AppConfig, failover: bool) -> ModelProvider:
    return create_routed_provider(config, failover=failover)


def _default_local_process_sandbox_factory(
    profile: SandboxProfile,
    workspace: Path,
    state_dir: Path,
) -> LocalProcessSandbox:
    """Choose the canonical local-process launcher for one session binding.

    为一个会话绑定选择规范本地进程启动器.

    ``off`` preserves the existing owned-process bridge. Enabled profiles use
    the platform's child adapter on Linux, macOS, or Windows W3; unsupported
    platform/profile combinations fail closed. The controller is never
    re-executed inside a sandbox namespace.

    ``off`` 保留既有的受管进程桥接器.每个启用的 profile 都通过 Linux Bubblewrap、
    macOS Seatbelt 或 Windows W3 child adapter 创建边界;不支持的平台/profile
    组合失败关闭.controller 不会重新执行到沙箱中.
    """

    if not profile.enabled:
        return ProcessTreeLocalProcessSandbox()
    platform = _runtime_platform()
    if platform.startswith("linux"):
        return LinuxBubblewrapLocalProcessSandbox(profile, workspace, state_dir)
    if platform == "darwin":
        from neuro_code.infrastructure.sandbox.macos_local_process import (
            MacOSSeatbeltLocalProcessSandbox,
        )

        return MacOSSeatbeltLocalProcessSandbox(profile, workspace, state_dir)
    if platform.startswith("win"):
        return WindowsNativeLocalProcessSandbox(profile, workspace, state_dir)
    raise SandboxError(f"sandbox profile {profile.value!r} is not enforceable on {platform}")


def _runtime_platform() -> str:
    return sys.platform


def _default_session_store_factory(path: Path) -> SessionStore:
    return SqliteSessionStore(path)


def _default_background_supervisor_factory() -> BackgroundTaskSupervisor:
    return LocalBackgroundTaskManager()


def _default_instruction_discovery_factory() -> InstructionDiscovery:
    return FilesystemInstructionDiscovery()


def _default_skill_discovery_factory() -> SkillDiscovery:
    return FilesystemSkillDiscovery()


def _default_workspace_change_observer_factory() -> WorkspaceChangeObserver:
    return FilesystemWorkspaceChangeObserver()
