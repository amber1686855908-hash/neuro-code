"""Application policy for producing normal-agent verification requirements.

This module owns the small amount of policy needed to make a newly accepted
normal user turn observable to the verification runtime.  It deliberately does
not infer task-specific acceptance criteria, inspect the workspace, discover
tests, or classify tool commands.

提供普通 Agent 回合验证要求的应用层策略。

本模块只负责让新接受的普通用户回合对验证运行时可见,不推断任务验收标准、检查工作区、
发现测试或分类工具命令。
"""

from __future__ import annotations

from neuro_code.domain.execution import (
    RequirementActivation,
    RequirementProvenance,
    RequirementSource,
    RequirementStrength,
    VerificationRequirement,
    VerificationRequirementsSnapshot,
)

# Keep this wording stable: it participates in the domain-owned requirement ID.
NORMAL_MUTATION_REQUIREMENT_CRITERION = (
    "After a workspace mutation, a recognized verification command must produce a current result."
)

DEFAULT_NORMAL_MUTATION_REQUIREMENT = VerificationRequirement.create(
    criterion=NORMAL_MUTATION_REQUIREMENT_CRITERION,
    strength=RequirementStrength.REQUIRED,
    activation=RequirementActivation.ON_WORKSPACE_MUTATION,
    provenance=(RequirementProvenance(RequirementSource.WORKSPACE_MUTATION),),
)
DEFAULT_NORMAL_MUTATION_REQUIREMENT_ID = DEFAULT_NORMAL_MUTATION_REQUIREMENT.requirement_id
DEFAULT_NORMAL_REQUIREMENTS = VerificationRequirementsSnapshot.from_requirements(
    (DEFAULT_NORMAL_MUTATION_REQUIREMENT,)
)


class NormalTurnRequirementsPolicy:
    """Resolve the effective requirements for one newly accepted normal turn.

    ``None`` means that the caller did not supply a structured declaration, so
    the normal turn receives the single deterministic mutation requirement.
    Explicit snapshots, including an empty structured snapshot, are already
    caller-owned decisions and are returned without hidden strengthening.
    """

    __slots__ = ()

    @staticmethod
    def resolve(
        requirements: VerificationRequirementsSnapshot | None,
    ) -> VerificationRequirementsSnapshot:
        if requirements is None:
            return DEFAULT_NORMAL_REQUIREMENTS
        if not isinstance(requirements, VerificationRequirementsSnapshot):
            raise TypeError(
                "verification requirements must be a VerificationRequirementsSnapshot or None"
            )
        return requirements


__all__ = [
    "DEFAULT_NORMAL_MUTATION_REQUIREMENT",
    "DEFAULT_NORMAL_MUTATION_REQUIREMENT_ID",
    "DEFAULT_NORMAL_REQUIREMENTS",
    "NORMAL_MUTATION_REQUIREMENT_CRITERION",
    "NormalTurnRequirementsPolicy",
]
