"""Compatibility facade for the canonical POSIX PTY adapter.

提供 POSIX PTY 适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.posix_pty import PosixPtyPlatform, PosixPtySession

__all__ = ["PosixPtyPlatform", "PosixPtySession"]
