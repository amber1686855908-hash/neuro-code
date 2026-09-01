from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemTargetRequest,
)
from neuro_code.infrastructure.lsp.positions import (
    PositionEncoding,
    from_lsp_position,
    model_range_from_lsp,
    to_lsp_position,
)
from neuro_code.infrastructure.lsp.protocol import (
    MAX_LSP_MESSAGE_BYTES,
    LspFrameReader,
    LspProtocolError,
    encode_message,
    write_message,
)
from neuro_code.infrastructure.lsp.uri import (
    display_path,
    file_uri_from_path,
    local_path_from_file_uri,
)
from neuro_code.infrastructure.workspace.paths import resolve_filesystem_access_targets
from neuro_code.shared.errors import ToolError


class _ChunkedStream:
    def __init__(self, data: bytes, chunk_size: int = 3) -> None:
        self._data = data
        self._chunk_size = chunk_size

    async def read(self, n: int = -1, /) -> bytes:
        if not self._data:
            return b""
        size = min(len(self._data), self._chunk_size, n if n >= 0 else len(self._data))
        result, self._data = self._data[:size], self._data[size:]
        await asyncio.sleep(0)
        return result


class _Writer:
    def __init__(self) -> None:
        self.data: list[bytes] = []

    async def write_stdin(self, data: bytes) -> None:
        self.data.append(data)


class LspProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_length_uses_utf8_bytes_and_fragmented_reads(self) -> None:
        frame = encode_message({"jsonrpc": "2.0", "result": {"text": "中文😀"}})
        message = await LspFrameReader(_ChunkedStream(frame)).read_message()
        self.assertEqual(message, {"jsonrpc": "2.0", "result": {"text": "中文😀"}})

    async def test_malformed_header_fails_closed(self) -> None:
        stream = _ChunkedStream(b"Content-Length nope\r\n\r\n{}")
        with self.assertRaises(LspProtocolError):
            await LspFrameReader(stream).read_message()

    async def test_duplicate_content_length_is_rejected(self) -> None:
        stream = _ChunkedStream(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
        with self.assertRaises(LspProtocolError):
            await LspFrameReader(stream).read_message()

    async def test_protocol_rejects_invalid_frames_and_json_shapes(self) -> None:
        self.assertIsNone(await LspFrameReader(_ChunkedStream(b"")).read_message())
        invalid_frames = (
            b"X-Test: yes\r\n\r\n{}",
            b"Content-Length: nope\r\n\r\n{}",
            b"Content-Length: -1\r\n\r\n",
            b"Content-Length: 2\r\n",
            b"Content-Length: 3\r\n\r\n{}",
            b"Content-Length: 4\r\n\r\nnull",
            b"Content-Length: 3\r\n\r\nNaN",
            b"Content-Length: 5\r\n\r\nnope!",
            "Content-Length: 2\u00e9\r\n\r\n{}".encode("utf-8"),
        )
        for frame in invalid_frames:
            with self.subTest(frame=frame), self.assertRaises(LspProtocolError):
                await LspFrameReader(_ChunkedStream(frame)).read_message()

        too_many_headers = (b"X-Test: yes\r\n" * 33) + b"\r\n"
        with self.assertRaises(LspProtocolError):
            await LspFrameReader(_ChunkedStream(too_many_headers)).read_message()

        nested: object = {}
        for _ in range(40):
            nested = {"next": nested}
        with self.assertRaises(LspProtocolError):
            encode_message({"jsonrpc": "2.0", "result": nested})

    async def test_protocol_writer_and_encoder_reject_unbounded_or_non_json_values(self) -> None:
        writer = _Writer()
        await write_message(writer, {"jsonrpc": "2.0", "result": {"ok": True}})
        self.assertEqual(len(writer.data), 1)
        self.assertEqual(
            await LspFrameReader(_ChunkedStream(writer.data[0])).read_message(),
            {"jsonrpc": "2.0", "result": {"ok": True}},
        )
        with self.assertRaises(LspProtocolError):
            encode_message([])  # type: ignore[arg-type]
        with self.assertRaises(LspProtocolError):
            encode_message({"value": float("nan")})
        with self.assertRaises(LspProtocolError):
            encode_message({"value": "x" * (MAX_LSP_MESSAGE_BYTES + 1)})
        with self.assertRaises(LspProtocolError):
            encode_message({"value": object()})

    def test_positions_are_one_based_and_encoding_aware(self) -> None:
        text = "汉😀x\n"
        self.assertEqual(
            to_lsp_position(
                text,
                line=1,
                column=3,
                encoding=PositionEncoding.UTF16,
            ),
            {"line": 0, "character": 3},
        )
        self.assertEqual(
            to_lsp_position(
                text,
                line=1,
                column=3,
                encoding=PositionEncoding.UTF8,
            ),
            {"line": 0, "character": 7},
        )
        self.assertEqual(
            from_lsp_position(
                text,
                line=0,
                character=3,
                encoding=PositionEncoding.UTF16,
            ),
            (1, 3),
        )
        with self.assertRaises(ValueError):
            from_lsp_position(
                text,
                line=0,
                character=2,
                encoding=PositionEncoding.UTF16,
            )

        self.assertEqual(PositionEncoding.from_server_value("utf8"), PositionEncoding.UTF8)
        self.assertEqual(PositionEncoding.from_server_value("utf-32"), PositionEncoding.UTF32)
        self.assertEqual(PositionEncoding.from_server_value("unknown"), PositionEncoding.UTF16)
        self.assertEqual(
            to_lsp_position(text, line=1, column=3, encoding=PositionEncoding.UTF32),
            {"line": 0, "character": 2},
        )
        self.assertEqual(
            from_lsp_position(text, line=0, character=2, encoding=PositionEncoding.UTF32),
            (1, 3),
        )
        self.assertEqual(
            model_range_from_lsp(
                text,
                {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 2},
                },
                encoding=PositionEncoding.UTF32,
            ),
            {
                "start": {"line": 1, "column": 1},
                "end": {"line": 1, "column": 3},
            },
        )
        for operation in (
            lambda: to_lsp_position(text, line=0, column=1, encoding=PositionEncoding.UTF16),
            lambda: to_lsp_position(text, line=1, column=99, encoding=PositionEncoding.UTF16),
            lambda: from_lsp_position(text, line=-1, character=0, encoding=PositionEncoding.UTF16),
            lambda: from_lsp_position(text, line=0, character=99, encoding=PositionEncoding.UTF16),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertIsNone(model_range_from_lsp(text, {}, encoding=PositionEncoding.UTF16))

    def test_uri_projection_rejects_outside_and_non_file_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=False)
            local = root.joinpath("a.py")
            self.assertEqual(local_path_from_file_uri(local.as_uri()), local)
            self.assertIsNone(local_path_from_file_uri("https://example.test/a.py"))
            outside = root.parent / "outside.py"
            self.assertEqual(local_path_from_file_uri(outside.as_uri()), outside)

    def test_uri_projection_rejects_ambiguous_file_forms_and_bounds_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=False)
            self.assertIsNone(local_path_from_file_uri("file://remote/a.py"))
            self.assertIsNone(local_path_from_file_uri(f"{root}/a.py?query"))
            self.assertIsNone(local_path_from_file_uri(f"{root}/a.py#fragment"))
            self.assertIsNone(local_path_from_file_uri("file:///%2Frelative"))
            self.assertIsNone(local_path_from_file_uri("file:///tmp/%00bad"))
            self.assertIsNone(local_path_from_file_uri("file:///tmp/%GGbad"))
        self.assertIsNone(file_uri_from_path(Path("relative.py")))
        self.assertEqual(display_path(root / "nested" / "a.py", root), "nested/a.py")
        outside = root.parent / "other.py"
        self.assertEqual(display_path(outside, root), outside.as_posix())

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve(strict=False)
            actual = workspace / "actual.py"
            actual.write_text("x\n", encoding="utf-8")
            link = workspace / "link.py"
            try:
                link.symlink_to(actual)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable")
            self.assertEqual(local_path_from_file_uri(link.as_uri()), link)
            self.assertEqual(file_uri_from_path(link), link.as_uri())
            with self.assertRaises(ToolError):
                resolve_filesystem_access_targets(
                    "lsp",
                    workspace,
                    (
                        FilesystemTargetRequest(
                            str(link),
                            FilesystemAccessOperation.READ,
                            must_exist=True,
                            reject_link_like=True,
                        ),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
