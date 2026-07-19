from __future__ import annotations

from collections.abc import Mapping


def windows_environment_block(environment: Mapping[str, str]) -> str:
    """Build a sorted double-null-terminated Unicode environment block."""

    entries: list[tuple[str, str]] = []
    for name, value in environment.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("environment names and values must be strings")
        special_drive_name = name.startswith("=") and "=" not in name[1:]
        if (
            not name
            or ("=" in name and not special_drive_name)
            or "\x00" in name
            or "\x00" in value
        ):
            raise ValueError("environment contains an invalid name or value")
        entries.append((name, value))
    entries.sort(key=lambda entry: entry[0].casefold())
    return "".join(f"{name}={value}\x00" for name, value in entries) + "\x00"


__all__ = ["windows_environment_block"]
