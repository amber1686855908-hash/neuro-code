"""Compatibility facade for the canonical failover provider adapter.

提供故障转移 Provider 适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.failover import (
    FailoverModelProvider,
    ProviderCandidate,
)

__all__ = ["FailoverModelProvider", "ProviderCandidate"]
