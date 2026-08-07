"""Session-scoped tracker for SKILL.md skill discovery.

The tracker maintains a moving ``_target`` path, mirroring the
:class:`InstructionTracker` design.  When file-access tools (read_file,
list_dir, grep) touch a path, ``check_path()`` updates the target so that
SKILL.md files from the accessed directory **upward** to the workspace root
are discovered in the next ``current_result()`` call.

Sibling subtrees are isolated: if the model switches from ``src/foo/``
to ``src/bar/``, the target moves and skills from ``src/foo/`` are no longer
included. Only the path from the *current* focus directory up to the
workspace root contributes skills.

The tracker is NOT a cache.  ``current_result()`` re-runs discovery on each
call, so skill content changes take effect on the next model step without
needing a session restart. No cross-call deduplication cache is maintained
-- the upward walk is bounded by directory depth, which is typically small,
and re-discovery ensures file changes are always picked up.

Single-target limitation: the tracker maintains a single ``_target``
path.  When multiple tools access different sibling subtrees in the same
model step, only the last path is retained (same tradeoff as
InstructionTracker).

提供会话范围的 SKILL.md 技能发现跟踪器. 目标变化时按当前路径向上重新发现技能,不使用跨调用缓存.
"""

from __future__ import annotations

from pathlib import Path

from neuro_code.application.ports.skills import SkillDiscovery
from neuro_code.domain.workspace.skills import SkillDiscoveryResult


class SkillTracker:
    """Tracks the current discovery target for SKILL.md skill discovery.

    The target starts at the workspace root (or CWD) and is updated when
    tools access files.  Discovery walks **upward** from the target to the
    workspace root (inclusive), finding skills at any depth in the workspace.
    Discovery is re-run on each ``current_result()`` call, so skill content
    is always fresh.

    跟踪 SKILL.md 技能发现的当前目标,并在文件工具访问路径时更新它.
    """

    def __init__(
        self,
        discovery: SkillDiscovery,
        workspace_root: Path,
        initial_target: Path | None = None,
    ) -> None:
        self._discovery = discovery
        try:
            self._workspace_root = workspace_root.resolve(strict=False)
        except (OSError, RuntimeError):
            self._workspace_root = workspace_root.absolute()
        try:
            self._target = (initial_target or workspace_root).resolve(strict=False)
        except (OSError, RuntimeError):
            self._target = self._workspace_root
        # Ensure target is within workspace; fall back to root otherwise.
        try:
            self._target.relative_to(self._workspace_root)
        except ValueError:
            self._target = self._workspace_root

    def check_path(self, target_path: Path) -> None:
        """Update the discovery target based on a tool's file access.

        The target moves to the directory containing *target_path* (if it's
        a file) or to *target_path* itself (if it's a directory).  Paths
        outside the workspace are silently ignored.  The target always stays
        within the workspace, ensuring subtree isolation.

        根据工具访问的文件路径更新发现目标.
        """
        try:
            resolved = target_path.resolve(strict=False)
        except (OSError, RuntimeError):
            return

        try:
            target_dir = resolved if resolved.is_dir() else resolved.parent
        except OSError:
            target_dir = resolved.parent

        try:
            target_dir.relative_to(self._workspace_root)
        except ValueError:
            return  # Outside workspace; ignore for subtree isolation.

        self._target = target_dir

    def current_result(self) -> SkillDiscoveryResult:
        """Return the current skill discovery result.

        Discovery walks upward from the tracked target to the workspace
        root (inclusive), finding skills at any depth in the workspace.

        返回当前技能发现结果.
        """
        return self._discovery.discover(self._workspace_root, target=self._target)

    @property
    def target(self) -> Path:
        """The current discovery target directory.

        返回当前技能发现目标目录."""
        return self._target

    @property
    def workspace_root(self) -> Path:
        """The workspace root that bounds all discovery.

        返回限制所有发现范围的工作区根目录."""
        return self._workspace_root


__all__ = ["SkillTracker"]
