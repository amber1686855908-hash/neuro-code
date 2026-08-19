"""Canonical runtime roles and model-route value objects.

定义规范的 Runtime Role 和 Model Route 值对象.

Routes are configuration projections with independent role ownership.  MAIN
continues to use the existing provider/fallback runtime, while the optional
WEB_SEARCH route is consumed by the hosted Web Search application boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from neuro_code.shared.errors import ConfigurationError


class RuntimeRole(StrEnum):
    MAIN = "main"
    WEB_SEARCH = "web_search"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """One role-to-profile/model binding with isolated fallbacks."""

    role: RuntimeRole
    provider_profile: str
    model: str
    fallback_profiles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.role, RuntimeRole):
            raise TypeError("model route role must be canonical")
        if not self.provider_profile.strip():
            raise ConfigurationError("model route provider profile must not be empty")
        if not self.model.strip():
            raise ConfigurationError("model route model must not be empty")
        if any(not profile.strip() for profile in self.fallback_profiles):
            raise ConfigurationError("model route fallback profiles must be non-empty")
        if len(set(self.fallback_profiles)) != len(self.fallback_profiles):
            raise ConfigurationError("model route fallback profiles must be unique")
        if self.provider_profile in self.fallback_profiles:
            raise ConfigurationError("model route fallbacks must not include the primary profile")

    def to_dict(self) -> dict[str, object]:
        """Return a credential-free inspection projection."""

        payload: dict[str, object] = {
            "role": self.role.value,
            "provider_profile": self.provider_profile,
            "model": self.model,
            "fallbacks": list(self.fallback_profiles),
        }
        return payload


__all__ = ["ModelRoute", "RuntimeRole"]
