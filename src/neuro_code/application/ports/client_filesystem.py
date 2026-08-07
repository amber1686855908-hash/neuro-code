"""Session-scoped text-file capability delegated to an ACP client.

定义委托给 ACP 客户端的会话范围文本文件能力."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ClientFileSystem(Protocol):
    """Capability-negotiated text file access owned by a connected client.

    Implementations must keep a request bound to its ACP session, preserve the
    workspace boundary chosen for that session, and fail closed when the client
    did not advertise the corresponding operation.

    定义由已连接客户端拥有且经过能力协商的文本文件访问.
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
