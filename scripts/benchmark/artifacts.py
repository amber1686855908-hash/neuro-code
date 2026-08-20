"""Isolated workspaces and bounded benchmark artifact persistence."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind

from .metrics import event_jsonl
from .models import (
    CORPUS_VERSION,
    SCHEMA_VERSION,
    AttemptResult,
    BenchmarkConfig,
    TaskSpec,
    corpus_hash,
    digest_text,
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)
_MAX_TEXT = 48_000


def _secret_environment_name(name: str) -> bool:
    upper = name.upper()
    return any(
        fragment in upper for fragment in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    )


def redact_text(value: str, explicit_values: Sequence[str] = ()) -> str:
    rendered = value
    for secret in explicit_values:
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        rendered = pattern.sub(r"\1[REDACTED]" if pattern.groups else "[REDACTED]", rendered)
    if len(rendered) > _MAX_TEXT:
        return rendered[:_MAX_TEXT] + "\n[truncated]"
    return rendered


def redact_value(value: Any, explicit_values: Sequence[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, explicit_values)
    if isinstance(value, Mapping):
        return {str(key): redact_value(item, explicit_values) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item, explicit_values) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return redact_text(repr(value), explicit_values)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _run_git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", *args),
        cwd=workspace,
        capture_output=True,
        check=check,
        env={
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Neuro Code Benchmark",
            "GIT_AUTHOR_EMAIL": "benchmark@invalid",
            "GIT_COMMITTER_NAME": "Neuro Code Benchmark",
            "GIT_COMMITTER_EMAIL": "benchmark@invalid",
        },
    )


def initialize_workspace(workspace: Path) -> None:
    _run_git(workspace, "init", "--quiet")
    _run_git(workspace, "config", "user.name", "Neuro Code Benchmark")
    _run_git(workspace, "config", "user.email", "benchmark@invalid")
    _run_git(workspace, "add", ".")
    _run_git(workspace, "commit", "--quiet", "-m", "benchmark seed")


def workspace_diff(workspace: Path) -> str:
    completed = _run_git(workspace, "diff", "HEAD", "--no-ext-diff", "--binary", check=False)
    chunks = [completed.stdout.decode("utf-8", "replace")]
    untracked = _run_git(
        workspace,
        "ls-files",
        "--others",
        "--exclude-standard",
        check=False,
    ).stdout.decode("utf-8", "replace")
    for relative in (line for line in untracked.splitlines() if line):
        added = _run_git(
            workspace,
            "diff",
            "--no-ext-diff",
            "--binary",
            "--no-index",
            "/dev/null",
            relative,
            check=False,
        )
        chunks.append(added.stdout.decode("utf-8", "replace"))
    return redact_text("\n".join(chunk for chunk in chunks if chunk))


def tool_trace(events: Sequence[AgentEvent]) -> list[dict[str, object]]:
    trace: list[dict[str, object]] = []
    for event in events:
        if event.kind not in {
            AgentEventKind.TOOL_REQUESTED,
            AgentEventKind.TOOL_STARTED,
            AgentEventKind.TOOL_COMPLETED,
            AgentEventKind.TOOL_FAILED,
        }:
            continue
        data = dict(event.data)
        trace.append(
            {
                "sequence": event.sequence,
                "kind": event.kind.value,
                "name": data.get("name"),
                "id": data.get("id"),
                "arguments": redact_value(data.get("arguments")),
                "is_error": data.get("is_error"),
                "duration_seconds": data.get("duration_seconds"),
            }
        )
    return trace


class ArtifactRun:
    """Own one run directory and temporary attempt roots."""

    def __init__(self, config: BenchmarkConfig, tasks: Sequence[TaskSpec]) -> None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{timestamp}-{uuid.uuid4().hex[:10]}"
        self.output_dir = config.output / self.run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tasks = tuple(tasks)
        self.config = config
        self.redaction_values = tuple(
            value for name, value in os.environ.items() if _secret_environment_name(name) and value
        )
        from .corpus import get_tasks

        full_corpus_hash = corpus_hash(get_tasks())
        endpoint = os.environ.get("NEURO_CODE_BENCHMARK_BASE_URL", "https://api.invalid/v1")
        self.manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "corpus_version": CORPUS_VERSION,
            "corpus_sha256": full_corpus_hash,
            "selection_sha256": corpus_hash(self.tasks),
            "created_at": datetime.now(UTC).isoformat(),
            "runtime": config.runtime.to_dict(),
            "provider": {
                "profile": config.provider or "benchmark",
                "service": "generic-openai-compatible",
                "model": config.model or "fixture-tool-caller",
                "protocol": "openai-chat",
                "endpoint_identity_sha256": digest_text(endpoint),
                "secrets_redacted": True,
            },
            "web": {
                "route": config.runtime.web_search_mode,
                "search_model": None,
                "fetch_mode": config.runtime.web_fetch_mode,
            },
            "live": config.live,
            "allow_paid": config.allow_paid,
            "baseline_status": "REQUESTED" if config.live else "BASELINE_NOT_RUN",
            "neuro_code_commit": _git_head(),
        }
        _write_json(
            self.output_dir / "manifest.json", redact_value(self.manifest, self.redaction_values)
        )

    def attempt_dir(self, task_id: str, attempt: int) -> Path:
        directory = self.output_dir / "attempts" / task_id
        return directory if attempt == 0 else directory / f"attempt-{attempt}"

    def write_attempt(
        self,
        result: AttemptResult,
        events: Sequence[AgentEvent],
        verifier_text: str,
        diff: str,
    ) -> None:
        directory = self.attempt_dir(result.task_id, result.attempt)
        _write_json(
            directory / "result.json", redact_value(result.to_dict(), self.redaction_values)
        )
        _write_text(
            directory / "events.jsonl", redact_text(event_jsonl(events), self.redaction_values)
        )
        _write_json(
            directory / "tool-trace.json", redact_value(tool_trace(events), self.redaction_values)
        )
        _write_text(directory / "diff.patch", redact_text(diff, self.redaction_values))
        _write_text(directory / "verifier.txt", redact_text(verifier_text, self.redaction_values))

    def write_summary(self, results: Sequence[AttemptResult]) -> None:
        result_dicts = [result.to_dict() for result in results]
        passed = sum(result.outcome.value == "PASS" for result in results)
        failed = sum(result.outcome.value == "FAIL" for result in results)
        harness_errors = sum(result.outcome.value == "HARNESS_ERROR" for result in results)
        summary = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "corpus_version": CORPUS_VERSION,
            "corpus_sha256": self.manifest["corpus_sha256"],
            "task_count": len(results),
            "pass": passed,
            "fail": failed,
            "harness_error": harness_errors,
            "results": result_dicts,
        }
        _write_json(self.output_dir / "summary.json", redact_value(summary, self.redaction_values))
        self.manifest["baseline_status"] = (
            "LIVE_COMPLETED" if self.config.live else "BASELINE_NOT_RUN"
        )
        _write_json(
            self.output_dir / "manifest.json", redact_value(self.manifest, self.redaction_values)
        )
        lines = [
            f"# Benchmark run {self.run_id}",
            "",
            f"- corpus: {CORPUS_VERSION}",
            f"- tasks: {len(results)}",
            f"- PASS: {passed}",
            f"- FAIL: {failed}",
            f"- HARNESS_ERROR: {harness_errors}",
            "",
            "| Task | Attempt | Outcome | Primary failure |",
            "| --- | ---: | --- | --- |",
        ]
        for result in results:
            lines.append(
                f"| {result.task_id} | {result.attempt} | {result.outcome.value} | {result.primary_failure or ''} |"
            )
        _write_text(self.output_dir / "summary.md", "\n".join(lines) + "\n")


def _git_head() -> str | None:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def temporary_attempt_root() -> TemporaryDirectory[str]:
    return TemporaryDirectory(prefix="neuro-code-benchmark-")


__all__ = [
    "ArtifactRun",
    "initialize_workspace",
    "redact_text",
    "redact_value",
    "temporary_attempt_root",
    "tool_trace",
    "workspace_diff",
]
