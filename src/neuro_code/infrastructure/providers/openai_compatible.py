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
from neuro_code.application.ports.model import (
    ModelCapability,
    ModelCapabilitySet,
    ModelToolPolicy,
    resolve_capabilities,
)
from neuro_code.domain.conversation.context import UPSTREAM_IMPORT_PROVIDER, ModelContext
from neuro_code.domain.conversation.events import (
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
from neuro_code.domain.conversation.reasoning import ReasoningEffort
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
)

BACKEND_SUMMARY_FIELD_CHARS = 1000
CODE_SUMMARY_CHARS = 100
MAX_NATIVE_CONTEXT_BYTES = 1_048_576


def _encode_bounded_native_context(payload: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProviderError.protocol(
            "provider native reasoning context is not JSON-safe"
        ) from error
    if len(encoded) > MAX_NATIVE_CONTEXT_BYTES:
        raise ProviderError.classified(
            ProviderFailureKind.CONTEXT_OVERFLOW,
            "provider native reasoning context exceeds its size limit",
        )
    return encoded


@dataclass(slots=True)
class _ToolCallBuffer:
    identifier: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class _ReasoningDetailsAccumulator:
    """Accumulate MiniMax's cumulative streamed reasoning-details text."""

    text: str = ""
    details: tuple[Mapping[str, object], ...] = ()

    def feed(self, value: object) -> tuple[str, tuple[Mapping[str, object], ...]]:
        if isinstance(value, Mapping):
            values: Sequence[object] = (value,)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values = value
        else:
            return "", self.details
        normalized = tuple(dict(item) for item in values if isinstance(item, Mapping))
        incoming = "".join(item["text"] for item in normalized if isinstance(item.get("text"), str))
        next_details = (
            normalized if incoming.startswith(self.text) else (*self.details, *normalized)
        )
        _encode_bounded_native_context({"details": [dict(detail) for detail in next_details]})
        if not incoming:
            if normalized:
                self.details = normalized
            return "", self.details
        if incoming.startswith(self.text):
            delta = incoming[len(self.text) :]
            self.details = normalized
        else:
            delta = incoming
            self.details = (*self.details, *normalized)
        self.text += delta
        return delta, self.details


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
                        raise ProviderError.protocol(
                            "provider returned incomplete DeepSeek DSML tool call"
                        )
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
                    raise ProviderError.protocol(
                        "provider returned malformed DeepSeek DSML content"
                    )
                if self._buffer:
                    visible.append(self._buffer)
                    self._buffer = ""
                return tuple(visible), tuple(calls)

            keep = self._partial_marker_suffix_length()
            for prefix in self._DSML_PREFIXES:
                index = self._buffer.find(prefix)
                if index >= 0 and index < len(self._buffer) - keep:
                    raise ProviderError.protocol(
                        "provider returned malformed DeepSeek DSML content"
                    )
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
                raise ProviderError.protocol("provider returned malformed DeepSeek DSML tool call")
            attributes = match.group("attributes")
            name_match = re.search(r'\bname\s*=\s*"([^"]+)"', attributes)
            if name_match is None:
                raise ProviderError.protocol(
                    "provider returned a DeepSeek DSML call without a name"
                )
            arguments: dict[str, object] = {}
            body = match.group("body")
            parameter_cursor = 0
            for parameter in self._PARAMETER_PATTERN.finditer(body):
                if body[parameter_cursor : parameter.start()].strip():
                    raise ProviderError.protocol(
                        "provider returned malformed DeepSeek DSML parameters"
                    )
                parameter_attributes = parameter.group("attributes")
                parameter_name = re.search(r'\bname\s*=\s*"([^"]+)"', parameter_attributes)
                string_flag = re.search(r'\bstring\s*=\s*"(true|false)"', parameter_attributes)
                if parameter_name is None or string_flag is None:
                    raise ProviderError.protocol(
                        "provider returned malformed DeepSeek DSML parameter"
                    )
                key = html.unescape(parameter_name.group(1))
                if key in arguments:
                    raise ProviderError.protocol(
                        "provider returned duplicate DeepSeek DSML parameter"
                    )
                raw_value = html.unescape(parameter.group("body"))
                if string_flag.group(1) == "true":
                    arguments[key] = raw_value
                else:
                    try:
                        arguments[key] = json.loads(raw_value)
                    except json.JSONDecodeError as error:
                        raise ProviderError.protocol(
                            "provider returned invalid JSON in a DeepSeek DSML parameter"
                        ) from error
                parameter_cursor = parameter.end()
            if body[parameter_cursor:].strip():
                raise ProviderError.protocol("provider returned malformed DeepSeek DSML parameters")
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
            raise ProviderError.protocol("provider returned malformed DeepSeek DSML tool call")
        return calls


class OpenAICompatibleProvider:
    """Streaming Chat Completions adapter.

    The dependency is imported lazily so configuration inspection and offline
    tests remain usable before optional/runtime dependencies are installed.

    提供流式 Chat Completions 适配器. 依赖项延迟导入,使配置检查和离线测试在可选运行时依赖未安装时仍可用.
    """

    @staticmethod
    def implementation_capabilities(*, dialect: str = "standard") -> ModelCapabilitySet:
        """Return capabilities implemented by this Chat wire adapter."""

        if dialect not in {"standard", "deepseek-v4", "kimi", "glm", "minimax"}:
            raise ConfigurationError(f"unsupported OpenAI-compatible dialect: {dialect}")
        supported = {
            ModelCapability.FUNCTION_TOOLS,
            ModelCapability.VISION,
        }
        if dialect in {"kimi", "glm", "minimax"}:
            supported.update(
                {
                    ModelCapability.PROMPT_CACHE,
                    ModelCapability.REASONING,
                }
            )
        return ModelCapabilitySet.from_supported(*supported)

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "openai-compatible",
        dialect: str = "standard",
        context_affinity: str | None = None,
        capabilities: ModelCapabilitySet | None = None,
        tool_choice: str | Mapping[str, object] | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
    ) -> None:
        if dialect not in {"standard", "deepseek-v4", "kimi", "glm", "minimax"}:
            raise ConfigurationError(f"unsupported OpenAI-compatible dialect: {dialect}")
        if tool_choice is not None and (
            not isinstance(tool_choice, (str, Mapping))
            or (isinstance(tool_choice, str) and not tool_choice.strip())
        ):
            raise ConfigurationError("tool_choice must be a non-empty string or mapping")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._dialect = dialect
        self._tool_choice = dict(tool_choice) if isinstance(tool_choice, Mapping) else tool_choice
        self._context_affinity = context_affinity
        upstream = capabilities or ModelCapabilitySet.all_unknown()
        self._capabilities = resolve_capabilities(
            upstream=upstream,
            implementation=self.implementation_capabilities(dialect=dialect),
        ).effective
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

    @property
    def capabilities(self) -> ModelCapabilitySet:
        return self._capabilities

    def _safe_detail(self, detail: str) -> str:
        return self._http_policy.redact(detail, self._api_key)

    def _uses_deepseek_dsml(self) -> bool:
        """Return whether the configured dialect emits DeepSeek DSML.

        返回当前显式配置的方言是否会发出 DeepSeek DSML.
        """

        return self._dialect == "deepseek-v4"

    def _preserves_reasoning_content(self) -> bool:
        if self._dialect in {"glm", "minimax"}:
            return True
        if self._dialect != "kimi":
            return False
        return self._model.casefold() in {
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        }

    def _kimi_thinking_enabled(self) -> bool:
        return self._dialect == "kimi" and self._model.casefold() in {
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
            "kimi-k2.5",
        }

    @staticmethod
    def _effort_name(effort: ReasoningEffort) -> str:
        return effort.effective.value

    def _apply_dialect_request_fields(
        self,
        body: dict[str, Any],
        *,
        context: ModelContext,
    ) -> None:
        model = self._model.casefold()
        if self._dialect == "kimi":
            if model == "kimi-k3":
                effort = self._effort_name(context.reasoning_effort)
                body["reasoning_effort"] = {
                    "low": "low",
                    "medium": "high",
                    "high": "high",
                    "xhigh": "max",
                }[effort]
            elif model in {"kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"}:
                body["thinking"] = {"type": "enabled", "keep": "all"}
            elif model == "kimi-k2.5":
                body["thinking"] = {"type": "enabled"}
        elif self._dialect == "glm":
            if model in {
                "glm-5.3",
                "glm-5.2",
                "glm-5.1",
                "glm-5",
                "glm-5-turbo",
                "glm-5v-turbo",
                "glm-4.7",
                "glm-4.6",
                "glm-4.6v",
                "glm-4.5",
            }:
                body["thinking"] = {"type": "enabled", "clear_thinking": False}
            if model == "glm-5.3":
                body["reasoning_effort"] = {
                    "low": "low",
                    "medium": "high",
                    "high": "high",
                    "xhigh": "max",
                }[self._effort_name(context.reasoning_effort)]
            elif model == "glm-5.2":
                body["reasoning_effort"] = {
                    "low": "high",
                    "medium": "high",
                    "high": "high",
                    "xhigh": "max",
                }[self._effort_name(context.reasoning_effort)]
        elif self._dialect == "minimax":
            body["max_completion_tokens"] = body.pop("max_tokens")
            body["reasoning_split"] = True

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def _first_usage_token(
        cls,
        usage: Mapping[str, object],
        *names: str,
    ) -> int | None:
        for name in names:
            value = cls._token_count(usage.get(name))
            if value is not None:
                return value
        return None

    @classmethod
    def _usage_from_payload(cls, usage: Mapping[str, object]) -> ModelUsage | None:
        """Normalize standard and compatible prompt-cache usage fields.

        DeepSeek-compatible endpoints commonly report the cache hit/miss
        values at the usage top level.  Other compatible endpoints may use
        OpenAI's nested ``prompt_tokens_details`` shape.  Preserve only fields
        that are actually reported rather than inferring a cache split.

        规范化标准和兼容端点的 prompt cache 用量字段。只保留实际上报字段,不推断
        缓存拆分。
        """

        details = usage.get("prompt_tokens_details")
        detail_usage = details if isinstance(details, Mapping) else {}
        cache_read_tokens = cls._first_usage_token(
            usage,
            "prompt_cache_hit_tokens",
            "cache_read_tokens",
            "cached_tokens",
        )
        if cache_read_tokens is None:
            cache_read_tokens = cls._first_usage_token(
                detail_usage,
                "cached_tokens",
                "prompt_cache_hit_tokens",
                "cache_read_tokens",
            )
        cache_write_tokens = cls._first_usage_token(
            usage,
            "prompt_cache_write_tokens",
            "cache_write_tokens",
        )
        if cache_write_tokens is None:
            cache_write_tokens = cls._first_usage_token(
                detail_usage,
                "prompt_cache_write_tokens",
                "cache_write_tokens",
            )
        cache_miss_tokens = cls._first_usage_token(
            usage,
            "prompt_cache_miss_tokens",
            "cache_miss_tokens",
        )
        if cache_miss_tokens is None:
            cache_miss_tokens = cls._first_usage_token(
                detail_usage,
                "prompt_cache_miss_tokens",
                "cache_miss_tokens",
            )
        normalized = ModelUsage(
            input_tokens=cls._first_usage_token(usage, "prompt_tokens"),
            output_tokens=cls._first_usage_token(usage, "completion_tokens"),
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )
        return normalized if normalized.has_reported_tokens else None

    @staticmethod
    def _merge_usage(current: ModelUsage | None, update: ModelUsage | None) -> ModelUsage | None:
        if update is None:
            return current
        if current is None:
            return update
        return ModelUsage(
            input_tokens=(
                update.input_tokens if update.input_tokens is not None else current.input_tokens
            ),
            output_tokens=(
                update.output_tokens if update.output_tokens is not None else current.output_tokens
            ),
            cache_read_tokens=(
                update.cache_read_tokens
                if update.cache_read_tokens is not None
                else current.cache_read_tokens
            ),
            cache_write_tokens=(
                update.cache_write_tokens
                if update.cache_write_tokens is not None
                else current.cache_write_tokens
            ),
            cache_miss_tokens=(
                update.cache_miss_tokens
                if update.cache_miss_tokens is not None
                else current.cache_miss_tokens
            ),
            input_token_semantics=update.input_token_semantics,
        )

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
            if part.kind is not ContentPartKind.IMAGE:
                blocks.append({"type": "text", "text": part.model_placeholder})
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
        if message.reasoning_content is not None and (
            self._preserves_reasoning_content()
            or (self._dialect not in {"kimi", "glm", "minimax"} and bool(message.tool_calls))
        ):
            payload["reasoning_content"] = message.reasoning_content
        return payload

    def _can_replay_native_context(self, context: ModelContext) -> bool:
        return (
            self._dialect == "minimax"
            and self._context_affinity is not None
            and context.source_provider == self._provider_name
            and context.source_model == self._model
            and context.source_context_affinity == self._context_affinity
        )

    def _native_reasoning_details(
        self,
        item: PreservedContextItem,
    ) -> tuple[Mapping[str, object], ...] | None:
        if item.kind is not ContextItemKind.REASONING:
            return None
        native = item.to_dict().get("native")
        if not isinstance(native, Mapping):
            return None
        if (
            native.get("type") != "openai-chat-reasoning-details"
            or native.get("provider") != self._provider_name
            or native.get("protocol") != "openai-chat"
            or native.get("model") != self._model
        ):
            return None
        details = native.get("details")
        if not isinstance(details, (list, tuple)) or not all(
            isinstance(detail, Mapping) for detail in details
        ):
            return None
        normalized = tuple(dict(detail) for detail in details)
        try:
            _encode_bounded_native_context({"details": [dict(detail) for detail in normalized]})
        except ProviderError:
            return None
        return normalized

    def _native_reasoning_item(
        self,
        details: tuple[Mapping[str, object], ...],
    ) -> PreservedContextItem:
        payload: dict[str, object] = {
            "type": ContextItemKind.REASONING.value,
            "native": {
                "type": "openai-chat-reasoning-details",
                "provider": self._provider_name,
                "protocol": "openai-chat",
                "model": self._model,
                "details": [dict(detail) for detail in details],
            },
        }
        _encode_bounded_native_context(payload)
        return PreservedContextItem(ContextItemKind.REASONING, payload)

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
        xai_import_affinity = self._has_xai_import_affinity(context)
        minimax_native_affinity = self._can_replay_native_context(context)
        if not xai_import_affinity and not minimax_native_affinity:
            return [self._message_payload(message) for message in context.messages]

        payloads: list[dict[str, Any]] = []
        pending_reasoning: list[str] = []
        pending_reasoning_details: tuple[Mapping[str, object], ...] = ()
        for item in context.items:
            if isinstance(item, PreservedContextItem):
                if xai_import_affinity and item.kind is ContextItemKind.REASONING:
                    text = self._reasoning_text(item)
                    if text:
                        pending_reasoning.append(text)
                if minimax_native_affinity:
                    details = self._native_reasoning_details(item)
                    if details is not None:
                        pending_reasoning_details = details
                if xai_import_affinity and item.kind is not ContextItemKind.REASONING:
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
                if pending_reasoning_details:
                    payload["reasoning_details"] = [
                        dict(detail) for detail in pending_reasoning_details
                    ]
                    pending_reasoning_details = ()
            else:
                pending_reasoning.clear()
                pending_reasoning_details = ()
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
            if self._tool_choice is not None:
                if self._dialect == "glm" and self._tool_choice != "auto":
                    raise ConfigurationError(
                        "GLM OpenAI compatibility currently supports only tool_choice 'auto'"
                    )
                if (
                    self._dialect == "kimi"
                    and self._kimi_thinking_enabled()
                    and (
                        isinstance(self._tool_choice, Mapping)
                        or self._tool_choice not in {"auto", "none"}
                    )
                ):
                    raise ConfigurationError(
                        "Kimi tool_choice is incompatible with thinking; thinking was not disabled"
                    )
                body["tool_choice"] = (
                    dict(self._tool_choice)
                    if isinstance(self._tool_choice, Mapping)
                    else self._tool_choice
                )
        self._apply_dialect_request_fields(body, context=context)
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
            raise ProviderError.local(
                "httpx is required for live model requests; install the project"
            ) from error

        body = self._request_body(context, tools, tool_policy=tool_policy)

        headers = {"Authorization": f"Bearer {self._api_key}"}
        buffers: dict[int, _ToolCallBuffer] = {}
        dsml_parser = _DeepSeekDSMLStreamParser() if self._uses_deepseek_dsml() else None
        reasoning_details = _ReasoningDetailsAccumulator() if self._dialect == "minimax" else None
        stop_reason = "stop"
        model_usage: ModelUsage | None = None
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
                    raise ProviderError.from_http(
                        response.status_code,
                        detail,
                        headers=response.headers,
                        failure_kind=classify_provider_failure(
                            ProviderFailureProtocol.OPENAI_COMPATIBLE,
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
                        chunk = json.loads(payload)
                    except json.JSONDecodeError as error:
                        raise ProviderError.protocol(
                            "provider returned malformed streaming JSON",
                            provider=self._provider_name,
                            model=self._model,
                        ) from error
                    usage = chunk.get("usage")
                    if isinstance(usage, Mapping):
                        model_usage = self._merge_usage(
                            model_usage,
                            self._usage_from_payload(usage),
                        )
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
                    elif reasoning_details is not None:
                        raw_details = delta.get("reasoning_details")
                        if raw_details is not None:
                            detail_delta, _ = reasoning_details.feed(raw_details)
                            if detail_delta:
                                yield ModelReasoningDelta(detail_delta)
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        self._accumulate_tool_calls(tool_calls, buffers)
        except ProviderError:
            raise
        except Exception as error:
            # Deliberately exclude request headers and body from the exception.
            raise ProviderError.from_runtime(
                error,
                provider=self._provider_name,
                model=self._model,
                redaction_values=(self._api_key, *self._http_policy.redaction_values),
                prefix="provider stream failed",
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
                raise ProviderError.protocol(
                    f"tool call {buffer.name!r} contained invalid JSON arguments"
                ) from error
            if not isinstance(arguments, dict):
                raise ProviderError.protocol(
                    f"tool call {buffer.name!r} arguments must be a JSON object"
                )
            if not buffer.identifier or not buffer.name:
                raise ProviderError.protocol("provider emitted an incomplete tool call")
            yield ModelToolCall(ToolCall(buffer.identifier, buffer.name, arguments))
        native_items: tuple[PreservedContextItem, ...] = ()
        if reasoning_details is not None and reasoning_details.details and self._context_affinity:
            native_items = (self._native_reasoning_item(reasoning_details.details),)
        yield ModelCompleted(stop_reason, context_items=native_items, usage=model_usage)

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
