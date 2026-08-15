"""Safe, structured diagnostics for Windows sandbox authority boundaries.

Diagnostics deliberately contain only operation names, exception type names,
and bounded numeric OS facts.  They never copy exception messages, commands,
paths, credentials, or payload bytes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any


def _safe_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


@dataclass(frozen=True, slots=True)
class WindowsSandboxOperationDiagnostic:
    """Safe facts about one native setup operation failure."""

    operation: str | None
    cause_type: str
    winerror: int | None = None
    errno: int | None = None
    returncode: int | None = None
    timed_out: bool = False

    def as_dict(self) -> dict[str, object]:
        """Return only machine-readable, non-secret fields."""

        return {
            "operation": self.operation,
            "cause_type": self.cause_type,
            "winerror": self.winerror,
            "errno": self.errno,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
        }


def diagnostic_for_exception(
    error: BaseException,
    *,
    operation: str | None = None,
) -> WindowsSandboxOperationDiagnostic:
    """Extract safe facts without inspecting or serializing error messages."""

    existing = getattr(error, "safe_diagnostic", None)
    if isinstance(existing, WindowsSandboxOperationDiagnostic):
        if operation is None or existing.operation == operation:
            return existing
        return WindowsSandboxOperationDiagnostic(
            operation,
            existing.cause_type,
            existing.winerror,
            existing.errno,
            existing.returncode,
            existing.timed_out,
        )

    return WindowsSandboxOperationDiagnostic(
        operation,
        type(error).__name__,
        _safe_int(getattr(error, "winerror", None)),
        _safe_int(getattr(error, "errno", None)),
        _safe_int(getattr(error, "returncode", None)),
        isinstance(error, subprocess.TimeoutExpired) or bool(getattr(error, "timed_out", False)),
    )


def diagnostic_kwargs(error: BaseException, *, operation: str | None = None) -> dict[str, Any]:
    """Return constructor kwargs for a typed Windows operation error."""

    diagnostic = diagnostic_for_exception(error, operation=operation)
    return {
        "safe_diagnostic": diagnostic,
    }


__all__ = [
    "WindowsSandboxOperationDiagnostic",
    "diagnostic_for_exception",
    "diagnostic_kwargs",
]
