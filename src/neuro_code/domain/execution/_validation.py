"""Private validation helpers shared by execution value objects.

提供执行值对象共享的私有验证辅助函数."""

from __future__ import annotations

import math


def require_positive_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def require_non_negative_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def require_optional_positive_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return require_positive_int(value, field_name=field_name)


def require_optional_positive_seconds(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be a finite positive number or None")
    return float(value)


def require_digest(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def require_tool_name(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty canonical tool name")
    return value


def require_optional_non_negative_tokens(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return require_non_negative_int(value, field_name=field_name)


__all__ = [
    "require_digest",
    "require_non_negative_int",
    "require_optional_non_negative_tokens",
    "require_optional_positive_int",
    "require_optional_positive_seconds",
    "require_positive_int",
    "require_tool_name",
]
