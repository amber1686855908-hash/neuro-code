"""Canonical read-only provider-catalog port."""

from __future__ import annotations

from typing import Protocol

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.domain.provider_catalog import ProviderCatalogResult, ProviderConnectionSpec


class ProviderCatalog(Protocol):
    """Read-only provider connectivity and model-discovery boundary."""

    async def discover_models(
        self,
        spec: ProviderConnectionSpec,
        *,
        http_policy: HttpClientPolicy,
    ) -> ProviderCatalogResult: ...


__all__ = ["ProviderCatalog"]
