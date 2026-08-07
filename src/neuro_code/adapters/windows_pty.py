"""Compatibility facade for the canonical Windows PTY adapter.

提供 Windows PTY 适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.windows_pty import (
    WindowsConPtyPlatform,
    WindowsConPtySession,
)

__all__ = ["WindowsConPtyPlatform", "WindowsConPtySession"]
