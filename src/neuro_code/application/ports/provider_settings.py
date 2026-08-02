"""Canonical provider-settings persistence port."""

from __future__ import annotations

from typing import Protocol

from neuro_code.domain.background_tasks import BackgroundTaskWakePolicy
from neuro_code.domain.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
)


class ProviderSettingsStore(Protocol):
    """Persist user-owned model profiles without exposing storage details to the TUI."""

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


__all__ = ["ProviderSettingsStore"]
