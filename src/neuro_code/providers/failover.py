from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import (
    ModelEvent,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.errors import ConfigurationError, ProviderError
from neuro_code.ports.model import ModelProvider

_FAILURE_MESSAGE_LIMIT = 500
_AGGREGATE_DETAIL_LIMIT = 2_000


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    name: str
    model: str
    context_affinity: str | None
    factory: Callable[[], ModelProvider]
    context_window_tokens: int | None = None


class FailoverModelProvider:
    """Monotonic provider chain that switches only before the first model event."""

    def __init__(self, candidates: Sequence[ProviderCandidate]) -> None:
        self._candidates = tuple(candidates)
        if len(self._candidates) < 2:
            raise ValueError("failover provider requires at least two candidates")
        if len({candidate.name for candidate in self._candidates}) != len(self._candidates):
            raise ValueError("failover provider candidates must have unique names")
        self._providers: dict[int, ModelProvider] = {}
        self._active_index: int | None = None
        self._announced_index: int | None = None

    @property
    def _current_candidate(self) -> ProviderCandidate:
        index = self._active_index if self._active_index is not None else 0
        return self._candidates[index]

    @property
    def provider_name(self) -> str:
        if self._active_index is not None and self._active_index in self._providers:
            return self._providers[self._active_index].provider_name
        return self._current_candidate.name

    @property
    def model_name(self) -> str:
        if self._active_index is not None and self._active_index in self._providers:
            return self._providers[self._active_index].model_name
        return self._current_candidate.model

    @property
    def context_affinity(self) -> str | None:
        if self._active_index is not None and self._active_index in self._providers:
            return self._providers[self._active_index].context_affinity
        return self._current_candidate.context_affinity

    def _provider(self, index: int) -> ModelProvider:
        provider = self._providers.get(index)
        if provider is None:
            provider = self._candidates[index].factory()
            self._providers[index] = provider
        return provider

    @staticmethod
    def _failure_message(error: ConfigurationError | ProviderError) -> str:
        return " ".join(str(error).split())[:_FAILURE_MESSAGE_LIMIT] or type(error).__name__

    @staticmethod
    def _aggregate_detail(failures: Sequence[tuple[str, str]]) -> str:
        detail = "; ".join(f"{name}: {message}" for name, message in failures)
        if len(detail) <= _AGGREGATE_DETAIL_LIMIT:
            return detail
        return f"{detail[: _AGGREGATE_DETAIL_LIMIT - 3]}..."

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        start_index = self._active_index if self._active_index is not None else 0
        failures: list[tuple[str, str]] = []
        for index in range(start_index, len(self._candidates)):
            candidate = self._candidates[index]
            try:
                provider = self._provider(index)
                iterator = provider.stream(context, tools)
                first_event = await anext(iterator)
            except (ConfigurationError, ProviderError) as error:
                message = self._failure_message(error)
                failures.append((candidate.name, message))
                yield ModelProviderAttemptFailed(
                    candidate.name,
                    candidate.model,
                    type(error).__name__,
                    message,
                )
                continue
            except StopAsyncIteration:
                message = "provider stream ended before emitting an event"
                failures.append((candidate.name, message))
                yield ModelProviderAttemptFailed(
                    candidate.name,
                    candidate.model,
                    "ProviderError",
                    message,
                )
                continue

            self._active_index = index
            if self._announced_index != index:
                self._announced_index = index
                yield ModelProviderSelected(
                    provider.provider_name,
                    provider.model_name,
                    provider.context_affinity,
                    failover=index > 0,
                    context_window_tokens=candidate.context_window_tokens,
                )
            yield first_event
            async for event in iterator:
                yield event
            return

        detail = self._aggregate_detail(failures)
        raise ProviderError(f"all configured model providers failed before output: {detail}")


__all__ = ["FailoverModelProvider", "ProviderCandidate"]
