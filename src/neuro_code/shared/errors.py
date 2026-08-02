"""Stable application error hierarchy.

Exceptions may contain operational context but must never contain credentials.
The CLI maps these errors to deterministic exit codes.
"""


class NeuroCodeError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(NeuroCodeError):
    """Configuration is missing, invalid, or contradictory."""


class ProviderError(NeuroCodeError):
    """A model provider failed or returned an invalid stream."""


class ToolError(NeuroCodeError):
    """A tool request is invalid or could not be completed."""


class BackgroundTaskCapacityError(ToolError):
    """A managed task supervisor cannot accept another task right now."""


class PermissionDenied(ToolError):
    """A tool call was rejected by the permission policy."""


class SandboxError(NeuroCodeError):
    """A requested operating-system sandbox could not be enforced."""


class TerminalError(NeuroCodeError):
    """An interactive terminal request or owned session failed."""


class SessionError(NeuroCodeError):
    """Session persistence or reconstruction failed."""


__all__ = [
    "BackgroundTaskCapacityError",
    "ConfigurationError",
    "NeuroCodeError",
    "PermissionDenied",
    "ProviderError",
    "SandboxError",
    "SessionError",
    "TerminalError",
    "ToolError",
]
