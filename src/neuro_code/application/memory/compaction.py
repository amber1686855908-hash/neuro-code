"""Deterministic context-compaction assessment and resume projection.

This module plans a possible compaction range and exposes bounded application
contracts for redacted summary input, durable summary creation, and explicit
resume reconstruction. It never calls a Provider, mutates the caller's
context, executes a tool, or enables automatic Runtime compaction.

为应用层记忆提供确定性的上下文压缩评估与恢复投影。

本模块规划可能的压缩范围, 并提供脱敏摘要输入、持久化摘要创建和显式恢复重建的有界应用契约。
它不会调用 Provider、修改调用方上下文、执行工具或启用 Runtime 自动压缩。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.domain.conversation.compaction import (
    MAX_DURABLE_COMPACTION_SUMMARY_BYTES,
    DurableCompactionItem,
    compute_compaction_source_fingerprint,
)
from neuro_code.domain.conversation.context import ModelContext, estimate_text_tokens
from neuro_code.domain.conversation.events import ModelCompleted, ModelTextDelta, ModelToolCall
from neuro_code.domain.conversation.messages import (
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    SyntheticReason,
)
from neuro_code.shared.errors import ProviderError
from neuro_code.shared.redaction import redact_sensitive_text

MAX_CONTEXT_SUMMARY_TOKENS = 4_096
_MAX_PROVIDER_CONTEXT_LABEL_LENGTH = 128
_MAX_CONTEXT_SUMMARY_ITEM_BYTES = 4_096
_MAX_CONTEXT_SUMMARY_ITEMS = 128
_MAX_CONTEXT_SUMMARY_TOOL_NAME_BYTES = 128
_MAX_CONTEXT_SUMMARY_PROMPT_BYTES = 32 * 1024
_MAX_SUMMARY_STOP_REASON_CHARS = 128
_COMPACTION_SUMMARY_HEADER = "[Earlier conversation summary]"
_SUMMARY_GENERATOR_GUIDANCE = """You are a bounded context-compaction summarizer.
Return only a concise factual summary of the supplied earlier conversation.
Do not call tools, request more files, run commands, or modify the workspace.
Preserve confirmed decisions, user goals, active plan state, relevant tool outcomes,
and unresolved questions. Do not include credentials, secret values, raw tool
arguments, full tool output, or internal fingerprints. Do not invent facts; mark
uncertainty explicitly. The summary will be inserted into a later model context.
"""


class ContextCompactionDecision(StrEnum):
    """Describe the assessment produced for one context snapshot.

    描述针对一次上下文快照生成的评估结果。
    """

    NOT_NEEDED = "not_needed"
    RECOMMENDED = "recommended"
    REQUIRED = "required"
    UNAVAILABLE = "unavailable"


def _require_int(name: str, value: int, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _require_label(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > _MAX_PROVIDER_CONTEXT_LABEL_LENGTH:
        raise ValueError(f"{name} must not exceed {_MAX_PROVIDER_CONTEXT_LABEL_LENGTH} characters")
    if "://" in value:
        raise ValueError(f"{name} must be an opaque label")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")


@dataclass(frozen=True, slots=True)
class ProviderContextWindow:
    """Identify a provider/model context capacity without credentials or URLs.

    表示 Provider/模型的上下文容量, 不包含凭据或 URL。
    """

    provider_name: str
    model_name: str
    capacity_tokens: int
    context_affinity: str | None = None

    def __post_init__(self) -> None:
        _require_label("provider_name", self.provider_name)
        _require_label("model_name", self.model_name)
        if self.context_affinity is not None:
            _require_label("context_affinity", self.context_affinity)
        _require_int("capacity_tokens", self.capacity_tokens, minimum=1)


@dataclass(frozen=True, slots=True)
class CompactionContextUsage:
    """Record bounded context usage without retaining model input text.

    记录有界的上下文用量, 但不保留模型输入文本。
    """

    used_tokens: int
    capacity_tokens: int | None
    estimated: bool
    provider_window: ProviderContextWindow | None = None

    def __post_init__(self) -> None:
        _require_int("used_tokens", self.used_tokens)
        if self.capacity_tokens is not None:
            _require_int("capacity_tokens", self.capacity_tokens, minimum=1)
        if not isinstance(self.estimated, bool):
            raise TypeError("estimated must be a bool")
        if self.provider_window is not None:
            if not isinstance(self.provider_window, ProviderContextWindow):
                raise TypeError("provider_window must be a ProviderContextWindow")
            if self.capacity_tokens != self.provider_window.capacity_tokens:
                raise ValueError("capacity_tokens must match provider_window")

    @classmethod
    def from_provider_window(
        cls,
        used_tokens: int,
        provider_window: ProviderContextWindow,
        *,
        estimated: bool,
    ) -> CompactionContextUsage:
        """Build usage tied to one provider/model window.

        构建绑定到一个 Provider/模型窗口的用量快照。
        """

        return cls(
            used_tokens=used_tokens,
            capacity_tokens=provider_window.capacity_tokens,
            estimated=estimated,
            provider_window=provider_window,
        )


@dataclass(frozen=True, slots=True)
class ContextCompactionPolicy:
    """Configure soft and hard context thresholds and retention boundaries.

    配置上下文软阈值、硬阈值以及保留边界。
    """

    soft_limit_ratio: float = 0.80
    hard_limit_ratio: float = 0.95
    minimum_recent_items: int = 8
    max_summary_tokens: int = 2_048

    def __post_init__(self) -> None:
        for name, ratio in (
            ("soft_limit_ratio", self.soft_limit_ratio),
            ("hard_limit_ratio", self.hard_limit_ratio),
        ):
            if isinstance(ratio, bool) or not isinstance(ratio, (float, int)):
                raise TypeError(f"{name} must be a finite number")
            if not math.isfinite(float(ratio)):
                raise ValueError(f"{name} must be finite")
        if not 0.0 < self.soft_limit_ratio < self.hard_limit_ratio <= 1.0:
            raise ValueError("soft and hard limit ratios must satisfy 0 < soft < hard <= 1")
        _require_int("minimum_recent_items", self.minimum_recent_items)
        _require_int("max_summary_tokens", self.max_summary_tokens, minimum=1)
        if self.max_summary_tokens > MAX_CONTEXT_SUMMARY_TOKENS:
            raise ValueError(f"max_summary_tokens must not exceed {MAX_CONTEXT_SUMMARY_TOKENS}")


@dataclass(frozen=True, slots=True)
class ContextCompactionPlan:
    """Expose a safe compaction plan using counts and half-open item indexes.

    使用计数和半开区间条目索引暴露安全的压缩计划。

    The plan intentionally contains no ``SessionItem`` values, summaries,
    prompts, tool output, credentials, or provider-specific payloads.
    计划刻意不包含 ``SessionItem``、摘要、提示词、工具输出、凭据或
    Provider 专用载荷。
    """

    decision: ContextCompactionDecision
    usage: CompactionContextUsage
    source_item_count: int
    protected_item_count: int
    recent_item_count: int
    candidate_range: tuple[int, int] | None
    soft_limit_tokens: int | None
    hard_limit_tokens: int | None
    target_tokens: int | None
    max_summary_tokens: int = 2_048

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ContextCompactionDecision):
            raise TypeError("decision must be a ContextCompactionDecision")
        if not isinstance(self.usage, CompactionContextUsage):
            raise TypeError("usage must be a CompactionContextUsage")
        _require_int("source_item_count", self.source_item_count)
        _require_int("protected_item_count", self.protected_item_count)
        _require_int("recent_item_count", self.recent_item_count)
        if self.protected_item_count > self.source_item_count:
            raise ValueError("protected_item_count must not exceed source_item_count")
        if self.recent_item_count > self.source_item_count - self.protected_item_count:
            raise ValueError("recent_item_count exceeds the unprotected item count")

        for name, value in (
            ("soft_limit_tokens", self.soft_limit_tokens),
            ("hard_limit_tokens", self.hard_limit_tokens),
            ("target_tokens", self.target_tokens),
        ):
            if value is not None:
                _require_int(name, value, minimum=1)
                if self.usage.capacity_tokens is not None and value > self.usage.capacity_tokens:
                    raise ValueError(f"{name} must not exceed capacity_tokens")
        _require_int("max_summary_tokens", self.max_summary_tokens, minimum=1)
        if self.max_summary_tokens > MAX_CONTEXT_SUMMARY_TOKENS:
            raise ValueError(f"max_summary_tokens must not exceed {MAX_CONTEXT_SUMMARY_TOKENS}")
        if (
            self.usage.capacity_tokens is not None
            and self.max_summary_tokens > self.usage.capacity_tokens
        ):
            raise ValueError("max_summary_tokens must not exceed capacity_tokens")

        if self.usage.capacity_tokens is None:
            if any(
                value is not None
                for value in (
                    self.soft_limit_tokens,
                    self.hard_limit_tokens,
                    self.target_tokens,
                )
            ):
                raise ValueError("token limits require a known context capacity")
        elif (
            self.soft_limit_tokens is None
            or self.hard_limit_tokens is None
            or self.soft_limit_tokens > self.hard_limit_tokens
        ):
            raise ValueError("known capacity requires ordered soft and hard limits")

        if self.decision is ContextCompactionDecision.UNAVAILABLE:
            if self.candidate_range is not None or self.target_tokens is not None:
                raise ValueError("unavailable compaction cannot have a candidate or target")
        elif self.decision is ContextCompactionDecision.NOT_NEEDED:
            if self.candidate_range is not None or self.target_tokens is not None:
                raise ValueError("not-needed compaction cannot have a candidate or target")
        elif self.target_tokens is None:
            raise ValueError("available compaction decisions require a target token count")

        if self.candidate_range is not None:
            if not isinstance(self.candidate_range, tuple) or len(self.candidate_range) != 2:
                raise TypeError("candidate_range must be a two-item tuple")
            start, end = self.candidate_range
            _require_int("candidate_range start", start)
            _require_int("candidate_range end", end)
            candidate_end = self.source_item_count - self.recent_item_count
            if start < self.protected_item_count or end <= start or end > candidate_end:
                raise ValueError("candidate_range must exclude protected and recent items")
            if self.decision not in {
                ContextCompactionDecision.RECOMMENDED,
                ContextCompactionDecision.REQUIRED,
            }:
                raise ValueError("candidate_range requires an actionable compaction decision")

    @property
    def candidate_item_count(self) -> int:
        """Return the number of items eligible for a future summarizer.

        返回未来总结器可处理的条目数量。
        """

        if self.candidate_range is None:
            return 0
        start, end = self.candidate_range
        return end - start


@dataclass(frozen=True, slots=True)
class ContextSummaryRequest:
    """Describe a future bounded summary request without carrying source items.

    描述未来的有界摘要请求, 但不携带源会话条目。

    The request is an application seam only. A later provider-backed service
    must build a redacted prompt from the selected range and preserve the
    provider affinity recorded here.
    该请求只是应用层接缝。后续 Provider 服务必须从选定范围构建脱敏提示,
    并保留这里记录的 Provider 亲和性。
    """

    provider_window: ProviderContextWindow
    source_item_count: int
    protected_item_count: int
    recent_item_count: int
    candidate_range: tuple[int, int]
    target_tokens: int
    max_summary_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.provider_window, ProviderContextWindow):
            raise TypeError("provider_window must be a ProviderContextWindow")
        _require_int("source_item_count", self.source_item_count)
        _require_int("protected_item_count", self.protected_item_count)
        _require_int("recent_item_count", self.recent_item_count)
        _require_int("target_tokens", self.target_tokens, minimum=1)
        _require_int("max_summary_tokens", self.max_summary_tokens, minimum=1)
        if self.max_summary_tokens > MAX_CONTEXT_SUMMARY_TOKENS:
            raise ValueError(f"max_summary_tokens must not exceed {MAX_CONTEXT_SUMMARY_TOKENS}")
        if self.target_tokens > self.provider_window.capacity_tokens:
            raise ValueError("target_tokens must not exceed provider context capacity")
        if self.max_summary_tokens > self.provider_window.capacity_tokens:
            raise ValueError("max_summary_tokens must not exceed provider context capacity")
        if self.protected_item_count > self.source_item_count:
            raise ValueError("protected_item_count must not exceed source_item_count")
        if self.recent_item_count > self.source_item_count - self.protected_item_count:
            raise ValueError("recent_item_count exceeds the unprotected item count")
        if not isinstance(self.candidate_range, tuple) or len(self.candidate_range) != 2:
            raise TypeError("candidate_range must be a two-item tuple")
        start, end = self.candidate_range
        _require_int("candidate_range start", start)
        _require_int("candidate_range end", end)
        if (
            start < self.protected_item_count
            or end <= start
            or end > self.source_item_count - self.recent_item_count
        ):
            raise ValueError("candidate_range must exclude protected and recent items")

    @classmethod
    def from_plan(cls, plan: ContextCompactionPlan) -> ContextSummaryRequest:
        """Create a summary request only when a provider-bound range exists.

        仅当存在绑定 Provider 的候选范围时创建摘要请求。
        """

        if not isinstance(plan, ContextCompactionPlan):
            raise TypeError("plan must be a ContextCompactionPlan")
        if plan.decision not in {
            ContextCompactionDecision.RECOMMENDED,
            ContextCompactionDecision.REQUIRED,
        }:
            raise ValueError("summary requires an actionable compaction decision")
        if plan.candidate_range is None:
            raise ValueError("summary requires a non-empty candidate range")
        provider_window = plan.usage.provider_window
        if provider_window is None:
            raise ValueError("summary requires provider context window metadata")
        if plan.target_tokens is None:
            raise ValueError("summary requires a target token count")
        if plan.max_summary_tokens > provider_window.capacity_tokens:
            raise ValueError("summary budget exceeds provider context capacity")
        return cls(
            provider_window=provider_window,
            source_item_count=plan.source_item_count,
            protected_item_count=plan.protected_item_count,
            recent_item_count=plan.recent_item_count,
            candidate_range=plan.candidate_range,
            target_tokens=plan.target_tokens,
            max_summary_tokens=plan.max_summary_tokens,
        )

    @property
    def candidate_item_count(self) -> int:
        """Return the bounded number of source items to summarize later.

        返回未来可总结的有界源条目数量。
        """

        start, end = self.candidate_range
        return end - start


class ContextSummarySourceKind(StrEnum):
    """Identify whether one summary item came from a message or preserved state.

    标识摘要条目来自消息还是保留的上下文状态。
    """

    MESSAGE = "message"
    PRESERVED_CONTEXT = "preserved_context"


def _sanitize_summary_text(value: str) -> str:
    return "".join(
        character
        if character in "\n\t" or (ord(character) >= 32 and ord(character) != 127)
        else "�"
        for character in value
    )


def _bounded_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _fit_summary_text(
    value: str,
    token_budget: int,
    token_estimator: Callable[[str], int],
) -> tuple[str, bool]:
    if token_estimator(value) <= token_budget:
        return value, False
    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if token_estimator(value[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return value[:low], True


@dataclass(frozen=True, slots=True)
class ContextSummaryItem:
    """Store one redacted, bounded source projection for a future summarizer.

    保存供未来总结器使用的一个脱敏、有界源投影。
    """

    source_index: int
    source_kind: ContextSummarySourceKind
    role: Role | None
    text: str = field(repr=False)
    estimated_tokens: int
    redacted: bool
    truncated: bool

    def __post_init__(self) -> None:
        _require_int("source_index", self.source_index)
        if not isinstance(self.source_kind, ContextSummarySourceKind):
            raise TypeError("source_kind must be a ContextSummarySourceKind")
        if self.source_kind is ContextSummarySourceKind.MESSAGE:
            if not isinstance(self.role, Role):
                raise TypeError("message summary items require a Role")
        elif self.role is not None:
            raise ValueError("preserved context summary items must not have a Role")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("summary item text must be non-empty")
        if _sanitize_summary_text(self.text) != self.text:
            raise ValueError("summary item text contains unsafe control characters")
        if len(self.text.encode("utf-8")) > _MAX_CONTEXT_SUMMARY_ITEM_BYTES:
            raise ValueError("summary item text exceeds the byte limit")
        _require_int("estimated_tokens", self.estimated_tokens)
        if not isinstance(self.redacted, bool) or not isinstance(self.truncated, bool):
            raise TypeError("summary item flags must be bools")


@dataclass(frozen=True, slots=True)
class ContextSummaryInput:
    """Represent a bounded, redacted input without raw session payloads.

    表示不携带原始会话载荷的有界、脱敏输入。
    """

    request: ContextSummaryRequest
    items: tuple[ContextSummaryItem, ...]
    input_budget_tokens: int
    estimated_input_tokens: int
    omitted_item_count: int
    redacted_item_count: int
    truncated_item_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.request, ContextSummaryRequest):
            raise TypeError("request must be a ContextSummaryRequest")
        object.__setattr__(self, "items", tuple(self.items))
        if not all(isinstance(item, ContextSummaryItem) for item in self.items):
            raise TypeError("summary input items must be ContextSummaryItem values")
        _require_int("input_budget_tokens", self.input_budget_tokens, minimum=1)
        _require_int("estimated_input_tokens", self.estimated_input_tokens)
        _require_int("omitted_item_count", self.omitted_item_count)
        _require_int("redacted_item_count", self.redacted_item_count)
        _require_int("truncated_item_count", self.truncated_item_count)
        if self.estimated_input_tokens > self.input_budget_tokens:
            raise ValueError("estimated input exceeds input budget")
        if self.request.candidate_item_count != len(self.items) + self.omitted_item_count:
            raise ValueError("summary item counts do not match the candidate range")
        indexes = tuple(item.source_index for item in self.items)
        if len(set(indexes)) != len(indexes):
            raise ValueError("summary source indexes must be unique")
        start, end = self.request.candidate_range
        if any(index < start or index >= end for index in indexes):
            raise ValueError("summary source index is outside the candidate range")
        if sum(item.estimated_tokens for item in self.items) != self.estimated_input_tokens:
            raise ValueError("estimated input does not match summary items")
        if sum(item.redacted for item in self.items) != self.redacted_item_count:
            raise ValueError("redacted item count does not match summary items")
        if sum(item.truncated for item in self.items) != self.truncated_item_count:
            raise ValueError("truncated item count does not match summary items")


class ContextSummaryInputBuilder:
    """Build a deterministic redacted summary input from one immutable context.

    从一个不可变上下文构建确定性的脱敏摘要输入。
    """

    __slots__ = ("_max_item_bytes", "_max_items", "_redaction_values", "_token_estimator")

    def __init__(
        self,
        redaction_values: Sequence[str] = (),
        *,
        max_item_bytes: int = _MAX_CONTEXT_SUMMARY_ITEM_BYTES,
        max_items: int = _MAX_CONTEXT_SUMMARY_ITEMS,
        token_estimator: Callable[[str], int] = estimate_text_tokens,
    ) -> None:
        values = tuple(redaction_values)
        if not all(isinstance(value, str) for value in values):
            raise TypeError("redaction_values must contain strings")
        _require_int("max_item_bytes", max_item_bytes, minimum=1)
        if max_item_bytes > _MAX_CONTEXT_SUMMARY_ITEM_BYTES:
            raise ValueError("max_item_bytes exceeds the summary item limit")
        _require_int("max_items", max_items, minimum=1)
        if max_items > _MAX_CONTEXT_SUMMARY_ITEMS:
            raise ValueError("max_items exceeds the summary item limit")
        if not callable(token_estimator):
            raise TypeError("token_estimator must be callable")
        self._redaction_values = tuple(value for value in values if value)
        self._max_item_bytes = max_item_bytes
        self._max_items = max_items
        self._token_estimator = token_estimator

    def build(
        self,
        context: ModelContext,
        request: ContextSummaryRequest,
    ) -> ContextSummaryInput:
        """Build only selected, redacted, token-bounded source projections.

        只构建选定的、脱敏的、受 token 限制的源投影。
        """

        if not isinstance(context, ModelContext):
            raise TypeError("context must be a ModelContext")
        if not isinstance(request, ContextSummaryRequest):
            raise TypeError("request must be a ContextSummaryRequest")
        if len(context.items) != request.source_item_count:
            raise ValueError("context item count does not match summary request")
        input_budget = request.provider_window.capacity_tokens - request.max_summary_tokens
        if input_budget < 1:
            raise ValueError("summary request leaves no input token budget")

        start, end = request.candidate_range
        selected_count = end - start
        projected: list[ContextSummaryItem] = []
        estimated_total = 0
        for source_index in range(start, min(end, start + self._max_items)):
            source_kind, role, text, projection_redacted = self._project_item(
                context.items[source_index]
            )
            safe_text, content_redacted, byte_truncated = self._prepare_text(text)
            remaining = input_budget - estimated_total
            if remaining <= 0:
                continue
            safe_text, token_truncated = _fit_summary_text(safe_text, remaining, self._estimate)
            if not safe_text:
                continue
            estimated = self._estimate(safe_text)
            if estimated > remaining:
                continue
            projected.append(
                ContextSummaryItem(
                    source_index=source_index,
                    source_kind=source_kind,
                    role=role,
                    text=safe_text,
                    estimated_tokens=estimated,
                    redacted=projection_redacted or content_redacted,
                    truncated=byte_truncated or token_truncated,
                )
            )
            estimated_total += estimated

        if not projected:
            raise ValueError("summary candidate has no representable content")
        return ContextSummaryInput(
            request=request,
            items=tuple(projected),
            input_budget_tokens=input_budget,
            estimated_input_tokens=estimated_total,
            omitted_item_count=selected_count - len(projected),
            redacted_item_count=sum(item.redacted for item in projected),
            truncated_item_count=sum(item.truncated for item in projected),
        )

    def _estimate(self, text: str) -> int:
        value = self._token_estimator(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("token_estimator must return an integer >= 0")
        return value

    def _prepare_text(self, text: str) -> tuple[str, bool, bool]:
        redacted_text = redact_sensitive_text(
            text,
            explicit_values=self._redaction_values,
        )
        content_redacted = redacted_text != text
        safe_text = _sanitize_summary_text(redacted_text)
        safe_text, byte_truncated = _bounded_utf8(safe_text, self._max_item_bytes)
        return safe_text, content_redacted, byte_truncated

    def _project_item(
        self,
        item: object,
    ) -> tuple[ContextSummarySourceKind, Role | None, str, bool]:
        if isinstance(item, Message):
            text = item.model_content()
            projection_redacted = False
            if item.tool_calls:
                tool_names = ", ".join(
                    _bounded_utf8(
                        redact_sensitive_text(
                            call.name,
                            explicit_values=self._redaction_values,
                        ),
                        _MAX_CONTEXT_SUMMARY_TOOL_NAME_BYTES,
                    )[0]
                    or "[unnamed]"
                    for call in item.tool_calls
                )
                tool_marker = f"[tool calls: {tool_names}; arguments omitted]"
                text = "\n".join(part for part in (text, tool_marker) if part)
                projection_redacted = True
            if item.reasoning_content is not None:
                text = "\n".join(part for part in (text, "[reasoning omitted]") if part)
                projection_redacted = True
            return (
                ContextSummarySourceKind.MESSAGE,
                item.role,
                text or "[empty message]",
                projection_redacted,
            )
        if isinstance(item, PreservedContextItem):
            marker = {
                ContextItemKind.REASONING: "[preserved reasoning context omitted]",
                ContextItemKind.BACKEND_TOOL_CALL: "[preserved backend tool state; payload omitted]",
            }[item.kind]
            return ContextSummarySourceKind.PRESERVED_CONTEXT, None, marker, True
        raise TypeError("unsupported session item")


@dataclass(frozen=True, slots=True)
class ContextSummaryGenerationResult:
    """Bounded, redacted output from one provider-backed summary request.

    表示一次 Provider 摘要请求产生的有界、脱敏输出。

    The summary is intentionally excluded from ``repr``. It is an in-memory
    application result and is not a durable record or a replacement context.
    摘要刻意不出现在 ``repr`` 中。该结果只属于内存中的应用层, 不是持久化记录或替换后的上下文。
    """

    summary: str = field(repr=False)
    summary_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    stop_reason: str
    source_item_count: int
    omitted_item_count: int
    redacted_item_count: int
    truncated_item_count: int
    summary_truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be non-empty")
        if _sanitize_summary_text(self.summary) != self.summary:
            raise ValueError("summary contains unsafe control characters")
        if len(self.summary.encode("utf-8")) > MAX_DURABLE_COMPACTION_SUMMARY_BYTES:
            raise ValueError("summary exceeds the durable summary byte limit")
        _require_int("summary_tokens", self.summary_tokens, minimum=1)
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if value is not None:
                _require_int(name, value, minimum=0)
        if not isinstance(self.stop_reason, str) or not self.stop_reason:
            raise ValueError("stop_reason must be a non-empty string")
        if len(self.stop_reason) > _MAX_SUMMARY_STOP_REASON_CHARS:
            raise ValueError("stop_reason is too long")
        _require_int("source_item_count", self.source_item_count, minimum=1)
        _require_int("omitted_item_count", self.omitted_item_count)
        if self.omitted_item_count > self.source_item_count:
            raise ValueError("omitted_item_count must not exceed source_item_count")
        _require_int("redacted_item_count", self.redacted_item_count)
        _require_int("truncated_item_count", self.truncated_item_count)
        if self.redacted_item_count > self.source_item_count:
            raise ValueError("redacted_item_count must not exceed source_item_count")
        if self.truncated_item_count > self.source_item_count:
            raise ValueError("truncated_item_count must not exceed source_item_count")
        if not isinstance(self.summary_truncated, bool):
            raise TypeError("summary_truncated must be a bool")


class ProviderContextSummaryGenerator:
    """Generate one strict, buffered summary from a redacted summary input.

    从已脱敏的摘要输入生成一次严格、缓冲式的 Provider 摘要。

    The generator intentionally performs exactly one model request. It has no
    retry loop, no tool executor, no persistence dependency, and no connection
    to the AgentRuntime. A caller may persist the returned summary through the
    existing durable-item builder after independently deciding that it is safe.
    该生成器刻意只执行一次模型请求,不包含重试循环、工具执行器、持久化依赖或 AgentRuntime 接入。
    调用方可在独立确认安全后,通过现有持久化条目构建器保存返回的摘要。
    """

    __slots__ = (
        "_max_prompt_bytes",
        "_max_summary_bytes",
        "_provider",
        "_redaction_values",
        "_token_estimator",
    )

    def __init__(
        self,
        provider: ModelProvider,
        *,
        redaction_values: Sequence[str] = (),
        max_prompt_bytes: int = _MAX_CONTEXT_SUMMARY_PROMPT_BYTES,
        max_summary_bytes: int = MAX_DURABLE_COMPACTION_SUMMARY_BYTES,
        token_estimator: Callable[[str], int] = estimate_text_tokens,
    ) -> None:
        # ``ModelProvider`` is intentionally a structural Protocol and is not
        # runtime-checkable. Reject the common accidental ``None``/wrong-object
        # case without imposing nominal inheritance on test or custom providers.
        # `ModelProvider` 刻意是结构化 Protocol, 不能直接做运行时检查; 这里只拒绝常见的错误对象,
        # 不要求测试或自定义 Provider 继承某个名义基类。
        if not callable(getattr(provider, "stream", None)):
            raise TypeError("provider must implement the ModelProvider stream protocol")
        values = tuple(redaction_values)
        if not all(isinstance(value, str) for value in values):
            raise TypeError("redaction_values must contain strings")
        _require_int("max_prompt_bytes", max_prompt_bytes, minimum=1)
        if max_prompt_bytes > _MAX_CONTEXT_SUMMARY_PROMPT_BYTES:
            raise ValueError("max_prompt_bytes exceeds the summary prompt limit")
        _require_int("max_summary_bytes", max_summary_bytes, minimum=1)
        if max_summary_bytes > MAX_DURABLE_COMPACTION_SUMMARY_BYTES:
            raise ValueError("max_summary_bytes exceeds the durable summary limit")
        if not callable(token_estimator):
            raise TypeError("token_estimator must be callable")
        self._provider = provider
        self._redaction_values = tuple(value for value in values if value)
        self._max_prompt_bytes = max_prompt_bytes
        self._max_summary_bytes = max_summary_bytes
        self._token_estimator = token_estimator

    def _estimate(self, text: str) -> int:
        value = self._token_estimator(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("token_estimator must return an integer >= 0")
        return value

    def _safe_text(self, value: str) -> str:
        return _sanitize_summary_text(
            redact_sensitive_text(value, explicit_values=self._redaction_values)
        )

    def _prompt_line(self, item: ContextSummaryItem) -> str:
        role = item.role.value if item.role is not None else "preserved"
        text = self._safe_text(item.text)
        return (
            f"[{item.source_index} kind={item.source_kind.value} role={role} "
            f"redacted={item.redacted} truncated={item.truncated}] {text}"
        )

    def _prompt(self, summary_input: ContextSummaryInput) -> str:
        request = summary_input.request
        metadata = (
            f"Provider: {self._safe_text(request.provider_window.provider_name)}\n"
            f"Model: {self._safe_text(request.provider_window.model_name)}\n"
            f"Candidate range: {request.candidate_range[0]}:{request.candidate_range[1]}\n"
            f"Source items: {request.source_item_count}; omitted items: "
            f"{summary_input.omitted_item_count}; redacted items: "
            f"{summary_input.redacted_item_count}; truncated items: "
            f"{summary_input.truncated_item_count}\n"
            "Candidate items:\n"
        )
        parts = [metadata]
        used_bytes = len("\n\n".join(parts).encode("utf-8"))
        omitted_by_prompt = 0
        for item in summary_input.items:
            line = self._prompt_line(item)
            line_bytes = len(line.encode("utf-8")) + 1
            if used_bytes + line_bytes > self._max_prompt_bytes:
                omitted_by_prompt += 1
                continue
            parts.append(line)
            used_bytes += line_bytes
        if omitted_by_prompt:
            parts.append(
                f"[{omitted_by_prompt} additional items omitted by the bounded prompt limit]"
            )
        prompt, _ = _bounded_utf8("\n\n".join(parts), self._max_prompt_bytes)
        if not prompt.strip():
            raise ValueError("summary prompt must not be empty")
        return prompt

    def _temporary_context(self, summary_input: ContextSummaryInput) -> ModelContext:
        request = summary_input.request
        return ModelContext(
            (
                Message(Role.SYSTEM, _SUMMARY_GENERATOR_GUIDANCE),
                Message(Role.USER, self._prompt(summary_input)),
            ),
            source_provider=request.provider_window.provider_name,
            source_model=request.provider_window.model_name,
            source_context_affinity=request.provider_window.context_affinity,
        )

    def _validate_provider_window(self, request: ContextSummaryRequest) -> None:
        expected = request.provider_window
        if self._provider.provider_name != expected.provider_name:
            raise ValueError("summary request provider does not match the generator provider")
        if self._provider.model_name != expected.model_name:
            raise ValueError("summary request model does not match the generator model")
        if (
            expected.context_affinity is not None
            and self._provider.context_affinity != expected.context_affinity
        ):
            raise ValueError("summary request affinity does not match the generator provider")

    def _safe_summary(self, value: str, max_summary_tokens: int) -> tuple[str, int, bool]:
        safe = self._safe_text(value).strip()
        safe, byte_truncated = _bounded_utf8(safe, self._max_summary_bytes)
        if not safe:
            raise ProviderError("context summary provider returned an empty response")
        safe, token_truncated = _fit_summary_text(safe, max_summary_tokens, self._estimate)
        safe = safe.strip()
        if not safe:
            raise ProviderError("context summary provider returned an empty response")
        summary_tokens = self._estimate(safe)
        if summary_tokens < 1:
            raise ProviderError("context summary provider returned no usable content")
        return safe, summary_tokens, byte_truncated or token_truncated

    async def generate(
        self,
        summary_input: ContextSummaryInput,
    ) -> ContextSummaryGenerationResult:
        """Generate one bounded summary using an explicit no-tool request.

        使用显式无工具请求生成一次有界摘要。
        """

        if not isinstance(summary_input, ContextSummaryInput):
            raise TypeError("summary_input must be a ContextSummaryInput")
        request = summary_input.request
        self._validate_provider_window(request)
        temporary_context = self._temporary_context(summary_input)
        text_parts: list[str] = []
        completion: ModelCompleted | None = None
        async for event in self._provider.stream(
            temporary_context,
            (),
            tool_policy=ModelToolPolicy.DISABLED,
        ):
            if isinstance(event, ModelTextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ModelToolCall):
                raise ProviderError("context summary provider emitted an unexpected tool call")
            elif isinstance(event, ModelCompleted):
                if completion is not None:
                    raise ProviderError("context summary provider emitted multiple completions")
                completion = event

        if completion is None:
            raise ProviderError("context summary provider ended without a completion event")
        response = completion.response_text
        if response is None:
            response = "".join(text_parts)
        if not isinstance(response, str):
            raise ProviderError("context summary provider returned an invalid response")
        summary, summary_tokens, summary_truncated = self._safe_summary(
            response,
            request.max_summary_tokens,
        )
        stop_reason = self._safe_text(completion.stop_reason).strip()
        if not stop_reason:
            raise ProviderError("context summary provider returned an empty stop reason")
        return ContextSummaryGenerationResult(
            summary=summary,
            summary_tokens=summary_tokens,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            stop_reason=stop_reason[:_MAX_SUMMARY_STOP_REASON_CHARS],
            source_item_count=request.source_item_count,
            omitted_item_count=summary_input.omitted_item_count,
            redacted_item_count=summary_input.redacted_item_count,
            truncated_item_count=summary_input.truncated_item_count,
            summary_truncated=summary_truncated,
        )


def build_durable_compaction_item(
    context: ModelContext,
    request: ContextSummaryRequest,
    *,
    compaction_id: str,
    summary: str,
    created_at: datetime,
    redaction_values: Sequence[str] = (),
    token_estimator: Callable[[str], int] = estimate_text_tokens,
) -> DurableCompactionItem:
    """Create a bounded durable summary without retaining source payloads.

    The summary is redacted, control-character sanitized, UTF-8 bounded, and
    fitted to the request's summary budget before it crosses the persistence
    boundary. The source fingerprint is computed from the exact ordered source
    range and is used only by the resume rebuilder.

    创建不保留源载荷的有界持久化摘要. 摘要在进入持久化边界前会脱敏、清理控制字符、限制 UTF-8 字节数并适配摘要预算.
    源指纹只由恢复重建器用于验证精确有序源范围.
    """

    if not isinstance(context, ModelContext):
        raise TypeError("context must be a ModelContext")
    if not isinstance(request, ContextSummaryRequest):
        raise TypeError("request must be a ContextSummaryRequest")
    if len(context.items) != request.source_item_count:
        raise ValueError("context item count does not match summary request")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-empty string")
    values = tuple(redaction_values)
    if not all(isinstance(value, str) for value in values):
        raise TypeError("redaction_values must contain strings")
    redacted_summary = redact_sensitive_text(summary, explicit_values=values)
    safe_summary = _sanitize_summary_text(redacted_summary)
    safe_summary, byte_truncated = _bounded_utf8(
        safe_summary,
        MAX_DURABLE_COMPACTION_SUMMARY_BYTES,
    )
    if not safe_summary:
        raise ValueError("summary has no representable content")
    estimate = token_estimator

    def summary_estimate(value: str) -> int:
        estimated = estimate(value)
        if isinstance(estimated, bool) or not isinstance(estimated, int) or estimated < 0:
            raise ValueError("token_estimator must return an integer >= 0")
        return estimated

    safe_summary, token_truncated = _fit_summary_text(
        safe_summary,
        request.max_summary_tokens,
        summary_estimate,
    )
    if not safe_summary:
        raise ValueError("summary has no representable content")
    summary_tokens = summary_estimate(safe_summary)
    if summary_tokens < 1:
        raise ValueError("summary must contain at least one estimated token")
    return DurableCompactionItem(
        compaction_id=compaction_id,
        provider_name=request.provider_window.provider_name,
        model_name=request.provider_window.model_name,
        capacity_tokens=request.provider_window.capacity_tokens,
        context_affinity=request.provider_window.context_affinity,
        source_item_count=request.source_item_count,
        protected_item_count=request.protected_item_count,
        recent_item_count=request.recent_item_count,
        candidate_range=request.candidate_range,
        target_tokens=request.target_tokens,
        summary_tokens=summary_tokens,
        source_fingerprint=compute_compaction_source_fingerprint(
            context.items,
            request.candidate_range,
        ),
        summary=safe_summary,
        summary_redacted=True,
        summary_truncated=byte_truncated or token_truncated,
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True)
class CompactionResumeResult:
    """Bounded result of applying durable summaries to a fresh model context.

    表示将持久化摘要应用到新的模型上下文后的有界结果.
    """

    context: ModelContext
    applied_compaction_ids: tuple[str, ...]
    omitted_item_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.context, ModelContext):
            raise TypeError("context must be a ModelContext")
        object.__setattr__(self, "applied_compaction_ids", tuple(self.applied_compaction_ids))
        if not all(isinstance(value, str) and value for value in self.applied_compaction_ids):
            raise TypeError("applied_compaction_ids must contain non-empty strings")
        if len(set(self.applied_compaction_ids)) != len(self.applied_compaction_ids):
            raise ValueError("applied_compaction_ids must be unique")
        _require_int("omitted_item_count", self.omitted_item_count)


@dataclass(frozen=True, slots=True)
class CompactionResumeRebuilder:
    """Rebuild a context from validated, non-overlapping durable summaries.

    This is an explicit projection boundary. It never runs a model, writes a
    session, replays a tool, or changes the caller's original context.

    根据经过校验且不重叠的持久化摘要重建上下文. 这是显式投影边界,不会运行模型、写入会话、重放工具或修改原始上下文.
    """

    def rebuild(
        self,
        context: ModelContext,
        records: Sequence[DurableCompactionItem],
    ) -> CompactionResumeResult:
        if not isinstance(context, ModelContext):
            raise TypeError("context must be a ModelContext")
        normalized = tuple(records)
        if not all(isinstance(record, DurableCompactionItem) for record in normalized):
            raise TypeError("records must contain DurableCompactionItem values")
        if not normalized:
            return CompactionResumeResult(context, (), 0)

        ordered = tuple(sorted(normalized, key=lambda record: record.candidate_range[0]))
        previous_end = 0
        rendered: list[SessionItem] = []
        applied_ids: list[str] = []
        omitted_count = 0
        for record in ordered:
            if record.source_item_count != len(context.items):
                raise ValueError("compaction record source item count is stale")
            self._validate_provider_origin(context, record)
            start, end = record.candidate_range
            if start < previous_end:
                raise ValueError("compaction records must not overlap")
            current_fingerprint = compute_compaction_source_fingerprint(
                context.items,
                record.candidate_range,
            )
            if current_fingerprint != record.source_fingerprint:
                raise ValueError("compaction record source fingerprint is stale")
            rendered.extend(context.items[previous_end:start])
            rendered.append(
                Message(
                    Role.USER,
                    f"{_COMPACTION_SUMMARY_HEADER}\n{record.summary}",
                    synthetic_reason=SyntheticReason.COMPACTION_SUMMARY,
                )
            )
            omitted_count += end - start
            applied_ids.append(record.compaction_id)
            previous_end = end
        rendered.extend(context.items[previous_end:])
        return CompactionResumeResult(
            context=ModelContext(
                tuple(rendered),
                source_provider=context.source_provider,
                source_model=context.source_model,
                source_context_affinity=context.source_context_affinity,
                reasoning_effort=context.reasoning_effort,
            ),
            applied_compaction_ids=tuple(applied_ids),
            omitted_item_count=omitted_count,
        )

    @staticmethod
    def _validate_provider_origin(
        context: ModelContext,
        record: DurableCompactionItem,
    ) -> None:
        if context.source_provider is not None and context.source_provider != record.provider_name:
            raise ValueError("compaction record provider does not match context origin")
        if context.source_model is not None and context.source_model != record.model_name:
            raise ValueError("compaction record model does not match context origin")
        if (
            context.source_context_affinity is not None
            and context.source_context_affinity != record.context_affinity
        ):
            raise ValueError("compaction record affinity does not match context origin")


@dataclass(frozen=True, slots=True)
class ContextCompactionPlanner:
    """Create a non-mutating compaction assessment for ordered session items.

    为有序会话条目创建不修改输入的压缩评估。
    """

    policy: ContextCompactionPolicy = field(default_factory=ContextCompactionPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ContextCompactionPolicy):
            raise TypeError("policy must be a ContextCompactionPolicy")

    def plan(
        self,
        items: Sequence[SessionItem],
        usage: CompactionContextUsage,
        *,
        protected_item_count: int = 0,
    ) -> ContextCompactionPlan:
        """Assess whether a future summarizer should compact a middle range.

        评估未来总结器是否应压缩中间条目范围。

        The protected prefix and recent suffix are represented only by counts;
        the input sequence is never changed and no item is copied into the plan.
        保护前缀和近期后缀只以计数表示; 输入序列不会被修改, 计划中也不会复制条目。
        """

        if not isinstance(usage, CompactionContextUsage):
            raise TypeError("usage must be a CompactionContextUsage")
        _require_int("protected_item_count", protected_item_count)
        source_item_count = len(tuple(items))
        if protected_item_count > source_item_count:
            raise ValueError("protected_item_count must not exceed item count")

        if usage.capacity_tokens is None:
            return ContextCompactionPlan(
                decision=ContextCompactionDecision.UNAVAILABLE,
                usage=usage,
                source_item_count=source_item_count,
                protected_item_count=protected_item_count,
                recent_item_count=0,
                candidate_range=None,
                soft_limit_tokens=None,
                hard_limit_tokens=None,
                target_tokens=None,
                max_summary_tokens=self.policy.max_summary_tokens,
            )

        soft_limit_tokens = self._limit_tokens(usage.capacity_tokens, self.policy.soft_limit_ratio)
        hard_limit_tokens = self._limit_tokens(usage.capacity_tokens, self.policy.hard_limit_ratio)
        max_summary_tokens = min(self.policy.max_summary_tokens, usage.capacity_tokens)
        if usage.used_tokens < soft_limit_tokens:
            decision = ContextCompactionDecision.NOT_NEEDED
        elif usage.used_tokens < hard_limit_tokens:
            decision = ContextCompactionDecision.RECOMMENDED
        else:
            decision = ContextCompactionDecision.REQUIRED

        recent_item_count = min(
            self.policy.minimum_recent_items,
            source_item_count - protected_item_count,
        )
        candidate_range: tuple[int, int] | None = None
        if decision in {
            ContextCompactionDecision.RECOMMENDED,
            ContextCompactionDecision.REQUIRED,
        }:
            candidate_start = protected_item_count
            candidate_end = source_item_count - recent_item_count
            if candidate_end > candidate_start:
                candidate_range = (candidate_start, candidate_end)

        return ContextCompactionPlan(
            decision=decision,
            usage=usage,
            source_item_count=source_item_count,
            protected_item_count=protected_item_count,
            recent_item_count=recent_item_count,
            candidate_range=candidate_range,
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            target_tokens=(
                soft_limit_tokens
                if decision
                in {
                    ContextCompactionDecision.RECOMMENDED,
                    ContextCompactionDecision.REQUIRED,
                }
                else None
            ),
            max_summary_tokens=max_summary_tokens,
        )

    @staticmethod
    def _limit_tokens(capacity_tokens: int, ratio: float) -> int:
        return max(1, math.floor(capacity_tokens * ratio))


__all__ = [
    "MAX_CONTEXT_SUMMARY_TOKENS",
    "CompactionContextUsage",
    "CompactionResumeRebuilder",
    "CompactionResumeResult",
    "ContextCompactionDecision",
    "ContextCompactionPlan",
    "ContextCompactionPlanner",
    "ContextCompactionPolicy",
    "ContextSummaryGenerationResult",
    "ContextSummaryInput",
    "ContextSummaryInputBuilder",
    "ContextSummaryItem",
    "ContextSummaryRequest",
    "ContextSummarySourceKind",
    "DurableCompactionItem",
    "ProviderContextSummaryGenerator",
    "ProviderContextWindow",
    "build_durable_compaction_item",
]
