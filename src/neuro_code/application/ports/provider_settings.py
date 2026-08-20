"""Canonical provider-settings value objects and persistence port.

定义规范的 Provider 设置值对象和持久化端口."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from neuro_code.application.ports.model import ModelCapabilitySet
from neuro_code.application.ports.provider_services import (
    DEFAULT_PROVIDER_SERVICE_CATALOG,
    SUPPORTED_PROTOCOLS,
    ProtocolSupportStatus,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskWakePolicy
from neuro_code.shared.errors import ConfigurationError

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_PROFILES = 64
_MAX_MODEL_CHARACTERS = 512
_MAX_URL_CHARACTERS = 2_048
_MAX_API_KEY_CHARACTERS = 16_384
_ENVIRONMENT_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SUPPORTED_PROXY_MODES = frozenset({"environment", "direct", "explicit"})


@dataclass(frozen=True, slots=True)
class ManagedProxyPolicy:
    """One non-secret proxy policy for managed provider configuration.

    表示受管理 Provider 配置使用的单个非秘密代理策略."""

    mode: str = "environment"
    proxy_url_env: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in _SUPPORTED_PROXY_MODES:
            raise ConfigurationError(
                "provider proxy_mode must be 'environment', 'direct', or 'explicit'"
            )
        if self.mode == "explicit":
            if (
                self.proxy_url_env is None
                or _ENVIRONMENT_VARIABLE.fullmatch(self.proxy_url_env) is None
            ):
                raise ConfigurationError(
                    "provider explicit proxy mode requires a valid proxy environment variable"
                )
        elif self.proxy_url_env is not None:
            raise ConfigurationError(
                "provider proxy environment variable requires proxy_mode 'explicit'"
            )


@dataclass(frozen=True, slots=True)
class ManagedProviderProfile:
    """One user-managed provider profile; its credential is never represented.

    表示一个用户管理的 Provider 配置档案,其中永不表示凭据."""

    name: str
    protocol: str
    model: str
    base_url: str
    dialect: str = "standard"
    service_id: str | None = None
    capability_overrides: ModelCapabilitySet = field(
        default_factory=ModelCapabilitySet.all_unknown,
        repr=False,
    )
    context_window_tokens: int | None = None
    proxy_mode: str | None = None
    proxy_url_env: str | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)
    background_task_wake_policy: BackgroundTaskWakePolicy | None = None

    def __post_init__(self) -> None:
        if _PROFILE_NAME.fullmatch(self.name) is None:
            raise ConfigurationError(
                "provider profile name must start with a letter or number and contain only "
                "letters, numbers, '.', '_' or '-' (maximum 64 characters)"
            )
        if not self.protocol:
            raise ConfigurationError("provider protocol must not be empty")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigurationError(f"unsupported provider protocol: {self.protocol}")
        if self.dialect not in {"standard", "xai", "deepseek-v4"}:
            raise ConfigurationError(f"unsupported provider dialect: {self.dialect}")
        if self.dialect == "xai" and self.protocol != "openai-responses":
            raise ConfigurationError("xAI dialect requires protocol 'openai-responses'")
        if self.dialect == "deepseek-v4" and self.protocol != "openai-chat":
            raise ConfigurationError("DeepSeek V4 dialect requires protocol 'openai-chat'")
        if self.service_id is not None and not self.service_id.strip():
            raise ConfigurationError("provider service_id must not be empty")
        if not isinstance(self.capability_overrides, ModelCapabilitySet):
            raise ConfigurationError("provider capability overrides must be canonical")
        if not self.model.strip():
            raise ConfigurationError("provider model must not be empty")
        if len(self.model) > _MAX_MODEL_CHARACTERS:
            raise ConfigurationError("provider model is too long")
        if not self.base_url.strip():
            raise ConfigurationError("provider base URL must not be empty")
        if len(self.base_url) > _MAX_URL_CHARACTERS:
            raise ConfigurationError("provider base URL is too long")
        try:
            parsed = urlsplit(self.base_url.strip())
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
        protocol_status = DEFAULT_PROVIDER_SERVICE_CATALOG.protocol_support_for_profile(
            service_id=self.service_id,
            protocol=self.protocol,
            dialect=self.dialect,
            base_url=self.base_url,
            model=self.model,
        )
        if protocol_status is ProtocolSupportStatus.UNSUPPORTED:
            raise ConfigurationError(
                f"provider service {self.service_id!r} does not document protocol "
                f"{self.protocol!r} for model {self.model!r}"
            )
        if self.context_window_tokens is not None and (
            isinstance(self.context_window_tokens, bool) or self.context_window_tokens <= 0
        ):
            raise ConfigurationError("provider context_window_tokens must be positive")
        if self.proxy_mode is None:
            if self.proxy_url_env is not None:
                raise ConfigurationError(
                    "provider proxy environment variable requires a proxy override"
                )
        else:
            ManagedProxyPolicy(self.proxy_mode, self.proxy_url_env)
        if self.api_key is not None and not self.api_key.strip():
            object.__setattr__(self, "api_key", None)
        if self.api_key is not None and len(self.api_key) > _MAX_API_KEY_CHARACTERS:
            raise ConfigurationError("provider API key is too long")
        if self.background_task_wake_policy is not None and not isinstance(
            self.background_task_wake_policy, BackgroundTaskWakePolicy
        ):
            raise ConfigurationError("provider background task wake policy must be canonical")

    def effective_proxy_policy(self, defaults: ManagedProxyPolicy) -> ManagedProxyPolicy:
        """Return this profile's explicit policy or the user-wide default.

        返回当前配置档案的显式策略,否则返回用户范围的默认策略."""

        if self.proxy_mode is None:
            return defaults
        return ManagedProxyPolicy(self.proxy_mode, self.proxy_url_env)

    def effective_background_task_wake_policy(
        self,
        default: BackgroundTaskWakePolicy,
    ) -> BackgroundTaskWakePolicy:
        """Return the profile override or the user-wide default.

        返回配置档案覆盖值,否则返回用户范围的默认值."""

        return self.background_task_wake_policy or default


@dataclass(frozen=True, slots=True)
class ManagedProviderSettings:
    profiles: tuple[ManagedProviderProfile, ...] = ()
    default_provider: str | None = None
    proxy_defaults: ManagedProxyPolicy = field(default_factory=ManagedProxyPolicy)
    background_task_wake_policy: BackgroundTaskWakePolicy = BackgroundTaskWakePolicy.DISABLED

    def __post_init__(self) -> None:
        if len(self.profiles) > _MAX_PROFILES:
            raise ConfigurationError("too many managed provider profiles")
        names = tuple(profile.name for profile in self.profiles)
        if len(names) != len(set(names)):
            raise ConfigurationError("managed provider profile names must be unique")
        if self.default_provider is not None and self.default_provider not in names:
            raise ConfigurationError("managed default provider does not exist")
        if not isinstance(self.background_task_wake_policy, BackgroundTaskWakePolicy):
            raise ConfigurationError("background task wake policy must be canonical")

    def profile(self, name: str) -> ManagedProviderProfile | None:
        return next((profile for profile in self.profiles if profile.name == name), None)

    def effective_background_task_wake_policy(
        self,
        provider_name: str | None,
    ) -> BackgroundTaskWakePolicy:
        """Resolve one profile against the persisted user-wide default.

        根据已持久化的用户范围默认值解析一个配置档案."""

        profile = self.profile(provider_name) if provider_name is not None else None
        if profile is None:
            return self.background_task_wake_policy
        return profile.effective_background_task_wake_policy(self.background_task_wake_policy)


class ProviderSettingsStore(Protocol):
    """Persist user-owned model profiles without exposing storage details to the TUI.

    持久化用户拥有的模型配置档案,但不向 TUI 暴露存储细节."""

    async def load(self) -> ManagedProviderSettings: ...

    async def save_profile(
        self,
        profile: ManagedProviderProfile,
        *,
        make_default: bool = True,
    ) -> ManagedProviderSettings: ...

    async def set_default(self, name: str) -> ManagedProviderSettings: ...

    async def save_proxy_defaults(
        self,
        proxy_defaults: ManagedProxyPolicy,
    ) -> ManagedProviderSettings: ...

    async def save_background_task_wake_policy(
        self,
        policy: BackgroundTaskWakePolicy,
    ) -> ManagedProviderSettings: ...

    async def delete_profile(self, name: str) -> ManagedProviderSettings: ...


__all__ = [
    "ManagedProviderProfile",
    "ManagedProviderSettings",
    "ManagedProxyPolicy",
    "ProviderSettingsStore",
]
