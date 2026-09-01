"""Canonical OpenAI Responses provider infrastructure adapter.

定义规范的 OpenAI Responses Provider 基础设施适配器."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import (
    ModelCapability,
    ModelCapabilitySet,
    ModelToolPolicy,
    resolve_capabilities,
)
from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.conversation.events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
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
from neuro_code.infrastructure.providers.failure_conformance import (
    ProviderFailureProtocol,
    classify_provider_failure,
)
from neuro_code.infrastructure.providers.image_references import (
    OPENAI_IMAGE_MEDIA_TYPES,
    OPENAI_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    parse_image_reference,
)
from neuro_code.shared.errors import (
    ConfigurationError,
    ProviderError,
    ProviderFailureKind,
    ProviderFailureOrigin,
)

_NATIVE_SOURCE_PROVIDERS = frozenset({UPSTREAM_IMPORT_PROVIDER, "xai-responses"})
_OUTPUT_BACKEND_TYPES = {
    "web_search_call": "web_search",
    "custom_tool_call": "x_search",
    "x_search_call": "x_search",
    "code_interpreter_call": "code_interpreter",
}
_DEFAULT_BACKEND_TYPES = {
    "web_search": "web_search_call",
    "x_search": "custom_tool_call",
    "code_interpreter": "code_interpreter_call",
}
_BUILTIN_TOOLS = frozenset(_DEFAULT_BACKEND_TYPES)
_BUILTIN_TOOL_INCLUDES = {
    "web_search": "web_search_call.action.sources",
    "code_interpreter": "code_interpreter_call.outputs",
}
_BACKEND_EVENT_PREFIXES = {
    "response.web_search_call.": "web_search",
    "response.x_search_call.": "x_search",
    "response.code_interpreter_call.": "code_interpreter",
}
_BACKEND_START_PHASES = frozenset({"in_progress", "searching", "interpreting"})
_MAX_VISIBLE_SOURCE_LINES = 32


def _citation_payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Normalize current nested citations and older flat compatibility shapes."""

    for key in ("url_citation", "url_citation_preview"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            return nested
    return raw


def _response_source_attributions(response: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return bounded visible URLs from structured Responses search output."""

    sources: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(raw: object) -> None:
        payload: Mapping[str, Any]
        url: object
        if isinstance(raw, str):
            payload = {}
            url = raw
        elif isinstance(raw, Mapping):
            payload = _citation_payload(raw)
            url = payload.get("url") or payload.get("link")
            if not isinstance(url, str):
                return
        else:
            return
        url = url.strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            return
        if parsed.scheme.casefold() not in {"http", "https"} or parsed.hostname is None:
            return
        key = url.casefold()
        if key in seen or len(sources) >= _MAX_VISIBLE_SOURCE_LINES:
            return
        title = payload.get("title")
        title_text = " ".join(title.split())[:256] if isinstance(title, str) else "Web source"
        seen.add(key)
        sources.append((title_text, url[:2048]))

    for item in response.get("output", ()) if isinstance(response.get("output"), list) else ():
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action")
            if isinstance(action, Mapping) and isinstance(action.get("sources"), list):
                for source in action["sources"]:
                    add(source)
            if isinstance(item.get("sources"), list):
                for source in item["sources"]:
                    add(source)
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping) or not isinstance(part.get("annotations"), list):
                continue
            for annotation in part["annotations"]:
                if isinstance(annotation, Mapping) and annotation.get("type") in {
                    "url_citation",
                    "url_citation_preview",
                }:
                    add(annotation)
    for key in ("sources", "results", "citations"):
        raw_values = response.get(key)
        if isinstance(raw_values, list):
            for raw_value in raw_values:
                add(raw_value)
    return tuple(sources)


def _source_footer(response: Mapping[str, Any]) -> str:
    sources = _response_source_attributions(response)
    if not sources:
        return ""
    lines = ["", "Sources:"]
    lines.extend(f"- {title}: {url}" for title, url in sources)
    return "\n".join(lines)


class OpenAIResponsesProvider:
    """Streaming Responses API adapter with optional dialect-specific behavior.

    提供带可选方言特定行为的流式 Responses API 适配器."""

    @staticmethod
    def implementation_capabilities(
        *,
        dialect: str = "standard",
        builtin_tools: Sequence[str] = (),
    ) -> ModelCapabilitySet:
        """Return capabilities implemented by this Responses configuration."""

        if dialect not in {"standard", "xai"}:
            raise ConfigurationError(f"unsupported Responses dialect: {dialect}")
        if isinstance(builtin_tools, (str, bytes)):
            raise ConfigurationError("xAI builtin_tools must be a sequence of tool names")
        normalized = tuple(builtin_tools)
        if any(not isinstance(name, str) or not name for name in normalized):
            raise ConfigurationError("xAI builtin_tools entries must be non-empty strings")
        allowed = {"web_search"} if dialect == "standard" else _BUILTIN_TOOLS
        unsupported = sorted(set(normalized) - allowed)
        if unsupported:
            names = ", ".join(repr(name) for name in unsupported)
            label = "OpenAI Responses" if dialect == "standard" else "xAI"
            raise ConfigurationError(f"unsupported {label} builtin_tools: {names}")
        hosted = {
            "web_search": ModelCapability.HOSTED_WEB_SEARCH,
            "x_search": ModelCapability.HOSTED_X_SEARCH,
            "code_interpreter": ModelCapability.HOSTED_CODE_INTERPRETER,
        }
        return ModelCapabilitySet.from_supported(
            ModelCapability.FUNCTION_TOOLS,
            ModelCapability.VISION,
            *(hosted[name] for name in normalized),
        )

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "openai-responses",
        dialect: str = "standard",
        context_affinity: str | None = None,
        capabilities: ModelCapabilitySet | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        builtin_tools: Sequence[str] = (),
        builtin_tool_options: Mapping[str, Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
        response_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if dialect not in {"standard", "xai"}:
            raise ConfigurationError(f"unsupported Responses dialect: {dialect}")
        if isinstance(builtin_tools, (str, bytes)):
            raise ConfigurationError("xAI builtin_tools must be a sequence of tool names")
        normalized_builtin_tools = tuple(builtin_tools)
        if any(not isinstance(name, str) or not name for name in normalized_builtin_tools):
            raise ConfigurationError("xAI builtin_tools entries must be non-empty strings")
        if len(set(normalized_builtin_tools)) != len(normalized_builtin_tools):
            raise ConfigurationError("xAI builtin_tools must not contain duplicates")
        allowed = {"web_search"} if dialect == "standard" else _BUILTIN_TOOLS
        unsupported = sorted(set(normalized_builtin_tools) - allowed)
        if unsupported:
            names = ", ".join(repr(name) for name in unsupported)
            label = "OpenAI Responses" if dialect == "standard" else "xAI"
            raise ConfigurationError(f"unsupported {label} builtin_tools: {names}")
        if builtin_tool_options is not None:
            if not isinstance(builtin_tool_options, Mapping):
                raise ConfigurationError("builtin_tool_options must be a mapping")
            unknown_options = sorted(set(builtin_tool_options) - set(normalized_builtin_tools))
            if unknown_options:
                names = ", ".join(repr(name) for name in unknown_options)
                raise ConfigurationError(
                    f"builtin_tool_options contain tools that are not enabled: {names}"
                )
            for name, options in builtin_tool_options.items():
                if not isinstance(name, str) or not name:
                    raise ConfigurationError("builtin_tool_options names must be non-empty strings")
                if not isinstance(options, Mapping):
                    raise ConfigurationError(f"builtin_tool_options[{name!r}] must be a mapping")
        if tool_choice is not None and (
            (not isinstance(tool_choice, str) and not isinstance(tool_choice, Mapping))
            or (isinstance(tool_choice, str) and not tool_choice.strip())
        ):
            raise ConfigurationError("tool_choice must be a non-empty string or mapping")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._dialect = dialect
        self._context_affinity = context_affinity
        upstream = capabilities or ModelCapabilitySet.all_unknown()
        self._capabilities = resolve_capabilities(
            upstream=upstream,
            implementation=self.implementation_capabilities(
                dialect=dialect,
                builtin_tools=normalized_builtin_tools,
            ),
        ).effective
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._builtin_tools = normalized_builtin_tools
        self._builtin_tool_options = {
            name: dict(options) for name, options in (builtin_tool_options or {}).items()
        }
        self._tool_choice = dict(tool_choice) if isinstance(tool_choice, Mapping) else tool_choice
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

    def _safe_detail(self, detail: str) -> str:
        return self._http_policy.redact(detail, self._api_key)

    @property
    def _endpoint(self) -> str:
        if self._base_url.endswith("/responses"):
            return self._base_url
        return f"{self._base_url}/responses"

    def _is_official_target(self) -> bool:
        try:
            endpoint = urlsplit(self._base_url)
            hostname = endpoint.hostname
            username = endpoint.username
            password = endpoint.password
            port = endpoint.port
        except ValueError:
            return False
        expected_hostname = "api.x.ai" if self._dialect == "xai" else "api.openai.com"
        return (
            endpoint.scheme == "https"
            and hostname == expected_hostname
            and username is None
            and password is None
            and port in {None, 443}
            and not endpoint.query
            and not endpoint.fragment
        )

    def _has_native_affinity(self, context: ModelContext) -> bool:
        if (
            self._context_affinity is not None
            and context.source_context_affinity == self._context_affinity
        ):
            return True
        return bool(
            context.source_context_affinity is None
            and self._is_official_target()
            and (
                context.source_provider in _NATIVE_SOURCE_PROVIDERS
                or context.source_provider == "openai-responses"
            )
            and context.source_model is not None
        ) or bool(
            self._builtin_tools
            and context.source_provider == self._provider_name
            and context.source_model == self._model
        )

    @staticmethod
    def _image_content(message: Message) -> str | list[dict[str, Any]]:
        if not message.content_parts or not any(
            part.kind is ContentPartKind.IMAGE for part in message.content_parts
        ):
            return message.content

        blocks: list[dict[str, Any]] = []
        for part in message.content_parts:
            if part.kind is ContentPartKind.TEXT:
                assert part.text is not None
                blocks.append({"type": "input_text", "text": part.text})
                continue
            if part.kind is not ContentPartKind.IMAGE:
                blocks.append({"type": "input_text", "text": part.model_placeholder})
                continue
            assert part.url is not None
            reference = parse_image_reference(
                part.url,
                allowed_media_types=OPENAI_IMAGE_MEDIA_TYPES,
                max_inline_bytes=OPENAI_MAX_INLINE_IMAGE_BYTES,
            )
            if reference is None:
                blocks.append({"type": "input_text", "text": IMAGE_MODEL_PLACEHOLDER})
                continue
            url = (
                reference.data_uri if isinstance(reference, InlineImageReference) else reference.url
            )
            blocks.append({"type": "input_image", "image_url": url, "detail": "auto"})
        return blocks

    @classmethod
    def _message_input_items(cls, message: Message) -> list[dict[str, Any]]:
        if message.role in {Role.SYSTEM, Role.USER}:
            content = (
                cls._image_content(message)
                if message.role is Role.USER
                else message.model_content()
            )
            return [{"type": "message", "role": message.role.value, "content": content}]

        if message.role is Role.ASSISTANT:
            items: list[dict[str, Any]] = []
            content = message.model_content()
            if content:
                items.append({"type": "message", "role": "assistant", "content": content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
                    }
                )
            return items

        if not message.tool_call_id:
            raise ProviderError.protocol("Responses API tool results require a tool call id")
        output: str | list[dict[str, Any]] = cls._image_content(message)
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": output,
            }
        ]

    @staticmethod
    def _text_blocks(value: object, expected_type: str) -> list[dict[str, str]] | None:
        if not isinstance(value, (list, tuple)):
            return None
        blocks: list[dict[str, str]] = []
        for block in value:
            if (
                not isinstance(block, Mapping)
                or block.get("type") != expected_type
                or not isinstance(block.get("text"), str)
            ):
                return None
            blocks.append({"type": expected_type, "text": block["text"]})
        return blocks

    @classmethod
    def _reasoning_input(cls, item: PreservedContextItem) -> dict[str, Any] | None:
        payload = item.payload
        identifier = payload.get("id", "")
        summary = cls._text_blocks(payload.get("summary", ()), "summary_text")
        if not isinstance(identifier, str) or summary is None:
            return None
        result: dict[str, Any] = {
            "type": "reasoning",
            "id": identifier,
            "summary": summary,
        }
        content = payload.get("content")
        if content is not None:
            content_blocks = cls._text_blocks(content, "reasoning_text")
            if content_blocks is None:
                return None
            result["content"] = content_blocks
        encrypted_content = payload.get("encrypted_content")
        if encrypted_content is not None:
            if not isinstance(encrypted_content, str):
                return None
            result["encrypted_content"] = encrypted_content
        return result

    @staticmethod
    def _backend_input(item: PreservedContextItem) -> dict[str, Any] | None:
        payload = item.to_dict()
        kind = payload.get("kind")
        if not isinstance(kind, dict):
            return None
        native = dict(kind)
        tool_type = native.pop("tool_type", None)
        if not isinstance(tool_type, str) or tool_type not in _DEFAULT_BACKEND_TYPES:
            return None
        native_type = native.pop("native_type", _DEFAULT_BACKEND_TYPES[tool_type])
        if (
            native_type not in _OUTPUT_BACKEND_TYPES
            or _OUTPUT_BACKEND_TYPES[native_type] != tool_type
        ):
            return None
        identifier = native.get("id")
        if not isinstance(identifier, str) or not identifier:
            return None
        native["type"] = native_type
        return native

    def _input_items(self, context: ModelContext) -> list[dict[str, Any]]:
        native_affinity = self._has_native_affinity(context)
        items: list[dict[str, Any]] = []
        for item in context.items:
            if isinstance(item, Message):
                items.extend(self._message_input_items(item))
                continue
            if not native_affinity:
                continue
            native = (
                self._reasoning_input(item)
                if item.kind is ContextItemKind.REASONING
                else self._backend_input(item)
            )
            if native is not None:
                items.append(native)
        return items

    def _request_body(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> dict[str, Any]:
        includes: list[str] = []
        if self._dialect == "xai" or self._context_affinity is not None:
            includes.append("reasoning.encrypted_content")
        if tool_policy is ModelToolPolicy.ALLOWED:
            includes.extend(
                _BUILTIN_TOOL_INCLUDES[name]
                for name in self._builtin_tools
                if name in _BUILTIN_TOOL_INCLUDES
            )
        body: dict[str, Any] = {
            "model": self._model,
            "input": self._input_items(context),
            "max_output_tokens": self._max_output_tokens,
            "store": False,
            "stream": True,
        }
        if self._dialect == "xai" or self._context_affinity is not None:
            body["reasoning"] = {"summary": "concise"}
        if includes:
            body["include"] = includes
        if tool_policy is ModelToolPolicy.ALLOWED:
            request_tools: list[dict[str, Any]] = []
            for name in self._builtin_tools:
                tool: dict[str, Any] = {"type": name}
                options = self._builtin_tool_options.get(name)
                if options:
                    tool.update(options)
                request_tools.append(tool)
            builtin_names = set(self._builtin_tools)
            request_tools.extend(
                [
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.input_schema),
                    }
                    for tool in tools
                    if tool.name not in builtin_names
                ]
            )
            if request_tools:
                body["tools"] = request_tools
                if self._tool_choice is not None:
                    body["tool_choice"] = (
                        dict(self._tool_choice)
                        if isinstance(self._tool_choice, Mapping)
                        else self._tool_choice
                    )
        return body

    @staticmethod
    def _response_output(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        output = response.get("output")
        if not isinstance(output, list):
            return []
        return [item for item in output if isinstance(item, Mapping)]

    @classmethod
    def _response_text(cls, response: Mapping[str, Any]) -> str:
        texts: list[str] = []
        for item in cls._response_output(response):
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, Mapping)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                    and part["text"]
                ):
                    texts.append(part["text"])
        return "\n".join(texts)

    @classmethod
    def _response_reasoning(cls, response: Mapping[str, Any]) -> str:
        texts: list[str] = []
        for item in cls._response_output(response):
            if item.get("type") != "reasoning":
                continue
            content = cls._text_blocks(item.get("content", ()), "reasoning_text")
            summary = cls._text_blocks(item.get("summary", ()), "summary_text")
            for blocks in (summary, content):
                if blocks is not None:
                    texts.extend(block["text"] for block in blocks if block["text"])
        return "\n".join(texts)

    @classmethod
    def _with_reasoning_fallback(
        cls,
        items: tuple[PreservedContextItem, ...],
        text: str,
    ) -> tuple[PreservedContextItem, ...]:
        if not text:
            return items
        for item in items:
            if item.kind is not ContextItemKind.REASONING:
                continue
            if cls._text_blocks(
                item.payload.get("summary", ()), "summary_text"
            ) or cls._text_blocks(item.payload.get("content", ()), "reasoning_text"):
                return items
        mutable = list(items)
        for index, item in enumerate(mutable):
            if item.kind is not ContextItemKind.REASONING:
                continue
            payload = item.to_dict()
            payload["summary"] = [{"type": "summary_text", "text": text}]
            mutable[index] = PreservedContextItem(ContextItemKind.REASONING, payload)
            return tuple(mutable)
        mutable.append(
            PreservedContextItem(
                ContextItemKind.REASONING,
                {
                    "type": "reasoning",
                    "id": "",
                    "summary": [{"type": "summary_text", "text": text}],
                },
            )
        )
        return tuple(mutable)

    def _preserved_output_items(
        self,
        response: Mapping[str, Any],
    ) -> tuple[PreservedContextItem, ...]:
        if (
            self._context_affinity is None
            and not self._is_official_target()
            and not self._builtin_tools
        ):
            return ()
        preserved: list[PreservedContextItem] = []
        for item in self._response_output(response):
            item_type = item.get("type")
            if item_type == "reasoning":
                identifier = item.get("id", "")
                summary = self._text_blocks(item.get("summary", ()), "summary_text")
                content = item.get("content")
                if not isinstance(identifier, str) or summary is None:
                    continue
                if content is not None and self._text_blocks(content, "reasoning_text") is None:
                    continue
                for field in ("encrypted_content", "status"):
                    value = item.get(field)
                    if value is not None and not isinstance(value, str):
                        break
                else:
                    payload = dict(item)
                    payload["id"] = identifier
                    payload["summary"] = summary
                    if content is not None:
                        payload["content"] = self._text_blocks(content, "reasoning_text")
                    preserved.append(PreservedContextItem(ContextItemKind.REASONING, payload))
                continue
            tool_type = _OUTPUT_BACKEND_TYPES.get(item_type) if isinstance(item_type, str) else None
            if tool_type is None:
                continue
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier:
                continue
            kind = {key: value for key, value in item.items() if key != "type"}
            kind["tool_type"] = tool_type
            if item_type != _DEFAULT_BACKEND_TYPES[tool_type]:
                kind["native_type"] = item_type
            preserved.append(
                PreservedContextItem(
                    ContextItemKind.BACKEND_TOOL_CALL,
                    {"type": "backend_tool_call", "kind": kind},
                )
            )
        return tuple(preserved)

    @classmethod
    def _response_tool_calls(cls, response: Mapping[str, Any]) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        identifiers: set[str] = set()
        for item in cls._response_output(response):
            if item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            name = item.get("name")
            raw_arguments = item.get("arguments", "{}")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise ProviderError.protocol("Responses API emitted an incomplete function call")
            if call_id in identifiers:
                raise ProviderError.protocol(
                    f"Responses API emitted duplicate function call id {call_id!r}"
                )
            try:
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
            except json.JSONDecodeError as error:
                raise ProviderError.protocol(
                    f"Responses API function call {name!r} contained invalid JSON arguments"
                ) from error
            if not isinstance(arguments, Mapping):
                raise ProviderError.protocol(
                    f"Responses API function call {name!r} arguments must be a JSON object"
                )
            identifiers.add(call_id)
            calls.append(ToolCall(call_id, name, dict(arguments)))
        return tuple(calls)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def _usage(cls, response: Mapping[str, Any]) -> ModelUsage | None:
        """Normalize Responses usage without inferring unreported cache values.

        Responses reports cached input in ``input_tokens_details.cached_tokens``.
        Keep the raw total input/output accounting and expose the detail only
        when this API response includes it.

        规范化 Responses 用量,不推断未上报的缓存值。Responses 在
        ``input_tokens_details.cached_tokens`` 中上报缓存输入。
        """

        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return None
        details = usage.get("input_tokens_details")
        detail_usage: Mapping[str, object] = details if isinstance(details, Mapping) else {}
        cache_read_tokens = cls._token_count(detail_usage.get("cached_tokens"))
        if cache_read_tokens is None:
            cache_read_tokens = cls._token_count(detail_usage.get("cache_read_tokens"))
        cache_miss_tokens = cls._token_count(detail_usage.get("cache_miss_tokens"))
        if cache_miss_tokens is None:
            cache_miss_tokens = cls._token_count(detail_usage.get("prompt_cache_miss_tokens"))
        normalized = ModelUsage(
            input_tokens=cls._token_count(usage.get("input_tokens")),
            output_tokens=cls._token_count(usage.get("output_tokens")),
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cls._token_count(detail_usage.get("cache_write_tokens")),
            cache_miss_tokens=cache_miss_tokens,
        )
        return normalized if normalized.has_reported_tokens else None

    @staticmethod
    def _stop_reason(response: Mapping[str, Any], tool_calls: Sequence[ToolCall]) -> str:
        if tool_calls:
            return "tool_calls"
        if response.get("status") == "incomplete":
            details = response.get("incomplete_details")
            if isinstance(details, Mapping):
                reason = details.get("reason")
                if isinstance(reason, str):
                    return reason
            return "incomplete"
        return "stop"

    @staticmethod
    def _backend_output_identity(item: Mapping[str, Any]) -> tuple[str, str] | None:
        item_type = item.get("type")
        name = _OUTPUT_BACKEND_TYPES.get(item_type) if isinstance(item_type, str) else None
        identifier = item.get("id")
        if name is None or not isinstance(identifier, str) or not identifier:
            return None
        return identifier, name

    @classmethod
    def _stream_backend_transition(
        cls,
        event: Mapping[str, Any],
    ) -> tuple[str, str, bool] | None:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return None

        if event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            if not isinstance(item, Mapping):
                return None
            identity = cls._backend_output_identity(item)
            if identity is None:
                return None
            return (*identity, event_type == "response.output_item.done")

        if event_type in {
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
        }:
            identifier = event.get("item_id")
            if isinstance(identifier, str) and identifier:
                return identifier, "x_search", False
            return None

        for prefix, name in _BACKEND_EVENT_PREFIXES.items():
            if not event_type.startswith(prefix):
                continue
            phase = event_type.removeprefix(prefix)
            if phase not in _BACKEND_START_PHASES and phase != "completed":
                return None
            identifier = event.get("item_id")
            if isinstance(identifier, str) and identifier:
                return identifier, name, phase == "completed"
            return None
        return None

    @staticmethod
    def _backend_lifecycle_events(
        call_id: str,
        name: str,
        completed: bool,
        started_calls: set[tuple[str, str]],
        completed_calls: set[tuple[str, str]],
    ) -> tuple[ModelBackendToolStarted | ModelBackendToolCompleted, ...]:
        key = (call_id, name)
        if key in completed_calls:
            return ()
        events: list[ModelBackendToolStarted | ModelBackendToolCompleted] = []
        if key not in started_calls:
            started_calls.add(key)
            events.append(ModelBackendToolStarted(call_id, name))
        if completed:
            completed_calls.add(key)
            events.append(ModelBackendToolCompleted(call_id, name))
        return tuple(events)

    @staticmethod
    def _failure_detail(event: Mapping[str, Any]) -> str:
        response = event.get("response")
        if isinstance(response, Mapping):
            error = response.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
                message = error.get("message")
                if isinstance(message, str):
                    return f"{code}: {message}" if isinstance(code, str) else message
        error = event.get("error")
        if isinstance(error, Mapping):
            error_message = error.get("message")
            if isinstance(error_message, str):
                return error_message
        message = event.get("message")
        return message if isinstance(message, str) else "unknown Responses API failure"

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
            raise ProviderError.local(
                "httpx is required for live model requests; install the project"
            ) from error

        body = self._request_body(context, tools, tool_policy=tool_policy)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        terminal: Mapping[str, Any] | None = None
        streamed_text = False
        streamed_text_parts: list[str] = []
        streamed_reasoning = False
        reasoning_parts: list[str] = []
        started_backend_calls: set[tuple[str, str]] = set()
        completed_backend_calls: set[tuple[str, str]] = set()
        try:
            timeout = httpx.Timeout(self._timeout_seconds)
            client_options = self._http_policy.client_options(
                timeout=timeout,
                transport=self._transport,
            )
            async with (
                httpx.AsyncClient(**client_options) as client,
                client.stream("POST", self._endpoint, headers=headers, json=body) as response,
            ):
                if response.status_code >= 400:
                    detail = self._safe_detail((await response.aread()).decode("utf-8", "replace"))
                    raise ProviderError.from_http(
                        response.status_code,
                        detail,
                        headers=response.headers,
                        failure_kind=classify_provider_failure(
                            ProviderFailureProtocol.OPENAI_RESPONSES,
                            detail,
                        ),
                        provider=self._provider_name,
                        model=self._model,
                        redaction_values=(self._api_key, *self._http_policy.redaction_values),
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
                        raise ProviderError.protocol(
                            "Responses API returned malformed streaming JSON",
                            provider=self._provider_name,
                            model=self._model,
                        ) from error
                    if not isinstance(event, Mapping):
                        raise ProviderError.protocol(
                            "Responses API emitted a non-object streaming event",
                            provider=self._provider_name,
                            model=self._model,
                        )
                    event_type = event.get("type")
                    backend_transition = self._stream_backend_transition(event)
                    if backend_transition is not None:
                        call_id, name, completed = backend_transition
                        for lifecycle_event in self._backend_lifecycle_events(
                            call_id,
                            name,
                            completed,
                            started_backend_calls,
                            completed_backend_calls,
                        ):
                            yield lifecycle_event
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            streamed_text = True
                            streamed_text_parts.append(delta)
                            yield ModelTextDelta(delta)
                    elif event_type in {
                        "response.reasoning_summary_text.delta",
                        "response.reasoning_text.delta",
                    }:
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            streamed_reasoning = True
                            reasoning_parts.append(delta)
                            yield ModelReasoningDelta(delta)
                    elif event_type in {"response.completed", "response.incomplete"}:
                        raw_response = event.get("response")
                        if not isinstance(raw_response, Mapping):
                            raise ProviderError.protocol(
                                "Responses API terminal event omitted its response",
                                provider=self._provider_name,
                                model=self._model,
                            )
                        terminal = raw_response
                    elif event_type in {"response.failed", "error", "response.error"}:
                        detail = self._safe_detail(self._failure_detail(event))
                        envelope = json.dumps(event, ensure_ascii=False)
                        raise ProviderError.classified(
                            classify_provider_failure(
                                ProviderFailureProtocol.OPENAI_RESPONSES,
                                envelope,
                            )
                            or ProviderFailureKind.UNKNOWN,
                            f"Responses API failed: {detail}",
                            provider=self._provider_name,
                            model=self._model,
                            origin=ProviderFailureOrigin.PROVIDER,
                            redaction_values=(self._api_key, *self._http_policy.redaction_values),
                        )
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError.from_runtime(
                error,
                provider=self._provider_name,
                model=self._model,
                redaction_values=(self._api_key, *self._http_policy.redaction_values),
                prefix="Responses API stream failed",
            ) from error

        if terminal is None:
            raise ProviderError.protocol(
                "Responses API stream ended without a terminal response",
                provider=self._provider_name,
                model=self._model,
            )
        if not isinstance(terminal.get("output"), list):
            raise ProviderError.protocol(
                "Responses API terminal response omitted its output items",
                provider=self._provider_name,
                model=self._model,
            )
        if self._response_observer is not None:
            try:
                self._response_observer(terminal)
            except Exception as error:
                raise ProviderError.local(
                    f"Responses API response observer failed: {type(error).__name__}"
                ) from error
        for item in self._response_output(terminal):
            identity = self._backend_output_identity(item)
            if identity is None:
                continue
            call_id, name = identity
            for lifecycle_event in self._backend_lifecycle_events(
                call_id,
                name,
                True,
                started_backend_calls,
                completed_backend_calls,
            ):
                yield lifecycle_event
        if not streamed_reasoning:
            reasoning = self._response_reasoning(terminal)
            if reasoning:
                yield ModelReasoningDelta(reasoning)
        final_response_text = self._response_text(terminal) or "".join(streamed_text_parts)
        source_footer = _source_footer(terminal) if "web_search" in self._builtin_tools else ""
        if source_footer:
            if streamed_text:
                yield ModelTextDelta(source_footer)
            else:
                yield ModelTextDelta(final_response_text + source_footer)
            final_response_text += source_footer
        elif not streamed_text and final_response_text:
            yield ModelTextDelta(final_response_text)
        tool_calls = self._response_tool_calls(terminal)
        for call in tool_calls:
            yield ModelToolCall(call)
        usage = self._usage(terminal)
        context_items = self._preserved_output_items(terminal)
        if self._context_affinity is not None or self._is_official_target():
            context_items = self._with_reasoning_fallback(
                context_items,
                "".join(reasoning_parts),
            )
        yield ModelCompleted(
            self._stop_reason(terminal, tool_calls),
            context_items=context_items,
            response_text=final_response_text,
            usage=usage,
        )


__all__ = ["OpenAIResponsesProvider"]
