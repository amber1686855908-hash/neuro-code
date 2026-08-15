"""Versioned, binary-safe protocol used by the Windows native runtime.

The controller and the trusted Windows runner communicate over a controller
owned local pipe.  Frames are deliberately independent from text encodings so
MCP payloads, NUL bytes, and output larger than a pipe write remain lossless.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Final

from neuro_code.shared.errors import SandboxError

PROTOCOL_VERSION: Final = 1
MAX_FRAME_PAYLOAD: Final = 16 * 1024 * 1024
_FRAME_HEADER = struct.Struct("<IB")


class RuntimeFrameType(IntEnum):
    """Messages exchanged between the controller and trusted runner."""

    SPAWN_REQUEST = 1
    SPAWN_READY = 2
    STDOUT = 3
    STDERR = 4
    STDIN = 5
    CLOSE_STDIN = 6
    TERMINATE = 7
    EXIT = 8
    ERROR = 9
    PTY_OUTPUT = 10
    RESIZE = 11


class RuntimeChannel(StrEnum):
    """One-way protocol channel used by the trusted runtime."""

    CONTROL = "control"
    EVENT = "event"


_CONTROL_FRAME_TYPES = frozenset(
    {
        RuntimeFrameType.SPAWN_REQUEST,
        RuntimeFrameType.STDIN,
        RuntimeFrameType.CLOSE_STDIN,
        RuntimeFrameType.TERMINATE,
        RuntimeFrameType.RESIZE,
    }
)
_EVENT_FRAME_TYPES = frozenset(
    {
        RuntimeFrameType.SPAWN_READY,
        RuntimeFrameType.STDOUT,
        RuntimeFrameType.STDERR,
        RuntimeFrameType.EXIT,
        RuntimeFrameType.ERROR,
        RuntimeFrameType.PTY_OUTPUT,
    }
)


def validate_channel_frame(channel: RuntimeChannel, kind: RuntimeFrameType) -> None:
    """Reject a frame that crosses the directional runtime boundary."""

    if not isinstance(channel, RuntimeChannel) or not isinstance(kind, RuntimeFrameType):
        raise TypeError("runtime channel and frame kind must be canonical")
    allowed = _CONTROL_FRAME_TYPES if channel is RuntimeChannel.CONTROL else _EVENT_FRAME_TYPES
    if kind not in allowed:
        raise SandboxError(f"runtime frame {kind.name} is invalid on {channel.value} channel")


@dataclass(frozen=True, slots=True)
class RuntimeFrame:
    """One decoded frame."""

    kind: RuntimeFrameType
    payload: bytes


def encode_frame(kind: RuntimeFrameType, payload: bytes = b"") -> bytes:
    """Encode one frame with a bounded little-endian length prefix."""

    if not isinstance(kind, RuntimeFrameType):
        raise TypeError("runtime frame kind must be canonical")
    if not isinstance(payload, bytes):
        raise TypeError("runtime frame payload must be bytes")
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError("runtime frame payload exceeds the configured limit")
    return _FRAME_HEADER.pack(len(payload), int(kind)) + payload


class RuntimeFrameDecoder:
    """Incrementally decode frames from arbitrary pipe read boundaries."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[RuntimeFrame, ...]:
        if not isinstance(data, bytes):
            raise TypeError("runtime frame data must be bytes")
        self._buffer.extend(data)
        frames: list[RuntimeFrame] = []
        while len(self._buffer) >= _FRAME_HEADER.size:
            payload_length, kind_value = _FRAME_HEADER.unpack_from(self._buffer)
            if payload_length > MAX_FRAME_PAYLOAD:
                raise SandboxError("Windows runtime IPC frame is too large")
            total = _FRAME_HEADER.size + payload_length
            if len(self._buffer) < total:
                break
            try:
                kind = RuntimeFrameType(kind_value)
            except ValueError as error:
                raise SandboxError("Windows runtime IPC frame kind is invalid") from error
            payload = bytes(self._buffer[_FRAME_HEADER.size : total])
            del self._buffer[:total]
            frames.append(RuntimeFrame(kind, payload))
        return tuple(frames)

    def finish(self) -> None:
        """Reject a truncated frame when the pipe closes."""

        if self._buffer:
            raise SandboxError("Windows runtime IPC ended with a truncated frame")


def encode_json(value: object) -> bytes:
    """Encode protocol metadata without depending on a locale or text mode."""

    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SandboxError("Windows runtime IPC metadata is not serializable") from error


def decode_json(payload: bytes) -> object:
    """Decode UTF-8 JSON metadata and fail closed on malformed input."""

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SandboxError("Windows runtime IPC metadata is invalid") from error


__all__ = [
    "MAX_FRAME_PAYLOAD",
    "PROTOCOL_VERSION",
    "RuntimeChannel",
    "RuntimeFrame",
    "RuntimeFrameDecoder",
    "RuntimeFrameType",
    "decode_json",
    "encode_frame",
    "encode_json",
    "validate_channel_frame",
]
