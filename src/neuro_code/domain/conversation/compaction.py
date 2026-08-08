"""Durable, provider-neutral context-compaction records.

持久化且与 Provider 无关的上下文压缩记录.

The domain record contains only bounded metadata and an already-redacted
summary.  It never stores a prompt, tool arguments, tool output, credentials,
or a complete source context.  The source fingerprint is an opaque guard used
only to reject stale resume projections.

领域记录只包含有界元数据和已经脱敏的摘要. 它绝不保存提示词、工具参数、工具输出、凭据或完整源上下文.
源指纹只是用于拒绝过期恢复投影的不透明保护值.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from neuro_code.domain.conversation.messages import SessionItem

MAX_DURABLE_COMPACTION_ID_BYTES = 128
MAX_DURABLE_COMPACTION_SUMMARY_BYTES = 8 * 1024
COMPACTION_SOURCE_FINGERPRINT_BYTES = 64


def _require_non_empty_label(name: str, value: str, *, limit: int = 128) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} exceeds the byte limit")
    if "://" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must be an opaque label")


def _require_non_negative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _require_sha256(name: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != COMPACTION_SOURCE_FINGERPRINT_BYTES
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _require_safe_summary(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("compaction summary must not be empty")
    if len(value.encode("utf-8")) > MAX_DURABLE_COMPACTION_SUMMARY_BYTES:
        raise ValueError("compaction summary exceeds the byte limit")
    if any(
        character not in "\n\t" and (ord(character) < 32 or ord(character) == 127)
        for character in value
    ):
        raise ValueError("compaction summary contains control characters")


def compute_compaction_source_fingerprint(
    items: Sequence[SessionItem],
    candidate_range: tuple[int, int],
) -> str:
    """Compute an opaque digest for one ordered source range.

    The digest is for stale-record detection only.  It must never be rendered
    to a model or an interface.

    计算有序源范围的不透明摘要,仅用于检测过期记录,不得展示给模型或界面.
    """

    if not isinstance(candidate_range, tuple) or len(candidate_range) != 2:
        raise TypeError("candidate_range must be a two-item tuple")
    start, end = candidate_range
    _require_non_negative_int("candidate_range start", start)
    _require_non_negative_int("candidate_range end", end)
    frozen_items = tuple(items)
    if end <= start or end > len(frozen_items):
        raise ValueError("candidate_range must be a non-empty in-bounds range")
    payload: dict[str, Any] = {
        "candidate_range": [start, end],
        "items": [frozen_items[index].to_dict() for index in range(start, end)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DurableCompactionItem:
    """A bounded summary tied to one exact source-context range.

    The record is safe for persistence and resume inspection.  ``summary`` is
    hidden from ``repr`` to reduce accidental logging, while all other fields
    are bounded metadata.

    绑定到精确源上下文范围的有界摘要. 该记录可安全持久化并用于恢复检查;
    ``summary`` 不进入 ``repr`` 以减少意外日志泄露.
    """

    compaction_id: str
    provider_name: str
    model_name: str
    capacity_tokens: int
    context_affinity: str | None
    source_item_count: int
    protected_item_count: int
    recent_item_count: int
    candidate_range: tuple[int, int]
    target_tokens: int
    summary_tokens: int
    source_fingerprint: str
    summary: str = field(repr=False)
    summary_redacted: bool = field(repr=False)
    summary_truncated: bool
    created_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty_label(
            "compaction_id",
            self.compaction_id,
            limit=MAX_DURABLE_COMPACTION_ID_BYTES,
        )
        _require_non_empty_label("provider_name", self.provider_name)
        _require_non_empty_label("model_name", self.model_name)
        if self.context_affinity is not None:
            _require_non_empty_label("context_affinity", self.context_affinity)
        _require_positive_int("capacity_tokens", self.capacity_tokens)
        _require_positive_int("source_item_count", self.source_item_count)
        _require_non_negative_int("protected_item_count", self.protected_item_count)
        _require_non_negative_int("recent_item_count", self.recent_item_count)
        if self.protected_item_count > self.source_item_count:
            raise ValueError("protected_item_count must not exceed source_item_count")
        if self.recent_item_count > self.source_item_count - self.protected_item_count:
            raise ValueError("recent_item_count exceeds the unprotected item count")
        if not isinstance(self.candidate_range, tuple) or len(self.candidate_range) != 2:
            raise TypeError("candidate_range must be a two-item tuple")
        start, end = self.candidate_range
        _require_non_negative_int("candidate_range start", start)
        _require_non_negative_int("candidate_range end", end)
        if (
            start < self.protected_item_count
            or end <= start
            or end > self.source_item_count - self.recent_item_count
        ):
            raise ValueError("candidate_range must exclude protected and recent items")
        _require_positive_int("target_tokens", self.target_tokens)
        _require_positive_int("summary_tokens", self.summary_tokens)
        if self.target_tokens > self.capacity_tokens:
            raise ValueError("target_tokens must not exceed capacity_tokens")
        if self.summary_tokens > self.capacity_tokens:
            raise ValueError("summary_tokens must not exceed capacity_tokens")
        _require_sha256("source_fingerprint", self.source_fingerprint)
        _require_safe_summary(self.summary)
        if self.summary_redacted is not True:
            raise ValueError("durable compaction summaries must be marked redacted")
        if not isinstance(self.summary_truncated, bool):
            raise TypeError("summary_truncated must be a bool")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


__all__ = [
    "COMPACTION_SOURCE_FINGERPRINT_BYTES",
    "MAX_DURABLE_COMPACTION_ID_BYTES",
    "MAX_DURABLE_COMPACTION_SUMMARY_BYTES",
    "DurableCompactionItem",
    "compute_compaction_source_fingerprint",
]
