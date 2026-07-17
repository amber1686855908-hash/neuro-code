#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def _markdown_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*.md")}


def main() -> int:
    docs = Path(__file__).resolve().parents[1] / "docs"
    english = _markdown_files(docs / "en")
    chinese = _markdown_files(docs / "zh-CN")
    missing_chinese = sorted(english - chinese)
    missing_english = sorted(chinese - english)
    if not missing_chinese and not missing_english:
        print(f"documentation parity ok: {len(english)} English/Chinese pairs")
        return 0
    for path in missing_chinese:
        print(f"missing Chinese document: docs/zh-CN/{path}", file=sys.stderr)
    for path in missing_english:
        print(f"missing English document: docs/en/{path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
