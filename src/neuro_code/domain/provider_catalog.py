"""Compatibility facade for the canonical provider-catalog port contract.

提供 Provider 目录端口契约的兼容门面,并重新导出规范实现."""

from neuro_code.application.ports.provider_catalog import (
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)

__all__ = [
    "ProviderCatalogError",
    "ProviderCatalogResult",
    "ProviderConnectionSpec",
]
