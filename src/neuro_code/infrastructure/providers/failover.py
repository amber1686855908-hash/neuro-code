"""Canonical failover provider infrastructure adapter.

定义规范的故障转移 Provider 基础设施适配器."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

from neuro_code.application.ports.model import (
    ModelCapabilitySet,
    ModelProvider,
    ModelToolPolicy,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelEvent,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
)
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.providers.failure_policy import ProviderFailurePolicy
from neuro_code.shared.errors import ConfigurationError, ProviderError, ProviderFailureKind
from neuro_code.shared.redaction import redact_sensitive_text

_FAILURE_MESSAGE_LIMIT = 500
_AGGREGATE_DETAIL_LIMIT = 2_000


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    name: str
    model: str
    context_affinity: str | None
    factory: Callable[[], ModelProvider]
    context_window_tokens: int | None = None
    capabilities: ModelCapabilitySet = field(default_factory=ModelCapabilitySet.all_unknown)


class FailoverModelProvider:
    """Monotonic provider chain that switches only before the first model event.

    定义单调 Provider 链,只允许在第一个模型事件之前切换.

    Before a candidate is selected, capability inspection returns the safe
    intersection of every configured candidate. Once a candidate is selected,
    inspection follows that active provider.
    """

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

    @property
    def capabilities(self) -> ModelCapabilitySet:
        if self._active_index is not None and self._active_index in self._providers:
            return self._providers[self._active_index].capabilities
        return ModelCapabilitySet.intersection(
            candidate.capabilities for candidate in self._candidates
        )

    def _provider(self, index: int) -> ModelProvider:
        provider = self._providers.get(index)
        if provider is None:
            provider = self._candidates[index].factory()
            self._providers[index] = provider
        return provider

    @staticmethod
    def _failure_message(error: ConfigurationError | ProviderError) -> str:
        detail = (
            error.failure.detail
            if isinstance(error, ProviderError)
            else redact_sensitive_text(str(error))
        )
        return " ".join(detail.split())[:_FAILURE_MESSAGE_LIMIT] or type(error).__name__

    @staticmethod
    def _failure_projection(
        error: ConfigurationError | ProviderError,
    ) -> tuple[str | None, int | None]:
        if not isinstance(error, ProviderError):
            return None, None
        return error.failure.kind.value, error.failure.status_code

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
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        start_index = self._active_index if self._active_index is not None else 0
        failures: list[tuple[str, str]] = []
        for index in range(start_index, len(self._candidates)):
            candidate = self._candidates[index]
            try:
                provider = self._provider(index)
                iterator = provider.stream(context, tools, tool_policy=tool_policy)
                first_event = await anext(iterator)
            except (ConfigurationError, ProviderError) as error:
                message = self._failure_message(error)
                failures.append((candidate.name, message))
                failure_kind, status_code = self._failure_projection(error)
                yield ModelProviderAttemptFailed(
                    candidate.name,
                    candidate.model,
                    type(error).__name__,
                    message,
                    failure_kind,
                    status_code,
                )
                if not ProviderFailurePolicy.decide_error(error).failover:
                    raise
                continue
            except StopAsyncIteration:
                empty_error = ProviderError.protocol(
                    "provider stream ended before emitting an event",
                    provider=candidate.name,
                    model=candidate.model,
                )
                message = self._failure_message(empty_error)
                failures.append((candidate.name, message))
                yield ModelProviderAttemptFailed(
                    candidate.name,
                    candidate.model,
                    "ProviderError",
                    message,
                    empty_error.failure.kind.value,
                    empty_error.failure.status_code,
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
        raise ProviderError.classified(
            ProviderFailureKind.UNKNOWN,
            f"all configured model providers failed before output: {detail}",
        )


__all__ = ["FailoverModelProvider", "ProviderCandidate"]
