"""Typed application seam for changing the active provider profile.

The profile conversation controller remains the owner of binding replacement,
turn serialization, background-task scope shutdown, and provider validation.
This module exposes only the inbound application intent and delegates to that
owner; it does not construct providers or manage conversation resources.

定义切换活动 Provider 配置档案的类型化应用接缝. 绑定替换、回合串行化和资源清理仍由现有控制器负责.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.providers.contracts import (
    ProviderOption,
    ProviderSelectionResult,
)


@dataclass(frozen=True, slots=True)
class ChangeProviderRequest:
    """Validated intent to select one configured provider profile.

    表示选择一个已配置 Provider 配置档案的已验证意图."""

    profile_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile_name, str) or not self.profile_name.strip():
            raise ValueError("profile_name must not be empty")


class ProviderProfileController(Protocol):
    """Minimal existing owner consumed by the application facade.

    表示应用门面使用的最小现有所有者契约."""

    @property
    def profiles(self) -> tuple[ProviderOption, ...]: ...

    @property
    def selected_profile(self) -> str: ...

    async def select_profile(self, name: str) -> ProviderSelectionResult: ...


class ProviderChangeService:
    """Expose ChangeProvider without owning provider or binding lifecycle.

    暴露 ChangeProvider 用例,但不拥有 Provider 或绑定生命周期."""

    __slots__ = ("_controller",)

    def __init__(self, controller: ProviderProfileController) -> None:
        self._controller = controller

    @property
    def profiles(self) -> tuple[ProviderOption, ...]:
        return self._controller.profiles

    @property
    def selected_profile(self) -> str:
        return self._controller.selected_profile

    async def change_provider(self, request: ChangeProviderRequest) -> ProviderSelectionResult:
        """Delegate a typed request while preserving cancellation and errors.

        委托类型化请求,同时保留取消和错误语义."""

        if not isinstance(request, ChangeProviderRequest):
            raise ValueError("change provider request must be canonical")
        return await self._controller.select_profile(request.profile_name)


__all__ = [
    "ChangeProviderRequest",
    "ProviderChangeService",
    "ProviderProfileController",
]
