"""Explicit application orchestration for durable context compaction.

This module connects the bounded summary input/generator contracts to the
canonical ``SessionStore`` port. It deliberately keeps model generation and
record persistence as two observable operations; it does not claim that a
provider request and a SQLite write share one transaction.

用于持久化上下文压缩的显式应用编排。

本模块把有界摘要输入/生成契约连接到规范的 ``SessionStore`` 端口。
它刻意把模型生成和记录持久化保留为两个可观察操作,不宣称 Provider 请求与 SQLite 写入共享同一事务.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from neuro_code.application.memory.compaction import (
    ContextSummaryGenerationResult,
    ContextSummaryInputBuilder,
    ContextSummaryRequest,
    ProviderContextSummaryGenerator,
    build_durable_compaction_item,
)
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.compaction import (
    COMPACTION_SOURCE_FINGERPRINT_BYTES,
    DurableCompactionItem,
    compute_compaction_source_fingerprint,
)
from neuro_code.domain.conversation.context import ModelContext, estimate_text_tokens


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _require_fingerprint(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != COMPACTION_SOURCE_FINGERPRINT_BYTES
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("expected_source_fingerprint must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PersistContextCompactionRequest:
    """Describe one explicit summary-to-record persistence operation.

    The source context and digest are hidden from ``repr`` because they may
    contain sensitive conversation data or an internal stale-source guard.

    描述一次显式的摘要到记录持久化操作。
    源上下文和摘要被隐藏在 ``repr`` 之外,因为它们可能包含敏感会话数据或内部过期源保护值.
    """

    session_id: str
    compaction_id: str
    context: ModelContext = field(repr=False)
    summary_request: ContextSummaryRequest
    expected_source_fingerprint: str = field(repr=False)
    created_at: datetime

    def __post_init__(self) -> None:
        _require_identifier("session_id", self.session_id)
        _require_identifier("compaction_id", self.compaction_id)
        if not isinstance(self.context, ModelContext):
            raise TypeError("context must be a ModelContext")
        if not isinstance(self.summary_request, ContextSummaryRequest):
            raise TypeError("summary_request must be a ContextSummaryRequest")
        _require_fingerprint(self.expected_source_fingerprint)
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ContextCompactionPersistenceResult:
    """Return the durable item and bounded generation accounting.

    ``generation`` is hidden from ``repr`` so the provider's summary text is
    not accidentally logged. The persisted item itself already hides its
    summary field from its representation.

    返回持久化条目和有界生成统计。
    ``generation`` 隐藏在 ``repr`` 之外,以免 Provider 摘要文本被意外记录;持久化条目本身也隐藏摘要字段.
    """

    item: DurableCompactionItem
    generation: ContextSummaryGenerationResult = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.item, DurableCompactionItem):
            raise TypeError("item must be a DurableCompactionItem")
        if not isinstance(self.generation, ContextSummaryGenerationResult):
            raise TypeError("generation must be a ContextSummaryGenerationResult")
        if self.item.summary_tokens != self.generation.summary_tokens:
            raise ValueError("persisted item token accounting does not match generation")
        if self.item.summary_truncated != self.generation.summary_truncated:
            raise ValueError("persisted item truncation does not match generation")


class ContextCompactionApplicationService:
    """Generate one summary and persist one validated durable compaction item.

    The service builds the redacted input from the supplied immutable context,
    verifies the caller's source fingerprint before contacting the Provider,
    performs exactly one generation request, and saves exactly one item. A
    storage error is propagated; the service never retries or reports success
    before ``save_compaction_item`` returns.

    生成一个摘要并持久化一个经过校验的上下文压缩条目。
    服务从传入的不可变上下文构建脱敏输入,在联系 Provider 前校验调用方源指纹,执行一次生成请求并保存一个条目.
    存储错误会继续传播;服务不会重试,也不会在 ``save_compaction_item`` 返回前报告成功.
    """

    __slots__ = (
        "_generator",
        "_input_builder",
        "_redaction_values",
        "_store",
        "_token_estimator",
    )

    def __init__(
        self,
        store: SessionStore,
        provider: ModelProvider,
        *,
        redaction_values: Sequence[str] = (),
        token_estimator: Callable[[str], int] = estimate_text_tokens,
    ) -> None:
        self._store = store
        self._redaction_values = tuple(redaction_values)
        self._token_estimator = token_estimator
        self._input_builder = ContextSummaryInputBuilder(
            self._redaction_values,
            token_estimator=token_estimator,
        )
        self._generator = ProviderContextSummaryGenerator(
            provider,
            redaction_values=self._redaction_values,
            token_estimator=token_estimator,
        )

    async def generate_and_save(
        self,
        request: PersistContextCompactionRequest,
    ) -> ContextCompactionPersistenceResult:
        """Generate and save one compaction record without runtime integration.

        生成并保存一个压缩记录,但不接入 Runtime.
        """

        if not isinstance(request, PersistContextCompactionRequest):
            raise TypeError("request must be a PersistContextCompactionRequest")
        summary_request = request.summary_request
        if len(request.context.items) != summary_request.source_item_count:
            raise ValueError("context item count does not match summary request")
        current_fingerprint = compute_compaction_source_fingerprint(
            request.context.items,
            summary_request.candidate_range,
        )
        if current_fingerprint != request.expected_source_fingerprint:
            raise ValueError("context source fingerprint is stale")

        summary_input = self._input_builder.build(request.context, summary_request)
        generation = await self._generator.generate(summary_input)
        if generation.source_item_count != summary_request.source_item_count:
            raise ValueError("summary generation source count does not match request")
        if generation.omitted_item_count != summary_input.omitted_item_count:
            raise ValueError("summary generation omitted count does not match input")
        if generation.redacted_item_count != summary_input.redacted_item_count:
            raise ValueError("summary generation redacted count does not match input")
        if generation.truncated_item_count != summary_input.truncated_item_count:
            raise ValueError("summary generation truncated count does not match input")

        item = build_durable_compaction_item(
            request.context,
            summary_request,
            compaction_id=request.compaction_id,
            summary=generation.summary,
            created_at=request.created_at,
            redaction_values=self._redaction_values,
            token_estimator=self._token_estimator,
        )
        await self._store.save_compaction_item(request.session_id, item)
        return ContextCompactionPersistenceResult(item=item, generation=generation)


__all__ = [
    "ContextCompactionApplicationService",
    "ContextCompactionPersistenceResult",
    "PersistContextCompactionRequest",
]
