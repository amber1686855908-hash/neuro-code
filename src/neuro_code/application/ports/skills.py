"""Canonical port for workspace skill file discovery.

定义工作区技能文件发现的规范端口."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from neuro_code.domain.workspace.skills import SkillDiscoveryResult


class SkillDiscovery(Protocol):
    """Discover SKILL.md skill files within a workspace boundary.

    Implementations must be deterministic, bounded, and fail-closed.  They
    never read from the network and never execute discovered files. They may
    read the bounded file to derive metadata and a content fingerprint, but
    only a compact listing (name + description + when-to-use) enters model
    context. A selected body is loaded separately through the read-only skill
    tool.

    在工作区边界内发现 SKILL.md 技能文件. 发现过程确定性、有界且失败关闭,模型上下文只接收精简元数据列表.
    """

    def discover(
        self,
        workspace_root: Path,
        target: Path | None = None,
    ) -> SkillDiscoveryResult:
        """Discover skill files in *workspace_root*.

        Scans configuration directories (``.neuro``, ``.agents``, etc.) for
        ``skills/`` subdirectories, then recursively walks each ``skills/``
        tree (up to ``MAX_SKILL_WALK_DEPTH``) looking for ``SKILL.md``
        files.

        When *target* is supplied, the adapter walks **upward** from
        *target* to *workspace_root* (inclusive), checking each ancestor
        directory for config dirs.  This discovers skills at any depth in
        the workspace, not just at the workspace root.  When *target* is
        ``None`` or equals *workspace_root*, the walk degenerates to
        scanning just the root level.  Deeper skills (closer to *target*)
        are collected first and win name-collision deduplication over
        shallower skills.

        在 *workspace_root* 中发现技能文件,并按作用域优先级去重.
        """
        ...


class SkillContextTracker(Protocol):
    """Tool-facing skill tracker contract owned by one binding.

    定义由单个绑定拥有的、面向工具的技能跟踪器契约."""

    def check_path(self, target_path: Path) -> None: ...

    def current_result(self) -> SkillDiscoveryResult: ...

    @property
    def workspace_root(self) -> Path: ...


__all__ = ["SkillContextTracker", "SkillDiscovery"]
