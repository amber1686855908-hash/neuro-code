from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from neuro_code.domain.messages import Message, PreservedContextItem, SessionItem

UPSTREAM_IMPORT_PROVIDER = "upstream-rust-import"


@dataclass(frozen=True, slots=True)
class ModelContext:
    """Ordered model input plus the session origin used for replay decisions."""

    items: tuple[SessionItem, ...]
    source_provider: str | None = None
    source_model: str | None = None
    source_context_affinity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if (self.source_provider is None) != (self.source_model is None):
            raise ValueError("model context source provider and model must be set together")
        if self.source_provider == "" or self.source_model == "":
            raise ValueError("model context source fields must not be empty")
        if self.source_context_affinity == "":
            raise ValueError("model context source affinity must not be empty")
        if self.source_context_affinity is not None and self.source_provider is None:
            raise ValueError("model context source affinity requires provider/model origin")

    @classmethod
    def from_messages(cls, messages: Sequence[Message]) -> ModelContext:
        return cls(tuple(messages))

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(item for item in self.items if isinstance(item, Message))

    @property
    def preserved_items(self) -> tuple[PreservedContextItem, ...]:
        return tuple(item for item in self.items if isinstance(item, PreservedContextItem))
