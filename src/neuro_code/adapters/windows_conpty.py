"""Compatibility facade for canonical Windows ConPTY infrastructure.

提供 Windows ConPTY 基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.windows_conpty import (
    WindowsPseudoConsoleSession,
)

__all__ = ["WindowsPseudoConsoleSession"]
