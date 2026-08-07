"""Safe, presentation-only projections used by the CLI.

CLI 使用的安全且仅面向展示的投影.

This module deliberately contains no storage access, provider calls, or
runtime orchestration. It keeps output shape decisions at the interface
boundary while preserving the existing CLI wire format.

本模块有意不访问存储、不调用 provider、也不编排 runtime.它把输出结构决策保留在接口边界,
同时保持现有 CLI 协议格式.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from neuro_code.application.sessions.catalog import SessionSearchInspectionPage
from neuro_code.application.tools.service import SessionToolOutputArtifact
from neuro_code.domain.conversation.messages import (
    ContextItemKind,
    PreservedContextItem,
    Role,
    SessionItem,
)
from neuro_code.domain.execution import AgentExecutionOutcome, SessionExecutionRecord

__all__ = [
    "render_session_markdown",
    "serialize_execution_outcome",
    "serialize_execution_record",
    "serialize_session_search_page",
    "serialize_tool_output_artifact",
    "serialize_tool_output_artifact_read",
]


def serialize_execution_outcome(
    outcome: AgentExecutionOutcome | None,
) -> dict[str, object] | None:
    """Serialize only stable, bounded execution outcome fields.

    只序列化稳定且有界的执行结果字段.
    """

    if outcome is None:
        return None
    return {
        "status": outcome.status.value,
        "reason": outcome.reason_code.value if outcome.reason_code is not None else None,
        "finalized": outcome.finalized,
        "recoverable": outcome.recoverable,
    }


def serialize_execution_record(
    record: SessionExecutionRecord | None,
) -> dict[str, object] | None:
    """Serialize a session execution record without internal evidence.

    序列化会话执行记录,不暴露内部证据.
    """

    if record is None:
        return None
    outcome = serialize_execution_outcome(record.outcome)
    if outcome is None:
        return None
    outcome["completed_at"] = record.completed_at.isoformat()
    return outcome


def serialize_tool_output_artifact(
    reference: SessionToolOutputArtifact,
) -> dict[str, object]:
    """Serialize only the bounded, session-safe artifact projection.

    只序列化有界且经过会话校验的 artifact 投影.

    The relative filesystem path is intentionally omitted. Interfaces receive
    an opaque ID and the application service remains the only reader boundary.

    有意省略相对文件系统路径.入站接口只接收不透明 ID,读取边界仍由应用服务持有.
    """

    return {
        "id": reference.artifact.artifact_id,
        "bytes": reference.artifact.byte_count,
        "truncated": reference.artifact.truncated,
        "event_sequence": reference.event_sequence,
    }


def serialize_tool_output_artifact_read(
    artifact_id: str,
    content: str,
    read_truncated: bool,
) -> dict[str, object]:
    """Serialize a bounded artifact read result.

    序列化有界的 artifact 读取结果.
    """

    return {
        "id": artifact_id,
        "content": content,
        "read_truncated": read_truncated,
    }


async def serialize_session_search_page(
    page: SessionSearchInspectionPage,
) -> dict[str, object]:
    """Serialize search results with the same safe execution projection as list.

    使用与 list 相同的安全执行投影序列化搜索结果.
    """

    results: list[dict[str, object]] = []
    for inspection in page.results:
        row = inspection.hit.to_dict()
        row["last_execution"] = serialize_execution_record(inspection.execution_record)
        results.append(row)
    return {
        "results": results,
        "next_offset": page.next_offset,
        "total_estimate": page.total_estimate,
    }


def _reasoning_markdown(item: PreservedContextItem) -> str:
    """Render preserved reasoning without exposing encrypted content.

    渲染保留的 reasoning,不暴露加密内容.
    """

    payload = item.to_dict()
    text_parts: list[str] = []
    for field in ("content", "summary"):
        blocks = payload.get(field)
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
    if text_parts:
        return "\n\n".join(text_parts)
    if payload.get("encrypted_content") is not None:
        return "_(encrypted reasoning preserved in JSON export)_"
    return "_(reasoning metadata preserved in JSON export)_"


def _backend_tool_markdown(item: PreservedContextItem) -> str:
    """Render bounded backend tool metadata for a session export.

    为会话导出渲染有界的 backend tool 元数据.
    """

    payload = item.to_dict()
    kind = payload.get("kind")
    if not isinstance(kind, dict):
        return "_(backend tool metadata preserved in JSON export)_"
    tool_type = kind.get("tool_type", "unknown")
    identifier = kind.get("id")
    lines = [f"Type: `{tool_type}`"]
    if isinstance(identifier, str) and identifier:
        lines.append(f"ID: `{identifier}`")
    action = kind.get("action")
    if isinstance(action, dict):
        action_type = action.get("type")
        if isinstance(action_type, str):
            lines.append(f"Action: `{action_type}`")
        for field in ("query", "url", "pattern"):
            value = action.get(field)
            if isinstance(value, str) and value:
                lines.append(f"{field.replace('_', ' ').title()}: {value}")
    return "\n\n".join(lines)


def render_session_markdown(items: Sequence[SessionItem]) -> str:
    """Render a session export using only persisted, presentation-safe items.

    仅使用已持久化且适合展示的条目渲染会话导出.
    """

    sections = ["# Neuro Code session export", ""]
    for item in items:
        if isinstance(item, PreservedContextItem):
            if item.kind is ContextItemKind.REASONING:
                sections.extend(("## Reasoning", "", _reasoning_markdown(item), ""))
            else:
                sections.extend(("## Backend tool call", "", _backend_tool_markdown(item), ""))
            continue
        message = item
        if message.role is Role.SYSTEM:
            continue
        title = {
            Role.USER: "User",
            Role.ASSISTANT: "Assistant",
            Role.TOOL: f"Tool: {message.name or 'unknown'}",
        }[message.role]
        sections.extend((f"## {title}", "", message.model_content() or "_(no text)_", ""))
        for call in message.tool_calls:
            sections.extend(
                (
                    f"### Tool call: `{call.name}`",
                    "",
                    "```json",
                    json.dumps(dict(call.arguments), ensure_ascii=False, indent=2),
                    "```",
                    "",
                )
            )
    return "\n".join(sections).rstrip() + "\n"
