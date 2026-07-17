from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pygrok_build.domain.messages import Message, Role, ToolCall
from pygrok_build.domain.sessions import SessionSnapshot, SessionSummary
from pygrok_build.errors import SessionError

RUST_IMPORT_PROVIDER = "grok-build-import"
MAX_SUMMARY_BYTES = 1024 * 1024
MAX_CHAT_RECORD_BYTES = 16 * 1024 * 1024
MAX_CHAT_RECORDS = 100_000
IMAGE_PLACEHOLDER = "[image omitted during Grok Build session import]"
UNKNOWN_CONTENT_PLACEHOLDER = "[unsupported content omitted during Grok Build session import]"


class _InvalidRecord(ValueError):
    pass


@dataclass(slots=True)
class _ImportStats:
    total_records: int = 0
    invalid_records: int = 0
    unsupported_records: int = 0
    omitted_images: int = 0
    omitted_content_parts: int = 0
    omitted_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class RustSessionImport:
    source: Path
    snapshot: SessionSnapshot
    total_records: int
    invalid_records: int
    unsupported_records: int
    omitted_images: int
    omitted_content_parts: int
    omitted_tool_calls: int

    @property
    def imported_messages(self) -> int:
        return len(self.snapshot.messages)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "session": self.snapshot.summary.to_dict(),
            "imported_messages": self.imported_messages,
            "total_records": self.total_records,
            "invalid_records": self.invalid_records,
            "unsupported_records": self.unsupported_records,
            "omitted_images": self.omitted_images,
            "omitted_content_parts": self.omitted_content_parts,
            "omitted_tool_calls": self.omitted_tool_calls,
        }


def load_rust_session(source: Path) -> RustSessionImport:
    """Read a pinned Grok Build JSONL session without mutating its files."""

    try:
        resolved_source = source.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SessionError(f"cannot resolve Rust session source {source}: {error}") from error

    if resolved_source.is_file() and resolved_source.name == "summary.json":
        session_dir = resolved_source.parent
    elif resolved_source.is_dir():
        session_dir = resolved_source
    else:
        raise SessionError("Rust session source must be a directory or summary.json")

    raw_summary = _read_summary(session_dir / "summary.json")
    info = _object(raw_summary.get("info"), "summary.info")
    session_id = _required_text(info.get("id"), "summary.info.id")
    cwd = _required_text(info.get("cwd"), "summary.info.cwd")
    model = _required_text(raw_summary.get("current_model_id"), "summary.current_model_id")
    created_at = _timestamp(raw_summary.get("created_at"), "summary.created_at")
    updated_at = _timestamp(raw_summary.get("updated_at"), "summary.updated_at")
    chat_format_version = raw_summary.get("chat_format_version", 0)
    if isinstance(chat_format_version, bool) or not isinstance(chat_format_version, int):
        raise SessionError("summary.chat_format_version must be an integer")
    if chat_format_version not in (0, 1):
        raise SessionError(f"unsupported Rust chat format version: {chat_format_version}")

    messages, stats = _read_chat_history(
        session_dir / "chat_history.jsonl",
        chat_format_version=chat_format_version,
    )
    snapshot = SessionSnapshot(
        summary=SessionSummary(
            id=session_id,
            cwd=cwd,
            provider=RUST_IMPORT_PROVIDER,
            model=model,
            created_at=created_at,
            updated_at=updated_at,
        ),
        messages=tuple(messages),
    )
    return RustSessionImport(
        source=session_dir,
        snapshot=snapshot,
        total_records=stats.total_records,
        invalid_records=stats.invalid_records,
        unsupported_records=stats.unsupported_records,
        omitted_images=stats.omitted_images,
        omitted_content_parts=stats.omitted_content_parts,
        omitted_tool_calls=stats.omitted_tool_calls,
    )


