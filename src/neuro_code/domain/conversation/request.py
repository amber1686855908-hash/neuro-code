"""Deterministic, non-secret model request evidence.

模型请求的确定性、非秘密证据。

The runtime sends the exact same ``ModelContext`` and tool-definition tuple to
the provider that it used to build a snapshot.  The snapshot deliberately
stores fingerprints and bounded shape information rather than prompt bodies,
tool arguments, or credentials.  A caller that still owns the source context
can therefore verify an exact reconstruction without turning the event log
into a second prompt store.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.messages import Message, SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.tools import ToolDefinition

REQUEST_SNAPSHOT_SCHEMA_VERSION = 1
MAX_REQUEST_SNAPSHOT_ID_BYTES = 128


def _canonical(value: Any) -> Any:
    """Convert supported JSON-like values into a stable JSON tree."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        return _canonical(value.value)
    raise TypeError(f"unsupported request snapshot value: {type(value).__name__}")


def _digest(value: Any) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _item_shape(item: SessionItem) -> dict[str, Any]:
    if isinstance(item, Message):
        return {
            "kind": "message",
            "role": item.role.value,
            "name": item.name,
            "tool_call_id": item.tool_call_id,
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "metadata": call.metadata,
                }
                for call in item.tool_calls
            ],
            "content_parts": [part.to_dict() for part in item.content_parts],
            "content": item.content,
            "reasoning_content": item.reasoning_content,
            "synthetic_reason": (
                item.synthetic_reason.value if item.synthetic_reason is not None else None
            ),
        }
    return {"kind": "preserved_context", "payload": item.payload}


def _stable_items(items: Sequence[SessionItem]) -> tuple[SessionItem, ...]:
    return tuple(items)


def _dynamic_items(items: Sequence[SessionItem]) -> tuple[SessionItem, ...]:
    return tuple(
        item for item in items if isinstance(item, Message) and item.synthetic_reason is not None
    )


@dataclass(frozen=True, slots=True)
class RequestContextFingerprints:
    """Fingerprints for stable and runtime-owned context segments."""

    context: str
    stable: str
    dynamic: str


def context_fingerprints(items: Sequence[SessionItem]) -> RequestContextFingerprints:
    normalized = _stable_items(items)
    dynamic = _dynamic_items(normalized)
    stable = tuple(item for item in normalized if item not in dynamic)
    return RequestContextFingerprints(
        context=_digest([_item_shape(item) for item in normalized]),
        stable=_digest([_item_shape(item) for item in stable]),
        dynamic=_digest([_item_shape(item) for item in dynamic]),
    )


@dataclass(frozen=True, slots=True)
class ModelRequestSnapshot:
    """Auditable identity of one concrete provider request.

    ``request_payload`` is intentionally private to the in-memory object and
    is never included by :meth:`to_event_data`.  It lets the runtime verify
    the exact source that was used, while the durable event retains only safe
    fingerprints and shape metadata.
    """

    request_id: str
    step: int
    provider: str
    model: str
    context_affinity: str | None
    reasoning_effort: ReasoningEffort
    tool_policy: str
    context_fingerprint: str
    stable_context_fingerprint: str
    dynamic_context_fingerprint: str
    tool_schema_fingerprint: str
    request_fingerprint: str
    message_count: int
    tool_count: int
    request_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be non-empty")
        if len(self.request_id.encode("utf-8")) > MAX_REQUEST_SNAPSHOT_ID_BYTES:
            raise ValueError("request_id is too large")
        if isinstance(self.step, bool) or not isinstance(self.step, int) or self.step < 0:
            raise ValueError("step must be a non-negative integer")
        for name in ("provider", "model", "tool_policy"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            raise TypeError("reasoning_effort must be a ReasoningEffort")
        for name in (
            "context_fingerprint",
            "stable_context_fingerprint",
            "dynamic_context_fingerprint",
            "tool_schema_fingerprint",
            "request_fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"{name} must be a SHA-256 hex digest")
        for name in ("message_count", "tool_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.request_payload, Mapping):
            raise TypeError("request_payload must be a mapping")
        object.__setattr__(self, "request_payload", MappingProxyType(dict(self.request_payload)))

    @classmethod
    def build(
        cls,
        *,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        provider: str,
        model: str,
        context_affinity: str | None,
        step: int,
        reasoning_effort: ReasoningEffort,
        tool_policy: str = "allowed",
        request_id: str | None = None,
    ) -> ModelRequestSnapshot:
        if not isinstance(context, ModelContext):
            raise TypeError("context must be a ModelContext")
        definitions = tuple(tools)
        if not all(isinstance(tool, ToolDefinition) for tool in definitions):
            raise TypeError("tools must contain ToolDefinition values")
        fingerprints = context_fingerprints(context.items)
        tool_payload = [tool.to_dict() for tool in definitions]
        payload: dict[str, Any] = {
            "context": [_item_shape(item) for item in context.items],
            "tools": tool_payload,
            "provider": provider,
            "model": model,
            "context_affinity": context_affinity,
            "reasoning_effort": reasoning_effort.value,
            "tool_policy": tool_policy,
        }
        return cls(
            request_id=request_id or f"request-{uuid.uuid4().hex}",
            step=step,
            provider=provider,
            model=model,
            context_affinity=context_affinity,
            reasoning_effort=reasoning_effort,
            tool_policy=tool_policy,
            context_fingerprint=fingerprints.context,
            stable_context_fingerprint=fingerprints.stable,
            dynamic_context_fingerprint=fingerprints.dynamic,
            tool_schema_fingerprint=_digest(tool_payload),
            request_fingerprint=_digest(payload),
            message_count=len(context.items),
            tool_count=len(definitions),
            request_payload=payload,
        )

    def verify_reconstruction(
        self,
        *,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        provider: str | None = None,
        model: str | None = None,
        context_affinity: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        tool_policy: str | None = None,
    ) -> None:
        """Fail if the supplied source cannot reproduce this request exactly."""

        rebuilt = self.build(
            context=context,
            tools=tools,
            provider=provider or self.provider,
            model=model or self.model,
            context_affinity=(
                self.context_affinity if context_affinity is None else context_affinity
            ),
            step=self.step,
            reasoning_effort=reasoning_effort or self.reasoning_effort,
            tool_policy=tool_policy or self.tool_policy,
            request_id=self.request_id,
        )
        if rebuilt.request_fingerprint != self.request_fingerprint:
            raise ValueError("model request snapshot reconstruction mismatch")

    def to_event_data(self) -> dict[str, Any]:
        return {
            "schema_version": REQUEST_SNAPSHOT_SCHEMA_VERSION,
            "request_id": self.request_id,
            "step": self.step,
            "provider": self.provider,
            "model": self.model,
            "context_affinity": self.context_affinity,
            "reasoning_effort": self.reasoning_effort.value,
            "tool_policy": self.tool_policy,
            "context_fingerprint": self.context_fingerprint,
            "stable_context_fingerprint": self.stable_context_fingerprint,
            "dynamic_context_fingerprint": self.dynamic_context_fingerprint,
            "tool_schema_fingerprint": self.tool_schema_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "payload_omitted": True,
        }


__all__ = [
    "MAX_REQUEST_SNAPSHOT_ID_BYTES",
    "REQUEST_SNAPSHOT_SCHEMA_VERSION",
    "ModelRequestSnapshot",
    "RequestContextFingerprints",
    "context_fingerprints",
]
