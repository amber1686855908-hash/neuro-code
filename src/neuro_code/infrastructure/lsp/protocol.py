"""Bounded stdio JSON-RPC framing for Language Server Protocol."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

MAX_LSP_HEADER_BYTES = 16 * 1024
MAX_LSP_MESSAGE_BYTES = 1 * 1024 * 1024
MAX_LSP_JSON_DEPTH = 32
MAX_LSP_JSON_NODES = 16_384
MAX_LSP_HEADER_LINES = 32


class LspByteStream(Protocol):
    async def read(self, n: int = -1, /) -> bytes: ...


class LspByteWriter(Protocol):
    async def write_stdin(self, data: bytes) -> None: ...


class LspProtocolError(ValueError):
    """The current LSP session received an invalid or oversized frame."""


def _reject_constant(value: str) -> None:
    raise LspProtocolError(f"non-finite JSON number is not allowed: {value}")


def _validate_json_shape(value: object, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    counts = nodes if nodes is not None else [0]
    counts[0] += 1
    if counts[0] > MAX_LSP_JSON_NODES:
        raise LspProtocolError("JSON node limit exceeded")
    if depth > MAX_LSP_JSON_DEPTH:
        raise LspProtocolError("JSON nesting depth limit exceeded")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise LspProtocolError("JSON object key must be text")
            _validate_json_shape(child, depth=depth + 1, nodes=counts)
    elif isinstance(value, list):
        for child in value:
            _validate_json_shape(child, depth=depth + 1, nodes=counts)


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Encode one JSON-RPC object using the LSP Content-Length framing."""

    if not isinstance(message, Mapping):
        raise LspProtocolError("LSP message must be a JSON object")
    _validate_json_shape(message)
    try:
        payload = json.dumps(
            dict(message),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LspProtocolError(f"LSP message is not JSON serializable: {error}") from error
    if len(payload) > MAX_LSP_MESSAGE_BYTES:
        raise LspProtocolError("LSP message exceeds the bounded payload limit")
    return b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload


class LspFrameReader:
    """Incrementally parse bounded LSP frames from a byte stream."""

    def __init__(self, stream: LspByteStream) -> None:
        self._stream = stream
        self._buffer = bytearray()

    async def read_message(self) -> dict[str, Any] | None:
        header_end = await self._read_header_end()
        if header_end is None:
            return None
        header = bytes(self._buffer[:header_end])
        del self._buffer[: header_end + 4]
        content_length = self._parse_content_length(header)
        await self._read_exact(content_length)
        body = bytes(self._buffer[:content_length])
        del self._buffer[:content_length]
        try:
            decoded = body.decode("utf-8")
            message = json.loads(decoded, parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LspProtocolError(f"invalid LSP JSON payload: {error}") from error
        if not isinstance(message, dict):
            raise LspProtocolError("LSP JSON-RPC payload must be an object")
        _validate_json_shape(message)
        return message

    async def _read_header_end(self) -> int | None:
        marker = b"\r\n\r\n"
        while True:
            location = self._buffer.find(marker)
            if location >= 0:
                if location > MAX_LSP_HEADER_BYTES:
                    raise LspProtocolError("LSP header exceeds the bounded limit")
                return location
            if len(self._buffer) > MAX_LSP_HEADER_BYTES:
                raise LspProtocolError("LSP header exceeds the bounded limit")
            chunk = await self._stream.read(min(4096, MAX_LSP_HEADER_BYTES + 4 - len(self._buffer)))
            if not chunk:
                if not self._buffer:
                    return None
                raise LspProtocolError("unexpected EOF in LSP header")
            self._buffer.extend(chunk)

    @staticmethod
    def _parse_content_length(header: bytes) -> int:
        try:
            text = header.decode("ascii")
        except UnicodeDecodeError as error:
            raise LspProtocolError("LSP headers must be ASCII") from error
        lines = text.split("\r\n")
        if len(lines) > MAX_LSP_HEADER_LINES:
            raise LspProtocolError("too many LSP header lines")
        content_lengths: list[int] = []
        for line in lines:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator or not name.strip() or not value.strip():
                raise LspProtocolError("malformed LSP header")
            if name.casefold() == "content-length":
                try:
                    parsed = int(value.strip(), 10)
                except ValueError as error:
                    raise LspProtocolError("LSP Content-Length is not an integer") from error
                content_lengths.append(parsed)
        if len(content_lengths) != 1:
            raise LspProtocolError("LSP frame requires exactly one Content-Length header")
        content_length = content_lengths[0]
        if not 0 <= content_length <= MAX_LSP_MESSAGE_BYTES:
            raise LspProtocolError("LSP Content-Length exceeds the bounded limit")
        return content_length

    async def _read_exact(self, length: int) -> None:
        while len(self._buffer) < length:
            chunk = await self._stream.read(length - len(self._buffer))
            if not chunk:
                raise LspProtocolError("unexpected EOF in LSP payload")
            self._buffer.extend(chunk)


async def read_message(stream: LspByteStream) -> dict[str, Any] | None:
    """Read one frame; callers that need multiple frames should retain a reader."""

    return await LspFrameReader(stream).read_message()


async def write_message(writer: LspByteWriter, message: Mapping[str, Any]) -> None:
    """Write one bounded frame through the owned-process port."""

    await writer.write_stdin(encode_message(message))


__all__ = [
    "MAX_LSP_HEADER_BYTES",
    "MAX_LSP_JSON_DEPTH",
    "MAX_LSP_JSON_NODES",
    "MAX_LSP_MESSAGE_BYTES",
    "LspFrameReader",
    "LspProtocolError",
    "encode_message",
    "read_message",
    "write_message",
]
