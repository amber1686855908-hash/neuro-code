"""Compatibility facade for the canonical bubblewrap sandbox adapter.

提供 bubblewrap 沙箱适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.sandbox.sandbox import (
    LinuxBubblewrapSandbox,
    create_shell_sandbox,
    enforce_configured_sandbox,
)

__all__ = [
    "LinuxBubblewrapSandbox",
    "create_shell_sandbox",
    "enforce_configured_sandbox",
]
