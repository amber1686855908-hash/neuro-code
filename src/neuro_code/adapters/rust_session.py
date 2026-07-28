from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from neuro_code.domain.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    ToolCall,
)
from neuro_code.domain.model_context import UPSTREAM_IMPORT_PROVIDER
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.sessions import SessionSnapshot, SessionSummary
from neuro_code.shared.errors import SessionError

MAX_SUMMARY_BYTES = 1024 * 1024
MAX_CHAT_RECORD_BYTES = 16 * 1024 * 1024
MAX_CHAT_RECORDS = 100_000
UNKNOWN_CONTENT_PLACEHOLDER = "[unsupported content omitted during upstream session import]"
RAW_OUTPUT_BACKEND_TYPES = {
    "web_search_call": "web_search",
    "custom_tool_call": "x_search",
    "code_interpreter_call": "code_interpreter",
}
RAW_OUTPUT_NON_CONTEXT_TYPES = {"message", "function_call", "mcp_call"}


class _InvalidRecord(ValueError):
    pass


@dataclass(slots=True)
class _ImportStats:
    total_records: int = 0
    invalid_records: int = 0
    unsupported_records: int = 0
    preserved_context_records: int = 0
    recovered_context_records: int = 0
    deduplicated_context_records: int = 0
    invalid_embedded_records: int = 0
    unsupported_embedded_records: int = 0
    preserved_images: int = 0
    omitted_content_parts: int = 0
    omitted_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class _ParsedContent:
    text: str
    parts: tuple[ContentPart, ...] = ()


@dataclass(frozen=True, slots=True)
class RustSessionImport:
    source: Path
    snapshot: SessionSnapshot
    total_records: int
    invalid_records: int
    unsupported_records: int
    preserved_context_records: int
    recovered_context_records: int
    deduplicated_context_records: int
    invalid_embedded_records: int
    unsupported_embedded_records: int
    preserved_images: int
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
            "preserved_context_records": self.preserved_context_records,
            "recovered_context_records": self.recovered_context_records,
            "deduplicated_context_records": self.deduplicated_context_records,
            "invalid_embedded_records": self.invalid_embedded_records,
            "unsupported_embedded_records": self.unsupported_embedded_records,
            "preserved_images": self.preserved_images,
            "omitted_content_parts": self.omitted_content_parts,
            "omitted_tool_calls": self.omitted_tool_calls,
        }


def load_rust_session(source: Path) -> RustSessionImport:
    """Read a pinned upstream Rust JSONL session without mutating its files."""

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
    title = _optional_title(raw_summary.get("generated_title"))
    sandbox_profile = _sandbox_profile(raw_summary.get("sandbox_profile"))
    chat_format_version = raw_summary.get("chat_format_version", 0)
    if isinstance(chat_format_version, bool) or not isinstance(chat_format_version, int):
        raise SessionError("summary.chat_format_version must be an integer")
    if chat_format_version not in (0, 1):
        raise SessionError(f"unsupported Rust chat format version: {chat_format_version}")

    items, stats = _read_chat_history(
        session_dir / "chat_history.jsonl",
        chat_format_version=chat_format_version,
    )
    snapshot = SessionSnapshot(
        summary=SessionSummary(
            id=session_id,
            cwd=cwd,
            provider=UPSTREAM_IMPORT_PROVIDER,
            model=model,
            created_at=created_at,
            updated_at=updated_at,
            sandbox_profile=sandbox_profile,
            title=title,
        ),
        items=tuple(items),
    )
    return RustSessionImport(
        source=session_dir,
        snapshot=snapshot,
        total_records=stats.total_records,
        invalid_records=stats.invalid_records,
        unsupported_records=stats.unsupported_records,
        preserved_context_records=stats.preserved_context_records,
        recovered_context_records=stats.recovered_context_records,
        deduplicated_context_records=stats.deduplicated_context_records,
        invalid_embedded_records=stats.invalid_embedded_records,
        unsupported_embedded_records=stats.unsupported_embedded_records,
        preserved_images=stats.preserved_images,
        omitted_content_parts=stats.omitted_content_parts,
        omitted_tool_calls=stats.omitted_tool_calls,
    )


