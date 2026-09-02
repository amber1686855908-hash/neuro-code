"""Bounded output projection for filesystem tools.

有界文件系统工具输出投影.
"""

from __future__ import annotations

from neuro_code.application.ports.tools import ToolContext
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text

_OUTPUT_TRUNCATION_MARKER = "\n[output truncated]"


def _bounded_output(value: str, *, byte_limit: int) -> tuple[str, bool]:
    if isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit < 1:
        raise ToolError("output_byte_limit must be positive")
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value, False
    marker = _OUTPUT_TRUNCATION_MARKER.encode("utf-8")
    if byte_limit <= len(marker):
        return marker[:byte_limit].decode("utf-8", "ignore"), True
    prefix = encoded[: byte_limit - len(marker)].decode("utf-8", "ignore")
    return f"{prefix}{_OUTPUT_TRUNCATION_MARKER}", True


def _safe_bounded_output(value: str, context: ToolContext) -> tuple[str, bool]:
    redacted = redact_sensitive_text(value, explicit_values=context.redaction_values)
    return _bounded_output(redacted, byte_limit=context.output_byte_limit)


def _numbered_lines(content: str, *, start_line: int) -> str:
    return "\n".join(
        f"{number:>6}\t{line}" for number, line in enumerate(content.splitlines(), start=start_line)
    )


__all__: list[str] = []
