from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.domain.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPartKind,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    ToolCall,
)
from neuro_code.domain.model_context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.model_events import (
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.providers.image_references import (
    OPENAI_IMAGE_MEDIA_TYPES,
    OPENAI_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    parse_image_reference,
)
from neuro_code.shared.errors import ConfigurationError, ProviderError

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


class OpenAIResponsesProvider:
    """Streaming Responses API adapter with optional dialect-specific behavior."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "openai-responses",
        dialect: str = "standard",
        context_affinity: str | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        builtin_tools: Sequence[str] = (),
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
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
        unsupported = sorted(set(normalized_builtin_tools) - _BUILTIN_TOOLS)
        if unsupported:
            names = ", ".join(repr(name) for name in unsupported)
            raise ConfigurationError(f"unsupported xAI builtin_tools: {names}")
        if normalized_builtin_tools and dialect != "xai":
            raise ConfigurationError("xAI builtin_tools require dialect 'xai'")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._dialect = dialect
        self._context_affinity = context_affinity
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._builtin_tools = normalized_builtin_tools
        self._transport = transport
        self._http_policy = http_policy or HttpClientPolicy()

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def context_affinity(self) -> str | None:
        return self._context_affinity

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
        return (
            self._dialect == "xai"
            and endpoint.scheme == "https"
            and hostname == "api.x.ai"
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
            and context.source_provider in _NATIVE_SOURCE_PROVIDERS
            and context.source_model is not None
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
            raise ProviderError("Responses API tool results require a tool call id")
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
            request_tools: list[dict[str, Any]] = [{"type": name} for name in self._builtin_tools]
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
        if self._context_affinity is None and not self._is_official_target():
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
                raise ProviderError("Responses API emitted an incomplete function call")
            if call_id in identifiers:
                raise ProviderError(f"Responses API emitted duplicate function call id {call_id!r}")
            try:
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
            except json.JSONDecodeError as error:
                raise ProviderError(
                    f"Responses API function call {name!r} contained invalid JSON arguments"
                ) from error
            if not isinstance(arguments, Mapping):
                raise ProviderError(
                    f"Responses API function call {name!r} arguments must be a JSON object"
                )
            identifiers.add(call_id)
            calls.append(ToolCall(call_id, name, dict(arguments)))
        return tuple(calls)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def _usage(cls, response: Mapping[str, Any]) -> tuple[int | None, int | None]:
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return None, None
        return cls._token_count(usage.get("input_tokens")), cls._token_count(
            usage.get("output_tokens")
        )

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
            raise ProviderError(
                "httpx is required for live model requests; install the project"
            ) from error

        body = self._request_body(context, tools, tool_policy=tool_policy)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        terminal: Mapping[str, Any] | None = None
        streamed_text = False
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
                    raise ProviderError(
                        f"Responses API request failed with HTTP {response.status_code}: {detail}"
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
                            "Responses API returned malformed streaming JSON"
                        ) from error
                    if not isinstance(event, Mapping):
                        raise ProviderError("Responses API emitted a non-object streaming event")
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
                            raise ProviderError("Responses API terminal event omitted its response")
                        terminal = raw_response
                    elif event_type in {"response.failed", "error", "response.error"}:
                        detail = self._safe_detail(self._failure_detail(event))
                        raise ProviderError(f"Responses API failed: {detail}")
        except ProviderError:
            raise
        except Exception as error:
            detail = self._safe_detail(str(error))
            raise ProviderError(
                f"Responses API stream failed: {type(error).__name__}: {detail}"
            ) from error

        if terminal is None:
            raise ProviderError("Responses API stream ended without a terminal response")
        if not isinstance(terminal.get("output"), list):
            raise ProviderError("Responses API terminal response omitted its output items")
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
        if not streamed_text:
            text = self._response_text(terminal)
            if text:
                yield ModelTextDelta(text)
        tool_calls = self._response_tool_calls(terminal)
        for call in tool_calls:
            yield ModelToolCall(call)
        input_tokens, output_tokens = self._usage(terminal)
        context_items = self._preserved_output_items(terminal)
        if self._context_affinity is not None or self._is_official_target():
            context_items = self._with_reasoning_fallback(
                context_items,
                "".join(reasoning_parts),
            )
        yield ModelCompleted(
            self._stop_reason(terminal, tool_calls),
            input_tokens,
            output_tokens,
            context_items,
            self._response_text(terminal),
        )


__all__ = ["OpenAIResponsesProvider"]
