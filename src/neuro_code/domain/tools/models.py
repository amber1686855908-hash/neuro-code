"""Canonical tool value objects.

Tool registration and execution remain outside the domain package.  This
module only owns the immutable request/result shapes shared by those ports.

定义规范的工具值对象. 工具注册和执行位于领域包之外,此处只拥有端口共享的不可变请求和结果形状.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ToolExecutionMode(StrEnum):
    """Scheduling policy advertised by a tool definition.

    ``AUTO`` is resolved from the tool's side-effect contract.  It keeps old
    tools safe while allowing new read-only tools to opt into bounded
    parallelism without weakening the permission pipeline.
    """

    AUTO = "auto"
    EXCLUSIVE = "exclusive"
    PARALLEL = "parallel"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    execution_mode: ToolExecutionMode = ToolExecutionMode.AUTO

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        if not isinstance(self.execution_mode, ToolExecutionMode):
            raise TypeError("execution_mode must be a ToolExecutionMode")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }
        # ``auto`` is an internal compatibility default and must not alter
        # existing provider wire payloads.
        if self.execution_mode is not ToolExecutionMode.AUTO:
            result["execution_mode"] = self.execution_mode.value
        return result


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


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Canonical bounded result shared by model, UI, ACP, and replay layers."""

    call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    metadata: Mapping[str, Any] | None = None
    duration_seconds: float | None = None
    not_started: bool = False
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ValueError("tool execution identity must be non-empty")
        if not isinstance(self.content, str):
            raise TypeError("tool execution content must be text")
        if not isinstance(self.is_error, bool):
            raise TypeError("is_error must be a bool")
        if self.metadata is not None:
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        if not isinstance(self.not_started, bool) or not isinstance(self.cancelled, bool):
            raise TypeError("tool execution flags must be bools")

    @classmethod
    def from_tool_result(
        cls,
        call_id: str,
        tool_name: str,
        result: ToolResult,
        *,
        duration_seconds: float | None = None,
        not_started: bool = False,
        cancelled: bool = False,
    ) -> ToolExecutionResult:
        if not isinstance(result, ToolResult):
            raise TypeError("result must be a ToolResult")
        return cls(
            call_id,
            tool_name,
            result.content,
            result.is_error,
            result.metadata,
            duration_seconds,
            not_started,
            cancelled,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.call_id,
            "name": self.tool_name,
            "content": self.content,
            "is_error": self.is_error,
            "not_started": self.not_started,
            "cancelled": self.cancelled,
        }
        if self.metadata is not None:
            result["metadata"] = dict(self.metadata)
        if self.duration_seconds is not None:
            result["duration_seconds"] = self.duration_seconds
        return result


__all__ = ["ToolDefinition", "ToolExecutionMode", "ToolExecutionResult", "ToolResult"]
