from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
from urllib.parse import quote

from pygrok_build.domain.messages import Message, Role, ToolCall
from pygrok_build.domain.model_events import (
    ModelCompleted,
    ModelEvent,
    ModelReasoningDelta,
    ModelTextDelta,
    ModelToolCall,
)
from pygrok_build.domain.tools import ToolDefinition
from pygrok_build.errors import ProviderError


class GeminiProvider:
    """Native Gemini ``streamGenerateContent`` SSE adapter."""

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
        self._model = model.removeprefix("models/")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._transport = transport

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def _endpoint(self) -> str:
        base_url = self._base_url
        if not base_url.endswith(("/v1", "/v1beta")):
            base_url = f"{base_url}/v1beta"
        return f"{base_url}/models/{quote(self._model, safe='')}:streamGenerateContent?alt=sse"

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

    @classmethod
    def _convert_messages(
        cls, messages: Sequence[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        calls_by_id: dict[str, ToolCall] = {}
        for message in messages:
            if message.role is Role.SYSTEM:
                if message.content:
                    system_parts.append(message.content)
                continue
            if message.role is Role.USER:
                cls._append_content(contents, "user", [{"text": message.content}])
                continue
            if message.role is Role.ASSISTANT:
                parts: list[dict[str, Any]] = []
                if message.content:
                    parts.append({"text": message.content})
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
                "response": cls._tool_response(message.content),
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
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> dict[str, Any]:
        system, contents = self._convert_messages(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self._max_output_tokens},
        }
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
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
        bounded = detail[:1000]
        return bounded.replace(self._api_key, "[REDACTED]") if self._api_key else bounded

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

        body = self._request_body(messages, tools)
        headers = {"x-goog-api-key": self._api_key, "accept": "text/event-stream"}
        stop_reason = "stop"
        input_tokens: int | None = None
        output_tokens: int | None = None
        tool_index = 0
        try:
            options: dict[str, Any] = {"timeout": httpx.Timeout(self._timeout_seconds)}
            if self._transport is not None:
                options["transport"] = self._transport
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
                        if isinstance(usage.get("promptTokenCount"), int):
                            input_tokens = usage["promptTokenCount"]
                        candidate_tokens = usage.get(
                            "candidatesTokenCount", usage.get("outputTokenCount")
                        )
                        if isinstance(candidate_tokens, int):
                            output_tokens = candidate_tokens
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

        yield ModelCompleted(stop_reason, input_tokens, output_tokens)
