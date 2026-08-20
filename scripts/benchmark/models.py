"""Versioned data contracts for the coding-agent benchmark harness.

The benchmark is deliberately kept outside ``neuro_code``.  It consumes the
same public application boundary as a headless caller, but production runtime
modules never import this package.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CORPUS_VERSION = "p4.0.0"


class TaskCategory(StrEnum):
    REPOSITORY_NAVIGATION = "A_repository_navigation"
    LOCALIZED_EDITING = "B_localized_editing"
    MULTI_FILE_CHANGE = "C_multi_file_change"
    BUG_DIAGNOSIS = "D_bug_diagnosis"
    TEST_DRIVEN_REPAIR = "E_test_driven_repair"
    REFACTOR_API_MIGRATION = "F_refactor_api_migration"
    LONG_RUNNING_TOOL_CONTROL = "G_long_running_tool_control"
    EXTERNAL_INFORMATION = "H_external_information"


class BenchmarkOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    HARNESS_ERROR = "HARNESS_ERROR"


TAXONOMY = (
    "MODEL_REASONING",
    "REPOSITORY_NAVIGATION",
    "TOOL_SELECTION",
    "TOOL_EXECUTION",
    "EDIT_FAILURE",
    "VERIFICATION_FAILURE",
    "CONTEXT_LOSS",
    "PLANNING_FAILURE",
    "PERMISSION_OR_SANDBOX",
    "BACKGROUND_PROCESS",
    "WEB_RESEARCH",
    "PROVIDER_PROTOCOL",
    "PROVIDER_TRANSIENT",
    "BUDGET_OR_TIMEOUT",
    "FINALIZATION",
    "HARNESS_ERROR",
    "UNKNOWN",
)


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    """Deterministic verifier instructions kept outside the agent workspace."""

    required_markers: tuple[tuple[str, tuple[str, ...]], ...] = ()
    forbidden_markers: tuple[tuple[str, tuple[str, ...]], ...] = ()
    run_pytest: bool = True
    required_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()
    timeout_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    category: TaskCategory
    prompt: str
    files: tuple[tuple[str, str], ...]
    verifier: VerifierSpec
    fake_commands: tuple[str, ...]
    public_tests: tuple[str, ...] = ()
    web: bool = False
    external_dependency: str | None = None
    external_reference_sha256: str | None = None
    required_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()

    def materialize(self, workspace: Path) -> None:
        """Create a clean task repository from the frozen seed."""

        for relative_path, content in self.files:
            target = workspace / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    @property
    def file_map(self) -> dict[str, str]:
        return dict(self.files)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Fixed benchmark runtime knobs recorded in every run manifest."""

    sandbox_profile: str = "workspace"
    interaction_mode: str = "auto"
    reasoning_effort: str = "high"
    max_model_steps: int = 12
    tool_budget: int = 48
    context_budget: int = 131_072
    timeout_seconds: float = 90.0
    background_task_limit: int = 0
    web_search_mode: str = "disabled"
    web_fetch_mode: str = "disabled"
    provider_failover: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "sandbox_profile": self.sandbox_profile,
            "interaction_mode": self.interaction_mode,
            "reasoning_effort": self.reasoning_effort,
            "max_model_steps": self.max_model_steps,
            "tool_budget": self.tool_budget,
            "context_budget": self.context_budget,
            "timeout_seconds": self.timeout_seconds,
            "background_task_limit": self.background_task_limit,
            "web_search_mode": self.web_search_mode,
            "web_fetch_mode": self.web_fetch_mode,
            "provider_failover": self.provider_failover,
        }


