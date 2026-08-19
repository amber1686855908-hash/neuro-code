"""Concrete local Web Fetch infrastructure adapters."""

from neuro_code.infrastructure.web_fetch.local import (
    LocalWebFetcher,
    is_public_destination,
)

__all__ = ["LocalWebFetcher", "is_public_destination"]
