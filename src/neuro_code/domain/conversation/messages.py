from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

IMAGE_MODEL_PLACEHOLDER = "[image content preserved in session; binary replay is unavailable]"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ContentPartKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"


class ContextItemKind(StrEnum):
    REASONING = "reasoning"
    BACKEND_TOOL_CALL = "backend_tool_call"


class SyntheticReason(StrEnum):
    """Marks a message as synthetic (not a genuine user/assistant turn).

    Synthetic messages are injected by the application for context shaping
    but must never masquerade as real conversation turns.

    This is deliberately an in-memory annotation. Synthetic context is
    rebuilt before every model step and is neither persisted nor projected
    through ACP/UI conversation history.

    将消息标记为合成消息,而不是真实的用户或助手回合. 该标记仅用于内存中的上下文组装,不会持久化.
    """

    PROJECT_INSTRUCTIONS = "project-instructions"
    AVAILABLE_SKILLS = "available-skills"


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ContentPart:
    kind: ContentPartKind
    text: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if self.kind is ContentPartKind.TEXT:
            if self.text is None or self.url is not None:
                raise ValueError("text content parts require text and forbid url")
        elif self.url is None or not self.url or self.text is not None:
            raise ValueError("image content parts require a non-empty url and forbid text")

    @classmethod
    def from_text(cls, text: str) -> ContentPart:
        return cls(ContentPartKind.TEXT, text=text)

    @classmethod
    def from_image(cls, url: str) -> ContentPart:
        return cls(ContentPartKind.IMAGE, url=url)

    def to_dict(self) -> dict[str, str]:
        if self.kind is ContentPartKind.TEXT:
            assert self.text is not None
            return {"type": self.kind.value, "text": self.text}
        assert self.url is not None
        return {"type": self.kind.value, "url": self.url}


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    content_parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    reasoning_content: str | None = None
    synthetic_reason: SyntheticReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "content_parts", tuple(self.content_parts))
        if self.synthetic_reason is not None:
            if self.role is not Role.USER:
                raise ValueError("synthetic context must use the user role")
            if not self.content:
                raise ValueError("synthetic context must not be empty")
            if (
                self.name is not None
                or self.tool_call_id is not None
                or self.tool_calls
                or self.content_parts
                or self.reasoning_content is not None
            ):
                raise ValueError("synthetic context must be a plain text user message")
        if self.reasoning_content is not None:
            if self.role is not Role.ASSISTANT:
                raise ValueError("reasoning content is only valid on assistant messages")
            if not self.reasoning_content:
                raise ValueError("reasoning content must not be empty")
        if self.content_parts:
            text = "\n".join(
                part.text
                for part in self.content_parts
                if part.kind is ContentPartKind.TEXT and part.text is not None
            )
            if self.content and self.content != text:
                raise ValueError("message content must match the text content-part projection")
            if not self.content:
                object.__setattr__(self, "content", text)

    def model_content(self) -> str:
        if not self.content_parts:
            return self.content
        parts: list[str] = []
        for part in self.content_parts:
            if part.kind is ContentPartKind.TEXT:
                assert part.text is not None
                parts.append(part.text)
            else:
                parts.append(IMAGE_MODEL_PLACEHOLDER)
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name is not None:
            result["name"] = self.name
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        if self.content_parts:
            result["content_parts"] = [part.to_dict() for part in self.content_parts]
        if self.reasoning_content is not None:
            result["reasoning_content"] = self.reasoning_content
        return result


@dataclass(frozen=True, slots=True)
class PreservedContextItem:
    kind: ContextItemKind
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        payload_type = self.payload.get("type")
        if payload_type != self.kind.value:
            raise ValueError(
                f"context payload type must be {self.kind.value!r}, got {payload_type!r}"
            )
        object.__setattr__(self, "payload", _freeze_json(self.payload))

    def to_dict(self) -> dict[str, Any]:
        thawed = _thaw_json(self.payload)
        assert isinstance(thawed, dict)
        return thawed


SessionItem = Message | PreservedContextItem

__all__ = [
    "IMAGE_MODEL_PLACEHOLDER",
    "ContentPart",
    "ContentPartKind",
    "ContextItemKind",
    "Message",
    "PreservedContextItem",
    "Role",
    "SessionItem",
    "SyntheticReason",
    "ToolCall",
]
