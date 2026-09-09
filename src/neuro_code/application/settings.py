"""Interface-neutral settings for composing one Neuro Code process.

提供组合一个 Neuro Code 进程所需的接口无关设置."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from neuro_code.application.execution_policy import ExecutionBudgetPolicy, ExecutionProfile
from neuro_code.application.permissions.policy import PermissionMode, PermissionRule
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.runtime.verification import validate_explicit_verification_command
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import ExecutionBudget
from neuro_code.shared.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """Interface-neutral settings for composing one Neuro Code process.

    提供组合一个 Neuro Code 进程所需的接口无关设置."""

    cwd: Path | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    sandbox: str | None = None
    failover: bool = True
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    permission_rules: tuple[PermissionRule, ...] = ()
    permission_rules_path: Path | None = None
    max_steps: int | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    execution_control_mode: ExecutionControlMode = ExecutionControlMode.FINALIZE_TERMINAL
    resume_id: str | None = None
    execution_profile: ExecutionProfile = ExecutionProfile.NORMAL
    verification_command: str | None = None
    _execution_budget: ExecutionBudget = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.execution_profile, ExecutionProfile):
            raise TypeError("execution_profile must be an ExecutionProfile")
        try:
            verification_command = validate_explicit_verification_command(self.verification_command)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"invalid verification command: {error}") from error
        object.__setattr__(self, "verification_command", verification_command)
        budget = ExecutionBudgetPolicy.resolve(
            self.execution_profile,
            max_steps=self.max_steps,
        )
        object.__setattr__(self, "max_steps", budget.max_model_calls)
        object.__setattr__(self, "_execution_budget", budget)

    @property
    def execution_budget(self) -> ExecutionBudget:
        """Return the single resolved ordinary Agent execution budget.

        返回唯一解析后的普通 Agent 执行预算。
        """

        return self._execution_budget
