"""Compatibility facade for the canonical provider catalog adapter.

提供 Provider 目录适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.provider_catalog import HttpProviderCatalog

__all__ = ["HttpProviderCatalog"]
