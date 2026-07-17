from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelEvent
from neuro_code.domain.tools import ToolDefinition


class ModelProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def context_affinity(self) -> str | None: ...

    def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]: ...
