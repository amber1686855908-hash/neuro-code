"""Canonical OpenAI-compatible Chat Completions provider adapter.

定义规范的 OpenAI 兼容 Chat Completions Provider 适配器."""

from __future__ import annotations

import html
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
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
    OPENAI_IMAGE_MEDIA_TYPES,
    OPENAI_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    parse_image_reference,
)
from neuro_code.shared.errors import ConfigurationError, ProviderError

BACKEND_SUMMARY_FIELD_CHARS = 1000
CODE_SUMMARY_CHARS = 100


@dataclass(slots=True)
class _ToolCallBuffer:
    identifier: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class _DeepSeekDSMLStreamParser:
    """Parse DeepSeek V4's streamed DSML tool-call dialect.

    DeepSeek can encode tool calls in ``content`` rather than the standard
    ``delta.tool_calls`` field.  Keeping this parser at the provider boundary
    prevents the proprietary wire format from leaking into runtime or UI
    layers, and allows incomplete protocol fragments to remain buffered across
    SSE chunks.
    """

    _buffer: str = ""
    _active_end: str | None = None
    _next_tool_id: int = 1

    _FULLWIDTH_TOKEN = "\uff5cDSML\uff5c"
    _OPEN_MARKERS = ("<|DSML|tool_calls>", f"<{_FULLWIDTH_TOKEN}tool_calls>")
    _DSML_PREFIXES = ("<|DSML|", f"<{_FULLWIDTH_TOKEN}")
    _INVOKE_PATTERN = re.compile(
        r"<(?P<token>\|DSML\||\uFF5CDSML\uFF5C)invoke(?P<attributes>[^>]*)>"
        r"(?P<body>.*?)</(?P=token)invoke>",
        re.DOTALL,
    )
    _PARAMETER_PATTERN = re.compile(
        r"<(?P<token>\|DSML\||\uFF5CDSML\uFF5C)parameter(?P<attributes>[^>]*)>"
        r"(?P<body>.*?)</(?P=token)parameter>",
        re.DOTALL,
    )

    def feed(self, text: str) -> tuple[tuple[str, ...], tuple[ToolCall, ...]]:
        self._buffer += text
        return self._drain(final=False)

    def finish(self) -> tuple[tuple[str, ...], tuple[ToolCall, ...]]:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> tuple[tuple[str, ...], tuple[ToolCall, ...]]:
        visible: list[str] = []
        calls: list[ToolCall] = []
        while True:
            if self._active_end is not None:
                end_index = self._buffer.find(self._active_end)
                if end_index < 0:
                    if final:
                        raise ProviderError("provider returned incomplete DeepSeek DSML tool call")
                    return tuple(visible), tuple(calls)
                block = self._buffer[:end_index]
                self._buffer = self._buffer[end_index + len(self._active_end) :]
                calls.extend(self._parse_block(block))
                self._active_end = None
                continue

            marker = self._find_open_marker()
            if marker is not None:
                index, opening, closing = marker
                if index:
                    visible.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(opening) :]
                self._active_end = closing
                continue

            if final:
                if any(prefix in self._buffer for prefix in self._DSML_PREFIXES):
                    raise ProviderError("provider returned malformed DeepSeek DSML content")
                if self._buffer:
                    visible.append(self._buffer)
                    self._buffer = ""
                return tuple(visible), tuple(calls)

            keep = self._partial_marker_suffix_length()
            for prefix in self._DSML_PREFIXES:
                index = self._buffer.find(prefix)
                if index >= 0 and index < len(self._buffer) - keep:
                    raise ProviderError("provider returned malformed DeepSeek DSML content")
            if keep:
                if len(self._buffer) > keep:
                    visible.append(self._buffer[:-keep])
                    self._buffer = self._buffer[-keep:]
                return tuple(visible), tuple(calls)
            if self._buffer:
                visible.append(self._buffer)
                self._buffer = ""
            return tuple(visible), tuple(calls)

    def _find_open_marker(self) -> tuple[int, str, str] | None:
        matches = [
            (self._buffer.find(marker), marker)
            for marker in self._OPEN_MARKERS
            if self._buffer.find(marker) >= 0
        ]
        if not matches:
            return None
        index, opening = min(matches, key=lambda item: item[0])
        token = "|DSML|" if opening.startswith("<|DSML|") else self._FULLWIDTH_TOKEN
        return index, opening, f"</{token}tool_calls>"

    def _partial_marker_suffix_length(self) -> int:
        longest = 0
        for marker in self._OPEN_MARKERS:
            for size in range(1, min(len(marker), len(self._buffer)) + 1):
                if self._buffer.endswith(marker[:size]):
                    longest = max(longest, size)
        for prefix in self._DSML_PREFIXES:
            for size in range(1, min(len(prefix), len(self._buffer)) + 1):
                if self._buffer.endswith(prefix[:size]):
                    longest = max(longest, size)
        return longest

    def _parse_block(self, block: str) -> list[ToolCall]:
        calls: list[ToolCall] = []
        cursor = 0
        for match in self._INVOKE_PATTERN.finditer(block):
            if block[cursor : match.start()].strip():
                raise ProviderError("provider returned malformed DeepSeek DSML tool call")
            attributes = match.group("attributes")
            name_match = re.search(r'\bname\s*=\s*"([^"]+)"', attributes)
            if name_match is None:
                raise ProviderError("provider returned a DeepSeek DSML call without a name")
            arguments: dict[str, object] = {}
            body = match.group("body")
            parameter_cursor = 0
            for parameter in self._PARAMETER_PATTERN.finditer(body):
                if body[parameter_cursor : parameter.start()].strip():
                    raise ProviderError("provider returned malformed DeepSeek DSML parameters")
                parameter_attributes = parameter.group("attributes")
                parameter_name = re.search(r'\bname\s*=\s*"([^"]+)"', parameter_attributes)
                string_flag = re.search(r'\bstring\s*=\s*"(true|false)"', parameter_attributes)
                if parameter_name is None or string_flag is None:
                    raise ProviderError("provider returned malformed DeepSeek DSML parameter")
                key = html.unescape(parameter_name.group(1))
                if key in arguments:
                    raise ProviderError("provider returned duplicate DeepSeek DSML parameter")
                raw_value = html.unescape(parameter.group("body"))
                if string_flag.group(1) == "true":
                    arguments[key] = raw_value
                else:
                    try:
                        arguments[key] = json.loads(raw_value)
                    except json.JSONDecodeError as error:
                        raise ProviderError(
                            "provider returned invalid JSON in a DeepSeek DSML parameter"
                        ) from error
                parameter_cursor = parameter.end()
            if body[parameter_cursor:].strip():
                raise ProviderError("provider returned malformed DeepSeek DSML parameters")
            calls.append(
                ToolCall(
                    f"dsml-{self._next_tool_id}",
                    html.unescape(name_match.group(1)),
                    arguments,
                )
            )
            self._next_tool_id += 1
            cursor = match.end()
        if not calls or block[cursor:].strip():
            raise ProviderError("provider returned malformed DeepSeek DSML tool call")
        return calls