def _sandbox_profile(value: object) -> SandboxProfile | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SessionError("summary sandbox profile must be a string")
    try:
        return SandboxProfile.parse(value)
    except ValueError as error:
        raise SessionError(f"unsupported Rust session sandbox profile: {value!r}") from error


def _optional_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SessionError("summary.generated_title must be a string")
    title = value.strip()
    return title or None


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
) -> tuple[list[SessionItem], _ImportStats]:
    stats = _ImportStats()
    items: list[SessionItem] = []
    tool_names: dict[str, str] = {}
    backend_tool_ids_seen: set[str] = set()
    if not path.exists():
        return items, stats
    if not path.is_file():
        raise SessionError("Rust chat history path is not a regular file")

    try:
        with path.open("rb") as stream:
            while raw_line := stream.readline(MAX_CHAT_RECORD_BYTES + 1):
                if len(raw_line) > MAX_CHAT_RECORD_BYTES:
                    while raw_line and not raw_line.endswith(b"\n"):
                        raw_line = stream.readline(MAX_CHAT_RECORD_BYTES + 1)
                    stats.total_records += 1
                    if stats.total_records > MAX_CHAT_RECORDS:
                        raise SessionError(
                            f"Rust chat history exceeds the {MAX_CHAT_RECORDS} record safety limit"
                        )
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
                    converted = _convert_record(
                        record,
                        chat_format_version=chat_format_version,
                        tool_names=tool_names,
                        backend_tool_ids_seen=backend_tool_ids_seen,
                        stats=stats,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, _InvalidRecord):
                    stats.invalid_records += 1
                    continue
                items.extend(converted)
                for item in converted:
                    identifier = _backend_tool_id(item)
                    if identifier is not None:
                        backend_tool_ids_seen.add(identifier)
    except OSError as error:
        raise SessionError(f"cannot read Rust chat history {path}: {error}") from error
    return items, stats


def _convert_record(
    record: dict[str, Any],
    *,
    chat_format_version: int,
    tool_names: dict[str, str],
    backend_tool_ids_seen: set[str],
    stats: _ImportStats,
) -> tuple[SessionItem, ...]:
    record_type = record.get("type")
    role = record.get("role")
    if isinstance(role, str) and (chat_format_version == 0 or not isinstance(record_type, str)):
        return _convert_legacy_message(
            record,
            role,
            tool_names,
            backend_tool_ids_seen,
            stats,
        )
    if not isinstance(record_type, str):
        raise _InvalidRecord("record has no type")

    if record_type == "system":
        content = _text_content(record.get("content", ""), stats)
        return (Message(Role.SYSTEM, content.text, content_parts=content.parts),)
    if record_type == "user":
        content = _text_content(record.get("content", []), stats)
        return (Message(Role.USER, content.text, content_parts=content.parts),)
    if record_type == "assistant":
        tool_calls = _tool_calls(record.get("tool_calls", []), tool_names, stats)
        content = _text_content(record.get("content", ""), stats)
        message = Message(
            Role.ASSISTANT,
            content.text,
            tool_calls=tool_calls,
            content_parts=content.parts,
        )
        siblings = _recover_legacy_context(record, backend_tool_ids_seen, stats)
        return (*siblings, message)
    if record_type == "tool_result":
        tool_message = _tool_result_message(record, tool_names, stats)
        return (tool_message,) if tool_message is not None else ()
    if record_type == "reasoning":
        stats.preserved_context_records += 1
        return (PreservedContextItem(ContextItemKind.REASONING, record),)
    if record_type == "backend_tool_call":
        stats.preserved_context_records += 1
        return (PreservedContextItem(ContextItemKind.BACKEND_TOOL_CALL, record),)
    stats.unsupported_records += 1
    return ()