def _read_summary(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size > MAX_SUMMARY_BYTES:
            raise SessionError("Rust session summary exceeds the 1 MiB safety limit")
        raw = path.read_bytes()
    except OSError as error:
        raise SessionError(f"cannot read Rust session summary {path}: {error}") from error
    try:
        loaded: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SessionError(f"Rust session summary is invalid JSON: {error}") from error
    if not isinstance(loaded, dict):
        raise SessionError("Rust session summary must be a JSON object")
    return cast(dict[str, Any], loaded)


def _read_chat_history(
    path: Path,
    *,
    chat_format_version: int,
) -> tuple[list[Message], _ImportStats]:
    stats = _ImportStats()
    messages: list[Message] = []
    tool_names: dict[str, str] = {}
    if not path.exists():
        return messages, stats
    if not path.is_file():
        raise SessionError("Rust chat history path is not a regular file")

    try:
        with path.open("rb") as stream:
            while raw_line := stream.readline(MAX_CHAT_RECORD_BYTES + 1):
                if len(raw_line) > MAX_CHAT_RECORD_BYTES:
                    while raw_line and not raw_line.endswith(b"\n"):
                        raw_line = stream.readline(MAX_CHAT_RECORD_BYTES + 1)
                    stats.total_records += 1
                    stats.invalid_records += 1
                    continue
                stripped = raw_line.strip()
                if not stripped:
                    continue
                stats.total_records += 1
                if stats.total_records > MAX_CHAT_RECORDS:
                    raise SessionError(
                        f"Rust chat history exceeds the {MAX_CHAT_RECORDS} record safety limit"
                    )
                try:
                    loaded: object = json.loads(stripped)
                    if not isinstance(loaded, dict):
                        raise _InvalidRecord("record must be an object")
                    record = cast(dict[str, Any], loaded)
                    message = _convert_record(
                        record,
                        chat_format_version=chat_format_version,
                        tool_names=tool_names,
                        stats=stats,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, _InvalidRecord):
                    stats.invalid_records += 1
                    continue
                if message is not None:
                    messages.append(message)
    except OSError as error:
        raise SessionError(f"cannot read Rust chat history {path}: {error}") from error
    return messages, stats


def _convert_record(
    record: dict[str, Any],
    *,
    chat_format_version: int,
    tool_names: dict[str, str],
    stats: _ImportStats,
) -> Message | None:
    record_type = record.get("type")
    role = record.get("role")
    if isinstance(role, str) and (chat_format_version == 0 or not isinstance(record_type, str)):
        return _convert_legacy_message(record, role, tool_names, stats)
    if not isinstance(record_type, str):
        raise _InvalidRecord("record has no type")

    if record_type == "system":
        return Message(Role.SYSTEM, _text_content(record.get("content", ""), stats))
    if record_type == "user":
        return Message(Role.USER, _text_content(record.get("content", []), stats))
    if record_type == "assistant":
        tool_calls = _tool_calls(record.get("tool_calls", []), tool_names, stats)
        return Message(
            Role.ASSISTANT,
            _text_content(record.get("content", ""), stats),
            tool_calls=tool_calls,
        )
    if record_type == "tool_result":
        return _tool_result_message(record, tool_names, stats)
    if record_type in {"reasoning", "backend_tool_call"}:
        stats.unsupported_records += 1
        return None
    stats.unsupported_records += 1
    return None


def _convert_legacy_message(
    record: dict[str, Any],
    role_name: str,
    tool_names: dict[str, str],
    stats: _ImportStats,
) -> Message | None:
    try:
        role = Role(role_name.lower())
    except ValueError as error:
        raise _InvalidRecord("unknown legacy role") from error
    content = _text_content(record.get("content", ""), stats)
    if role is Role.ASSISTANT:
        calls = _tool_calls(record.get("tool_calls", []), tool_names, stats)
        return Message(role, content, tool_calls=calls)
    if role is Role.TOOL:
        return _tool_result_message(record, tool_names, stats)
    return Message(role, content)


def _tool_calls(
    raw_calls: object,
    tool_names: dict[str, str],
    stats: _ImportStats,
) -> tuple[ToolCall, ...]:
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, list):
        raise _InvalidRecord("tool_calls must be a list")
    calls: list[ToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            stats.omitted_tool_calls += 1
            continue
        call = cast(dict[str, Any], raw_call)
        function = call.get("function")
        if isinstance(function, dict):
            function_data = cast(dict[str, Any], function)
            name_value = function_data.get("name")
            arguments_value = function_data.get("arguments")
        else:
            name_value = call.get("name")
            arguments_value = call.get("arguments")
        call_id = call.get("id")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name_value, str)
            or not name_value
            or call_id in tool_names
        ):
            stats.omitted_tool_calls += 1
            continue
        arguments = _tool_arguments(arguments_value)
        if arguments is None:
            stats.omitted_tool_calls += 1
            continue
        tool_names[call_id] = name_value
        calls.append(ToolCall(call_id, name_value, arguments))
    return tuple(calls)


def _tool_arguments(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if not isinstance(value, str):
        return None
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None


def _tool_result_message(
    record: dict[str, Any],
    tool_names: dict[str, str],
    stats: _ImportStats,
) -> Message | None:
    call_id = record.get("tool_call_id")
    if not isinstance(call_id, str) or call_id not in tool_names:
        stats.unsupported_records += 1
        return None
    content = _text_content(record.get("content", ""), stats)
    images = record.get("images", [])
    if isinstance(images, list) and images:
        content = _append_placeholders(content, len(images), stats)
    return Message(
        Role.TOOL,
        content,
        name=tool_names[call_id],
        tool_call_id=call_id,
    )


def _text_content(value: object, stats: _ImportStats) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise _InvalidRecord("message content must be text or content parts")
    parts: list[str] = []
    for raw_part in value:
        if not isinstance(raw_part, dict):
            stats.omitted_content_parts += 1
            parts.append(UNKNOWN_CONTENT_PLACEHOLDER)
            continue
        part = cast(dict[str, Any], raw_part)
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            parts.append(cast(str, part["text"]))
        elif part_type in {"image", "image_url"}:
            stats.omitted_images += 1
            parts.append(IMAGE_PLACEHOLDER)
        else:
            stats.omitted_content_parts += 1
            parts.append(UNKNOWN_CONTENT_PLACEHOLDER)
    return "\n".join(parts)


def _append_placeholders(content: str, count: int, stats: _ImportStats) -> str:
    stats.omitted_images += count
    suffix = "\n".join(IMAGE_PLACEHOLDER for _ in range(count))
    return f"{content}\n{suffix}" if content else suffix


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise SessionError(f"{field} is not a valid timestamp") from error
    if parsed.tzinfo is None:
        raise SessionError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)
