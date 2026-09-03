"""Contracts shared by the inbound CLI command handlers.

CLI 命令处理器共享的入站 contract.

The contracts describe only the capabilities selected by bootstrap.  Concrete
provider, persistence, and filesystem choices remain outside the interface
package.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.tools.service import (
    SessionToolOutputArtifactApplicationService,
)
from neuro_code.domain.sessions import SessionSnapshot
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult

if TYPE_CHECKING:
    from neuro_code.application.ports.configuration import AppConfig


class ImportedRustSession(Protocol):
    """CLI-facing view of an imported historical session.

    表示 CLI 使用的已导入历史会话视图.
    """

    @property
    def snapshot(self) -> SessionSnapshot: ...

    @property
    def total_records(self) -> int: ...

    @property
    def invalid_records(self) -> int: ...

    @property
    def unsupported_records(self) -> int: ...

    @property
    def preserved_context_records(self) -> int: ...

    @property
    def recovered_context_records(self) -> int: ...

    @property
    def deduplicated_context_records(self) -> int: ...

    @property
    def invalid_embedded_records(self) -> int: ...

    @property
    def unsupported_embedded_records(self) -> int: ...

    @property
    def imported_messages(self) -> int: ...

    def to_dict(self) -> dict[str, object]: ...


class CliServices(Protocol):
    """Capabilities selected by bootstrap for CLI command handling.

    表示 bootstrap 为 CLI 命令处理选择的能力集合.
    """

    async def open_application(self, settings: ApplicationSettings) -> Any: ...

    def load_config(self, cwd: Path | None) -> AppConfig: ...

    def discover_instructions(self, cwd: Path) -> InstructionDiscoveryResult: ...

    def discover_skills(self, cwd: Path) -> SkillDiscoveryResult: ...

    async def create_session_store(self, config: AppConfig) -> SessionStore: ...

    def create_tool_output_artifact_service(
        self,
        config: AppConfig,
        store: SessionStore,
    ) -> SessionToolOutputArtifactApplicationService: ...

    async def load_rust_session(self, source: Path) -> ImportedRustSession: ...

    async def run_acp(
        self,
        args: argparse.Namespace,
        settings: ApplicationSettings,
    ) -> int: ...

    async def run_tui(
        self,
        args: argparse.Namespace,
        settings: ApplicationSettings,
    ) -> int: ...


__all__ = ["CliServices", "ImportedRustSession"]
