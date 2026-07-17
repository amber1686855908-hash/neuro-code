from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class HttpClientPolicy:
    """Resolved HTTPX client policy without secret-bearing representation."""

    trust_env: bool = True
    proxy_url: str | None = field(default=None, repr=False)
    redaction_values: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.trust_env and self.proxy_url is not None:
            raise ValueError("an explicit proxy requires trust_env=False")
        values = tuple(dict.fromkeys(value for value in self.redaction_values if value))
        object.__setattr__(self, "redaction_values", values)

    def client_options(
        self,
        *,
        timeout: object,
        transport: object | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"timeout": timeout, "trust_env": self.trust_env}
        if self.proxy_url is not None:
            options["proxy"] = self.proxy_url
        if transport is not None:
            options["transport"] = transport
        return options

    def redact(self, detail: str, *additional_values: str, limit: int = 1_000) -> str:
        redacted = detail
        values = (*self.redaction_values, *additional_values)
        for value in sorted({value for value in values if value}, key=len, reverse=True):
            redacted = redacted.replace(value, "[REDACTED]")
        return redacted[:limit]
