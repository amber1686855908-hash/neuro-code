"""Bounded provider retry, circuit, and health instrumentation.

有界 Provider 重试、熔断和健康状态观测.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from time import monotonic

from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider, ModelToolPolicy
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelEvent
from neuro_code.domain.tools import ToolDefinition
from neuro_code.shared.errors import ConfigurationError, ProviderError


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    attempts: int
    successes: int
    consecutive_failures: int
    circuit_open: bool
    last_error_type: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "attempts": self.attempts,
            "successes": self.successes,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.circuit_open,
            "last_error_type": self.last_error_type,
        }


class ResilientModelProvider:
    """Retry only pre-output failures and fail closed when the circuit opens."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_attempts: int = 2,
        backoff_seconds: float = 0.05,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> None:
        if not 1 <= max_attempts <= 4:
            raise ValueError("provider max_attempts must be between 1 and 4")
        if backoff_seconds < 0 or backoff_seconds > 10:
            raise ValueError("provider backoff_seconds is out of bounds")
        if not 1 <= failure_threshold <= 10:
            raise ValueError("provider failure_threshold is out of bounds")
        if cooldown_seconds <= 0 or cooldown_seconds > 600:
            raise ValueError("provider cooldown_seconds is out of bounds")
        self._provider = provider
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._opened_until = 0.0
        self._attempts = 0
        self._successes = 0
        self._consecutive_failures = 0
        self._last_error_type: str | None = None

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    @property
    def context_affinity(self) -> str | None:
        return self._provider.context_affinity

    @property
    def capabilities(self) -> ModelCapabilitySet:
        return self._provider.capabilities

    @property
    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_name,
            self._attempts,
            self._successes,
            self._consecutive_failures,
            self._circuit_is_open(),
            self._last_error_type,
        )

    def _circuit_is_open(self) -> bool:
        if self._opened_until <= 0:
            return False
        if monotonic() >= self._opened_until:
            self._opened_until = 0.0
            return False
        return True

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        if isinstance(error, ConfigurationError):
            return False
        if isinstance(error, ProviderError):
            message = str(error).casefold()
            return not any(
                marker in message
                for marker in ("authentication", "unauthorized", "forbidden", "401", "403")
            )
        return isinstance(error, (TimeoutError, OSError, asyncio.TimeoutError))

    def _record_failure(self, error: BaseException) -> None:
        self._consecutive_failures += 1
        self._last_error_type = type(error).__name__
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_until = monotonic() + self._cooldown_seconds

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        if self._circuit_is_open():
            raise ProviderError("provider circuit is open")
        for attempt in range(self._max_attempts):
            self._attempts += 1
            emitted = False
            try:
                iterator = self._provider.stream(context, tools, tool_policy=tool_policy)
                async for event in iterator:
                    emitted = True
                    yield event
                self._successes += 1
                self._consecutive_failures = 0
                self._last_error_type = None
                return
            except asyncio.CancelledError:
                raise
            except (ConfigurationError, ProviderError, OSError, TimeoutError) as error:
                self._record_failure(error)
                if emitted or not self._retryable(error) or attempt + 1 >= self._max_attempts:
                    raise
                if self._backoff_seconds:
                    await asyncio.sleep(self._backoff_seconds * (attempt + 1))


__all__ = ["ProviderHealth", "ResilientModelProvider"]
