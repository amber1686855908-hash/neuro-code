from __future__ import annotations

from enum import StrEnum


class UiLanguage(StrEnum):
    """Languages supported by the application-owned terminal interface."""

    ENGLISH = "en"
    SIMPLIFIED_CHINESE = "zh-CN"


__all__ = ["UiLanguage"]
