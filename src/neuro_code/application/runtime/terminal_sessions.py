"""Compatibility facade for the application terminal-session owner.

The bounded interactive terminal lifecycle is application session behavior, not
AgentRuntime kernel behavior.  Keep this import path temporarily for callers
that still use the historical runtime module.

提供兼容门面,代理应用层终端会话所有者.
"""

from neuro_code.application.sessions.terminal_sessions import (
    LocalInteractiveTerminalManager,
    LocalInteractiveTerminalSession,
)

__all__ = ["LocalInteractiveTerminalManager", "LocalInteractiveTerminalSession"]
