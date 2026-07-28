from __future__ import annotations

import re
from collections.abc import Iterable

_SECRET_KEY = (
    r"(?:[a-z0-9.]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
    r"secret|password|passwd|authorization|private[_-]?key)"
)
_QUOTED_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_SECRET_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\r\n]*?)(?P=quote)"
)
_UNQUOTED_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_SECRET_KEY}[\"']?\s*[:=]\s*)(?P<value>[^\s,;\"']+)"
)
_BEARER_CREDENTIAL = re.compile(r"(?i)(\bbearer\s+)[a-z0-9._~+/=-]{8,}")
_URL_PASSWORD = re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]+(@)")
_TOKEN_SHAPE = re.compile(r"\b(?:sk|xai|gh[pousr])[-_][a-z0-9_-]{8,}\b", re.IGNORECASE)


def redact_sensitive_text(text: str, *, explicit_values: Iterable[str] = ()) -> str:
    """Redact likely credentials from text intended for logs or the local UI.

    Explicit configured values are replaced first. Conservative shape and assignment
    matching then covers common credentials that originated in workspace files or tool
    output rather than the active provider configuration.
    """

    redacted = text
    for value in dict.fromkeys(value for value in explicit_values if value):
        redacted = redacted.replace(value, "[REDACTED]")
    redacted = _URL_PASSWORD.sub(r"\1[REDACTED]\2", redacted)
    redacted = _BEARER_CREDENTIAL.sub(r"\1[REDACTED]", redacted)
    redacted = _TOKEN_SHAPE.sub("[REDACTED]", redacted)
    redacted = _QUOTED_SECRET_ASSIGNMENT.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        redacted,
    )
    return _UNQUOTED_SECRET_ASSIGNMENT.sub(r"\g<prefix>[REDACTED]", redacted)


__all__ = ["redact_sensitive_text"]
