"""Compatibility facade for the canonical provider settings adapter.

提供 Provider 设置适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.providers.provider_settings import JsonProviderSettingsStore

__all__ = ["JsonProviderSettingsStore"]
