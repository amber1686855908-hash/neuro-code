"""Compatibility facade for the canonical UI-preferences persistence adapter.

提供 UI 偏好持久化适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.persistence.ui_preferences import JsonUiPreferencesStore

__all__ = ["JsonUiPreferencesStore"]
