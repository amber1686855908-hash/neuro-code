"""Session-scoped tracker for AGENTS.md instruction discovery.

The tracker is an application binding concern: it remembers which workspace
subtree is currently in focus and which instruction result was injected into
the latest model context. Discovery itself remains behind the
``InstructionDiscovery`` port.

提供会话范围的 AGENTS.md 指令发现跟踪器. 每个模型步骤重新发现指令,并保持当前工作区子树焦点.
"""

from __future__ import annotations

from pathlib import Path

from neuro_code.application.ports.instructions import InstructionDiscovery
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult


class InstructionTracker:
    """Track the current discovery target for one conversation binding.

    Discovery is re-run for every model step, so instruction content changes
    take effect without a session restart. A single target keeps sibling
    subtrees isolated; the last accessed path becomes the next focus.

    跟踪一个会话绑定当前的发现目标.
    """

    def __init__(
        self,
        discovery: InstructionDiscovery,
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
        try:
            self._target.relative_to(self._workspace_root)
        except ValueError:
            self._target = self._workspace_root
        self._last_context_result: InstructionDiscoveryResult | None = None

    def check_path(self, target_path: Path) -> None:
        """Move the focus to a workspace-contained file or directory.

        将焦点移动到工作区内的文件或目录."""
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
            return

        self._target = target_dir

    def check_path_for_write(self, target_path: Path) -> InstructionDiscoveryResult | None:
        """Return newly visible instructions before a write may proceed.

        在写入继续之前返回新变得可见的指令."""
        try:
            write_target = target_path if target_path.is_dir() else target_path.parent
        except OSError:
            write_target = target_path.parent
        self.check_path(write_target)

        new_result = self.current_result()
        seen_content = {
            instruction.relative_path: instruction.content
            for instruction in (
                self._last_context_result.files if self._last_context_result is not None else ()
            )
        }
        if any(
            seen_content.get(instruction.relative_path) != instruction.content
            for instruction in new_result.files
        ):
            return new_result
        return None

    def model_context_result(self) -> InstructionDiscoveryResult:
        """Discover and remember exactly what the next model step receives.

        发现并记录下一个模型步骤实际接收的内容."""
        result = self.current_result()
        self._last_context_result = result
        return result

    def current_result(self) -> InstructionDiscoveryResult:
        """Return fresh instructions from workspace root to the focus.

        返回从工作区根目录到当前焦点的最新指令."""
        return self._discovery.discover(self._workspace_root, target=self._target)

    @property
    def target(self) -> Path:
        """The current discovery target directory.

        返回当前指令发现目标目录."""
        return self._target

    @property
    def workspace_root(self) -> Path:
        """The workspace root that bounds discovery.

        返回限制发现范围的工作区根目录."""
        return self._workspace_root


__all__ = ["InstructionTracker"]
