"""Canonical Anthropic Messages API provider infrastructure adapter.

定义规范的 Anthropic Messages API Provider 基础设施适配器."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
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
    Message,
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
from neuro_code.shared.errors import ProviderError


@dataclass(slots=True)
class _ToolUseBuffer:
    identifier: str = ""
    name: str = ""
    initial_input: dict[str, Any] = field(default_factory=dict)
    partial_json: str = ""


class AnthropicProvider:
    """Native Anthropic Messages API streaming adapter.

    提供原生 Anthropic Messages API 流式适配器."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "anthropic",
        context_affinity: str | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        prompt_caching: bool = True,
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
    ) -> None:
        if not isinstance(prompt_caching, bool):
            raise TypeError("prompt_caching must be a bool")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._context_affinity = context_affinity
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._prompt_caching = prompt_caching
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
    def _endpoint(self) -> str:
        if self._base_url.endswith("/messages"):
            return self._base_url
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/messages"
        return f"{self._base_url}/v1/messages"

    @staticmethod
    def _append_content(
        messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
    ) -> None:
        if messages and messages[-1].get("role") == role:
            content = cast(list[dict[str, Any]], messages[-1]["content"])
            content.extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    @staticmethod
    def _content_blocks(message: Message) -> list[dict[str, Any]]:
        if not message.content_parts:
            return [{"type": "text", "text": message.content}]

        blocks: list[dict[str, Any]] = []
        for part in message.content_parts:
            if part.kind is ContentPartKind.TEXT:
                assert part.text is not None
                blocks.append({"type": "text", "text": part.text})
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
                        "source": {"type": "url", "url": reference.url},
                    }
                )
        return blocks

    @classmethod
    def _convert_messages(
        cls, messages: Sequence[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            content = message.model_content()
            if message.role is Role.SYSTEM:
                if content:
                    system_parts.append(content)
                continue
            if message.role is Role.USER:
                cls._append_content(converted, "user", cls._content_blocks(message))
                continue
            if message.role is Role.ASSISTANT:
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": dict(call.arguments),
                    }
                    for call in message.tool_calls
                )
                if blocks:
                    cls._append_content(converted, "assistant", blocks)
                continue
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
                            cls._content_blocks(message)
                            if message.content_parts
                            and any(
                                part.kind is ContentPartKind.IMAGE for part in message.content_parts
                            )
                            else content
                        ),
                    }
                ],
            )
        return ("\n\n".join(system_parts) or None), converted

    def _request_body(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> dict[str, Any]:
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
            # Anthropic automatic prompt caching advances the cache breakpoint
            # to the latest cacheable conversation block.  That keeps a
            # growing Agent transcript cacheable instead of pinning the cache
            # at the static system message only.
            #
            # Anthropic 的自动提示词缓存会将缓存断点推进到最新的可缓存对话块,
            # 因而持续增长的 Agent transcript 可被缓存,而不是只固定缓存 system 消息。
            body["cache_control"] = {"type": "ephemeral"}
        if tool_policy is ModelToolPolicy.ALLOWED and tools:
            body["tools"] = [tool.to_dict() for tool in tools]
        return body

    def _safe_detail(self, detail: str) -> str:
        return self._http_policy.redact(detail, self._api_key)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _finish_tool(buffer: _ToolUseBuffer) -> ToolCall:
        if not buffer.identifier or not buffer.name:
            raise ProviderError("Anthropic emitted an incomplete tool call")
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
        return ToolCall(buffer.identifier, buffer.name, arguments)

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

        body = self._request_body(context.messages, tools, tool_policy=tool_policy)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "accept": "text/event-stream",
        }
        buffers: dict[int, _ToolUseBuffer] = {}
        stop_reason = "end_turn"
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_tokens: int | None = None
        cache_write_tokens: int | None = None
        saw_message_stop = False
        try:
            options = self._http_policy.client_options(
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            )
            async with (
                httpx.AsyncClient(**options) as client,
                client.stream("POST", self._endpoint, headers=headers, json=body) as response,
            ):
                if response.status_code >= 400:
                    detail = self._safe_detail((await response.aread()).decode("utf-8", "replace"))
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
                        raise ProviderError(f"Anthropic stream error: {self._safe_detail(detail)}")
                    if event_type == "message_start":
                        message = event.get("message")
                        usage = message.get("usage") if isinstance(message, dict) else None
                        if isinstance(usage, dict):
                            input_tokens = self._token_count(usage.get("input_tokens"))
                            output_tokens = self._token_count(usage.get("output_tokens"))
                            cache_read_tokens = self._token_count(
                                usage.get("cache_read_input_tokens")
                            )
                            cache_write_tokens = self._token_count(
                                usage.get("cache_creation_input_tokens")
                            )
                    elif event_type == "content_block_start":
                        index = event.get("index")
                        block = event.get("content_block")
                        if not isinstance(index, int) or not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "tool_use":
                            initial_input = block.get("input")
                            if not isinstance(initial_input, dict):
                                raise ProviderError("Anthropic tool input must be a JSON object")
                            raw_identifier = block.get("id")
                            raw_name = block.get("name")
                            buffers[index] = _ToolUseBuffer(
                                identifier=(
                                    raw_identifier if isinstance(raw_identifier, str) else ""
                                ),
                                name=raw_name if isinstance(raw_name, str) else "",
                                initial_input=dict(initial_input),
                            )
                        elif block_type == "text" and isinstance(block.get("text"), str):
                            if block["text"]:
                                yield ModelTextDelta(block["text"])
                        elif block_type == "thinking" and isinstance(block.get("thinking"), str):
                            if block["thinking"]:
                                yield ModelReasoningDelta(block["thinking"])
                    elif event_type == "content_block_delta":
                        index = event.get("index")
                        delta = event.get("delta")
                        if not isinstance(index, int) or not isinstance(delta, dict):
                            continue
                        delta_type = delta.get("type")
                        if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                            if delta["text"]:
                                yield ModelTextDelta(delta["text"])
                        elif delta_type == "thinking_delta" and isinstance(
                            delta.get("thinking"), str
                        ):
                            if delta["thinking"]:
                                yield ModelReasoningDelta(delta["thinking"])
                        elif delta_type == "input_json_delta" and isinstance(
                            delta.get("partial_json"), str
                        ):
                            buffers.setdefault(index, _ToolUseBuffer()).partial_json += delta[
                                "partial_json"
                            ]
                    elif event_type == "content_block_stop":
                        index = event.get("index")
                        if isinstance(index, int) and index in buffers:
                            yield ModelToolCall(self._finish_tool(buffers.pop(index)))
                    elif event_type == "message_delta":
                        delta = event.get("delta")
                        usage = event.get("usage")
                        if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                            stop_reason = delta["stop_reason"]
                        if isinstance(usage, dict):
                            updated_output_tokens = self._token_count(usage.get("output_tokens"))
                            if updated_output_tokens is not None:
                                output_tokens = updated_output_tokens
                    elif event_type == "message_stop":
                        saw_message_stop = True
        except ProviderError:
            raise
        except Exception as error:
            detail = self._safe_detail(str(error))
            raise ProviderError(
                f"Anthropic stream failed: {type(error).__name__}: {detail}"
            ) from error

        if buffers:
            raise ProviderError("Anthropic stream ended during a tool call")
        if not saw_message_stop:
            raise ProviderError("Anthropic stream ended without message_stop")
        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            input_token_semantics=ModelInputTokenSemantics.UNCACHED_TAIL,
        )
        yield ModelCompleted(
            stop_reason,
            usage=(usage if usage.has_reported_tokens else None),
        )
