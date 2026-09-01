"""Safe deterministic projection and publication of parent conversation context."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from neuro_code.application.ports.parent_context_relay import (
    ParentContextRelayError,
    ParentContextRelayStore,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.sessions.item_queries import (
    LoadSessionItemsRequest,
    SessionItemQueryService,
)
from neuro_code.domain.conversation.messages import Message, Role, SessionItem
from neuro_code.domain.parent_context_relay import (
    MAX_PARENT_RELAY_ITEM_BYTES,
    MAX_PARENT_RELAY_ITEMS,
    MAX_PARENT_RELAY_PROJECTED_BYTES,
    ParentContextRelay,
    ParentContextRelayItem,
)
from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceLease
from neuro_code.shared.errors import ConfigurationError
from neuro_code.shared.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from neuro_code.application.sessions.binding import ConversationBinding


def _now() -> datetime:
    return datetime.now(UTC)


def _bounded_utf8_text(value: str, *, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    suffix = "..."
    if limit <= len(suffix):
        return suffix[:limit], True
    prefix = encoded[: limit - len(suffix)].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}", True


def _eligible_visible_text(item: SessionItem) -> str | None:
    if not isinstance(item, Message):
        return None
    if item.role not in {Role.USER, Role.ASSISTANT} or item.synthetic_reason is not None:
        return None
    if (
        item.name is not None
        or item.tool_call_id is not None
        or item.tool_calls
        or item.content_parts
        or not item.content
    ):
        return None
    return item.content


def project_parent_context_items(
    source_items: Sequence[SessionItem],
    *,
    redaction_values: Iterable[str] = (),
) -> tuple[tuple[ParentContextRelayItem, ...], bool]:
    """Select newest safe text under count, per-item, and total UTF-8 byte bounds."""

    selected: list[ParentContextRelayItem] = []
    total = 0
    truncated = False
    normalized_redaction_values = tuple(redaction_values)
    for source_index in range(len(source_items) - 1, -1, -1):
        visible = _eligible_visible_text(source_items[source_index])
        if visible is None:
            continue
        if len(selected) >= MAX_PARENT_RELAY_ITEMS:
            truncated = True
            break
        redacted = redact_sensitive_text(
            visible,
            explicit_values=normalized_redaction_values,
        )
        if not redacted:
            continue
        per_item_text, per_item_truncated = _bounded_utf8_text(
            redacted,
            limit=MAX_PARENT_RELAY_ITEM_BYTES,
        )
        remaining = MAX_PARENT_RELAY_PROJECTED_BYTES - total
        if remaining <= 0:
            truncated = True
            break
        bounded_text, total_truncated = _bounded_utf8_text(per_item_text, limit=remaining)
        if not bounded_text:
            truncated = True
            break
        item = source_items[source_index]
        assert isinstance(item, Message)
        selected.append(
            ParentContextRelayItem(
                source_index=source_index,
                role=item.role,
                text=bounded_text,
                truncated=per_item_truncated or total_truncated,
            )
        )
        total += len(bounded_text.encode("utf-8"))
        truncated = truncated or per_item_truncated or total_truncated
        if total_truncated:
            break
    selected.reverse()
    return tuple(selected), truncated


class ParentContextRelayApplicationService:
    """Publish a relay only from the actual durable parent binding session."""

    __slots__ = (
        "_clock",
        "_parent_binding",
        "_parent_session_id",
        "_queries",
        "_redaction_values",
        "_relay_store",
    )

    def __init__(
        self,
        store: SessionStore,
        relay_store: ParentContextRelayStore,
        *,
        parent_binding: ConversationBinding,
        redaction_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        from neuro_code.application.sessions.binding import (
            ConversationBinding as CanonicalConversationBinding,
        )

        if not isinstance(parent_binding, CanonicalConversationBinding):
            raise ConfigurationError("parent relay requires the actual parent binding")
        parent_session_id = parent_binding.runner.session_id
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ConfigurationError("parent relay requires a durable parent session")
        self._queries = SessionItemQueryService(store)
        self._relay_store = relay_store
        self._parent_binding = parent_binding
        self._parent_session_id = parent_session_id
        self._redaction_values = tuple(redaction_values)
        self._clock = clock

    async def publish(
        self,
        lease: WritableSubagentWorkspaceLease,
        *,
        prompt: str,
    ) -> ParentContextRelay:
        if not isinstance(lease, WritableSubagentWorkspaceLease):
            raise ConfigurationError("parent relay lease must be canonical")
        if lease.parent_session_id != self._parent_session_id:
            raise ConfigurationError("parent relay lease does not belong to the actual parent")
        if self._parent_binding.runner.session_id != self._parent_session_id:
            raise ConfigurationError("parent relay binding identity changed")
        if (
            lease.child_session_id is None
            or lease.baseline_checkpoint_id is None
            or lease.capability_fingerprint is None
            or lease.grant_fingerprint is None
            or lease.worktree is None
        ):
            raise ConfigurationError("parent relay worker identity is incomplete")
        source = await self._queries.load_session_items(
            LoadSessionItemsRequest(self._parent_session_id)
        )
        items, truncated = project_parent_context_items(
            source,
            redaction_values=self._redaction_values,
        )
        relay = ParentContextRelay.create(
            relay_id=f"pcr-{uuid.uuid4().hex}",
            parent_session_id=self._parent_session_id,
            parent_task_id=lease.parent_task_id,
            child_session_id=lease.child_session_id,
            lease_id=lease.lease_id,
            worktree_id=lease.worktree_id,
            baseline_checkpoint_id=lease.baseline_checkpoint_id,
            base_commit_sha=lease.base_commit_sha,
            capability_fingerprint=lease.capability_fingerprint,
            grant_fingerprint=lease.grant_fingerprint,
            task_prompt_fingerprint=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            source_item_count=len(source),
            items=items,
            truncated=truncated,
            created_at=self._clock().astimezone(UTC),
        )
        try:
            published = await self._relay_store.insert_parent_context_relay(relay)
            verified = await self._relay_store.get_parent_context_relay(relay.relay_id)
        except ParentContextRelayError as error:
            raise ConfigurationError(f"parent context relay publication failed: {error}") from error
        if verified is None or verified != published or published != relay:
            raise ConfigurationError("parent context relay durability could not be verified")
        return verified


__all__ = [
    "ParentContextRelayApplicationService",
    "project_parent_context_items",
]
