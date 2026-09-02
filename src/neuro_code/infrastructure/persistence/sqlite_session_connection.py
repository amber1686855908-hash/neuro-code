"""SQLite connection and lifecycle primitives."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

from neuro_code.infrastructure.persistence.sqlite_session_constants import _SQLITE_TIMEOUT_SECONDS


class _SqliteSessionPersistenceContext:
    """Type contract for state supplied by the composed SQLite store."""

    _database_path: Path
    _write_lock: asyncio.Lock

    if TYPE_CHECKING:

        def _connect(self) -> sqlite3.Connection: ...


class SqliteSessionConnectionMixin(_SqliteSessionPersistenceContext):
    """Own the SQLite connection policy shared by persistence slices."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._write_lock = asyncio.Lock()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=_SQLITE_TIMEOUT_SECONDS)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1_000)}")
            deadline = time.monotonic() + _SQLITE_TIMEOUT_SECONDS
            while True:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    return connection
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).casefold() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        except BaseException:
            connection.close()
            raise
