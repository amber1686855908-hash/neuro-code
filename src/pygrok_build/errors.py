"""Stable application error hierarchy.

Exceptions may contain operational context but must never contain credentials.
The CLI maps these errors to deterministic exit codes.
"""


class PyGrokBuildError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(PyGrokBuildError):
    """Configuration is missing, invalid, or contradictory."""


class ProviderError(PyGrokBuildError):
    """A model provider failed or returned an invalid stream."""


class ToolError(PyGrokBuildError):
    """A tool request is invalid or could not be completed."""


class PermissionDenied(ToolError):
    """A tool call was rejected by the permission policy."""


class SessionError(PyGrokBuildError):
    """Session persistence or reconstruction failed."""
