"""Provider-independent MCP resources, prompts, and request callbacks.

与具体 SDK 无关的 MCP 资源、提示模板和请求回调契约。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class McpResource:
    server_name: str
    name: str
    uri: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "server": self.server_name,
            "name": self.name,
            "uri": self.uri,
        }
        for name, value in (
            ("title", self.title),
            ("description", self.description),
            ("mimeType", self.mime_type),
            ("size", self.size),
        ):
            if value is not None:
                result[name] = value
        return result


@dataclass(frozen=True, slots=True)
class McpResourceTemplate:
    server_name: str
    name: str
    uri_template: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "server": self.server_name,
            "name": self.name,
            "uriTemplate": self.uri_template,
        }
        for name, value in (
            ("title", self.title),
            ("description", self.description),
            ("mimeType", self.mime_type),
        ):
            if value is not None:
                result[name] = value
        return result


@dataclass(frozen=True, slots=True)
class McpPrompt:
    server_name: str
    name: str
    title: str | None = None
    description: str | None = None
    arguments: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", tuple(dict(argument) for argument in self.arguments))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"server": self.server_name, "name": self.name}
        for name, value in (("title", self.title), ("description", self.description)):
            if value is not None:
                result[name] = value
        if self.arguments:
            result["arguments"] = [dict(argument) for argument in self.arguments]
        return result


@dataclass(frozen=True, slots=True)
class McpResourceContent:
    uri: str
    mime_type: str | None = None
    text: str | None = None
    blob: str | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.blob is None):
            raise ValueError("MCP resource content must contain exactly one text or blob")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"uri": self.uri}
        if self.mime_type is not None:
            result["mimeType"] = self.mime_type
        if self.text is not None:
            result["text"] = self.text
        if self.blob is not None:
            result["blob"] = self.blob
        return result


@dataclass(frozen=True, slots=True)
class McpPromptMessage:
    role: str
    content: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("MCP prompt message role is invalid")
        object.__setattr__(self, "content", dict(self.content))

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": dict(self.content)}


class McpSamplingHandler(Protocol):
    async def __call__(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_preferences: Mapping[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> Mapping[str, Any]: ...


class McpElicitationHandler(Protocol):
    async def __call__(
        self,
        message: str,
        schema: Mapping[str, Any] | None = None,
        *,
        url: str | None = None,
    ) -> Mapping[str, Any]: ...


__all__ = [
    "McpElicitationHandler",
    "McpPrompt",
    "McpPromptMessage",
    "McpResource",
    "McpResourceContent",
    "McpResourceTemplate",
    "McpSamplingHandler",
]
