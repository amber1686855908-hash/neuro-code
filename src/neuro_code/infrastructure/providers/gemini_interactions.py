"""Gemini Interactions API infrastructure adapter.

This adapter is deliberately separate from ``gemini.py``.  The legacy
``generateContent`` contract and the Interactions step contract have different
replay and streaming semantics; sharing the wire parser would make one
protocol silently inherit the other's assumptions.
"""

from __future__ import annotations

import copy
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
from neuro_code.domain.conversation.context import ModelContext
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
    GEMINI_IMAGE_MEDIA_TYPES,
    GEMINI_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    RemoteImageReference,
    is_gemini_file_uri,
    parse_image_reference,
)
from neuro_code.shared.errors import (
    ConfigurationError,
    ProviderError,
    ProviderFailureKind,
    ProviderFailureOrigin,
)

GEMINI_INTERACTIONS_PROTOCOL = "gemini-interactions"
GEMINI_INTERACTIONS_API_VERSION = "v1"
MAX_NATIVE_CONTEXT_BYTES = 1_048_576
MAX_VISIBLE_SOURCE_LINES = 32

_GOOGLE_SEARCH_MODELS = frozenset(
    {
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-flash-image-preview",
        "gemini-3.1-pro-preview",
        "gemini-3-pro-image-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
    }
)
_URL_CONTEXT_MODELS = frozenset(
    {
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    }
)
_MIXED_TOOL_MODELS = frozenset(
    {
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
    }
)
_BUILTIN_TOOLS = frozenset({"google_search", "url_context"})
_TOOL_CHOICE_ENUMS = frozenset({"auto", "any", "none", "validated"})
_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "error",
        "errored",
        "cancelled",
        "canceled",
        "incomplete",
        "unsafe",
        "blocked",
    }
)
_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "complete", "ok"})


def _model_id(model: str) -> str:
    return model.strip().removeprefix("models/")