def _convert_legacy_message(
    record: dict[str, Any],
    role_name: str,
    tool_names: dict[str, str],
    backend_tool_ids_seen: set[str],
    stats: _ImportStats,
) -> tuple[SessionItem, ...]:
    try:
        role = Role(role_name.lower())
    except ValueError as error:
        raise _InvalidRecord("unknown legacy role") from error
    content = _text_content(record.get("content", ""), stats)
    if role is Role.ASSISTANT:
        calls = _tool_calls(record.get("tool_calls", []), tool_names, stats)
        message = Message(
            role,
            content.text,
            tool_calls=calls,
            content_parts=content.parts,
        )
        siblings = _recover_legacy_context(record, backend_tool_ids_seen, stats)
        return (*siblings, message)
    if role is Role.TOOL:
        tool_message = _tool_result_message(record, tool_names, stats)
        return (tool_message,) if tool_message is not None else ()
    return (Message(role, content.text, content_parts=content.parts),)


def _recover_legacy_context(
    record: dict[str, Any],
    backend_tool_ids_seen: set[str],
    stats: _ImportStats,
) -> tuple[PreservedContextItem, ...]:
    raw_output = record.get("raw_output")
    if isinstance(raw_output, list):
        recovered: list[PreservedContextItem] = []
        for raw_entry in raw_output:
            if not isinstance(raw_entry, dict):
                stats.invalid_embedded_records += 1
                continue
            entry = cast(dict[str, Any], raw_entry)
            entry_type = entry.get("type")
            if entry_type == "reasoning":
                if not _valid_reasoning_entry(entry):
                    stats.invalid_embedded_records += 1
                    continue
                recovered.append(PreservedContextItem(ContextItemKind.REASONING, entry))
                stats.preserved_context_records += 1
                stats.recovered_context_records += 1
                continue
            if isinstance(entry_type, str) and entry_type in RAW_OUTPUT_BACKEND_TYPES:
                identifier = entry.get("id")
                if not isinstance(identifier, str):
                    stats.invalid_embedded_records += 1
                    continue
                if identifier in backend_tool_ids_seen:
                    stats.deduplicated_context_records += 1
                    continue
                backend_tool_ids_seen.add(identifier)
                kind = {
                    **{key: value for key, value in entry.items() if key != "type"},
                    "tool_type": RAW_OUTPUT_BACKEND_TYPES[entry_type],
                }
                recovered.append(
                    PreservedContextItem(
                        ContextItemKind.BACKEND_TOOL_CALL,
                        {"type": "backend_tool_call", "kind": kind},
                    )
                )
                stats.preserved_context_records += 1
                stats.recovered_context_records += 1
                continue
            if isinstance(entry_type, str) and entry_type in RAW_OUTPUT_NON_CONTEXT_TYPES:
                continue
            if isinstance(entry_type, str):
                stats.unsupported_embedded_records += 1
            else:
                stats.invalid_embedded_records += 1
        return tuple(recovered)

    is_v1_assistant = record.get("type") == "assistant"
    reasoning = record.get("reasoning")
    if is_v1_assistant and isinstance(reasoning, dict):
        reasoning_data = cast(dict[str, Any], reasoning)
        item = _synthetic_reasoning(
            identifier=reasoning_data.get("id"),
            text=reasoning_data.get("text"),
            encrypted=reasoning_data.get("encrypted"),
        )
        if item is not None:
            stats.preserved_context_records += 1
            stats.recovered_context_records += 1
            return (item,)
        return ()

    is_v0_assistant = record.get("role") == "assistant"
    if is_v0_assistant:
        item = _synthetic_reasoning(
            identifier="",
            text=record.get("reasoning_content"),
            encrypted=None,
        )
        if item is not None:
            stats.preserved_context_records += 1
            stats.recovered_context_records += 1
            return (item,)
    return ()


