#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="verify the pinned Rust and .ua source baseline")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs" / "source-baseline.json",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=(
            Path(os.environ["PYGROK_SOURCE_REPOSITORY"])
            if os.environ.get("PYGROK_SOURCE_REPOSITORY")
            else None
        ),
        help="local read-only grok-build clone (or set PYGROK_SOURCE_REPOSITORY)",
    )
    parser.add_argument(
        "--ua-directory",
        type=Path,
        default=(
            Path(os.environ["PYGROK_UA_DIRECTORY"])
            if os.environ.get("PYGROK_UA_DIRECTORY")
            else None
        ),
        help="Understand Anything analysis directory (defaults to SOURCE/.ua)",
    )
    args = parser.parse_args()
    baseline = _load_json(args.baseline)
    if args.source is None:
        print(
            "source baseline check requires --source or PYGROK_SOURCE_REPOSITORY",
            file=sys.stderr,
        )
        return 2
    source = args.source.expanduser().resolve()
    ua_directory = (
        args.ua_directory.expanduser().resolve()
        if args.ua_directory is not None
        else source / ".ua"
    )
    expected = str(baseline["source_commit"])
    try:
        actual = subprocess.run(
            ("git", "-C", str(source), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"source baseline check failed: {error}", file=sys.stderr)
        return 2
    ua_meta = _load_json(ua_directory / "meta.json")
    result = {
        "expected_source_commit": expected,
        "actual_source_commit": actual,
        "ua_commit": ua_meta.get("gitCommitHash"),
        "source_matches": actual == expected,
        "ua_matches": ua_meta.get("gitCommitHash") == expected,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["source_matches"] and result["ua_matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
