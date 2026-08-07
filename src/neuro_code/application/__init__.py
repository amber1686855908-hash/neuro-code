"""Application-layer public settings export.

应用层公开设置的导出入口."""

from __future__ import annotations

from typing import Any

__all__ = ["ApplicationSettings"]


def __getattr__(name: str) -> Any:
    """Lazily provide the package-level settings value type.

    按需提供包级别的设置类型."""
    if name != "ApplicationSettings":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from neuro_code.application.settings import ApplicationSettings

    return ApplicationSettings