def _valid_reasoning_entry(entry: dict[str, Any]) -> bool:
    if not isinstance(entry.get("id"), str) or not isinstance(entry.get("summary"), list):
        return False
    for field, expected_type in (("encrypted_content", str), ("status", str)):
        value = entry.get(field)
        if value is not None and not isinstance(value, expected_type):
            return False
    block_types = {
        "summary": "summary_text",
        "content": "reasoning_text",
    }
    for field, expected_block_type in block_types.items():
        blocks = entry.get(field)
        if blocks is None:
            continue
        if not isinstance(blocks, list):
            return False
        if any(
            not isinstance(block, dict)
            or block.get("type") != expected_block_type
            or not isinstance(block.get("text"), str)
            for block in blocks
        ):
            return False
    return True


def _synthetic_reasoning(
    *,
    identifier: object,
    text: object,
    encrypted: object,
) -> PreservedContextItem | None:
    visible_text = text if isinstance(text, str) and text else None
    encrypted_text = encrypted if isinstance(encrypted, str) and encrypted else None
    if visible_text is None and encrypted_text is None:
        return None
    payload: dict[str, Any] = {
        "type": "reasoning",
        "id": identifier if isinstance(identifier, str) else "",
        "summary": (
            [{"type": "summary_text", "text": visible_text}] if visible_text is not None else []
        ),
    }
    if encrypted_text is not None:
        payload["encrypted_content"] = encrypted_text
    return PreservedContextItem(ContextItemKind.REASONING, payload)


def _backend_tool_id(item: SessionItem) -> str | None:
    if not isinstance(item, PreservedContextItem):
        return None
    if item.kind is not ContextItemKind.BACKEND_TOOL_CALL:
        return None
    kind = item.payload.get("kind")
    if not isinstance(kind, Mapping):
        return None
    identifier = kind.get("id")
    return identifier if isinstance(identifier, str) else None


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
        image_content = _text_content(images, stats)
        parts = list(content.parts)
        if not parts and content.text:
            parts.append(ContentPart.from_text(content.text))
        parts.extend(image_content.parts)
        content = _ParsedContent(
            "\n".join(part.text for part in parts if part.text is not None),
            tuple(parts),
        )
    elif not isinstance(images, list):
        raise _InvalidRecord("tool result images must be a list")
    return Message(
        Role.TOOL,
        content.text,
        name=tool_names[call_id],
        tool_call_id=call_id,
        content_parts=content.parts,
    )


def _text_content(value: object, stats: _ImportStats) -> _ParsedContent:
    if isinstance(value, str):
        return _ParsedContent(value)
    if not isinstance(value, list):
        raise _InvalidRecord("message content must be text or content parts")
    parts: list[ContentPart] = []
    for raw_part in value:
        if not isinstance(raw_part, dict):
            stats.omitted_content_parts += 1
            parts.append(ContentPart.from_text(UNKNOWN_CONTENT_PLACEHOLDER))
            continue
        part = cast(dict[str, Any], raw_part)
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            parts.append(ContentPart.from_text(cast(str, part["text"])))
        elif part_type in {"image", "image_url"}:
            url = _image_url(part)
            if url is None:
                stats.omitted_content_parts += 1
                parts.append(ContentPart.from_text(UNKNOWN_CONTENT_PLACEHOLDER))
            else:
                stats.preserved_images += 1
                parts.append(ContentPart.from_image(url))
        else:
            stats.omitted_content_parts += 1
            parts.append(ContentPart.from_text(UNKNOWN_CONTENT_PLACEHOLDER))
    return _ParsedContent(
        "\n".join(part.text for part in parts if part.text is not None),
        tuple(parts),
    )


def _image_url(part: dict[str, Any]) -> str | None:
    value = part.get("url")
    if isinstance(value, str) and value:
        return value
    image_url = part.get("image_url")
    if isinstance(image_url, str) and image_url:
        return image_url
    if isinstance(image_url, dict):
        nested = image_url.get("url")
        if isinstance(nested, str) and nested:
            return nested
    return None


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
