"""Instruction and skill discovery composition ownership.

指令与技能发现的组合根 ownership.

The discovery adapters are selected once by the composition root and reused by
per-binding trackers.  This module exposes the small inspection and
rediscovery surface without owning any filesystem discovery implementation.
"""

from __future__ import annotations

from pathlib import Path

from neuro_code.application.ports.instructions import InstructionDiscovery
from neuro_code.application.ports.skills import SkillDiscovery
from neuro_code.bootstrap.composition_contracts import CompositionRootMixin
from neuro_code.bootstrap.factories import (
    _default_instruction_discovery_factory,
    _default_skill_discovery_factory,
)
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult


class CompositionDiscoveryMixin(CompositionRootMixin):
    """Expose composition-selected instruction and skill discovery adapters."""

    @property
    def instruction_result(self: CompositionRootMixin) -> InstructionDiscoveryResult | None:
        """Return a fresh instruction discovery result for the application workspace.

        This uses the same ``InstructionDiscovery`` adapter that per-binding
        trackers use, but with a fresh target of the application CWD.  CLI
        ``inspect`` uses this to render what would be discovered at the
        workspace root level.

        返回一个新的指令发现结果用于该应用工作区.
        """

        return self._instruction_discovery.discover(self.config.cwd, target=self.config.cwd)

    @property
    def skill_result(self: CompositionRootMixin) -> SkillDiscoveryResult | None:
        """Return a fresh skill discovery result for the application workspace.

        This uses the same ``SkillDiscovery`` adapter that per-binding trackers
        use.  CLI ``inspect`` uses this to render what would be discovered at
        the workspace root level.

        返回一个新的技能发现结果用于该应用工作区.
        """

        return self._skill_discovery.discover(self.config.cwd)

    @staticmethod
    def default_instruction_discovery() -> InstructionDiscovery:
        """Return the default instruction discovery adapter.

        This is the same factory default that ``ApplicationComposition.open()``
        uses when no explicit ``instruction_discovery_factory`` is provided.
        CLI ``inspect`` calls this so that it uses the same discovery
        implementation and port contract as a full application session,
        without the overhead of opening a store or background task scope.

        返回默认的指令发现适配器. 该工厂与完整 ApplicationComposition 使用的默认实现和端口契约保持一致.
        """

        return _default_instruction_discovery_factory()

    @staticmethod
    def default_skill_discovery() -> SkillDiscovery:
        """Return the default skill discovery adapter.

        This is the same factory default that ``ApplicationComposition.open()``
        uses when no explicit ``skill_discovery_factory`` is provided.
        CLI ``inspect`` calls this so that it uses the same discovery
        implementation and port contract as a full application session.

        返回默认的技能发现适配器,与完整应用会话使用的默认实现保持一致.
        """

        return _default_skill_discovery_factory()

    def rediscover_instructions(
        self: CompositionRootMixin,
        cwd: Path | None = None,
    ) -> InstructionDiscoveryResult:
        """Re-run instruction discovery, detecting changes since the last pass.

        重新运行指令发现,并检测上一次发现之后的变化.
        """

        workspace = cwd or self.config.cwd
        return self._instruction_discovery.discover(workspace, target=workspace)

    def rediscover_skills(
        self: CompositionRootMixin,
        cwd: Path | None = None,
    ) -> SkillDiscoveryResult:
        """Re-run skill discovery, detecting changes since the last pass.

        重新运行技能发现,并检测上一次发现之后的变化.
        """

        workspace = cwd or self.config.cwd
        return self._skill_discovery.discover(workspace, target=workspace)


__all__ = ["CompositionDiscoveryMixin"]
