from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"content": self.content, "is_error": self.is_error}
        if self.metadata is not None:
            result["metadata"] = dict(self.metadata)
        return result
