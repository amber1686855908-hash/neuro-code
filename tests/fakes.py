"""Explicit test doubles for application ports.

提供应用端口使用的显式测试替身."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeReport,
)


class FakeWorkspaceIdentity:
    """Explicit workspace identity fake without filesystem dependencies.

    提供不依赖文件系统的显式工作区身份测试替身."""

    def __init__(
        self,
        *,
        matches_result: bool = True,
        error: BaseException | None = None,
    ) -> None:
        self._matches_result = matches_result
        self._error = error
        self.calls: list[tuple[str | Path, str | Path]] = []

    def matches(
        self,
        recorded: str | Path,
        requested: str | Path,
        /,
    ) -> bool:
        self.calls.append((recorded, requested))
        if self._error is not None:
            raise self._error
        return self._matches_result


class FakeWorkspacePathResolver:
    """Explicit path resolver fake without filesystem dependencies.

    提供不依赖文件系统的显式路径解析器测试替身."""

    def __init__(
        self,
        *,
        resolved_path: Path,
        error: BaseException | None = None,
        on_resolve: Callable[[Path, str], None] | None = None,
    ) -> None:
        self._resolved_path = resolved_path
        self._error = error
        self._on_resolve = on_resolve
        self.calls: list[tuple[Path, str]] = []

    def resolve_existing(
        self,
        workspace: Path,
        requested: str,
        /,
    ) -> Path:
        self.calls.append((workspace, requested))
        if self._on_resolve is not None:
            self._on_resolve(workspace, requested)
        if self._error is not None:
            raise self._error
        return self._resolved_path


class EmptyWorkspaceChangeCheckpoint(WorkspaceChangeCheckpoint):
    """Opaque checkpoint used by tests unrelated to filesystem observation.

    提供与文件系统观察无关测试使用的不透明检查点."""

    __slots__ = ()


class EmptyWorkspaceChangeObserver:
    """Explicit observer fake that reports no workspace changes.

    提供明确报告无工作区变更的观察器测试替身."""

    def capture(self, root: Path, /) -> WorkspaceChangeCheckpoint:
        del root
        return EmptyWorkspaceChangeCheckpoint()

    def compare(
        self,
        before: WorkspaceChangeCheckpoint,
        after: WorkspaceChangeCheckpoint,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport:
        del before, after, explicit_redactions
        return WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=False)
