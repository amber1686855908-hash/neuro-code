from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from pygrok_build.domain.messages import (
    IMAGE_MODEL_PLACEHOLDER,
    ContentPartKind,
    Message,
    Role,
    ToolCall,
)
from pygrok_build.domain.model_events import (
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from pygrok_build.domain.tools import ToolDefinition
from pygrok_build.errors import ProviderError
from pygrok_build.providers.image_references import (
    OPENAI_IMAGE_MEDIA_TYPES,
    OPENAI_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    parse_image_reference,
)


@dataclass(slots=True)
class _ToolCallBuffer:
    identifier: str = ""
    name: str = ""
    arguments: str = ""


class OpenAICompatibleProvider:
    """Streaming Chat Completions adapter.

    The dependency is imported lazily so configuration inspection and offline
    tests remain usable before optional/runtime dependencies are installed.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        transport: Any | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "openai-compatible"

    @property
    def model_name(self) -> str:
        return self._model

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

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        try:
            import httpx
        except ImportError as error:
            raise ProviderError(
                "httpx is required for live model requests; install the project"
            ) from error

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message_payload(message) for message in messages],
            "max_tokens": self._max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
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

        headers = {"Authorization": f"Bearer {self._api_key}"}
        buffers: dict[int, _ToolCallBuffer] = {}
        stop_reason = "stop"
        input_tokens: int | None = None
        output_tokens: int | None = None
        endpoint = f"{self._base_url}/chat/completions"
        try:
            timeout = httpx.Timeout(self._timeout_seconds)
            client_options: dict[str, Any] = {"timeout": timeout}
            if self._transport is not None:
                client_options["transport"] = self._transport
            async with (
                httpx.AsyncClient(**client_options) as client,
                client.stream("POST", endpoint, headers=headers, json=body) as response,
            ):
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:1000]
                    if self._api_key:
                        detail = detail.replace(self._api_key, "[REDACTED]")
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
                        yield ModelTextDelta(content)
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
            detail = str(error)
            if self._api_key:
                detail = detail.replace(self._api_key, "[REDACTED]")
            raise ProviderError(
                f"provider stream failed: {type(error).__name__}: {detail}"
            ) from error

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
