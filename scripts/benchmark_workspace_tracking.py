"""Synthetic traversal measurement for task-scoped workspace tracking.

This is intentionally a standalone diagnostic, not a pytest benchmark.  It
compares the number of files visited by a targeted structured-edit capture
with the bounded full observation used for unknown-side-effect commands.
运行方式:

    .venv/bin/python scripts/benchmark_workspace_tracking.py

The benchmark reports traversal counts rather than enforcing a machine-
dependent time threshold.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from neuro_code.infrastructure.tools.workspace_diff import WorkspaceMutationJournal
from neuro_code.infrastructure.workspace import changes


def _make_workspace(root: Path, count: int) -> Path:
    for index in range(count):
        (root / f"file_{index:06d}.txt").write_text("baseline\n", encoding="utf-8")
    target = root / "file_000000.txt"
    return target


def _measure(root: Path, target: Path) -> tuple[int, int, float, float]:
    walk_calls = 0
    original_walk = changes.os.walk

    def counted_walk(*args: object, **kwargs: object):
        nonlocal walk_calls
        walk_calls += 1
        yield from original_walk(*args, **kwargs)

    journal = WorkspaceMutationJournal()
    journal.begin_task()
    started = perf_counter()
    with patch.object(changes.os, "walk", counted_walk):
        journal.before_mutation(
            (root,),
            tool_name="search_replace",
            explicit_redactions=(),
            target_paths=(target.name,),
        )
        target.write_text("changed\n", encoding="utf-8")
        journal.after_mutation(
            (root,),
            tool_name="search_replace",
            mutation_metadata=None,
            explicit_redactions=(),
            target_paths=(target.name,),
        )
    targeted_seconds = perf_counter() - started

    full_walk_calls = 0

    def counted_full_walk(*args: object, **kwargs: object):
        nonlocal full_walk_calls
        full_walk_calls += 1
        yield from original_walk(*args, **kwargs)

    journal.begin_task()
    started = perf_counter()
    with patch.object(changes.os, "walk", counted_full_walk):
        journal.before_mutation(
            (root,),
            tool_name="bash",
            explicit_redactions=(),
        )
    full_seconds = perf_counter() - started
    return walk_calls, full_walk_calls, targeted_seconds, full_seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=(1_000, 10_000, 50_000),
        help="synthetic file counts to measure",
    )
    args = parser.parse_args()
    for size in args.sizes:
        with tempfile.TemporaryDirectory(prefix=f"neuro-code-workspace-{size}-") as directory:
            root = Path(directory)
            target = _make_workspace(root, size)
            targeted, full, targeted_seconds, full_seconds = _measure(root, target)
            print(
                f"files={size:>6} targeted_walks={targeted} full_walks={full} "
                f"targeted_seconds={targeted_seconds:.4f} full_seconds={full_seconds:.4f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
