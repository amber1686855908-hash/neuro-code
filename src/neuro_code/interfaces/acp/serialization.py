"""Bounded ACP stop-reason and execution-outcome projections.

ACP stop reason 与执行结果的有界协议投影.

The functions in this module only map typed domain values to legal ACP wire
values. They do not inspect sessions, call providers, or execute tools.

本模块只把类型化领域值映射为合法 ACP wire 值,不读取会话、不调用 provider、也不执行工具.
"""

from __future__ import annotations

import json
from typing import Literal, cast

from neuro_code.domain.execution import (
    AgentExecutionOutcome,
    AgentExecutionStatus,
    SupervisorReasonCode,
)
from neuro_code.shared.redaction import redact_sensitive_text

__all__ = [
    "AcpStopReason",
    "execution_outcome_metadata",
    "execution_outcome_stop_reason",
    "map_stop_reason",
    "safe_output_text",
    "sanitize_controls",
    "serialized_size_bytes",
    "truncate_utf8",
]

AcpStopReason = Literal[
    "end_turn",
    "max_tokens",
    "max_turn_requests",
    "refusal",
    "cancelled",
]

_ALLOWED_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)


def sanitize_controls(text: str) -> str:
    """Replace unsafe control characters while preserving permitted whitespace.

    替换不安全控制字符,同时保留协议允许的空白字符.
    """

    return "".join(
        character
        if character in {"\n", "\r", "\t"} or ord(character) >= 32
        else "\N{REPLACEMENT CHARACTER}"
        for character in text
    ).replace("\x7f", "\N{REPLACEMENT CHARACTER}")


def truncate_utf8(text: str, limit: int, *, marker: str = "\n… [truncated]") -> str:
    """Truncate UTF-8 bytes without leaving a partial code point.

    按 UTF-8 字节有界截断文本,不会留下不完整的码点.
    """

    payload = text.encode("utf-8")
    if len(payload) <= limit:
        return text
    marker_bytes = marker.encode("utf-8")
    retained = payload[: max(0, limit - len(marker_bytes))]
    while retained:
        try:
            prefix = retained.decode("utf-8")
        except UnicodeDecodeError:
            retained = retained[:-1]
            continue
        return prefix + marker
    return marker[:limit]


def safe_output_text(
    value: object,
    limit: int,
    *,
    explicit_redactions: tuple[str, ...],
) -> str:
    """Sanitize, redact, and bound protocol output before it reaches ACP.

    在 ACP 输出前依次完成控制字符清理、脱敏和有界截断.
    """

    text = value if isinstance(value, str) else ""
    text = sanitize_controls(text)
    text = redact_sensitive_text(text, explicit_values=explicit_redactions)
    return truncate_utf8(text, limit)


def serialized_size_bytes(value: object) -> int:
    """Return the canonical UTF-8 size used by ACP payload limits.

    返回 ACP payload 限制使用的规范 UTF-8 字节大小.
    """

    return len(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def map_stop_reason(value: object) -> AcpStopReason:
    """Normalize a provider stop reason to the ACP protocol vocabulary.

    将 provider 的停止原因规范化为 ACP 协议词汇.
    """

    if value in _ALLOWED_STOP_REASONS:
        return cast(AcpStopReason, value)
    if value in {"length", "max_output_tokens"}:
        return "max_tokens"
    return "end_turn"


def execution_outcome_stop_reason(
    outcome: AgentExecutionOutcome | None,
) -> AcpStopReason | None:
    """Map a typed terminal outcome without inspecting presentation text.

    不读取展示文本,只将类型化终态结果映射为 ACP 停止原因.
    """

    if outcome is None:
        return None
    if outcome.status is AgentExecutionStatus.STUCK:
        return "end_turn"
    if outcome.status is not AgentExecutionStatus.BUDGET_LIMITED:
        return None
    if outcome.reason_code in {
        SupervisorReasonCode.INPUT_TOKEN_BUDGET,
        SupervisorReasonCode.OUTPUT_TOKEN_BUDGET,
        SupervisorReasonCode.TOTAL_TOKEN_BUDGET,
    }:
        return "max_tokens"
    return "max_turn_requests"


def execution_outcome_metadata(
    outcome: AgentExecutionOutcome | None,
) -> dict[str, str | bool] | None:
    """Expose only bounded execution status metadata supported by ACP.

    只暴露 ACP 支持的有界执行状态 metadata.
    """

    if outcome is None:
        return None
    return {
        "neuro_code.execution_status": outcome.status.value,
        "neuro_code.execution_reason": (
            outcome.reason_code.value if outcome.reason_code is not None else "none"
        ),
        "neuro_code.finalized": outcome.finalized,
        "neuro_code.recoverable": outcome.recoverable,
    }
