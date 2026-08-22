"""Model-facing one-based positions and negotiated LSP encodings."""

from __future__ import annotations

from enum import StrEnum


class PositionEncoding(StrEnum):
    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"

    @classmethod
    def from_server_value(cls, value: object) -> PositionEncoding:
        if isinstance(value, str):
            normalized = value.casefold().replace("_", "-")
            if normalized in {"utf-8", "utf8"}:
                return cls.UTF8
            if normalized in {"utf-16", "utf16"}:
                return cls.UTF16
            if normalized in {"utf-32", "utf32"}:
                return cls.UTF32
        return cls.UTF16


def _line_text(text: str, line: int) -> str:
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise ValueError("model line must be one-based")
    lines = text.splitlines()
    if not lines:
        lines = [""]
    if line > len(lines):
        raise ValueError("model line is outside the document")
    return lines[line - 1]


def _encoded_length(text: str, encoding: PositionEncoding) -> int:
    if encoding is PositionEncoding.UTF8:
        return len(text.encode("utf-8"))
    if encoding is PositionEncoding.UTF32:
        return len(text)
    return len(text.encode("utf-16-le")) // 2


def to_lsp_position(
    text: str,
    *,
    line: int,
    column: int,
    encoding: PositionEncoding,
) -> dict[str, int]:
    """Convert a one-based Unicode-code-point model position to LSP units."""

    if not isinstance(text, str):
        raise TypeError("document text must be a string")
    if not isinstance(encoding, PositionEncoding):
        raise TypeError("position encoding must be canonical")
    if not isinstance(column, int) or isinstance(column, bool) or column < 1:
        raise ValueError("model column must be one-based")
    current = _line_text(text, line)
    if column > len(current) + 1:
        raise ValueError("model column is outside the document")
    prefix = current[: column - 1]
    return {"line": line - 1, "character": _encoded_length(prefix, encoding)}


def from_lsp_position(
    text: str,
    *,
    line: int,
    character: int,
    encoding: PositionEncoding,
) -> tuple[int, int]:
    """Convert an LSP zero-based encoded position to a one-based model position."""

    if not isinstance(text, str):
        raise TypeError("document text must be a string")
    if not isinstance(encoding, PositionEncoding):
        raise TypeError("position encoding must be canonical")
    if not isinstance(line, int) or isinstance(line, bool) or line < 0:
        raise ValueError("LSP line must be zero-based")
    if not isinstance(character, int) or isinstance(character, bool) or character < 0:
        raise ValueError("LSP character must be non-negative")
    lines = text.splitlines()
    if not lines:
        lines = [""]
    if line >= len(lines):
        raise ValueError("LSP line is outside the document")
    current = lines[line]
    consumed = 0
    for index, item in enumerate(current):
        width = _encoded_length(item, encoding)
        if consumed == character:
            return line + 1, index + 1
        if consumed < character < consumed + width:
            raise ValueError("LSP position splits a Unicode code point")
        consumed += width
    if consumed == character:
        return line + 1, len(current) + 1
    raise ValueError("LSP character is outside the document")


def model_range_from_lsp(
    text: str,
    value: object,
    *,
    encoding: PositionEncoding,
) -> dict[str, dict[str, int]] | None:
    """Project a server range into the stable one-based model coordinate form."""

    if not isinstance(value, dict):
        return None
    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    try:
        start_line, start_column = from_lsp_position(
            text,
            line=start["line"],
            character=start["character"],
            encoding=encoding,
        )
        end_line, end_column = from_lsp_position(
            text,
            line=end["line"],
            character=end["character"],
            encoding=encoding,
        )
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "start": {"line": start_line, "column": start_column},
        "end": {"line": end_line, "column": end_column},
    }


__all__ = [
    "PositionEncoding",
    "from_lsp_position",
    "model_range_from_lsp",
    "to_lsp_position",
]
