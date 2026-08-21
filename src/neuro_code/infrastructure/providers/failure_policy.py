"""Canonical policy for typed model-provider failures.

Provider adapters report facts in ``shared.errors``. This module is the only
owner of the independent retry, circuit, and pre-output failover decisions.

类型化模型 Provider 失败的规范策略.

Provider 适配器在 ``shared.errors`` 中报告事实,本模块是重试、熔断和首个输出前故障转移
三个独立决策的唯一所有者.
"""

from __future__ import annotations

from dataclasses import dataclass

from neuro_code.shared.errors import (
    ConfigurationError,
    ProviderError,
    ProviderFailure,
    ProviderFailureKind,
)


@dataclass(frozen=True, slots=True)
class ProviderFailureDecision:
    """Independent actions selected for one failure fact."""

    retry: bool
    counts_toward_circuit: bool
    failover: bool


_POLICY: dict[ProviderFailureKind, ProviderFailureDecision] = {
    ProviderFailureKind.AUTHENTICATION: ProviderFailureDecision(False, False, True),
    ProviderFailureKind.AUTHORIZATION: ProviderFailureDecision(False, False, True),
    ProviderFailureKind.RATE_LIMIT: ProviderFailureDecision(True, False, True),
    ProviderFailureKind.INVALID_REQUEST: ProviderFailureDecision(False, False, False),
    ProviderFailureKind.MODEL_NOT_FOUND: ProviderFailureDecision(False, False, True),
    ProviderFailureKind.CONTEXT_OVERFLOW: ProviderFailureDecision(False, False, True),
    ProviderFailureKind.SERVER: ProviderFailureDecision(True, True, True),
    ProviderFailureKind.TIMEOUT: ProviderFailureDecision(True, True, True),
    ProviderFailureKind.NETWORK: ProviderFailureDecision(True, True, True),
    ProviderFailureKind.PROTOCOL: ProviderFailureDecision(False, False, True),
    # Unknown failures are conservative: do not repeat the same request, mark
    # the provider unhealthy, and isolate the candidate before any output.
    ProviderFailureKind.UNKNOWN: ProviderFailureDecision(False, True, True),
}


class ProviderFailurePolicy:
    """Pure policy boundary for provider failure facts."""

    @staticmethod
    def decide(
        failure: ProviderFailure,
        *,
        output_observed: bool = False,
    ) -> ProviderFailureDecision:
        if output_observed:
            # A partial stream cannot be safely replayed or switched. It also
            # does not prove that a clean provider request failed.
            return ProviderFailureDecision(False, False, False)
        return _POLICY[failure.kind]

    @staticmethod
    def decide_error(
        error: BaseException,
        *,
        output_observed: bool = False,
        provider: str | None = None,
        model: str | None = None,
    ) -> ProviderFailureDecision:
        if isinstance(error, ConfigurationError):
            if output_observed:
                return ProviderFailureDecision(False, False, False)
            # Configuration remains a separate error hierarchy. A configured
            # candidate can still be skipped, but it must not poison a
            # transient model-provider circuit.
            return ProviderFailureDecision(False, False, True)
        failure = ProviderError.failure_for_exception(
            error,
            provider=provider,
            model=model,
        )
        return ProviderFailurePolicy.decide(failure, output_observed=output_observed)


__all__ = ["ProviderFailureDecision", "ProviderFailurePolicy"]
