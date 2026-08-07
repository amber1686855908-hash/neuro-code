"""Typed provider projections shared by application entry points.

定义由应用入口共享的类型化 Provider 投影,不拥有 Provider 生命周期.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """A bounded, presentation-safe view of one configured provider.

    表示一个已配置 Provider 的有界且适合展示的视图.
    """

    name: str
    protocol: str
    model: str
    available: bool
    credential_configured: bool
    default: bool = False
    selected: bool = False
    context_window_tokens: int | None = None

    @property
    def selectable(self) -> bool:
        """Return whether this option may be selected by an interface.

        返回该选项是否可以被界面选中.
        """

        return self.available and self.credential_configured


@dataclass(frozen=True, slots=True)
class ProviderSelectionResult:
    """Describe the observable result of selecting a provider profile.

    描述选择 Provider 配置档案后对入口可见的结果.
    """

    profile_name: str
    provider_name: str
    model_name: str
    previous_session_id: str | None
    changed: bool
    stopped_background_tasks: int = 0
    context_window_tokens: int | None = None


__all__ = ["ProviderOption", "ProviderSelectionResult"]
