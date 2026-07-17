from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


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

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name is not None:
            result["name"] = self.name
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        return result
