from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from pygrok_build.domain.messages import Message
from pygrok_build.domain.model_events import ModelEvent
from pygrok_build.domain.tools import ToolDefinition


class ModelProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]: ...
