"""Per-turn context builder collaborator.

Stage 3C of the Runtime Kernel split: this module owns the request-scoped
policy guidance, repository instruction refresh, and skill listing injection
previously embedded in ``AgentRuntime``.  It also owns the mutable
``reasoning_effort``, ``interaction_mode``, ``plan``, and ``plan_comments``
state so the runtime loop and external setters share one source of truth.

The module intentionally does not import :mod:`agent`; it depends only on
domain values and callable providers.

提供每个回合使用的上下文构建协作者,负责策略指引、指令刷新和技能注入.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from neuro_code.domain.conversation.interaction_mode import (
    InteractionMode,
    interaction_mode_guidance,
)
from neuro_code.domain.conversation.messages import Message, Role, SessionItem, SyntheticReason
from neuro_code.domain.conversation.reasoning import ReasoningEffort, reasoning_guidance
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult


class ContextBuilder:
    """Build each model request's guided, injected context.

    Guidance (reasoning effort, interaction mode, plan state) is applied to
    the system message without persisting control text.  Repository
    ``AGENTS.md`` instructions and available skills are refreshed before each
    model step and injected as synthetic ``User`` messages; the latest
    discovery results remain observable via ``instruction_result`` and
    ``skill_result``.

    为每个模型请求构建带指引和注入内容的上下文. 指令和技能会在每个模型步骤刷新并作为合成 User 消息注入.
    """

    __slots__ = (
        "_instruction_provider",
        "_interaction_mode",
        "_last_instruction_result",
        "_last_skill_result",
        "_plan",
        "_plan_comments",
        "_reasoning_effort",
        "_skill_provider",
    )

    def __init__(
        self,
        *,
        reasoning_effort: ReasoningEffort,
        interaction_mode: InteractionMode,
        plan: SessionPlan | None,
        instruction_provider: Callable[[], InstructionDiscoveryResult | None] | None,
        skill_provider: Callable[[], SkillDiscoveryResult | None] | None,
    ) -> None:
        self._reasoning_effort = reasoning_effort
        self._interaction_mode = interaction_mode
        self._plan = plan
        self._plan_comments: tuple[PlanComment, ...] = ()
        self._instruction_provider = instruction_provider
        self._skill_provider = skill_provider
        self._last_instruction_result: InstructionDiscoveryResult | None = None
        self._last_skill_result: SkillDiscoveryResult | None = None

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        if not isinstance(effort, ReasoningEffort):
            raise TypeError("reasoning effort must be a ReasoningEffort")
        self._reasoning_effort = effort

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._interaction_mode

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        if not isinstance(mode, InteractionMode):
            raise TypeError("interaction mode must be an InteractionMode")
        self._interaction_mode = mode

    @property
    def plan(self) -> SessionPlan | None:
        return self._plan

    def set_plan(self, plan: SessionPlan | None) -> None:
        if plan is not None and not isinstance(plan, SessionPlan):
            raise TypeError("plan must be a SessionPlan or None")
        self._plan = plan
        self._plan_comments = ()

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]:
        return self._plan_comments

    def set_plan_comments(self, comments: Sequence[PlanComment]) -> None:
        normalized = tuple(comments)
        if not all(isinstance(comment, PlanComment) for comment in normalized):
            raise TypeError("plan comments must be PlanComment values")
        if normalized and self._plan is None:
            raise ValueError("plan comments require a saved plan")
        if self._plan is not None and any(
            comment.step_index > len(self._plan.steps) for comment in normalized
        ):
            raise ValueError("plan comments must refer to saved steps")
        self._plan_comments = normalized

    @property
    def instruction_result(self) -> InstructionDiscoveryResult | None:
        """Return the most recent instruction discovery result, if any.

        返回最近一次指令发现结果,如果存在."""
        return self._last_instruction_result

    @property
    def skill_result(self) -> SkillDiscoveryResult | None:
        """Return the most recent skill discovery result, if any.

        返回最近一次技能发现结果,如果存在."""
        return self._last_skill_result

    def build(self, items: Sequence[SessionItem]) -> tuple[SessionItem, ...]:
        """Apply the selected policy to a request without persisting control text.

        Reasoning effort and interaction mode guidance are appended to the
        system message.  Repository AGENTS.md instructions are injected as a
        separate synthetic ``User`` message tagged with
        ``SyntheticReason.PROJECT_INSTRUCTIONS``, placed after the system
        message and before the first genuine user message.  This follows the
        Rust baseline's ``ProjectInstructions`` synthetic user item pattern:
        the instruction content never masquerades as a system or genuine user
        message.

        将选定策略应用到请求,但不持久化控制文本. 仓库指令作为标记为合成原因的独立 User 消息注入.
        """

        guidance_parts = [
            reasoning_guidance(self._reasoning_effort),
            interaction_mode_guidance(self._interaction_mode),
        ]
        if self._plan is not None:
            guidance_parts.append(self._plan.model_guidance())
            comments = self._plan.comment_guidance(self._plan_comments)
            if comments:
                guidance_parts.append(comments)
        guidance = "\n\n".join(guidance_parts)
        rendered = list(items)

        # Apply guidance to the system message (or create one if missing).
        system_index: int | None = None
        for index, item in enumerate(rendered):
            if isinstance(item, Message) and item.role is Role.SYSTEM:
                system_index = index
                break
        if system_index is not None:
            original = rendered[system_index]
            assert isinstance(original, Message)
            guided = Message(Role.SYSTEM, f"{original.model_content()}\n\n{guidance}")
            rendered[system_index] = guided
        else:
            rendered.insert(0, Message(Role.SYSTEM, guidance))
            system_index = 0

        # Refresh and inject repository instructions as a synthetic User message.
        instruction_result = self._refresh_instructions()
        if instruction_result is not None and instruction_result.files:
            instruction_msg = instruction_result.instruction_message()
            # Insert after the system message.
            rendered.insert(system_index + 1, instruction_msg)

        # Refresh and inject available skills as a synthetic User message.
        # Inserted after the instruction message (or after the system message
        # if no instructions were found) so the model sees skills after
        # project conventions.
        skill_result = self._refresh_skills()
        if skill_result is not None and skill_result.files:
            skill_msg = skill_result.skill_message()
            # Find the insertion point: after the instruction message if
            # present, otherwise after the system message.
            insert_at = system_index + 1
            for i in range(system_index + 1, min(len(rendered), system_index + 3)):
                item = rendered[i]
                if (
                    isinstance(item, Message)
                    and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
                ):
                    insert_at = i + 1
                    break
            rendered.insert(insert_at, skill_msg)

        return tuple(rendered)

    def _refresh_instructions(self) -> InstructionDiscoveryResult | None:
        """Call the instruction provider to get fresh discovered instructions.

        This is called before each model step so that instruction file changes
        within the same session are picked up on the next turn.

        调用指令 Provider 获取最新发现的指令. 每个模型步骤都会重新执行.
        """
        if self._instruction_provider is None:
            self._last_instruction_result = None
            return None
        self._last_instruction_result = self._instruction_provider()
        return self._last_instruction_result

    def _refresh_skills(self) -> SkillDiscoveryResult | None:
        """Call the skill provider to get fresh discovered skills.

        This is called before each model step so that skill file changes
        within the same session are picked up on the next turn.

        调用技能 Provider 获取最新发现的技能. 每个模型步骤都会重新执行.
        """
        if self._skill_provider is None:
            self._last_skill_result = None
            return None
        self._last_skill_result = self._skill_provider()
        return self._last_skill_result


__all__ = ["ContextBuilder"]
