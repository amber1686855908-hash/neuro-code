"""Session-scoped text-file capability delegated to an ACP client."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ClientFileSystem(Protocol):
    """Capability-negotiated text file access owned by a connected client.

    Implementations must keep a request bound to its ACP session, preserve the
    workspace boundary chosen for that session, and fail closed when the client
    did not advertise the corresponding operation.
    """

    @property
    def supports_read(self) -> bool: ...

    @property
    def supports_write(self) -> bool: ...

    async def read_text_file(
        self,
        path: Path,
        /,
        *,
        line: int | None = None,
        limit: int | None = None,
    ) -> str: ...

    async def write_text_file(self, path: Path, content: str, /) -> None: ...


__all__ = ["ClientFileSystem"]
