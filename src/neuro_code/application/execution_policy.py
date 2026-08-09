"""Application-owned policy for resolving one ordinary Agent execution budget.

The domain keeps :class:`ExecutionBudget` as the single budget value.  This
module only owns named product profiles and the compatibility mapping from the
legacy ``max_steps`` option into every count-based ordinary execution limit.
Finalizer attempts remain an independent runtime setting.

定义应用层的普通 Agent 执行预算解析策略。领域层继续以 ``ExecutionBudget`` 作为唯一预算值;
本模块只负责具名产品档位以及旧 ``max_steps`` 选项到全部计数型普通执行上限的兼容映射。
Finalizer 尝试仍由独立运行时设置管理。
"""

from __future__ import annotations

from enum import StrEnum

from neuro_code.domain.execution import ExecutionBudget, ToolCallBudget


class ExecutionProfile(StrEnum):
    """Named, bounded product profiles for one ordinary Agent turn.

    定义一次普通 Agent 回合使用的具名且有界的产品档位。
    """

    NORMAL = "normal"
    DEEP = "deep"


def _budget_for_model_calls(max_model_calls: int) -> ExecutionBudget:
    if (
        not isinstance(max_model_calls, int)
        or isinstance(max_model_calls, bool)
        or max_model_calls < 1
    ):
        raise ValueError("max_steps must be a positive integer")

    stricter_side_effect_limit = max(1, max_model_calls // 3)
    state_transition_limit = max(1, max_model_calls // 2)
    return ExecutionBudget(
        max_model_calls=max_model_calls,
        max_tool_rounds=max_model_calls,
        max_tool_calls=max_model_calls * 4,
        max_calls_per_tool=max_model_calls,
        max_wall_seconds=None,
        max_input_tokens=None,
        max_output_tokens=None,
        max_total_tokens=None,
        per_tool_limits=(
            ToolCallBudget("bash", stricter_side_effect_limit),
            ToolCallBudget("kill_task", state_transition_limit),
            ToolCallBudget("search_replace", stricter_side_effect_limit),
            ToolCallBudget("terminal_exec", stricter_side_effect_limit),
            ToolCallBudget("terminal_kill", state_transition_limit),
            ToolCallBudget("terminal_start", stricter_side_effect_limit),
            ToolCallBudget("update_plan", state_transition_limit),
        ),
    )


NORMAL_EXECUTION_BUDGET = _budget_for_model_calls(48)
DEEP_EXECUTION_BUDGET = _budget_for_model_calls(96)


class ExecutionBudgetPolicy:
    """Resolve product profiles and legacy step overrides to ``ExecutionBudget``.

    将产品档位和旧步骤覆盖值解析为 ``ExecutionBudget``。
    """

    @staticmethod
    def for_profile(profile: ExecutionProfile) -> ExecutionBudget:
        if not isinstance(profile, ExecutionProfile):
            raise TypeError("execution profile must be an ExecutionProfile")
        if profile is ExecutionProfile.DEEP:
            return DEEP_EXECUTION_BUDGET
        return NORMAL_EXECUTION_BUDGET

    @staticmethod
    def from_max_steps(max_steps: int) -> ExecutionBudget:
        """Map the legacy step option to the complete ordinary budget.

        将旧步骤选项映射为完整的普通执行预算。
        """

        return _budget_for_model_calls(max_steps)

    @classmethod
    def resolve(
        cls,
        profile: ExecutionProfile,
        *,
        max_steps: int | None,
    ) -> ExecutionBudget:
        if max_steps is not None:
            return cls.from_max_steps(max_steps)
        return cls.for_profile(profile)


__all__ = [
    "DEEP_EXECUTION_BUDGET",
    "NORMAL_EXECUTION_BUDGET",
    "ExecutionBudgetPolicy",
    "ExecutionProfile",
]
