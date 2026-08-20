"""Command line entry point for the P4 benchmark boundary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .corpus import TASKS, task_by_id
from .harness import BenchmarkHarness, estimate_run
from .models import (
    CORPUS_VERSION,
    BenchmarkConfig,
    RuntimeConfig,
    TaskCategory,
    validate_corpus,
)


def _category(value: str) -> TaskCategory:
    normalized = value.strip().casefold()
    for category in TaskCategory:
        if normalized in {category.value.casefold(), category.value[0].casefold()}:
            return category
    choices = ", ".join(category.value for category in TaskCategory)
    raise argparse.ArgumentTypeError(f"unknown category {value!r}; choose one of {choices}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate", help="validate the frozen corpus without model calls"
    )
    validate.add_argument("--json", action="store_true")
    estimate = subparsers.add_parser("estimate", help="show bounded cost/time estimates")
    _add_selection(estimate)
    estimate.add_argument("--max-steps", type=int, default=12)
    estimate.add_argument("--timeout", type=float, default=90.0)
    run = subparsers.add_parser("run", help="run tasks through the headless production path")
    _add_selection(run)
    run.add_argument("--output", type=Path, default=Path("benchmark-results"))
    run.add_argument("--provider", default=None)
    run.add_argument("--model", default=None)
    run.add_argument(
        "--sandbox", default="workspace", choices=("off", "workspace", "read-only", "strict")
    )
    run.add_argument("--max-steps", type=int, default=12)
    run.add_argument("--timeout", type=float, default=90.0)
    run.add_argument("--live", action="store_true", help="use the configured routed provider")
    run.add_argument(
        "--allow-paid", action="store_true", help="explicitly allow live model charges"
    )
    run.add_argument("--rerun-failures", action="store_true")
    return parser


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", action="append", dest="tasks", metavar="TASK_ID")
    parser.add_argument("--category", type=_category)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="select one task per category")


def _selected(args: argparse.Namespace) -> tuple:
    selectors = sum(bool(value) for value in (args.tasks, args.category, args.all, args.smoke))
    if selectors != 1:
        raise ValueError("choose exactly one of --task, --category, --all, or --smoke")
    if args.tasks:
        selected = []
        for task_id in args.tasks:
            try:
                selected.append(task_by_id(task_id))
            except KeyError:
                raise ValueError(f"unknown task id: {task_id}") from None
        return tuple(selected)
    if args.category:
        return tuple(task for task in TASKS if task.category is args.category)
    if args.smoke:
        return tuple(
            next(task for task in TASKS if task.category is category) for category in TaskCategory
        )
    return TASKS


def _run(args: argparse.Namespace) -> int:
    if args.max_steps < 1 or args.timeout <= 0:
        raise ValueError("max steps and timeout must be positive")
    tasks = _selected(args)
    config = BenchmarkConfig(
        runtime=RuntimeConfig(
            sandbox_profile=args.sandbox,
            max_model_steps=args.max_steps,
            timeout_seconds=args.timeout,
        ),
        provider=args.provider,
        model=args.model,
        live=args.live,
        allow_paid=args.allow_paid,
        output=args.output,
        timeout_seconds=args.timeout,
    )
    if args.live and not args.allow_paid:
        raise ValueError("--live requires explicit --allow-paid; no model call was made")
    if args.live and not args.model:
        raise ValueError("--live requires an explicit --model; no model call was made")
    if args.all:
        estimate = estimate_run(tasks, config.runtime)
        print("Bounded --all estimate:", file=sys.stderr)
        print(json.dumps(estimate, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    results = BenchmarkHarness(config).run(tasks, rerun_failures=args.rerun_failures)
    print(
        json.dumps(
            {
                "corpus_version": CORPUS_VERSION,
                "task_count": len(results),
                "outcomes": {
                    outcome: sum(result.outcome.value == outcome for result in results)
                    for outcome in ("PASS", "FAIL", "HARNESS_ERROR")
                },
                "baseline_status": "LIVE_RUN" if args.live else "BASELINE_NOT_RUN",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if all(result.outcome.value == "PASS" for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        errors = validate_corpus(TASKS)
        payload = {
            "schema_version": 1,
            "corpus_version": CORPUS_VERSION,
            "task_count": len(TASKS),
            "errors": list(errors),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        elif errors:
            print("\n".join(errors), file=sys.stderr)
        else:
            print(f"corpus valid: {CORPUS_VERSION}, {len(TASKS)} tasks")
        return 0 if not errors else 1
    try:
        if args.command == "estimate":
            tasks = _selected(args)
            print(
                json.dumps(
                    estimate_run(
                        tasks,
                        RuntimeConfig(max_model_steps=args.max_steps, timeout_seconds=args.timeout),
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        return _run(args)
    except ValueError as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 2


__all__ = ["main"]
