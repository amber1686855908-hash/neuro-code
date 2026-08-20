"""Tool for model-managed, durable structured session plans.

提供由模型管理的持久化结构化会话计划工具."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.plans import PlanStep, SessionPlan, plan_from_update_arguments
from neuro_code.domain.tools import ToolDefinition, ToolExecutionMode, ToolResult
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text


class UpdatePlanTool:
    """Validate a complete plan replacement for AgentRuntime to persist.

    验证供 AgentRuntime 持久化的完整计划替换内容."""

    definition = ToolDefinition(
        name="update_plan",
        execution_mode=ToolExecutionMode.EXCLUSIVE,
        description=(
            "Replace the current session plan with a concise, structured set of steps. "
            "Use this for multi-step work, keeping exactly one step in_progress when "
            "practical. This changes session planning metadata only; it never changes "
            "workspace files or runs commands."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "explanation": {
                    "type": ["string", "null"],
                    "description": "Optional short purpose or decision summary for the plan.",
                },
                "plan": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["step", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["explanation", "plan"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        try:
            plan = plan_from_update_arguments(arguments)
        except ValueError as error:
            raise ToolError(str(error)) from None
        plan = _redact_plan(plan, explicit_values=context.redaction_values)
        return ToolResult(_render_plan(plan), metadata={"plan": plan.to_dict()})


def _render_plan(plan: SessionPlan) -> str:
    lines = ["Plan updated."]
    if plan.explanation is not None:
        lines.append(f"Purpose: {plan.explanation}")
    lines.extend(
        f"{index}. [{step.status.value}] {step.step}"
        for index, step in enumerate(plan.steps, start=1)
    )
    return "\n".join(lines)


def _redact_plan(plan: SessionPlan, *, explicit_values: tuple[str, ...]) -> SessionPlan:
    return SessionPlan(
        tuple(
            PlanStep(
                redact_sensitive_text(step.step, explicit_values=explicit_values),
                step.status,
            )
            for step in plan.steps
        ),
        (
            redact_sensitive_text(plan.explanation, explicit_values=explicit_values)
            if plan.explanation is not None
            else None
        ),
    )


__all__ = ["UpdatePlanTool"]
