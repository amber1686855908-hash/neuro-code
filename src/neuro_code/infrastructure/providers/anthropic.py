"""Canonical Anthropic Messages API provider infrastructure adapter.

定义规范的 Anthropic Messages API 流式适配器.

The adapter owns Anthropic's server-tool wire protocol. Server tools remain
provider-native until the application boundary turns their citations and
sources into the canonical hosted-search contract.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import (
    ModelCapability,
    ModelCapabilitySet,
    ModelToolPolicy,
    resolve_capabilities,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelInputTokenSemantics,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
    ModelUsage,
)
from neuro_code.domain.conversation.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.image_references import (
    ANTHROPIC_IMAGE_MEDIA_TYPES,
    ANTHROPIC_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    parse_image_reference,
)
from neuro_code.shared.errors import ConfigurationError, ProviderError
from neuro_code.shared.redaction import redact_sensitive_text

ANTHROPIC_WEB_SEARCH_TOOL_TYPE = "web_search_20260318"
ANTHROPIC_WEB_FETCH_TOOL_TYPE = "web_fetch_20260318"
ANTHROPIC_SERVER_TOOL_CALLER = "direct"
MAX_SERVER_TOOL_CONTINUATIONS = 3
MAX_NATIVE_CONTEXT_BYTES = 1_048_576
MAX_VISIBLE_SOURCE_LINES = 32
MAX_SERVER_TOOL_CALL_ID_CHARS = 256
DEFAULT_WEB_SEARCH_MAX_USES = 8
DEFAULT_WEB_FETCH_MAX_USES = 4
DEFAULT_WEB_FETCH_MAX_CONTENT_TOKENS = 32_000

_ANTHROPIC_BUILTIN_TOOLS = frozenset({"web_search", "web_fetch"})
_SERVER_RESULT_TYPES = {
    "web_search_tool_result": "web_search",
    "web_fetch_tool_result": "web_fetch",
}
_SERVER_ERROR_TYPES = {
    "web_search_tool_result_error": "web_search",
    "web_fetch_tool_result_error": "web_fetch",
}


@dataclass(slots=True)
class _ToolUseBuffer:
    identifier: str = ""
    name: str = ""
    initial_input: dict[str, Any] = field(default_factory=dict)
    partial_json: str = ""


def _known_anthropic_server_model(model: str) -> bool:
    """Return conservative capability evidence for documented model families."""

    return model.strip().casefold() in {
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-mythos-5",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    }


class AnthropicProvider:
    """Native Anthropic Messages API streaming adapter."""

    @staticmethod
    def implementation_capabilities(
        *,
        model: str | None = None,
        builtin_tools: Sequence[str] = (),
        prompt_caching: bool = True,
    ) -> ModelCapabilitySet:
        """Return capabilities implemented by this Messages adapter."""

        if isinstance(builtin_tools, (str, bytes)):
            raise ConfigurationError("Anthropic builtin_tools must be a sequence of tool names")
        normalized_tools = tuple(builtin_tools)
        if len(set(normalized_tools)) != len(normalized_tools):
            raise ConfigurationError("Anthropic builtin_tools must not contain duplicates")
        unsupported = sorted(set(normalized_tools) - _ANTHROPIC_BUILTIN_TOOLS)
        if unsupported:
            names = ", ".join(repr(name) for name in unsupported)
            raise ConfigurationError(f"unsupported Anthropic builtin_tools: {names}")
        capabilities = [ModelCapability.FUNCTION_TOOLS, ModelCapability.VISION]
        if prompt_caching:
            capabilities.append(ModelCapability.PROMPT_CACHE)
        if model is not None and _known_anthropic_server_model(model):
            if "web_search" in normalized_tools:
                capabilities.append(ModelCapability.HOSTED_WEB_SEARCH)
            if "web_fetch" in normalized_tools:
                capabilities.append(ModelCapability.HOSTED_WEB_FETCH)
        return ModelCapabilitySet.from_supported(*capabilities)

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "anthropic",
        context_affinity: str | None = None,
        capabilities: ModelCapabilitySet | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        prompt_caching: bool = True,
        builtin_tools: Sequence[str] = (),
        builtin_tool_options: Mapping[str, Mapping[str, object]] | None = None,
        tool_choice: Mapping[str, object] | None = None,
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
        response_observer: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        if not isinstance(prompt_caching, bool):
            raise TypeError("prompt_caching must be a bool")
        if isinstance(builtin_tools, (str, bytes)):
            raise ConfigurationError("Anthropic builtin_tools must be a sequence of tool names")
        normalized_tools = tuple(builtin_tools)
        if any(not isinstance(name, str) or not name for name in normalized_tools):
            raise ConfigurationError("Anthropic builtin_tools names must be non-empty strings")
        if len(set(normalized_tools)) != len(normalized_tools):
            raise ConfigurationError("Anthropic builtin_tools must not contain duplicates")
        unsupported = sorted(set(normalized_tools) - _ANTHROPIC_BUILTIN_TOOLS)
        if unsupported:
            names = ", ".join(repr(name) for name in unsupported)
            raise ConfigurationError(f"unsupported Anthropic builtin_tools: {names}")
        if builtin_tool_options is not None and not isinstance(builtin_tool_options, Mapping):
            raise ConfigurationError("builtin_tool_options must be a mapping")
        normalized_options: dict[str, dict[str, object]] = {}
        for name, options in (builtin_tool_options or {}).items():
            if name not in normalized_tools:
                raise ConfigurationError(
                    f"builtin_tool_options contain disabled Anthropic tool {name!r}"
                )
            if not isinstance(options, Mapping):
                raise ConfigurationError(f"builtin_tool_options[{name!r}] must be a mapping")
            normalized_options[name] = dict(options)
        if tool_choice is not None:
            if not isinstance(tool_choice, Mapping):
                raise ConfigurationError("Anthropic tool_choice must be a mapping")
            choice_type = tool_choice.get("type")
            if choice_type not in {"auto", "any", "none", "tool"}:
                raise ConfigurationError("Anthropic tool_choice has an unsupported type")
            if choice_type == "tool" and (
                not isinstance(tool_choice.get("name"), str) or not tool_choice["name"]
            ):
                raise ConfigurationError("Anthropic tool_choice 'tool' requires a name")

        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._context_affinity = context_affinity
        self._builtin_tools = normalized_tools
        self._builtin_tool_options = normalized_options
        self._tool_choice = dict(tool_choice) if tool_choice is not None else None
        upstream = capabilities or ModelCapabilitySet.all_unknown()
        self._capabilities = resolve_capabilities(
            upstream=upstream,
            implementation=self.implementation_capabilities(
                model=model,
                builtin_tools=normalized_tools,
                prompt_caching=prompt_caching,
            ),
        ).effective
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._prompt_caching = prompt_caching
        self._transport = transport
        self._http_policy = http_policy or HttpClientPolicy()
        self._response_observer = response_observer

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def context_affinity(self) -> str | None:
        return self._context_affinity

    @property
    def capabilities(self) -> ModelCapabilitySet:
        return self._capabilities

    @property
    def _endpoint(self) -> str:
        if self._base_url.endswith("/messages"):
            return self._base_url
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/messages"
        return f"{self._base_url}/v1/messages"

    @property
    def _redaction_values(self) -> tuple[str, ...]:
        return (*self._http_policy.redaction_values, self._api_key)

    def _redact_user_text(self, value: str) -> str:
        return redact_sensitive_text(value, explicit_values=self._redaction_values)

    @staticmethod
    def _append_content(
        messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
    ) -> None:
        if not blocks:
            return
        if messages and messages[-1].get("role") == role:
            content = cast(list[dict[str, Any]], messages[-1]["content"])
            content.extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    @staticmethod
    def _content_blocks(
        message: Message,
        *,
        explicit_values: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        if not message.content_parts:
            return [
                {
                    "type": "text",
                    "text": redact_sensitive_text(
                        message.content,
                        explicit_values=explicit_values,
                    ),
                }
            ]

        blocks: list[dict[str, Any]] = []
        for part in message.content_parts:
            if part.kind is ContentPartKind.TEXT:
                assert part.text is not None
                blocks.append(
                    {
                        "type": "text",
                        "text": redact_sensitive_text(
                            part.text,
                            explicit_values=explicit_values,
                        ),
                    }
                )
                continue
            if part.kind is not ContentPartKind.IMAGE:
                blocks.append(
                    {
                        "type": "text",
                        "text": redact_sensitive_text(
                            part.model_placeholder,
                            explicit_values=explicit_values,
                        ),
                    }
                )
                continue
            assert part.url is not None
            reference = parse_image_reference(
                part.url,
                allowed_media_types=ANTHROPIC_IMAGE_MEDIA_TYPES,
                max_inline_bytes=ANTHROPIC_MAX_INLINE_IMAGE_BYTES,
            )
            if reference is None:
                blocks.append({"type": "text", "text": IMAGE_MODEL_PLACEHOLDER})
            elif isinstance(reference, InlineImageReference):
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": reference.media_type,
                            "data": reference.data,
                        },
                    }
                )
            else:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": redact_sensitive_text(
                                reference.url,
                                explicit_values=explicit_values,
                            ),
                        },
                    }
                )
        return blocks

    @classmethod
    def _append_message(
        cls,
        converted: list[dict[str, Any]],
        message: Message,
        *,
        explicit_values: Sequence[str] = (),
    ) -> None:
        content = message.model_content()
        if message.role is Role.USER:
            cls._append_content(
                converted,
                "user",
                cls._content_blocks(message, explicit_values=explicit_values),
            )
            return
        if message.role is Role.ASSISTANT:
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append(
                    {
                        "type": "text",
                        "text": redact_sensitive_text(
                            content,
                            explicit_values=explicit_values,
                        ),
                    }
                )
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
                for call in message.tool_calls
            )
            cls._append_content(converted, "assistant", blocks)
            return
        if message.role is not Role.TOOL:
            raise ProviderError(f"unsupported Anthropic message role: {message.role}")
        if not message.tool_call_id:
            raise ProviderError("Anthropic tool results require a tool_call_id")
        cls._append_content(
            converted,
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": (
                        cls._content_blocks(message, explicit_values=explicit_values)
                        if message.content_parts
                        and any(
                            part.kind is ContentPartKind.IMAGE for part in message.content_parts
                        )
                        else redact_sensitive_text(
                            content,
                            explicit_values=explicit_values,
                        )
                    ),
                }
            ],
        )

    @classmethod
    def _convert_messages(
        cls, messages: Sequence[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role is Role.SYSTEM:
                content = message.model_content()
                if content:
                    system_parts.append(content)
                continue
            cls._append_message(converted, message)
        return ("\n\n".join(system_parts) or None), converted

    @staticmethod
    def _native_content(item: PreservedContextItem) -> list[dict[str, Any]] | None:
        if item.kind is not ContextItemKind.BACKEND_TOOL_CALL:
            return None
        payload = item.to_dict()
        kind = payload.get("kind")
        if not isinstance(kind, Mapping):
            return None
        if (
            kind.get("provider") != "anthropic-messages"
            or kind.get("native_type") != "anthropic_message_content"
        ):
            return None
        content = kind.get("content")
        if not isinstance(content, list) or not all(
            isinstance(block, Mapping) for block in content
        ):
            return None
        return [dict(block) for block in content]

    def _can_replay_native_context(self, context: ModelContext) -> bool:
        return (
            context.source_provider == self._provider_name
            and context.source_model == self._model
            and (
                self._context_affinity is None
                or context.source_context_affinity == self._context_affinity
            )
        )

    def _convert_context(self, context: ModelContext) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        replay_native = self._can_replay_native_context(context)
        skip_standard_assistant = False
        for item in context.items:
            if isinstance(item, PreservedContextItem):
                native = self._native_content(item) if replay_native else None
                if native is not None:
                    # Consecutive native responses are separate Anthropic
                    # continuation turns. Do not merge them into one assistant
                    # message or the byte/ordering contract is lost.
                    converted.append({"role": "assistant", "content": copy.deepcopy(native)})
                    skip_standard_assistant = True
                continue
            if skip_standard_assistant:
                if item.role is Role.ASSISTANT:
                    skip_standard_assistant = False
                    continue
                skip_standard_assistant = False
            if item.role is Role.SYSTEM:
                content = item.model_content()
                if content:
                    system_parts.append(self._redact_user_text(content))
                continue
            self._append_message(
                converted,
                item,
                explicit_values=self._redaction_values,
            )
        return ("\n\n".join(system_parts) or None), converted

    def _server_tool_definition(self, name: str) -> dict[str, Any]:
        options = dict(self._builtin_tool_options.get(name, {}))
        options["allowed_callers"] = [ANTHROPIC_SERVER_TOOL_CALLER]
        if name == "web_search":
            definition: dict[str, Any] = {
                "type": ANTHROPIC_WEB_SEARCH_TOOL_TYPE,
                "name": name,
                "max_uses": DEFAULT_WEB_SEARCH_MAX_USES,
            }
        elif name == "web_fetch":
            definition = {
                "type": ANTHROPIC_WEB_FETCH_TOOL_TYPE,
                "name": name,
                "max_uses": DEFAULT_WEB_FETCH_MAX_USES,
                "max_content_tokens": DEFAULT_WEB_FETCH_MAX_CONTENT_TOKENS,
                "citations": {"enabled": True},
            }
        else:  # pragma: no cover - constructor validation owns this branch.
            raise ConfigurationError(f"unsupported Anthropic server tool: {name}")
        for key, value in options.items():
            if key == "allowed_callers":
                continue
            if key == "citations" and isinstance(value, Mapping):
                definition["citations"] = dict(value)
            else:
                definition[key] = copy.deepcopy(value)
        definition["allowed_callers"] = [ANTHROPIC_SERVER_TOOL_CALLER]
        return definition

    def _request_body(
        self,
        messages: ModelContext | Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> dict[str, Any]:
        if isinstance(messages, ModelContext):
            system, converted = self._convert_context(messages)
        else:
            system, converted = self._convert_messages(messages)
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_output_tokens,
            "messages": converted,
            "stream": True,
        }
        if system is not None:
            body["system"] = system
        if self._prompt_caching:
            body["cache_control"] = {"type": "ephemeral"}
        if tool_policy is ModelToolPolicy.ALLOWED:
            configured_tools = [self._server_tool_definition(name) for name in self._builtin_tools]
            builtin_names = set(self._builtin_tools)
            configured_tools.extend(
                tool.to_dict() for tool in tools if tool.name not in builtin_names
            )
            if configured_tools:
                body["tools"] = configured_tools
                if self._tool_choice is not None:
                    body["tool_choice"] = copy.deepcopy(self._tool_choice)
        return body

    def _safe_detail(self, detail: str) -> str:
        return self._http_policy.redact(detail, self._api_key)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _finish_arguments(buffer: _ToolUseBuffer) -> dict[str, Any]:
        arguments = dict(buffer.initial_input)
        if buffer.partial_json:
            try:
                parsed = json.loads(buffer.partial_json)
            except json.JSONDecodeError as error:
                raise ProviderError(
                    f"tool call {buffer.name!r} contained invalid JSON arguments"
                ) from error
            if not isinstance(parsed, dict):
                raise ProviderError(f"tool call {buffer.name!r} arguments must be a JSON object")
            arguments.update(parsed)
        return arguments

    @classmethod
    def _finish_tool(cls, buffer: _ToolUseBuffer) -> ToolCall:
        if not buffer.identifier or not buffer.name:
            raise ProviderError("Anthropic emitted an incomplete tool call")
        arguments = cls._finish_arguments(buffer)
        return ToolCall(buffer.identifier, buffer.name, arguments)

    @staticmethod
    def _usage_value(usage: Mapping[str, Any] | None, key: str) -> int | None:
        return AnthropicProvider._token_count(usage.get(key)) if usage is not None else None

    @staticmethod
    def _add_optional(left: int | None, right: int | None) -> int | None:
        if left is None:
            return right
        if right is None:
            return left
        return left + right

    @classmethod
    def _native_context_item(
        cls,
        blocks: Sequence[Mapping[str, Any]],
        stop_reason: str,
    ) -> PreservedContextItem | None:
        if not any(
            isinstance(block.get("type"), str)
            and block["type"] in {"server_tool_use", *_SERVER_RESULT_TYPES, *_SERVER_ERROR_TYPES}
            for block in blocks
        ):
            return None
        content = [dict(block) for block in blocks]
        payload = {
            "type": "backend_tool_call",
            "kind": {
                "provider": "anthropic-messages",
                "tool_type": "anthropic_server_turn",
                "native_type": "anthropic_message_content",
                "content": content,
                "stop_reason": stop_reason,
            },
        }
        try:
            if (
                len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                > MAX_NATIVE_CONTEXT_BYTES
            ):
                raise ProviderError("Anthropic native server-tool context exceeds its size limit")
        except (TypeError, ValueError) as error:
            raise ProviderError("Anthropic native server-tool context is not JSON-safe") from error
        return PreservedContextItem(ContextItemKind.BACKEND_TOOL_CALL, payload)

    @staticmethod
    def _server_identity(
        block: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        identifier = block.get("id")
        name = block.get("name")
        if (
            isinstance(identifier, str)
            and identifier
            and len(identifier) <= MAX_SERVER_TOOL_CALL_ID_CHARS
            and identifier.isprintable()
            and isinstance(name, str)
            and name
        ):
            return identifier, name
        return None

    @classmethod
    def _seed_server_lifecycle(
        cls,
        context: ModelContext,
        allowed_names: set[str],
    ) -> tuple[dict[str, str], set[str]]:
        started: dict[str, str] = {}
        completed: set[str] = set()
        for item in context.preserved_items:
            blocks = cls._native_content(item)
            if blocks is None:
                continue
            for block in blocks:
                if block.get("type") == "server_tool_use":
                    identity = cls._server_identity(block)
                    if identity is not None and identity[1] in allowed_names:
                        started[identity[0]] = identity[1]
                elif block.get("type") in _SERVER_RESULT_TYPES:
                    identifier = block.get("tool_use_id")
                    if isinstance(identifier, str) and identifier:
                        completed.add(identifier)
        return started, completed

    @staticmethod
    def _server_error_detail(block: Mapping[str, Any]) -> str:
        code = block.get("error_code")
        content = block.get("content")
        if not isinstance(code, str) and isinstance(content, Mapping):
            code = content.get("error_code")
        if not isinstance(code, str):
            code = "server_tool_error"
        return code[:128]

    def _source_footer(self, blocks: Sequence[Mapping[str, Any]]) -> str:
        sources: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(raw: object, title: object = None) -> None:
            if not isinstance(raw, str) or not raw.startswith(("http://", "https://")):
                return
            original_url = raw.strip()[:2048]
            url = redact_sensitive_text(
                original_url,
                explicit_values=self._redaction_values,
            )
            if url != original_url:
                return
            key = url.casefold()
            if key in seen or len(sources) >= MAX_VISIBLE_SOURCE_LINES:
                return
            title_text = (
                title.strip()[:256] if isinstance(title, str) and title.strip() else "Web source"
            )
            seen.add(key)
            sources.append((title_text, url))

        def walk(value: object) -> None:
            if not isinstance(value, Mapping):
                return
            block_type = value.get("type")
            if block_type in {"web_search_result", "web_fetch_result"}:
                add(value.get("url"), value.get("title"))
                document = value.get("document")
                if isinstance(document, Mapping):
                    add(document.get("source"), document.get("title"))
            for key, child in value.items():
                if key in {"encrypted_content", "encrypted_index"}:
                    continue
                if isinstance(child, list):
                    for item in child:
                        walk(item)
                elif isinstance(child, Mapping):
                    walk(child)

        for block in blocks:
            walk(block)
        if not sources:
            return ""
        return "\n\nSources:\n" + "\n".join(f"- {title}: {url}" for title, url in sources)

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        try:
            import httpx
        except ImportError as error:
            raise ProviderError(
                "httpx is required for live model requests; install the project"
            ) from error

        body = self._request_body(context, tools, tool_policy=tool_policy)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "accept": "text/event-stream",
        }
        wire_messages = cast(list[dict[str, Any]], body["messages"])
        started_server_calls, completed_server_calls = self._seed_server_lifecycle(
            context,
            set(self._builtin_tools),
        )
        all_text_parts: list[str] = []
        all_native_items: list[PreservedContextItem] = []
        final_stop_reason = "end_turn"
        total_input_tokens: int | None = None
        total_output_tokens: int | None = None
        total_cache_read_tokens: int | None = None
        total_cache_write_tokens: int | None = None
        continuations = 0

        try:
            options = self._http_policy.client_options(
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            )
            async with httpx.AsyncClient(**options) as client:
                while True:
                    buffers: dict[int, _ToolUseBuffer] = {}
                    blocks_by_index: dict[int, dict[str, Any]] = {}
                    block_order: list[int] = []
                    stop_reason = "end_turn"
                    message_start: Mapping[str, Any] = {}
                    start_usage: Mapping[str, Any] | None = None
                    output_tokens: int | None = None
                    saw_message_stop = False
                    response_tool_calls: list[ToolCall] = []
                    server_buffers: dict[int, _ToolUseBuffer] = {}

                    async with client.stream(
                        "POST",
                        self._endpoint,
                        headers=headers,
                        json={**body, "messages": wire_messages},
                    ) as response:
                        if response.status_code >= 400:
                            detail = self._safe_detail(
                                (await response.aread()).decode("utf-8", "replace")
                            )
                            raise ProviderError(
                                f"Anthropic request failed with HTTP {response.status_code}: {detail}"
                            )
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                event = json.loads(payload)
                            except json.JSONDecodeError as error:
                                raise ProviderError(
                                    "Anthropic returned malformed streaming JSON"
                                ) from error
                            if not isinstance(event, dict):
                                continue
                            event_type = event.get("type")
                            if event_type == "error":
                                error_data = event.get("error")
                                detail = json.dumps(error_data, ensure_ascii=False)
                                raise ProviderError(
                                    f"Anthropic stream error: {self._safe_detail(detail)}"
                                )
                            if event_type == "message_start":
                                raw_message = event.get("message")
                                if isinstance(raw_message, Mapping):
                                    message_start = dict(raw_message)
                                    usage = raw_message.get("usage")
                                    if isinstance(usage, Mapping):
                                        start_usage = usage
                                        output_tokens = self._token_count(
                                            usage.get("output_tokens")
                                        )
                                continue
                            if event_type == "content_block_start":
                                index = event.get("index")
                                block = event.get("content_block")
                                if not isinstance(index, int) or not isinstance(block, dict):
                                    continue
                                block_copy = copy.deepcopy(block)
                                block_type = block_copy.get("type")
                                blocks_by_index[index] = block_copy
                                block_order.append(index)
                                if block_type == "tool_use":
                                    initial_input = block_copy.get("input")
                                    if not isinstance(initial_input, dict):
                                        raise ProviderError(
                                            "Anthropic tool input must be a JSON object"
                                        )
                                    raw_identifier = block_copy.get("id")
                                    raw_name = block_copy.get("name")
                                    buffers[index] = _ToolUseBuffer(
                                        identifier=(
                                            raw_identifier
                                            if isinstance(raw_identifier, str)
                                            else ""
                                        ),
                                        name=raw_name if isinstance(raw_name, str) else "",
                                        initial_input=dict(initial_input),
                                    )
                                elif block_type == "server_tool_use":
                                    identity = self._server_identity(block_copy)
                                    if identity is None or identity[1] not in self._builtin_tools:
                                        raise ProviderError(
                                            "Anthropic emitted an unsupported server tool call"
                                        )
                                    call_id, name = identity
                                    if call_id not in started_server_calls:
                                        started_server_calls[call_id] = name
                                        yield ModelBackendToolStarted(call_id, name)
                                    initial_input = block_copy.get("input", {})
                                    if not isinstance(initial_input, dict):
                                        raise ProviderError(
                                            "Anthropic server tool input must be a JSON object"
                                        )
                                    server_buffers[index] = _ToolUseBuffer(
                                        identifier=call_id,
                                        name=name,
                                        initial_input=dict(initial_input),
                                    )
                                elif block_type in _SERVER_RESULT_TYPES:
                                    tool_call_id = block_copy.get("tool_use_id")
                                    name = _SERVER_RESULT_TYPES[block_type]
                                    result_content = block_copy.get("content")
                                    result_content_type = (
                                        result_content.get("type")
                                        if isinstance(result_content, Mapping)
                                        else None
                                    )
                                    result_error = (
                                        block_copy.get("is_error") is True
                                        or isinstance(block_copy.get("error_code"), str)
                                        or (
                                            isinstance(result_content, Mapping)
                                            and (
                                                (
                                                    isinstance(result_content_type, str)
                                                    and result_content_type in _SERVER_ERROR_TYPES
                                                )
                                                or isinstance(result_content.get("error_code"), str)
                                            )
                                        )
                                    )
                                    if result_error:
                                        raise ProviderError(
                                            "Anthropic server tool failed: "
                                            + self._server_error_detail(block_copy)
                                        )
                                    if (
                                        not isinstance(tool_call_id, str)
                                        or not tool_call_id
                                        or name not in self._builtin_tools
                                    ):
                                        raise ProviderError(
                                            "Anthropic emitted an invalid server tool result"
                                        )
                                    started_name = started_server_calls.get(tool_call_id)
                                    if started_name is not None and started_name != name:
                                        raise ProviderError(
                                            "Anthropic server tool result did not match its call"
                                        )
                                    if started_name is None:
                                        started_server_calls[tool_call_id] = name
                                        yield ModelBackendToolStarted(tool_call_id, name)
                                    if tool_call_id not in completed_server_calls:
                                        completed_server_calls.add(tool_call_id)
                                        yield ModelBackendToolCompleted(tool_call_id, name)
                                elif block_type in _SERVER_ERROR_TYPES:
                                    raise ProviderError(
                                        "Anthropic server tool failed: "
                                        + self._server_error_detail(block_copy)
                                    )
                                elif block_type == "text" and isinstance(
                                    block_copy.get("text"), str
                                ):
                                    if block_copy["text"]:
                                        all_text_parts.append(block_copy["text"])
                                        yield ModelTextDelta(block_copy["text"])
                                elif block_type == "thinking" and isinstance(
                                    block_copy.get("thinking"), str
                                ):
                                    if block_copy["thinking"]:
                                        yield ModelReasoningDelta(block_copy["thinking"])
                                continue
                            if event_type == "content_block_delta":
                                index = event.get("index")
                                delta = event.get("delta")
                                if not isinstance(index, int) or not isinstance(delta, dict):
                                    continue
                                block = blocks_by_index.get(index)
                                delta_type = delta.get("type")
                                if delta_type == "text_delta" and isinstance(
                                    delta.get("text"), str
                                ):
                                    text = delta["text"]
                                    if isinstance(block, dict):
                                        block["text"] = str(block.get("text", "")) + text
                                    if text:
                                        all_text_parts.append(text)
                                        yield ModelTextDelta(text)
                                elif delta_type == "thinking_delta" and isinstance(
                                    delta.get("thinking"), str
                                ):
                                    thinking = delta["thinking"]
                                    if isinstance(block, dict):
                                        block["thinking"] = (
                                            str(block.get("thinking", "")) + thinking
                                        )
                                    if thinking:
                                        yield ModelReasoningDelta(thinking)
                                elif delta_type == "citations_delta" and isinstance(block, dict):
                                    citation = delta.get("citation")
                                    if isinstance(citation, Mapping):
                                        citations = block.setdefault("citations", [])
                                        if isinstance(citations, list):
                                            citations.append(copy.deepcopy(dict(citation)))
                                elif delta_type == "input_json_delta" and isinstance(
                                    delta.get("partial_json"), str
                                ):
                                    if index in server_buffers:
                                        server_buffers[index].partial_json += delta["partial_json"]
                                    else:
                                        buffers.setdefault(
                                            index, _ToolUseBuffer()
                                        ).partial_json += delta["partial_json"]
                                continue
                            if event_type == "content_block_stop":
                                index = event.get("index")
                                if isinstance(index, int) and index in server_buffers:
                                    buffer = server_buffers.pop(index)
                                    arguments = self._finish_arguments(buffer)
                                    if index in blocks_by_index:
                                        blocks_by_index[index]["input"] = arguments
                                    continue
                                if isinstance(index, int) and index in buffers:
                                    buffer = buffers.pop(index)
                                    call = self._finish_tool(buffer)
                                    response_tool_calls.append(call)
                                    if index in blocks_by_index:
                                        blocks_by_index[index]["input"] = dict(call.arguments)
                                    yield ModelToolCall(call)
                                continue
                            if event_type == "message_delta":
                                delta = event.get("delta")
                                usage = event.get("usage")
                                if isinstance(delta, Mapping) and isinstance(
                                    delta.get("stop_reason"), str
                                ):
                                    stop_reason = delta["stop_reason"]
                                if isinstance(usage, Mapping):
                                    updated_output_tokens = self._token_count(
                                        usage.get("output_tokens")
                                    )
                                    if updated_output_tokens is not None:
                                        output_tokens = updated_output_tokens
                                continue
                            if event_type == "message_stop":
                                saw_message_stop = True

                    for index, buffer in server_buffers.items():
                        arguments = self._finish_arguments(buffer)
                        if index in blocks_by_index:
                            blocks_by_index[index]["input"] = arguments
                    if buffers:
                        raise ProviderError("Anthropic stream ended during a tool call")
                    if not saw_message_stop:
                        raise ProviderError("Anthropic stream ended without message_stop")
                    response_blocks = [
                        blocks_by_index[index] for index in block_order if index in blocks_by_index
                    ]
                    final_stop_reason = stop_reason
                    native_item = self._native_context_item(response_blocks, stop_reason)
                    if native_item is not None:
                        all_native_items.append(native_item)

                    input_tokens = self._usage_value(start_usage, "input_tokens")
                    cache_read = self._usage_value(start_usage, "cache_read_input_tokens")
                    cache_write = self._usage_value(start_usage, "cache_creation_input_tokens")
                    total_input_tokens = self._add_optional(total_input_tokens, input_tokens)
                    total_output_tokens = self._add_optional(total_output_tokens, output_tokens)
                    total_cache_read_tokens = self._add_optional(
                        total_cache_read_tokens, cache_read
                    )
                    total_cache_write_tokens = self._add_optional(
                        total_cache_write_tokens,
                        cache_write,
                    )

                    terminal_usage: dict[str, int] = {}
                    if input_tokens is not None:
                        terminal_usage["input_tokens"] = input_tokens
                    if output_tokens is not None:
                        terminal_usage["output_tokens"] = output_tokens
                    if cache_read is not None:
                        terminal_usage["cache_read_input_tokens"] = cache_read
                    if cache_write is not None:
                        terminal_usage["cache_creation_input_tokens"] = cache_write
                    terminal: dict[str, Any] = {
                        "type": "message",
                        "id": message_start.get("id", ""),
                        "role": "assistant",
                        "model": message_start.get("model", self._model),
                        "content": copy.deepcopy(response_blocks),
                        "stop_reason": stop_reason,
                        "usage": terminal_usage,
                    }
                    if self._response_observer is not None:
                        try:
                            self._response_observer(terminal)
                        except Exception as error:
                            raise ProviderError(
                                f"Anthropic response observer failed: {type(error).__name__}"
                            ) from error

                    if stop_reason == "pause_turn" and not response_tool_calls:
                        continuations += 1
                        if continuations > MAX_SERVER_TOOL_CONTINUATIONS:
                            raise ProviderError("Anthropic server-tool continuation limit exceeded")
                        wire_messages.append(
                            {"role": "assistant", "content": copy.deepcopy(response_blocks)}
                        )
                        continue

                    source_footer = self._source_footer(response_blocks)
                    if source_footer:
                        all_text_parts.append(source_footer)
                        yield ModelTextDelta(source_footer)
                    usage = ModelUsage(
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        cache_read_tokens=total_cache_read_tokens,
                        cache_write_tokens=total_cache_write_tokens,
                        input_token_semantics=ModelInputTokenSemantics.UNCACHED_TAIL,
                    )
                    yield ModelCompleted(
                        final_stop_reason,
                        context_items=tuple(all_native_items),
                        response_text="".join(all_text_parts) or None,
                        usage=(usage if usage.has_reported_tokens else None),
                    )
                    return
        except ProviderError:
            raise
        except Exception as error:
            detail = self._safe_detail(str(error))
            raise ProviderError(
                f"Anthropic stream failed: {type(error).__name__}: {detail}"
            ) from error


__all__ = [
    "ANTHROPIC_SERVER_TOOL_CALLER",
    "ANTHROPIC_WEB_FETCH_TOOL_TYPE",
    "ANTHROPIC_WEB_SEARCH_TOOL_TYPE",
    "MAX_SERVER_TOOL_CONTINUATIONS",
    "AnthropicProvider",
]
