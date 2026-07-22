from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from neuro_code.errors import ConfigurationError

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_PROFILES = 64
_MAX_MODEL_CHARACTERS = 512
_MAX_URL_CHARACTERS = 2_048
_MAX_API_KEY_CHARACTERS = 16_384
_SUPPORTED_PROTOCOLS = frozenset(
    {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini-generate-content",
    }
)


@dataclass(frozen=True, slots=True)
class ManagedProviderProfile:
    """One user-managed provider profile; its credential is never represented."""

    name: str
    protocol: str
    model: str
    base_url: str
    dialect: str = "standard"
    api_key: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if _PROFILE_NAME.fullmatch(self.name) is None:
            raise ConfigurationError(
                "provider profile name must start with a letter or number and contain only "
                "letters, numbers, '.', '_' or '-' (maximum 64 characters)"
            )
        if not self.protocol:
            raise ConfigurationError("provider protocol must not be empty")
        if self.protocol not in _SUPPORTED_PROTOCOLS:
            raise ConfigurationError(f"unsupported provider protocol: {self.protocol}")
        if self.dialect not in {"standard", "xai"}:
            raise ConfigurationError(f"unsupported provider dialect: {self.dialect}")
        if self.dialect == "xai" and self.protocol != "openai-responses":
            raise ConfigurationError("xAI dialect requires protocol 'openai-responses'")
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
        if self.api_key is not None and not self.api_key.strip():
            object.__setattr__(self, "api_key", None)
        if self.api_key is not None and len(self.api_key) > _MAX_API_KEY_CHARACTERS:
            raise ConfigurationError("provider API key is too long")


@dataclass(frozen=True, slots=True)
class ManagedProviderSettings:
    profiles: tuple[ManagedProviderProfile, ...] = ()
    default_provider: str | None = None

    def __post_init__(self) -> None:
        if len(self.profiles) > _MAX_PROFILES:
            raise ConfigurationError("too many managed provider profiles")
        names = tuple(profile.name for profile in self.profiles)
        if len(names) != len(set(names)):
            raise ConfigurationError("managed provider profile names must be unique")
        if self.default_provider is not None and self.default_provider not in names:
            raise ConfigurationError("managed default provider does not exist")

    def profile(self, name: str) -> ManagedProviderProfile | None:
        return next((profile for profile in self.profiles if profile.name == name), None)


__all__ = ["ManagedProviderProfile", "ManagedProviderSettings"]
