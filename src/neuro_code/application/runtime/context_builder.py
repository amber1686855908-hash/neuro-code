"""Per-turn context builder collaborator.

Stage 3C of the Runtime Kernel split: this module owns the request-scoped
policy guidance, repository instruction refresh, and skill listing injection
previously embedded in ``AgentRuntime``.  It owns the stable request prefix
and the current ``reasoning_effort``, ``interaction_mode``, ``plan``, and
``plan_comments`` values so the runtime loop and external setters share one
source of truth.

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
from neuro_code.domain.execution import (
    ExecutionBudgetPressure,
    ExecutionBudgetUsage,
    ExecutionSegmentCheckpoint,
)
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult

BATCH_FIRST_RUNTIME_GUIDANCE = """Runtime evidence-gathering guidance:
When multiple read-only operations are independent, request them in the same model step instead
of alternating one read with one reasoning round. Prefer list_tree, grep_many, and read_files for
bounded repository-wide evidence gathering. For repository analysis, first map the repository,
then locate relevant symbols, batch-read the related evidence, analyze it together, and only then
perform targeted follow-up. Use glob when a known filename or path pattern must be located.
Review related edits with workspace_diff before verification; keep dependent operations sequential."""

REPLAN_RUNTIME_GUIDANCE = """Runtime supervision guidance:
The current approach is repeating results without sufficient progress. Change strategy. Avoid
repeating the same tool or action with equivalent arguments. Narrow or broaden the search,
inspect different evidence, or revise the current assumption."""


def _budget_runtime_guidance(pressure: ExecutionBudgetPressure) -> str:
    pressure_guidance = {
        ExecutionBudgetPressure.CONSERVE: (
            "Prioritize the core question and avoid open-ended exploration."
        ),
        ExecutionBudgetPressure.FOCUS: (
            "Merge independent searches and reads; perform only necessary follow-up."
        ),
        ExecutionBudgetPressure.FINAL_STAGE: (
            "Stop nonessential exploration, complete only necessary verification, and prepare the answer."
        ),
    }[pressure]
    return f"Runtime budget guidance ({pressure.value}):\n{pressure_guidance}"


def _segment_runtime_guidance(checkpoint: ExecutionSegmentCheckpoint) -> str:
    progress = ", ".join(kind.value for kind in checkpoint.progress_kinds)
    return (
        "Runtime segment checkpoint:\n"
        f"Segment {checkpoint.segment_number} completed after "
        f"{checkpoint.model_calls} model calls, {checkpoint.tool_rounds} tool rounds, and "
        f"{checkpoint.tool_calls} tool calls. Confirmed progress categories: {progress}. "
        "Continue the same user task in the next bounded segment. Reuse recorded evidence, avoid "
        "repeating equivalent actions, and do not claim unrecorded work."
    )


class ContextBuilder:
    """Build each model request's guided, injected context.

    Stable guidance (reasoning effort, interaction mode, batch-first work)
    is applied to the system message without persisting control text.
    Repository
    ``AGENTS.md`` instructions and available skills are refreshed before each
    model step and injected as synthetic ``User`` messages; the latest
    discovery results remain observable via ``instruction_result`` and
    ``skill_result``.

    Dynamic plan and runtime state are rendered by explicit helper methods;
    the loop appends those notices at safe turn boundaries rather than
    rewriting the early request prefix.

    为每个模型请求构建带指引和注入内容的上下文. 指令和技能会在每个模型步骤刷新并作为合成 User 消息注入.
    动态计划和运行时状态由显式辅助方法渲染,并在安全的回合边界追加,不会改写请求前缀.
    """

    __slots__ = (
        "_dag_result_relay_message",
        "_instruction_provider",
        "_interaction_mode",
        "_last_instruction_result",
        "_last_skill_result",
        "_parent_relay_message",
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
        parent_relay_message: Message | None = None,
        dag_result_relay_message: Message | None = None,
    ) -> None:
        self._reasoning_effort = reasoning_effort
        self._interaction_mode = interaction_mode
        self._plan = plan
        self._plan_comments: tuple[PlanComment, ...] = ()
        self._instruction_provider = instruction_provider
        self._skill_provider = skill_provider
        if parent_relay_message is not None and (
            not isinstance(parent_relay_message, Message)
            or parent_relay_message.synthetic_reason is not SyntheticReason.PARENT_RELAY
        ):
            raise TypeError("parent relay message must be canonical synthetic context")
        if dag_result_relay_message is not None and (
            not isinstance(dag_result_relay_message, Message)
            or dag_result_relay_message.synthetic_reason
            is not SyntheticReason.DAG_PREDECESSOR_RESULTS
        ):
            raise TypeError("DAG result relay message must be canonical synthetic context")
        self._parent_relay_message = parent_relay_message
        self._dag_result_relay_message = dag_result_relay_message
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

    def plan_runtime_message(self) -> Message | None:
        """Render the current plan as one append-only runtime notice.

        The caller owns placement and must keep this message out of durable
        session history.  Rendering it separately prevents plan revisions
        from invalidating the stable system prefix of prior requests.

        将当前计划渲染为一条仅追加的运行时通知。调用方负责放置该消息并确保其不进入
        持久会话历史,独立渲染可避免计划修订使早先请求的稳定 system 前缀失效。
        """

        if self._plan is None:
            return None
        parts = [
            "Runtime plan update:\n"
            "The following plan supersedes every earlier runtime plan notice.",
            self._plan.model_guidance(),
        ]
        comments = self._plan.comment_guidance(self._plan_comments)
        if comments:
            parts.append(comments)
        return Message(
            Role.USER,
            "\n\n".join(parts),
            synthetic_reason=SyntheticReason.RUNTIME_PLAN,
        )

    @staticmethod
    def budget_runtime_message(usage: ExecutionBudgetUsage) -> Message | None:
        """Render a pressure transition without mutable exact counters.

        Normal execution receives no budget notice.  Higher pressure levels
        are discrete and intentionally contain no remaining-count values, so
        a stable pressure does not rewrite or churn request context.

        渲染不含精确动态计数的预算压力转换。正常执行不发送预算通知,更高压力级别
        使用离散状态,避免稳定压力反复改写或抖动请求上下文。
        """

        if usage.pressure is ExecutionBudgetPressure.NORMAL:
            return None
        return Message(
            Role.USER,
            _budget_runtime_guidance(usage.pressure),
            synthetic_reason=SyntheticReason.RUNTIME_BUDGET,
        )

    @staticmethod
    def segment_runtime_message(checkpoint: ExecutionSegmentCheckpoint) -> Message:
        """Render one immutable segment checkpoint runtime notice.

        渲染一条不可变的分段检查点运行时通知。
        """

        return Message(
            Role.USER,
            _segment_runtime_guidance(checkpoint),
            synthetic_reason=SyntheticReason.RUNTIME_CHECKPOINT,
        )

    @staticmethod
    def supervision_runtime_message(*, resolved: bool = False) -> Message:
        """Render a replan instruction or its append-only resolution notice.

        渲染重新规划指引,或其仅追加的已解决通知。
        """

        content = (
            "Runtime supervision update:\nNew evidence has been recorded after the prior "
            "replan notice. Continue from that evidence without repeating the earlier approach."
            if resolved
            else REPLAN_RUNTIME_GUIDANCE
        )
        return Message(
            Role.USER,
            content,
            synthetic_reason=SyntheticReason.RUNTIME_SUPERVISION,
        )

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

        Reasoning effort, interaction mode, and batch-first guidance are
        appended to the system message.  Repository AGENTS.md instructions are injected as a
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
            BATCH_FIRST_RUNTIME_GUIDANCE,
        ]
        guidance = "\n\n".join(guidance_parts)
        rendered = [
            item
            for item in items
            if not (
                isinstance(item, Message)
                and item.synthetic_reason
                in {
                    SyntheticReason.PARENT_RELAY,
                    SyntheticReason.DAG_PREDECESSOR_RESULTS,
                }
            )
        ]

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

        # The immutable parent relay is context rather than authority. Insert
        # its single owned copy after stable workspace context and before
        # genuine child history on every request.
        if self._parent_relay_message is not None:
            insert_at = system_index + 1
            while insert_at < len(rendered):
                item = rendered[insert_at]
                if not isinstance(item, Message) or item.synthetic_reason not in {
                    SyntheticReason.PROJECT_INSTRUCTIONS,
                    SyntheticReason.AVAILABLE_SKILLS,
                }:
                    break
                insert_at += 1
            rendered.insert(insert_at, self._parent_relay_message)

        # The dependency relay is a separate channel from parent context.  Its
        # canonical copy is owned by the application and replaces any caller-
        # supplied synthetic message removed above.
        if self._dag_result_relay_message is not None:
            insert_at = system_index + 1
            while insert_at < len(rendered):
                item = rendered[insert_at]
                if not isinstance(item, Message) or item.synthetic_reason not in {
                    SyntheticReason.PROJECT_INSTRUCTIONS,
                    SyntheticReason.AVAILABLE_SKILLS,
                    SyntheticReason.PARENT_RELAY,
                }:
                    break
                insert_at += 1
            rendered.insert(insert_at, self._dag_result_relay_message)

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


__all__ = [
    "BATCH_FIRST_RUNTIME_GUIDANCE",
    "REPLAN_RUNTIME_GUIDANCE",
    "ContextBuilder",
]
