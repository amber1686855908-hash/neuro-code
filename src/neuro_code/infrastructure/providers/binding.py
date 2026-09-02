"""Resolve concrete provider runtime inputs at the infrastructure boundary.

将环境凭据和可选 HTTP 能力解析为 Provider 运行时绑定,不泄漏到 application port.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.util import find_spec

from neuro_code.application.ports.configuration import ProviderProfile
from neuro_code.application.ports.http import HttpClientPolicy


@dataclass(frozen=True, slots=True)
class ProviderRuntimeBinding:
    """Concrete credential and HTTP policy required by one provider adapter."""

    api_key: str = field(repr=False)
    http_policy: HttpClientPolicy


def socks_support_available() -> bool:
    """Return whether the optional HTTPX SOCKS transport is installed."""

    return find_spec("socksio") is not None


def resolve_provider_binding(
    profile: ProviderProfile,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProviderRuntimeBinding:
    """Resolve environment-backed provider inputs for a concrete adapter."""

    source = os.environ if environ is None else environ
    return ProviderRuntimeBinding(
        api_key=profile.api_key(source),
        http_policy=profile.http_client_policy(
            source,
            socks_supported=socks_support_available(),
        ),
    )


__all__ = [
    "ProviderRuntimeBinding",
    "resolve_provider_binding",
    "socks_support_available",
]
