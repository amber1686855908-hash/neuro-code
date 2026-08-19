"""Canonical Gemini Generate Content provider infrastructure adapter.

定义规范的 Gemini Generate Content Provider 基础设施适配器."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
from urllib.parse import quote

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import (
    ModelCapability,
    ModelCapabilitySet,
    ModelToolPolicy,
    resolve_capabilities,
)
from neuro_code.domain.conversation.context import ModelContext
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
    Message,
    Role,
    ToolCall,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.image_references import (
    GEMINI_IMAGE_MEDIA_TYPES,
    GEMINI_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    RemoteImageReference,
    is_gemini_file_uri,
    parse_image_reference,
)
from neuro_code.shared.errors import ProviderError


class GeminiProvider:
    """Native Gemini ``streamGenerateContent`` SSE adapter.

    提供原生 Gemini ``streamGenerateContent`` SSE 适配器."""

    @staticmethod
    def implementation_capabilities() -> ModelCapabilitySet:
        """Return capabilities implemented by this generateContent adapter."""

        return ModelCapabilitySet.from_supported(
            ModelCapability.FUNCTION_TOOLS,
            ModelCapability.VISION,
        )

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "gemini",
        context_affinity: str | None = None,
        capabilities: ModelCapabilitySet | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
    ) -> None:
        self._model = model.removeprefix("models/")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._context_affinity = context_affinity
        upstream = capabilities or ModelCapabilitySet.all_unknown()
        self._capabilities = resolve_capabilities(
            upstream=upstream,
            implementation=self.implementation_capabilities(),
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

    @property
    def _endpoint(self) -> str:
        base_url = self._base_url
        if not base_url.endswith(("/v1", "/v1beta")):
            base_url = f"{base_url}/v1beta"
        return f"{base_url}/models/{quote(self._model, safe='')}:streamGenerateContent?alt=sse"

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _append_content(
        contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]
    ) -> None:
        if contents and contents[-1].get("role") == role:
            existing = cast(list[dict[str, Any]], contents[-1]["parts"])
            existing.extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    @staticmethod
    def _tool_response(content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"result": content}
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    @staticmethod
    def _user_parts(message: Message) -> list[dict[str, Any]]:
        if not message.content_parts:
            return [{"text": message.content}]

        parts: list[dict[str, Any]] = []
        for part in message.content_parts:
            if part.kind is ContentPartKind.TEXT:
                assert part.text is not None
                parts.append({"text": part.text})
                continue
            assert part.url is not None
            reference = parse_image_reference(
                part.url,
                allowed_media_types=GEMINI_IMAGE_MEDIA_TYPES,
                max_inline_bytes=GEMINI_MAX_INLINE_IMAGE_BYTES,
            )
            if isinstance(reference, InlineImageReference):
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": reference.media_type,
                            "data": reference.data,
                        }
                    }
                )
            elif isinstance(reference, RemoteImageReference) and is_gemini_file_uri(reference):
                parts.append({"fileData": {"fileUri": reference.url}})
            else:
                parts.append({"text": IMAGE_MODEL_PLACEHOLDER})
        return parts

    @classmethod
    def _convert_messages(
        cls, messages: Sequence[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        calls_by_id: dict[str, ToolCall] = {}
        for message in messages:
            content = message.model_content()
            if message.role is Role.SYSTEM:
                if content:
                    system_parts.append(content)
                continue
            if message.role is Role.USER:
                cls._append_content(contents, "user", cls._user_parts(message))
                continue
            if message.role is Role.ASSISTANT:
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"text": content})
                for call in message.tool_calls:
                    calls_by_id[call.id] = call
                    function_call: dict[str, Any] = {
                        "name": call.name,
                        "args": dict(call.arguments),
                    }
                    provider_call_id = call.metadata.get("gemini.call_id")
                    if isinstance(provider_call_id, str):
                        function_call["id"] = provider_call_id
                    part: dict[str, Any] = {"functionCall": function_call}
                    thought_signature = call.metadata.get("gemini.thought_signature")
                    if isinstance(thought_signature, str):
                        part["thoughtSignature"] = thought_signature
                    parts.append(part)
                if parts:
                    cls._append_content(contents, "model", parts)
                continue
            if not message.name:
                raise ProviderError("Gemini tool results require a tool name")
            function_response: dict[str, Any] = {
                "name": message.name,
                "response": cls._tool_response(content),
            }
            previous_call = calls_by_id.get(message.tool_call_id or "")
            if previous_call is not None:
                provider_call_id = previous_call.metadata.get("gemini.call_id")
                if isinstance(provider_call_id, str):
                    function_response["id"] = provider_call_id
            cls._append_content(
                contents,
                "user",
                [{"functionResponse": function_response}],
            )
        return ("\n\n".join(system_parts) or None), contents

    def _request_body(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> dict[str, Any]:
        system, contents = self._convert_messages(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self._max_output_tokens},
        }
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tool_policy is ModelToolPolicy.ALLOWED and tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": dict(tool.input_schema),
                        }
                        for tool in tools
                    ]
                }
            ]
        return body

    def _safe_detail(self, detail: str) -> str:
        return self._http_policy.redact(detail, self._api_key)

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
        headers = {"x-goog-api-key": self._api_key, "accept": "text/event-stream"}
        stop_reason = "stop"
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_tokens: int | None = None
        tool_index = 0
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
                        f"Gemini request failed with HTTP {response.status_code}: {detail}"
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
                        raise ProviderError("Gemini returned malformed streaming JSON") from error
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:
                        detail = json.dumps(chunk["error"], ensure_ascii=False)
                        raise ProviderError(f"Gemini stream error: {self._safe_detail(detail)}")
                    usage = chunk.get("usageMetadata")
                    if isinstance(usage, dict):
                        parsed_input_tokens = self._token_count(usage.get("promptTokenCount"))
                        if parsed_input_tokens is not None:
                            input_tokens = parsed_input_tokens
                        candidate_tokens = usage.get(
                            "candidatesTokenCount", usage.get("outputTokenCount")
                        )
                        parsed_output_tokens = self._token_count(candidate_tokens)
                        if parsed_output_tokens is not None:
                            output_tokens = parsed_output_tokens
                        parsed_cached_tokens = self._token_count(
                            usage.get("cachedContentTokenCount")
                        )
                        if parsed_cached_tokens is None:
                            parsed_cached_tokens = self._token_count(usage.get("totalCachedTokens"))
                        if parsed_cached_tokens is not None:
                            cache_read_tokens = parsed_cached_tokens
                    candidates = chunk.get("candidates")
                    if not isinstance(candidates, list) or not candidates:
                        feedback = chunk.get("promptFeedback")
                        if isinstance(feedback, dict) and isinstance(
                            feedback.get("blockReason"), str
                        ):
                            raise ProviderError(
                                f"Gemini blocked the prompt: {feedback['blockReason']}"
                            )
                        continue
                    candidate = candidates[0]
                    if not isinstance(candidate, dict):
                        continue
                    if isinstance(candidate.get("finishReason"), str):
                        stop_reason = candidate["finishReason"].lower()
                    content = candidate.get("content")
                    parts = content.get("parts") if isinstance(content, dict) else None
                    if not isinstance(parts, list):
                        continue
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        part_text = part.get("text")
                        if isinstance(part_text, str) and part_text:
                            if part.get("thought") is True:
                                yield ModelReasoningDelta(part_text)
                            else:
                                yield ModelTextDelta(part_text)
                        function_call = part.get("functionCall")
                        if not isinstance(function_call, dict):
                            continue
                        name = function_call.get("name")
                        arguments = function_call.get("args", {})
                        if not isinstance(name, str) or not name:
                            raise ProviderError("Gemini emitted an incomplete tool call")
                        if not isinstance(arguments, dict):
                            raise ProviderError(
                                f"tool call {name!r} arguments must be a JSON object"
                            )
                        provider_call_id = function_call.get("id")
                        metadata: dict[str, Any] = {}
                        if isinstance(provider_call_id, str) and provider_call_id:
                            identifier = provider_call_id
                            metadata["gemini.call_id"] = provider_call_id
                        else:
                            tool_index += 1
                            identifier = f"gemini-call-{tool_index}"
                        thought_signature = part.get("thoughtSignature")
                        if isinstance(thought_signature, str):
                            metadata["gemini.thought_signature"] = thought_signature
                        yield ModelToolCall(ToolCall(identifier, name, arguments, metadata))
        except ProviderError:
            raise
        except Exception as error:
            detail = self._safe_detail(str(error))
            raise ProviderError(
                f"Gemini stream failed: {type(error).__name__}: {detail}"
            ) from error

        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        yield ModelCompleted(
            stop_reason,
            usage=(usage if usage.has_reported_tokens else None),
        )
