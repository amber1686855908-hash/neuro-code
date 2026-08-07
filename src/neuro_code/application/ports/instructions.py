"""Canonical port for workspace instruction file discovery.

定义工作区指令文件发现的规范端口."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult


class InstructionDiscovery(Protocol):
    """Discover AGENTS.md instruction files within a workspace boundary.

    Implementations must be deterministic, bounded, and fail-closed.  They
    never read from the network, never execute discovered files, and never
    follow references to plugins or scripts within them.

    在工作区边界内发现 AGENTS.md 指令文件. 实现必须确定性、有界且失败关闭,不读取网络、不执行发现的文件.
    """

    def discover(
        self,
        workspace_root: Path,
        target: Path | None = None,
    ) -> InstructionDiscoveryResult:
        """Discover instruction files from workspace root toward *target*.

        When *target* is ``None`` or equals *workspace_root*, only the
        workspace-root instruction file is considered.  Otherwise, files are
        discovered at every directory level from the root down to (and
        including) the target directory.

        The result is ordered root-first so that deeper instructions can
        refine or override shallower ones with a stable, testable order.

        从工作区根目录向 *target* 发现指令文件,并按根目录优先的顺序返回.
        """
        ...


class InstructionContextTracker(Protocol):
    """Tool-facing instruction tracker contract owned by one binding.

    定义由单个绑定拥有的、面向工具的指令跟踪器契约."""

    def check_path(self, target_path: Path) -> None: ...

    def check_path_for_write(
        self,
        target_path: Path,
    ) -> InstructionDiscoveryResult | None: ...


__all__ = ["InstructionContextTracker", "InstructionDiscovery"]
