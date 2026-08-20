from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from scripts.benchmark.artifacts import (
    initialize_workspace,
    redact_text,
    tool_trace,
    workspace_diff,
)
from scripts.benchmark.cli import main
from scripts.benchmark.corpus import TASKS, task_by_id
from scripts.benchmark.harness import BenchmarkHarness, estimate_run
from scripts.benchmark.metrics import project_metrics
from scripts.benchmark.models import (
    BenchmarkConfig,
    BenchmarkOutcome,
    RuntimeConfig,
    TaskCategory,
    TaskSpec,
    VerifierSpec,
    corpus_hash,
    validate_corpus,
)
from scripts.benchmark.provider import ScriptedBenchmarkProvider
from scripts.benchmark.taxonomy import (
    classify_failure,
    summarize_failure_distribution,
    validate_taxonomy,
)
from scripts.benchmark.verifier import verify_workspace

from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.shared.errors import ProviderError


def test_frozen_corpus_has_expected_shape_and_stable_hash() -> None:
    assert len(TASKS) == 40
    assert validate_corpus(TASKS) == ()
    assert {task.category for task in TASKS} == set(TaskCategory)
    assert sum(task.web for task in TASKS) == 5
    assert corpus_hash(TASKS) == "c760d11e86947cc273c46910f75dd8fe427abea8512c1ff34d126817198f7dd7"
    assert all(
        len(dict(task.files)["src/miniapp/catalog.py"].splitlines()) >= 520 for task in TASKS
    )


def test_validate_corpus_rejects_duplicate_and_unsafe_task() -> None:
    task = task_by_id("A01-repository-lookup")
    invalid = replace(task, task_id="../unsafe", files=(("../secret", "x"),))
    errors = validate_corpus((invalid,))
    assert any("unsafe relative path" in error for error in errors)
    assert any("expected 5" in error for error in errors)


def test_verifier_is_deterministic_and_checks_hidden_requirements(tmp_path: Path) -> None:
    task = task_by_id("A01-repository-lookup")
    task.materialize(tmp_path)
    failed = verify_workspace(task, tmp_path)
    assert not failed.passed
    assert any("marker missing" in failure for failure in failed.failures)
    tmp_path.joinpath("src/miniapp/lookup.py").write_text(
        'def lookup(items, key):\n    return next((item for item in items if item.get("id") == key), None)\n',
        encoding="utf-8",
    )
    passed = verify_workspace(task, tmp_path)
    assert passed.passed
    assert passed.command[-1] == "-q"


def test_workspace_seed_and_untracked_diff_are_preserved(tmp_path: Path) -> None:
    task = task_by_id("G03-tool-control-manifest")
    task.materialize(tmp_path)
    initialize_workspace(tmp_path)
    target = tmp_path / "new.txt"
    target.write_text("new\n", encoding="utf-8")
    diff = workspace_diff(tmp_path)
    assert "new.txt" in diff
    assert "new" in diff


def test_redaction_and_tool_trace_do_not_expose_credentials() -> None:
    redacted = redact_text("Authorization: Bearer secret-token api_key=sk-secret123456")
    assert "secret-token" not in redacted
    assert "sk-secret123456" not in redacted
    event = AgentEvent.create(
        1,
        AgentEventKind.TOOL_REQUESTED,
        {"id": "1", "name": "bash", "arguments": {"command": "echo api_key=sk-secret123456"}},
    )
    trace = tool_trace((event,))
    assert "secret123456" not in json.dumps(trace)


def test_metrics_project_observable_counts_only() -> None:
    events = (
        AgentEvent.create(1, AgentEventKind.MODEL_STEP_STARTED, {"step": 1}),
        AgentEvent.create(
            2,
            AgentEventKind.TOOL_REQUESTED,
            {"id": "1", "name": "bash", "arguments": {"command": "pytest -q"}},
        ),
        AgentEvent.create(3, AgentEventKind.TOOL_COMPLETED, {"id": "1", "name": "bash"}),
        AgentEvent.create(
            4,
            AgentEventKind.TURN_COMPLETED,
            {
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_tokens": 2,
                "cache_write_tokens": 1,
            },
        ),
    )
    metrics = project_metrics(events, wall_time_seconds=1.25, outcome="PASS")
    assert metrics["model_steps"] == 1
    assert metrics["input_tokens"] == 10
    assert metrics["cache_read_tokens"] == 2
    assert metrics["cache_write_tokens"] == 1
    assert metrics["tool_counts"]["bash_count"] == 1
    assert metrics["agent_invoked_final_verification"] is True


