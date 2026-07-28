"""Application-layer public settings export."""

from __future__ import annotations

from typing import Any

__all__ = ["ApplicationSettings"]


def __getattr__(name: str) -> Any:
    """Lazily provide the package-level settings value type."""
    if name != "ApplicationSettings":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from neuro_code.application.settings import ApplicationSettings

    return ApplicationSettings