def _json_copy(value: object, *, error_message: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise ProviderError.protocol(error_message) from error


def _string_status(value: object) -> str | None:
    return value.casefold() if isinstance(value, str) else None


def _is_failure_step(step: Mapping[str, Any]) -> bool:
    if step.get("is_error") is True or isinstance(step.get("error"), Mapping):
        return True
    status = _string_status(step.get("status"))
    return status in _FAILURE_STATUSES


def _step_identity(step: Mapping[str, Any], index: int) -> str:
    for key in ("id", "call_id"):
        value = step.get(key)
        if isinstance(value, str) and value:
            return value
    return f"gemini-interactions-{index}"


def _model_output_text(steps: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for step in steps:
        if step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, Mapping)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
    return "".join(parts)


def _thought_text(steps: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for step in steps:
        if step.get("type") != "thought":
            continue
        for key in ("summary", "content"):
            blocks = step.get(key)
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if (
                    isinstance(block, Mapping)
                    and block.get("type") in {"text", "thought"}
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(block["text"])
    return "".join(parts)


def _source_footer(steps: Sequence[Mapping[str, Any]]) -> str:
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for step in steps:
        if step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            annotations = block.get("annotations")
            if not isinstance(annotations, list):
                continue
            for annotation in annotations:
                if not isinstance(annotation, Mapping) or annotation.get("type") != "url_citation":
                    continue
                url = annotation.get("url")
                if not isinstance(url, str):
                    continue
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                key = url.casefold()
                if key in seen:
                    continue
                seen.add(key)
                title = annotation.get("title")
                sources.append(
                    (
                        title.strip()
                        if isinstance(title, str) and title.strip()
                        else parsed.hostname,
                        url,
                    )
                )
                if len(sources) >= MAX_VISIBLE_SOURCE_LINES:
                    break
            if len(sources) >= MAX_VISIBLE_SOURCE_LINES:
                break
        if len(sources) >= MAX_VISIBLE_SOURCE_LINES:
            break
    if not sources:
        return ""
    return "\n\nSources:\n" + "\n".join(f"- {title}: {url}" for title, url in sources)


class GeminiInteractionsProvider:
    """Native stateless Gemini Interactions streaming adapter."""

    @staticmethod
    def implementation_capabilities(
        *, model: str, builtin_tools: Sequence[str] = ()
    ) -> ModelCapabilitySet:
        model_name = _model_id(model)
        supported = {
            ModelCapability.FUNCTION_TOOLS,
            ModelCapability.VISION,
            ModelCapability.REASONING,
        }
        configured = frozenset(builtin_tools)
        if "google_search" in configured and model_name in _GOOGLE_SEARCH_MODELS:
            supported.add(ModelCapability.HOSTED_WEB_SEARCH)
        if "url_context" in configured and model_name in _URL_CONTEXT_MODELS:
            supported.add(ModelCapability.HOSTED_WEB_FETCH)
        if configured & _BUILTIN_TOOLS and model_name in _MIXED_TOOL_MODELS:
            supported.add(ModelCapability.MIXED_HOSTED_AND_CLIENT_TOOLS)
        return ModelCapabilitySet.from_supported(*supported)

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider_name: str = "gemini",
        service_id: str | None = None,
        context_affinity: str | None = None,
        capabilities: ModelCapabilitySet | None = None,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 8192,
        builtin_tools: Sequence[str] = (),
        builtin_tool_options: Mapping[str, Mapping[str, object]] | None = None,
        tool_choice: str | Mapping[str, object] | None = None,
        transport: Any | None = None,
        http_policy: HttpClientPolicy | None = None,
        response_observer: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        normalized_tools = tuple(builtin_tools)
        if len(set(normalized_tools)) != len(normalized_tools):
            raise ConfigurationError(
                "Gemini Interactions builtin_tools must not contain duplicates"
            )
        unsupported = sorted(set(normalized_tools) - _BUILTIN_TOOLS)
        if unsupported:
            raise ConfigurationError(
                f"unsupported Gemini Interactions builtin_tools: {unsupported}"
            )
        self._model = _model_id(model)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._service_id = service_id or provider_name
        self._context_affinity = context_affinity
        self._builtin_tools = normalized_tools
        self._builtin_tool_options = {
            name: dict(options)
            for name, options in (builtin_tool_options or {}).items()
            if name in _BUILTIN_TOOLS
        }
        self._tool_choice = self._validate_tool_choice(tool_choice)
        upstream = capabilities or ModelCapabilitySet.all_unknown()
        self._capabilities = resolve_capabilities(
            upstream=upstream,
            implementation=self.implementation_capabilities(
                model=self._model,
                builtin_tools=self._builtin_tools,
            ),
        ).effective
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._transport = transport
        self._http_policy = http_policy or HttpClientPolicy()
        self._response_observer = response_observer

    @staticmethod
    def _validate_tool_choice(
        value: str | Mapping[str, object] | None,
    ) -> str | Mapping[str, object] | None:
        if value is None:
            return None
        if isinstance(value, str):
            if value not in _TOOL_CHOICE_ENUMS:
                raise ConfigurationError(f"unsupported Gemini Interactions tool_choice: {value}")
            return value
        if not isinstance(value, Mapping):
            raise ConfigurationError("Gemini Interactions tool_choice must be a string or mapping")
        allowed = value.get("allowed_tools")
        if not isinstance(allowed, Mapping):
            raise ConfigurationError("Gemini Interactions tool_choice requires allowed_tools")
        mode = allowed.get("mode", "auto")
        names = allowed.get("tools", ())
        if (
            mode not in _TOOL_CHOICE_ENUMS
            or not isinstance(names, Sequence)
            or isinstance(names, (str, bytes, bytearray))
            or any(not isinstance(name, str) or not name for name in names)
        ):
            raise ConfigurationError("Gemini Interactions tool_choice.allowed_tools is invalid")
        copied = _json_copy(
            value,
            error_message="Gemini Interactions tool_choice is not JSON-safe",
        )
        if not isinstance(copied, dict):
            raise ConfigurationError("Gemini Interactions tool_choice must be a JSON object")
        return copied

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
        for suffix in ("/interactions", "/v1beta2", "/v1beta", "/v1"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
                break
        return f"{base_url}/{GEMINI_INTERACTIONS_API_VERSION}/interactions"

    @staticmethod
    def _content_parts(message: Message) -> list[dict[str, Any]]:
        if not message.content_parts:
            return [{"type": "text", "text": message.content}]
        parts: list[dict[str, Any]] = []
        for part in message.content_parts:
            if part.kind is ContentPartKind.TEXT:
                assert part.text is not None
                parts.append({"type": "text", "text": part.text})
                continue
            if part.kind is not ContentPartKind.IMAGE:
                parts.append({"type": "text", "text": part.model_placeholder})
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
                        "type": "image",
                        "data": reference.data,
                        "mime_type": reference.media_type,
                    }
                )
            elif isinstance(reference, RemoteImageReference) and is_gemini_file_uri(reference):
                parts.append({"type": "image", "uri": reference.url})
            else:
                parts.append({"type": "text", "text": IMAGE_MODEL_PLACEHOLDER})
        return parts

    @staticmethod
    def _function_result(content: str) -> object:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return [{"type": "text", "text": content}]

    @staticmethod
    def _standard_input_item(message: Message) -> dict[str, Any] | None:
        if message.role is Role.SYSTEM:
            return None
        if message.role is Role.USER:
            return {
                "type": "user_input",
                "content": GeminiInteractionsProvider._content_parts(message),
            }
        if message.role is Role.ASSISTANT:
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            if content:
                return {"type": "model_output", "content": content}
            return None
        if not message.name:
            raise ProviderError.classified(
                ProviderFailureKind.INVALID_REQUEST,
                "Gemini Interactions tool results require a tool name",
            )
        return {
            "type": "function_result",
            "name": message.name,
            "call_id": message.tool_call_id or "",
            "result": GeminiInteractionsProvider._function_result(message.content),
        }

    def _native_steps(self, item: PreservedContextItem) -> list[dict[str, Any]] | None:
        if item.kind is not ContextItemKind.BACKEND_TOOL_CALL:
            return None
        payload = item.to_dict()
        kind = payload.get("kind")
        if not isinstance(kind, Mapping):
            return None
        if (
            kind.get("provider") != self._provider_name
            or kind.get("service") != self._service_id
            or kind.get("protocol") != GEMINI_INTERACTIONS_PROTOCOL
            or kind.get("model") != self._model
            or kind.get("native_type") != "gemini_interactions_steps"
        ):
            return None
        raw_steps = kind.get("steps")
        if not isinstance(raw_steps, list) or not all(
            isinstance(step, Mapping) for step in raw_steps
        ):
            return None
        return [
            dict(_json_copy(step, error_message="Gemini native replay is not JSON-safe"))
            for step in raw_steps
        ]

    def _can_replay_native_context(self, context: ModelContext) -> bool:
        return (
            self._context_affinity is not None
            and context.source_provider == self._provider_name
            and context.source_model == self._model
            and context.source_context_affinity == self._context_affinity
        )

    def _input_items(self, context: ModelContext) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        items: list[dict[str, Any]] = []
        replay_native = self._can_replay_native_context(context)
        skip_standard_assistant = False
        for item in context.items:
            if isinstance(item, PreservedContextItem):
                native = self._native_steps(item) if replay_native else None
                if native is not None:
                    items.extend(native)
                    skip_standard_assistant = True
                continue
            if skip_standard_assistant:
                if item.role is Role.ASSISTANT:
                    skip_standard_assistant = False
                    continue
                skip_standard_assistant = False
            if item.role is Role.SYSTEM:
                if item.model_content():
                    system_parts.append(item.model_content())
                continue
            standard = self._standard_input_item(item)
            if standard is not None:
                if standard.get("type") == "function_result" and not standard.get("call_id"):
                    raise ProviderError.classified(
                        ProviderFailureKind.INVALID_REQUEST,
                        "Gemini Interactions function result requires call_id",
                    )
                if standard.get("type") == "function_result":
                    call_id = standard.get("call_id")
                    for previous in reversed(items):
                        if previous.get("type") != "function_call":
                            continue
                        if previous.get("id") != call_id:
                            continue
                        signature = previous.get("signature")
                        if isinstance(signature, str) and signature:
                            standard["signature"] = signature
                        break
                items.append(standard)
            if item.role is Role.ASSISTANT:
                for call in item.tool_calls:
                    metadata = dict(call.metadata)
                    function_call: dict[str, Any] = {
                        "type": "function_call",
                        "name": call.name,
                        "id": metadata.get("gemini.call_id", call.id),
                        "arguments": dict(call.arguments),
                    }
                    signature = metadata.get(
                        "gemini.signature",
                        metadata.get("gemini.thought_signature"),
                    )
                    if isinstance(signature, str):
                        function_call["signature"] = signature
                    items.append(function_call)
        return ("\n\n".join(system_parts) or None), items

    def _request_body(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> dict[str, Any]:
        system, input_items = self._input_items(context)
        body: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "generation_config": {"max_output_tokens": self._max_output_tokens},
            "store": False,
            "stream": True,
        }
        if system is not None:
            body["system_instruction"] = system
        if tool_policy is ModelToolPolicy.ALLOWED:
            request_tools: list[dict[str, Any]] = []
            for name in self._builtin_tools:
                definition: dict[str, Any] = {"type": name}
                options = self._builtin_tool_options.get(name)
                if options:
                    definition.update(copy.deepcopy(options))
                request_tools.append(definition)
            builtin_names = set(self._builtin_tools)
            request_tools.extend(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                }
                for tool in tools
                if tool.name not in builtin_names
            )
            if request_tools:
                body["tools"] = request_tools
                tool_choice = self._tool_choice
                if (
                    tool_choice is None
                    and self._builtin_tools
                    and any(tool.name not in builtin_names for tool in tools)
                ):
                    # Google documents ``validated`` for tool-context
                    # circulation; ``auto`` is not supported for this mixed
                    # built-in/custom combination.
                    tool_choice = "validated"
                if tool_choice is not None:
                    body["generation_config"]["tool_choice"] = copy.deepcopy(tool_choice)
        return body

    def _safe_detail(self, detail: str) -> str:
        return self._http_policy.redact(detail, self._api_key)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def _usage(cls, interaction: Mapping[str, Any]) -> ModelUsage | None:
        raw_usage = interaction.get("usage")
        if not isinstance(raw_usage, Mapping):
            return None
        input_tokens = cls._token_count(raw_usage.get("total_input_tokens"))
        if input_tokens is None:
            input_tokens = cls._token_count(raw_usage.get("input_tokens"))
        if input_tokens is None:
            input_tokens = cls._token_count(raw_usage.get("prompt_tokens"))
        output_tokens = cls._token_count(raw_usage.get("total_output_tokens"))
        if output_tokens is None:
            output_tokens = cls._token_count(raw_usage.get("output_tokens"))
        if output_tokens is None:
            output_tokens = cls._token_count(raw_usage.get("completion_tokens"))
        cache_read_tokens = cls._token_count(raw_usage.get("total_cached_tokens"))
        if cache_read_tokens is None:
            cache_read_tokens = cls._token_count(raw_usage.get("cached_tokens"))
        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        return usage if usage.has_reported_tokens else None

    @staticmethod
    def _step_text_delta(step: dict[str, Any], delta: Mapping[str, Any]) -> str | None:
        text = delta.get("text")
        if not isinstance(text, str):
            return None
        content = step.setdefault("content", [])
        if not isinstance(content, list):
            raise ProviderError.protocol("Gemini Interactions model output content is invalid")
        if content and isinstance(content[-1], dict) and content[-1].get("type") == "text":
            content[-1]["text"] = f"{content[-1].get('text', '')}{text}"
        else:
            content.append({"type": "text", "text": text})
        annotations = delta.get("annotations")
        if isinstance(annotations, list):
            existing = content[-1].get("annotations")
            if isinstance(existing, list):
                existing.extend(copy.deepcopy(annotations))
            else:
                content[-1]["annotations"] = copy.deepcopy(annotations)
        return text

    @staticmethod
    def _step_thought_delta(step: dict[str, Any], delta: Mapping[str, Any]) -> str | None:
        text = delta.get("text")
        if not isinstance(text, str):
            return None
        summary = step.setdefault("summary", [])
        if not isinstance(summary, list):
            raise ProviderError.protocol("Gemini Interactions thought summary is invalid")
        if summary and isinstance(summary[-1], dict) and summary[-1].get("type") == "text":
            summary[-1]["text"] = f"{summary[-1].get('text', '')}{text}"
        else:
            summary.append({"type": "text", "text": text})
        return text

    @classmethod
    def _finish_function_arguments(
        cls,
        step: dict[str, Any],
        argument_buffer: str,
    ) -> dict[str, Any]:
        initial = step.get("arguments", {})
        if isinstance(initial, str):
            argument_buffer = f"{initial}{argument_buffer}"
            initial_arguments: object = {}
        else:
            initial_arguments = initial
        if not isinstance(initial_arguments, Mapping):
            raise ProviderError.protocol(
                "Gemini Interactions function call arguments must be a JSON object"
            )
        arguments: dict[str, Any] = dict(initial_arguments)
        if argument_buffer:
            try:
                parsed = json.loads(argument_buffer)
            except json.JSONDecodeError as error:
                raise ProviderError.protocol(
                    "Gemini Interactions function call contained invalid JSON arguments"
                ) from error
            if not isinstance(parsed, Mapping):
                raise ProviderError.protocol(
                    "Gemini Interactions function call arguments must be a JSON object"
                )
            arguments.update(parsed)
        step["arguments"] = copy.deepcopy(arguments)
        return arguments

    @classmethod
    def _function_call(
        cls, step: Mapping[str, Any], arguments: Mapping[str, Any], index: int
    ) -> ToolCall:
        name = step.get("name")
        identifier = step.get("id")
        if not isinstance(name, str) or not name:
            raise ProviderError.protocol("Gemini Interactions emitted an incomplete function call")
        if not isinstance(identifier, str) or not identifier:
            raise ProviderError.protocol("Gemini Interactions function call omitted id")
        call_id = step.get("call_id")
        canonical_id = call_id if isinstance(call_id, str) and call_id else identifier
        metadata: dict[str, Any] = {"gemini.call_id": canonical_id}
        signature = step.get("signature")
        if isinstance(signature, str) and signature:
            metadata["gemini.signature"] = signature
            metadata["gemini.thought_signature"] = signature
        if canonical_id != identifier:
            metadata["gemini.step_id"] = identifier
        return ToolCall(canonical_id, name, dict(arguments), metadata)

    @staticmethod
    def _backend_events(
        step: Mapping[str, Any],
        index: int,
        started: set[tuple[str, str]],
        completed: set[tuple[str, str]],
    ) -> tuple[ModelBackendToolStarted | ModelBackendToolCompleted, ...]:
        step_type = step.get("type")
        if step_type not in {
            "google_search_call",
            "google_search_result",
            "url_context_call",
            "url_context_result",
        }:
            return ()
        is_call = step_type.endswith("_call")
        name = "google_search" if step_type.startswith("google_search") else "url_context"
        identifier = _step_identity(step, index)
        if not is_call:
            call_id = step.get("call_id")
            if isinstance(call_id, str) and call_id:
                identifier = call_id
            matching = [
                key[0] for key in started if key[1] == name and (key[0], name) not in completed
            ]
            if len(matching) == 1 and (identifier, name) not in started:
                # Some current high-level examples omit the result call_id;
                # correlate an unambiguous single call without guessing across
                # multiple concurrent built-in tool executions.
                identifier = matching[0]
            key = (identifier, name)
            if key in completed or key not in started or _is_failure_step(step):
                return ()
            status = _string_status(step.get("status"))
            if status not in {None, *_SUCCESS_STATUSES}:
                return ()
            completed.add(key)
            return (ModelBackendToolCompleted(identifier, name),)
        key = (identifier, name)
        if key in started:
            return ()
        started.add(key)
        return (ModelBackendToolStarted(identifier, name),)

    @classmethod
    def _native_context_item(
        cls,
        *,
        provider: str,
        service: str,
        model: str,
        steps: Sequence[Mapping[str, Any]],
    ) -> PreservedContextItem:
        payload = {
            "type": "backend_tool_call",
            "kind": {
                "provider": provider,
                "service": service,
                "protocol": GEMINI_INTERACTIONS_PROTOCOL,
                "model": model,
                "tool_type": "gemini_interactions_steps",
                "native_type": "gemini_interactions_steps",
                "steps": [dict(step) for step in steps],
            },
        }
        try:
            if (
                len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                > MAX_NATIVE_CONTEXT_BYTES
            ):
                raise ProviderError.classified(
                    ProviderFailureKind.CONTEXT_OVERFLOW,
                    "Gemini Interactions native context exceeds its size limit",
                )
        except (TypeError, ValueError) as error:
            raise ProviderError.protocol(
                "Gemini Interactions native context is not JSON-safe"
            ) from error
        return PreservedContextItem(ContextItemKind.BACKEND_TOOL_CALL, payload)

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
        headers = {"x-goog-api-key": self._api_key, "accept": "text/event-stream"}
        steps_by_index: dict[int, dict[str, Any]] = {}
        argument_buffers: dict[int, str] = {}
        stopped: set[int] = set()
        started_backend: set[tuple[str, str]] = set()
        completed_backend: set[tuple[str, str]] = set()
        function_calls: list[ToolCall] = []
        streamed_text = False
        streamed_reasoning = False
        terminal_interaction: dict[str, Any] | None = None
        terminal_status: str | None = None
        interaction_id: str | None = None

        def process_event(event: Mapping[str, Any]) -> list[ModelEvent]:
            nonlocal \
                streamed_text, \
                streamed_reasoning, \
                terminal_interaction, \
                terminal_status, \
                interaction_id
            event_type = event.get("event_type", event.get("type"))
            if not isinstance(event_type, str):
                return []
            if event_type == "error":
                detail = json.dumps(event.get("error", event), ensure_ascii=False)
                raise ProviderError.classified(
                    classify_provider_failure(
                        ProviderFailureProtocol.GEMINI_INTERACTIONS,
                        json.dumps(event, ensure_ascii=False),
                    )
                    or ProviderFailureKind.UNKNOWN,
                    f"Gemini Interactions stream error: {self._safe_detail(detail)}",
                    provider=self._provider_name,
                    model=self._model,
                    origin=ProviderFailureOrigin.PROVIDER,
                    redaction_values=(self._api_key, *self._http_policy.redaction_values),
                )
            if event_type in {
                "interaction.created",
                "interaction.in_progress",
                "interaction.status_update",
            }:
                interaction = event.get("interaction")
                if isinstance(interaction, Mapping):
                    raw_id = interaction.get("id")
                    if isinstance(raw_id, str):
                        interaction_id = raw_id
                    status = _string_status(interaction.get("status"))
                    if status:
                        terminal_status = status
                raw_id = event.get("interaction_id")
                if isinstance(raw_id, str):
                    interaction_id = raw_id
                status = _string_status(event.get("status"))
                if status:
                    terminal_status = status
                if status in _FAILURE_STATUSES:
                    raise ProviderError.classified(
                        classify_provider_failure(
                            ProviderFailureProtocol.GEMINI_INTERACTIONS,
                            json.dumps(event, ensure_ascii=False),
                        )
                        or ProviderFailureKind.UNKNOWN,
                        f"Gemini Interactions failed with status {status}",
                        provider=self._provider_name,
                        model=self._model,
                        origin=ProviderFailureOrigin.PROVIDER,
                    )
                return []
            if event_type in {"interaction.requires_action", "interaction.completed"}:
                interaction = event.get("interaction")
                if not isinstance(interaction, Mapping):
                    raise ProviderError.protocol(
                        "Gemini Interactions terminal event omitted interaction"
                    )
                terminal_interaction = dict(interaction)
                raw_id = interaction.get("id")
                if isinstance(raw_id, str):
                    interaction_id = raw_id
                status = _string_status(interaction.get("status"))
                terminal_status = status or (
                    "requires_action" if event_type.endswith("requires_action") else "completed"
                )
                if terminal_status in _FAILURE_STATUSES:
                    raise ProviderError.classified(
                        classify_provider_failure(
                            ProviderFailureProtocol.GEMINI_INTERACTIONS,
                            json.dumps(event, ensure_ascii=False),
                        )
                        or ProviderFailureKind.UNKNOWN,
                        f"Gemini Interactions failed with status {terminal_status}",
                        provider=self._provider_name,
                        model=self._model,
                        origin=ProviderFailureOrigin.PROVIDER,
                    )
                return []
            if event_type == "step.start":
                index = event.get("index")
                step = event.get("step")
                if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                    raise ProviderError.protocol(
                        "Gemini Interactions emitted an invalid step index"
                    )
                if not isinstance(step, Mapping):
                    raise ProviderError.protocol(
                        "Gemini Interactions emitted an invalid step.start"
                    )
                normalized_step = dict(
                    _json_copy(step, error_message="Gemini Interactions step is not JSON-safe")
                )
                if index in steps_by_index:
                    if steps_by_index[index] != normalized_step:
                        raise ProviderError.protocol(
                            "Gemini Interactions emitted conflicting step.start"
                        )
                    return []
                steps_by_index[index] = normalized_step
                return list(
                    self._backend_events(
                        steps_by_index[index], index, started_backend, completed_backend
                    )
                )
            if event_type == "step.delta":
                index = event.get("index")
                delta = event.get("delta")
                if (
                    not isinstance(index, int)
                    or index not in steps_by_index
                    or not isinstance(delta, Mapping)
                ):
                    raise ProviderError.protocol(
                        "Gemini Interactions emitted an invalid step.delta"
                    )
                step = steps_by_index[index]
                delta_type = delta.get("type")
                events: list[ModelEvent] = []
                if delta_type == "text":
                    text = self._step_text_delta(step, delta)
                    if text:
                        streamed_text = True
                        events.append(ModelTextDelta(text))
                elif delta_type == "thought":
                    text = self._step_thought_delta(step, delta)
                    if text:
                        streamed_reasoning = True
                        events.append(ModelReasoningDelta(text))
                elif delta_type == "thought_signature":
                    signature = delta.get("signature")
                    if isinstance(signature, str):
                        step["signature"] = f"{step.get('signature', '')}{signature}"
                elif delta_type in {"arguments", "arguments_delta"}:
                    partial = delta.get("partial_arguments", delta.get("arguments"))
                    if not isinstance(partial, str):
                        raise ProviderError.protocol(
                            "Gemini Interactions function arguments delta is invalid"
                        )
                    argument_buffers[index] = f"{argument_buffers.get(index, '')}{partial}"
                else:
                    for key, value in delta.items():
                        if key != "type":
                            step[key] = copy.deepcopy(value)
                return events
            if event_type == "step.stop":
                index = event.get("index")
                if not isinstance(index, int) or index not in steps_by_index:
                    raise ProviderError.protocol("Gemini Interactions emitted an invalid step.stop")
                if index in stopped:
                    return []
                stopped.add(index)
                step = steps_by_index[index]
                step_type = step.get("type")
                events = list(self._backend_events(step, index, started_backend, completed_backend))
                if step_type == "function_call":
                    arguments = self._finish_function_arguments(
                        step, argument_buffers.get(index, "")
                    )
                    call = self._function_call(step, arguments, len(function_calls) + 1)
                    function_calls.append(call)
                    events.append(ModelToolCall(call))
                elif step_type == "model_output" and not streamed_text:
                    text = _model_output_text((step,))
                    if text:
                        streamed_text = True
                        events.append(ModelTextDelta(text))
                elif step_type == "thought" and not streamed_reasoning:
                    text = _thought_text((step,))
                    if text:
                        streamed_reasoning = True
                        events.append(ModelReasoningDelta(text))
                return events
            return []

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
                    raise ProviderError.from_http(
                        response.status_code,
                        detail,
                        headers=response.headers,
                        failure_kind=classify_provider_failure(
                            ProviderFailureProtocol.GEMINI_INTERACTIONS,
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
                            "Gemini Interactions returned malformed streaming JSON",
                            provider=self._provider_name,
                            model=self._model,
                        ) from error
                    if not isinstance(event, Mapping):
                        raise ProviderError.protocol(
                            "Gemini Interactions emitted a non-object streaming event",
                            provider=self._provider_name,
                            model=self._model,
                        )
                    for model_event in process_event(event):
                        yield model_event
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError.from_runtime(
                error,
                provider=self._provider_name,
                model=self._model,
                redaction_values=(self._api_key, *self._http_policy.redaction_values),
                prefix="Gemini Interactions stream failed",
            ) from error

        if terminal_interaction is None:
            raise ProviderError.protocol(
                "Gemini Interactions stream ended without a terminal interaction",
                provider=self._provider_name,
                model=self._model,
            )
        terminal_steps = terminal_interaction.get("steps")
        if isinstance(terminal_steps, list) and all(
            isinstance(step, Mapping) for step in terminal_steps
        ):
            steps = [
                dict(
                    _json_copy(
                        step, error_message="Gemini Interactions terminal steps are not JSON-safe"
                    )
                )
                for step in terminal_steps
            ]
        else:
            steps = [steps_by_index[index] for index in sorted(steps_by_index)]
        if not steps:
            raise ProviderError.protocol(
                "Gemini Interactions terminal interaction omitted steps",
                provider=self._provider_name,
                model=self._model,
            )
        if self._response_observer is not None:
            observed: dict[str, Any] = {
                "type": "interaction",
                "id": interaction_id or "",
                "model": self._model,
                "status": terminal_status or "completed",
                "steps": steps,
            }
            usage = terminal_interaction.get("usage")
            if isinstance(usage, Mapping):
                observed["usage"] = dict(usage)
            try:
                self._response_observer(observed)
            except Exception as error:
                raise ProviderError.local(
                    f"Gemini Interactions response observer failed: {type(error).__name__}"
                ) from error
        final_text = _model_output_text(steps)
        if not streamed_text and final_text:
            yield ModelTextDelta(final_text)
        reasoning = _thought_text(steps)
        if not streamed_reasoning and reasoning:
            yield ModelReasoningDelta(reasoning)
        source_footer = _source_footer(steps)
        if source_footer:
            yield ModelTextDelta(source_footer)
            final_text += source_footer
        usage = self._usage(terminal_interaction)
        context_item = self._native_context_item(
            provider=self._provider_name,
            service=self._service_id,
            model=self._model,
            steps=steps,
        )
        stop_reason = (
            "tool_calls" if function_calls or terminal_status == "requires_action" else "stop"
        )
        yield ModelCompleted(
            stop_reason,
            context_items=(context_item,),
            response_text=final_text,
            usage=usage,
        )


__all__ = [
    "GEMINI_INTERACTIONS_API_VERSION",
    "GEMINI_INTERACTIONS_PROTOCOL",
    "GeminiInteractionsProvider",
]
