"""Compatibility exports for the provider-settings port value objects.

Provider configuration records are application port contracts rather than domain
business entities.  Keep this historical import path as a one-way facade while
callers migrate to :mod:`neuro_code.application.ports.provider_settings`.

导出 Provider 设置端口的值对象,并保留旧导入路径作为单向兼容门面.
"""

from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
)

__all__ = [
    "ManagedProviderProfile",
    "ManagedProviderSettings",
    "ManagedProxyPolicy",
]
