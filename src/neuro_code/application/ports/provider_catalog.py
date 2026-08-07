"""Canonical provider-catalog request, result, and port contracts.

定义规范的 Provider 目录请求,结果和端口契约."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.shared.errors import ConfigurationError, ProviderError

_SUPPORTED_PROTOCOLS = frozenset(
    {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini-generate-content",
    }
)
_SUPPORTED_DIALECTS = frozenset({"standard", "xai"})
_MAX_URL_CHARACTERS = 2_048
_MAX_API_KEY_CHARACTERS = 16_384
_MAX_MODELS = 200
_MAX_MODEL_CHARACTERS = 512


@dataclass(frozen=True, slots=True)
class ProviderConnectionSpec:
    """Credential-bearing, ephemeral input for a read-only provider probe.

    表示只读 Provider 探测使用的临时凭据输入,不作为持久状态保存."""

    protocol: str
    base_url: str
    api_key: str = field(repr=False)
    dialect: str = "standard"

    def __post_init__(self) -> None:
        if self.protocol not in _SUPPORTED_PROTOCOLS:
            raise ConfigurationError(f"unsupported provider protocol: {self.protocol}")
        if self.dialect not in _SUPPORTED_DIALECTS:
            raise ConfigurationError(f"unsupported provider dialect: {self.dialect}")
        if self.dialect == "xai" and self.protocol != "openai-responses":
            raise ConfigurationError("xAI dialect requires protocol 'openai-responses'")
        base_url = self.base_url.strip()
        if not base_url:
            raise ConfigurationError("provider base URL must not be empty")
        if len(base_url) > _MAX_URL_CHARACTERS:
            raise ConfigurationError("provider base URL is too long")
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError as error:
            raise ConfigurationError("provider base URL is invalid") from error
        if parsed.scheme not in {"http", "https"} or hostname is None:
            raise ConfigurationError("provider base URL must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError("provider base URL must not contain user information")
        if parsed.query or parsed.fragment:
            raise ConfigurationError("provider base URL must not contain a query or fragment")
        api_key = self.api_key.strip()
        if not api_key:
            raise ConfigurationError("provider connection test requires an API key")
        if len(api_key) > _MAX_API_KEY_CHARACTERS:
            raise ConfigurationError("provider API key is too long")
        object.__setattr__(self, "base_url", base_url.rstrip("/"))
        object.__setattr__(self, "api_key", api_key)


@dataclass(frozen=True, slots=True)
class ProviderCatalogResult:
    """A bounded model catalog safe to keep in memory and render in the TUI.

    表示可安全保存在内存并渲染到 TUI 的有界模型目录."""

    models: tuple[str, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        if len(self.models) > _MAX_MODELS:
            raise ValueError("provider model catalog exceeds the entry limit")
        if len(self.models) != len(set(self.models)):
            raise ValueError("provider model catalog contains duplicate identifiers")
        if any(
            not model
            or len(model) > _MAX_MODEL_CHARACTERS
            or any(ord(character) < 32 or ord(character) == 127 for character in model)
            for model in self.models
        ):
            raise ValueError("provider model catalog contains an invalid identifier")


class ProviderCatalogError(ProviderError):
    """Classified, redacted provider discovery failure for local UI recovery.

    表示经过分类和脱敏的 Provider 发现失败,供本地 UI 恢复处理."""

    def __init__(
        self,
        kind: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.detail = detail
        status = f" (HTTP {status_code})" if status_code is not None else ""
        suffix = f": {detail}" if detail else ""
        super().__init__(f"provider model discovery failed: {kind}{status}{suffix}")


class ProviderCatalog(Protocol):
    """Read-only provider connectivity and model-discovery boundary.

    定义只读的 Provider 连接和模型发现边界."""

    async def discover_models(
        self,
        spec: ProviderConnectionSpec,
        *,
        http_policy: HttpClientPolicy,
    ) -> ProviderCatalogResult: ...


__all__ = [
    "ProviderCatalog",
    "ProviderCatalogError",
    "ProviderCatalogResult",
    "ProviderConnectionSpec",
]
