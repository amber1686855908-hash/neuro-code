"""Bounded, provider-neutral structured plans for one durable session.

定义一个持久化会话使用的有界且与 Provider 无关的结构化计划."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

MAX_PLAN_COMMENT_BYTES = 2_000
MAX_PLAN_COMMENT_ID_BYTES = 80
MAX_PLAN_COMMENTS = 48
MAX_PLAN_EXPLANATION_BYTES = 2_000
MAX_PLAN_STEP_BYTES = 600
MAX_PLAN_STEPS = 12


class PlanStepStatus(StrEnum):
    """Visible lifecycle states for one user-facing plan step.

    定义一个面向用户的计划步骤可见生命周期状态."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


def _bounded_text(value: object, *, field_name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"plan {field_name} must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"plan {field_name} must not be empty")
    if "\x00" in normalized or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"plan {field_name} contains control characters")
    if len(normalized.encode("utf-8")) > limit:
        raise ValueError(f"plan {field_name} is too large")
    return normalized


@dataclass(frozen=True, slots=True)
class PlanStep:
    step: str
    status: PlanStepStatus = PlanStepStatus.PENDING

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "step",
            _bounded_text(self.step, field_name="step", limit=MAX_PLAN_STEP_BYTES),
        )
        if not isinstance(self.status, PlanStepStatus):
            raise ValueError("plan step status must be canonical")

    def to_dict(self) -> dict[str, str]:
        return {"step": self.step, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class PlanComment:
    """Bounded user feedback anchored to one visible plan step.

    表示绑定到一个可见计划步骤的有界用户反馈."""

    comment_id: str
    step_index: int
    content: str
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            not self.comment_id
            or "\x00" in self.comment_id
            or len(self.comment_id.encode("utf-8")) > MAX_PLAN_COMMENT_ID_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in self.comment_id)
        ):
            raise ValueError("plan comment id is invalid")
        if not isinstance(self.step_index, int) or isinstance(self.step_index, bool):
            raise ValueError("plan comment step index must be an integer")
        if not 1 <= self.step_index <= MAX_PLAN_STEPS:
            raise ValueError(f"plan comment step index must be between 1 and {MAX_PLAN_STEPS}")
        object.__setattr__(
            self,
            "content",
            _bounded_text(self.content, field_name="comment", limit=MAX_PLAN_COMMENT_BYTES),
        )
        if self.created_at.tzinfo is None:
            raise ValueError("plan comment time must be timezone-aware")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "comment_id": self.comment_id,
            "step_index": self.step_index,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """The bounded current plan associated with one session.

    表示一个会话关联的有界当前计划."""

    steps: tuple[PlanStep, ...]
    explanation: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        if not self.steps:
            raise ValueError("plan must contain at least one step")
        if len(self.steps) > MAX_PLAN_STEPS:
            raise ValueError(f"plan must contain at most {MAX_PLAN_STEPS} steps")
        if not all(isinstance(step, PlanStep) for step in self.steps):
            raise ValueError("plan steps must be canonical")
        if self.explanation is not None:
            object.__setattr__(
                self,
                "explanation",
                _bounded_text(
                    self.explanation,
                    field_name="explanation",
                    limit=MAX_PLAN_EXPLANATION_BYTES,
                ),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "explanation": self.explanation,
            "plan": [step.to_dict() for step in self.steps],
        }

    @property
    def fingerprint(self) -> str:
        """Return the stable identity used to attach user feedback to this revision.

        返回用于将用户反馈附加到此版本的稳定身份."""

        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> SessionPlan:
        if not isinstance(value, Mapping):
            raise ValueError("plan payload must be an object")
        if set(value) != {"explanation", "plan"}:
            raise ValueError("plan payload has unsupported fields")
        raw_explanation = value.get("explanation")
        if raw_explanation is not None and not isinstance(raw_explanation, str):
            raise ValueError("plan explanation must be a string or null")
        raw_steps = value.get("plan")
        if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str | bytes):
            raise ValueError("plan steps must be an array")
        steps: list[PlanStep] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping) or set(raw_step) != {"step", "status"}:
                raise ValueError("plan step has unsupported fields")
            raw_status = raw_step.get("status")
            if not isinstance(raw_status, str):
                raise ValueError("plan step status is invalid")
            try:
                status = PlanStepStatus(raw_status)
            except ValueError:
                raise ValueError("plan step status is invalid") from None
            steps.append(
                PlanStep(
                    _bounded_text(
                        raw_step.get("step"), field_name="step", limit=MAX_PLAN_STEP_BYTES
                    ),
                    status,
                )
            )
        return cls(tuple(steps), raw_explanation)

    def model_guidance(self) -> str:
        """Render stable control text without adding a provider-specific protocol.

        渲染稳定的控制文本,不增加 Provider 特定协议."""

        lines = ["Current structured plan:"]
        if self.explanation is not None:
            lines.append(f"Purpose: {self.explanation}")
        for index, step in enumerate(self.steps, start=1):
            lines.append(f"{index}. [{step.status.value}] {step.step}")
        lines.append("Keep this plan current with update_plan when work changes state.")
        return "\n".join(lines)

    def comment_guidance(self, comments: Sequence[PlanComment]) -> str:
        """Render current-revision user feedback for the next provider request.

        为下一次 Provider 请求渲染当前版本的用户反馈."""

        normalized = tuple(comments)
        if not normalized:
            return ""
        if not all(isinstance(comment, PlanComment) for comment in normalized):
            raise ValueError("plan comments must be canonical")
        if any(comment.step_index > len(self.steps) for comment in normalized):
            raise ValueError("plan comment refers to an unknown step")
        lines = ["User comments on the current structured plan:"]
        lines.extend(f"Step {comment.step_index}: {comment.content}" for comment in normalized)
        lines.append("Address this feedback by replacing the plan with update_plan when needed.")
        return "\n".join(lines)


def plan_from_update_arguments(arguments: Mapping[str, Any]) -> SessionPlan:
    """Validate the public update-plan schema independently of a UI/provider.

    独立于 UI 和 Provider 验证公开的 update-plan 数据结构."""

    return SessionPlan.from_dict(arguments)


__all__ = [
    "MAX_PLAN_COMMENTS",
    "MAX_PLAN_COMMENT_BYTES",
    "MAX_PLAN_COMMENT_ID_BYTES",
    "MAX_PLAN_EXPLANATION_BYTES",
    "MAX_PLAN_STEPS",
    "MAX_PLAN_STEP_BYTES",
    "PlanComment",
    "PlanStep",
    "PlanStepStatus",
    "SessionPlan",
    "plan_from_update_arguments",
]