class OpenAICompatibleProvider:
    """Streaming Chat Completions adapter.

    The dependency is imported lazily so configuration inspection and offline
    tests remain usable before optional/runtime dependencies are installed.

    提供流式 Chat Completions 适配器. 依赖项延迟导入,使配置检查和离线测试在可选运行时依赖未安装时仍可用.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "openai-compatible",
        dialect: str = "standard",
        context_affinity: str | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
    ) -> None:
        if dialect not in {"standard", "deepseek-v4"}:
            raise ConfigurationError(f"unsupported OpenAI-compatible dialect: {dialect}")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._dialect = dialect
        self._context_affinity = context_affinity
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
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

    def _uses_deepseek_dsml(self) -> bool:
        """Return whether the configured dialect emits DeepSeek DSML.

        返回当前显式配置的方言是否会发出 DeepSeek DSML.
        """

        return self._dialect == "deepseek-v4"

    @staticmethod
    def _user_content(message: Message) -> str | list[dict[str, Any]]:
        if not message.content_parts or not any(
            part.kind is ContentPartKind.IMAGE for part in message.content_parts
        ):
            return message.content

        blocks: list[dict[str, Any]] = []
        for part in message.content_parts:
            if part.kind is ContentPartKind.TEXT:
                assert part.text is not None
                blocks.append({"type": "text", "text": part.text})
                continue
            assert part.url is not None
            reference = parse_image_reference(
                part.url,
                allowed_media_types=OPENAI_IMAGE_MEDIA_TYPES,
                max_inline_bytes=OPENAI_MAX_INLINE_IMAGE_BYTES,
            )
            if reference is None:
                blocks.append({"type": "text", "text": IMAGE_MODEL_PLACEHOLDER})
                continue
            url = (
                reference.data_uri if isinstance(reference, InlineImageReference) else reference.url
            )
            blocks.append({"type": "image_url", "image_url": {"url": url}})
        return blocks

    def _message_payload(self, message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": message.role.value,
            "content": (
                self._user_content(message)
                if message.role is Role.USER
                else message.model_content()
            ),
        }
        if message.role is Role.TOOL:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
            if message.reasoning_content is not None:
                payload["reasoning_content"] = message.reasoning_content
        return payload

    def _has_xai_import_affinity(self, context: ModelContext) -> bool:
        if context.source_provider != UPSTREAM_IMPORT_PROVIDER or context.source_model is None:
            return False
        try:
            endpoint = urlsplit(self._base_url)
            hostname = endpoint.hostname
            username = endpoint.username
            password = endpoint.password
            port = endpoint.port
        except ValueError:
            return False
        return (
            endpoint.scheme == "https"
            and hostname == "api.x.ai"
            and username is None
            and password is None
            and port in {None, 443}
            and not endpoint.query
            and not endpoint.fragment
        )

    @staticmethod
    def _reasoning_text(item: PreservedContextItem) -> str:
        for field, block_type in (("content", "reasoning_text"), ("summary", "summary_text")):
            blocks = item.payload.get(field)
            if not isinstance(blocks, tuple):
                continue
            texts: list[str] = []
            for block in blocks:
                if not isinstance(block, Mapping) or block.get("type") != block_type:
                    continue
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
            if texts:
                return "\n".join(texts)
        return ""

    @staticmethod
    def _backend_tool_summary(item: PreservedContextItem) -> str | None:
        def preview(value: object, *, fallback: str = "?", limit: int) -> str:
            text = value if isinstance(value, str) else fallback
            return text if len(text) <= limit else f"{text[:limit]}..."

        kind = item.payload.get("kind")
        if not isinstance(kind, Mapping):
            return None
        tool_type = kind.get("tool_type")
        if tool_type == "web_search":
            action = kind.get("action")
            if not isinstance(action, Mapping):
                return "[backend web_search]"
            action_type = action.get("type")
            if action_type == "search":
                query = preview(action.get("query"), limit=BACKEND_SUMMARY_FIELD_CHARS)
                return f"[backend web_search] search: {query}"
            if action_type == "open_page":
                url = preview(action.get("url"), limit=BACKEND_SUMMARY_FIELD_CHARS)
                return f"[backend web_search] open: {url}"
            if action_type in {"find", "find_in_page"}:
                pattern = preview(action.get("pattern"), limit=BACKEND_SUMMARY_FIELD_CHARS)
                url = preview(action.get("url"), limit=BACKEND_SUMMARY_FIELD_CHARS)
                return f'[backend web_search] find "{pattern}" in {url}'
            return "[backend web_search]"
        if tool_type == "x_search":
            name = preview(kind.get("name"), limit=BACKEND_SUMMARY_FIELD_CHARS)
            raw_input = preview(kind.get("input"), fallback="", limit=BACKEND_SUMMARY_FIELD_CHARS)
            return f"[backend x_search] {name}({raw_input})"
        if tool_type == "code_interpreter":
            code = kind.get("code")
            code_preview = preview(code, fallback="", limit=CODE_SUMMARY_CHARS)
            return f"[backend code_interpreter] {code_preview}"
        return None

    def _message_payloads(self, context: ModelContext) -> list[dict[str, Any]]:
        if not self._has_xai_import_affinity(context):
            return [self._message_payload(message) for message in context.messages]

        payloads: list[dict[str, Any]] = []
        pending_reasoning: list[str] = []
        for item in context.items:
            if isinstance(item, PreservedContextItem):
                if item.kind is ContextItemKind.REASONING:
                    text = self._reasoning_text(item)
                    if text:
                        pending_reasoning.append(text)
                else:
                    summary = self._backend_tool_summary(item)
                    if summary is not None:
                        payloads.append({"role": Role.ASSISTANT.value, "content": summary})
                continue

            payload = self._message_payload(item)
            if item.role is Role.ASSISTANT:
                if pending_reasoning:
                    existing = payload.get("reasoning_content")
                    reasoning = list(pending_reasoning)
                    if isinstance(existing, str) and existing and existing not in reasoning:
                        reasoning.append(existing)
                    payload["reasoning_content"] = "\n".join(reasoning)
                    pending_reasoning.clear()
            else:
                pending_reasoning.clear()
            payloads.append(payload)
        return payloads

    def _request_body(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": self._message_payloads(context),
            "max_tokens": self._max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tool_policy is ModelToolPolicy.ALLOWED and tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.input_schema),
                    },
                }
                for tool in tools
            ]
        return body

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
        buffers: dict[int, _ToolCallBuffer] = {}
        dsml_parser = _DeepSeekDSMLStreamParser() if self._uses_deepseek_dsml() else None
        stop_reason = "stop"
        input_tokens: int | None = None
        output_tokens: int | None = None
        endpoint = f"{self._base_url}/chat/completions"
        try:
            timeout = httpx.Timeout(self._timeout_seconds)
            client_options = self._http_policy.client_options(
                timeout=timeout,
                transport=self._transport,
            )
            async with (
                httpx.AsyncClient(**client_options) as client,
                client.stream("POST", endpoint, headers=headers, json=body) as response,
            ):
                if response.status_code >= 400:
                    detail = self._safe_detail((await response.aread()).decode("utf-8", "replace"))
                    raise ProviderError(
                        f"provider request failed with HTTP {response.status_code}: {detail}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError as error:
                        raise ProviderError("provider returned malformed streaming JSON") from error
                    usage = chunk.get("usage")
                    if isinstance(usage, dict):
                        if isinstance(usage.get("prompt_tokens"), int):
                            input_tokens = usage["prompt_tokens"]
                        if isinstance(usage.get("completion_tokens"), int):
                            output_tokens = usage["completion_tokens"]
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    if isinstance(choice.get("finish_reason"), str):
                        stop_reason = choice["finish_reason"]
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        if dsml_parser is None:
                            yield ModelTextDelta(content)
                        else:
                            visible, dsml_calls = dsml_parser.feed(content)
                            for text in visible:
                                if text:
                                    yield ModelTextDelta(text)
                            for call in dsml_calls:
                                yield ModelToolCall(call)
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        yield ModelReasoningDelta(reasoning)
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        self._accumulate_tool_calls(tool_calls, buffers)
        except ProviderError:
            raise
        except Exception as error:
            # Deliberately exclude request headers and body from the exception.
            detail = self._safe_detail(str(error))
            raise ProviderError(
                f"provider stream failed: {type(error).__name__}: {detail}"
            ) from error

        if dsml_parser is not None:
            visible, dsml_calls = dsml_parser.finish()
            for text in visible:
                if text:
                    yield ModelTextDelta(text)
            for call in dsml_calls:
                yield ModelToolCall(call)

        for index in sorted(buffers):
            buffer = buffers[index]
            try:
                arguments = json.loads(buffer.arguments or "{}")
            except json.JSONDecodeError as error:
                raise ProviderError(
                    f"tool call {buffer.name!r} contained invalid JSON arguments"
                ) from error
            if not isinstance(arguments, dict):
                raise ProviderError(f"tool call {buffer.name!r} arguments must be a JSON object")
            if not buffer.identifier or not buffer.name:
                raise ProviderError("provider emitted an incomplete tool call")
            yield ModelToolCall(ToolCall(buffer.identifier, buffer.name, arguments))
        yield ModelCompleted(stop_reason, input_tokens, output_tokens)

    @staticmethod
    def _accumulate_tool_calls(
        chunks: list[object],
        buffers: dict[int, _ToolCallBuffer],
    ) -> None:
        for raw in chunks:
            if not isinstance(raw, dict):
                continue
            index = raw.get("index", 0)
            if not isinstance(index, int):
                continue
            buffer = buffers.setdefault(index, _ToolCallBuffer())
            identifier = raw.get("id")
            if isinstance(identifier, str):
                buffer.identifier += identifier
            function = raw.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(name, str):
                buffer.name += name
            if isinstance(arguments, str):
                buffer.arguments += arguments
