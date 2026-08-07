"""Canonical tool value objects.

Tool registration and execution remain outside the domain package.  This
module only owns the immutable request/result shapes shared by those ports.

定义规范的工具值对象. 工具注册和执行位于领域包之外,此处只拥有端口共享的不可变请求和结果形状.
"""

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


__all__ = ["ToolDefinition", "ToolResult"]