@dataclass(frozen=True, slots=True)
class AttemptResult:
    task_id: str
    attempt: int
    outcome: BenchmarkOutcome
    primary_failure: str | None
    secondary_failures: tuple[str, ...]
    evidence: tuple[str, ...]
    metrics: dict[str, object]
    verifier: dict[str, object]
    error: str | None = None
    workspace: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "outcome": self.outcome.value,
            "failure": {
                "primary": self.primary_failure,
                "secondary": list(self.secondary_failures),
                "evidence": list(self.evidence),
            },
            "metrics": self.metrics,
            "verifier": self.verifier,
            "error": self.error,
            "workspace": self.workspace,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Execution controls with an explicit opt-in for potentially paid runs."""

    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    provider: str | None = None
    model: str | None = None
    live: bool = False
    allow_paid: bool = False
    output: Path = Path("benchmark-results")
    timeout_seconds: float | None = None

    def effective_timeout(self) -> float:
        return (
            self.runtime.timeout_seconds if self.timeout_seconds is None else self.timeout_seconds
        )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def corpus_payload(tasks: tuple[TaskSpec, ...]) -> list[dict[str, object]]:
    return [
        {
            "task_id": task.task_id,
            "category": task.category.value,
            "prompt": task.prompt,
            "files": [[path, content] for path, content in task.files],
            "public_tests": list(task.public_tests),
            "web": task.web,
            "external_dependency": task.external_dependency,
            "external_reference_sha256": task.external_reference_sha256,
            "required_files": list(task.required_files),
            "forbidden_files": list(task.forbidden_files),
        }
        for task in tasks
    ]


def corpus_hash(tasks: tuple[TaskSpec, ...]) -> str:
    return digest_text(canonical_json(corpus_payload(tasks)))


def ensure_safe_relative_path(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {value!r}")
    return value


def validate_task(task: TaskSpec) -> tuple[str, ...]:
    errors: list[str] = []
    if not task.task_id or len(task.task_id) > 80:
        errors.append("task_id must be non-empty and bounded")
    for path, content in task.files:
        try:
            ensure_safe_relative_path(path)
        except ValueError as error:
            errors.append(str(error))
        if not isinstance(content, str):
            errors.append(f"file {path!r} content is not text")
    if len({path for path, _ in task.files}) != len(task.files):
        errors.append("task seed contains duplicate paths")
    if not task.prompt.strip():
        errors.append("prompt must be non-empty")
    for path in (*task.required_files, *task.forbidden_files):
        try:
            ensure_safe_relative_path(path)
        except ValueError as error:
            errors.append(str(error))
    if task.web and not task.external_dependency:
        errors.append("WEB task must record an external dependency")
    if task.external_dependency and not task.external_reference_sha256:
        errors.append("external dependency must record its frozen reference hash")
    return tuple(errors)


def validate_corpus(tasks: tuple[TaskSpec, ...]) -> tuple[str, ...]:
    errors: list[str] = []
    expected_categories = set(TaskCategory)
    counts = dict.fromkeys(TaskCategory, 0)
    seen: set[str] = set()
    for task in tasks:
        errors.extend(f"{task.task_id}: {error}" for error in validate_task(task))
        if task.task_id in seen:
            errors.append(f"duplicate task id: {task.task_id}")
        seen.add(task.task_id)
        counts[task.category] += 1
    if set(counts) != expected_categories:
        errors.append("corpus categories are incomplete")
    for category, count in counts.items():
        if count != 5:
            errors.append(f"{category.value} has {count} tasks, expected 5")
    if sum(task.web for task in tasks) != 5:
        errors.append("corpus must contain exactly 5 WEB tasks")
    if len(tasks) != 40:
        errors.append(f"corpus has {len(tasks)} tasks, expected 40")
    return tuple(errors)


__all__ = [
    "CORPUS_VERSION",
    "SCHEMA_VERSION",
    "TAXONOMY",
    "AttemptResult",
    "BenchmarkConfig",
    "BenchmarkOutcome",
    "RuntimeConfig",
    "TaskCategory",
    "TaskSpec",
    "VerifierSpec",
    "canonical_json",
    "corpus_hash",
    "corpus_payload",
    "digest_text",
    "validate_corpus",
    "validate_task",
]