def test_taxonomy_is_bounded_and_deterministic() -> None:
    task = task_by_id("A01-repository-lookup")
    failure_event = AgentEvent.create(1, AgentEventKind.TOOL_FAILED, {"name": "bash"})
    verifier = verify_workspace(task, Path("/tmp"))
    primary, secondary, evidence = classify_failure(task, verifier, (failure_event,))
    assert primary == "TOOL_EXECUTION"
    assert validate_taxonomy(primary, secondary) == ()
    assert evidence
    assert summarize_failure_distribution(({"failure": {"primary": primary}},)) == {
        "TOOL_EXECUTION": 1
    }


def test_provider_error_is_not_misreported_as_harness_error() -> None:
    primary, secondary, evidence = classify_failure(
        task_by_id("A01-repository-lookup"),
        None,
        (),
        error=ProviderError("fixture provider unavailable"),
    )
    assert primary == "PROVIDER_PROTOCOL"
    assert secondary == ()
    assert evidence == ("harness exception type=ProviderError",)


def test_scripted_provider_emits_normal_tool_call_events() -> None:
    provider = ScriptedBenchmarkProvider(("echo ok",))

    async def consume() -> list[object]:
        return [
            event
            async for event in provider.stream(
                context=ModelContext(()),
                tools=(),
            )
        ]

    events = asyncio.run(consume())
    assert isinstance(events[0], object)
    assert any(type(event).__name__ == "ModelToolCall" for event in events)
    assert any(type(event).__name__ == "ModelCompleted" for event in events)


def test_headless_harness_smoke_uses_real_application_path(tmp_path: Path) -> None:
    task = task_by_id("A01-repository-lookup")
    config = BenchmarkConfig(output=tmp_path / "results")
    results = BenchmarkHarness(config).run((task,))
    assert results[0].outcome is BenchmarkOutcome.PASS
    attempt = config.output / next(config.output.iterdir()).name / "attempts" / task.task_id
    assert {path.name for path in attempt.iterdir()} == {
        "result.json",
        "events.jsonl",
        "tool-trace.json",
        "diff.patch",
        "verifier.txt",
    }
    assert "session_started" in (attempt / "events.jsonl").read_text(encoding="utf-8")


def test_harness_reruns_only_failed_tasks(tmp_path: Path) -> None:
    task = task_by_id("A01-repository-lookup")
    broken = replace(task, fake_commands=("false",))
    results = BenchmarkHarness(BenchmarkConfig(output=tmp_path / "results")).run(
        (broken,),
        rerun_failures=True,
    )
    assert len(results) == 3
    assert all(result.outcome is BenchmarkOutcome.FAIL for result in results)
    assert [result.attempt for result in results] == [0, 1, 2]


def test_cost_guard_rejects_live_without_opt_in(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allow-paid"):
        BenchmarkHarness(BenchmarkConfig(live=True, output=tmp_path / "results")).run(
            (task_by_id("A01-repository-lookup"),)
        )


def test_estimate_records_web_and_bounded_resources() -> None:
    smoke = tuple(
        next(task for task in TASKS if task.category is category) for category in TaskCategory
    )
    estimate = estimate_run(smoke, RuntimeConfig(max_model_steps=4, timeout_seconds=3))
    assert estimate["task_count"] == 8
    assert estimate["max_model_steps"] == 4
    assert estimate["web_tasks"] == 1
    assert estimate["worst_case_wall_seconds"] == 24


def test_cli_validate_is_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"task_count": 40' in output
    assert "errors" in output


def test_verifier_timeout_is_reported(tmp_path: Path) -> None:
    task = TaskSpec(
        "timeout-fixture",
        TaskCategory.LONG_RUNNING_TOOL_CONTROL,
        "timeout",
        (
            ("pyproject.toml", "[tool.pytest.ini_options]\npythonpath = ['.']\n"),
            ("test_sleep.py", "import time\ntime.sleep(0.2)\n"),
        ),
        VerifierSpec(run_pytest=True, timeout_seconds=0.001),
        (),
    )
    task.materialize(tmp_path)
    result = verify_workspace(task, tmp_path)
    assert result.timed_out
    assert not result.passed
