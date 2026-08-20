"""Hidden deterministic verification for one benchmark workspace.

The verifier is called by the harness with a workspace path.  It is never
materialized below that path and its result contains bounded command output,
not the verifier implementation or answer data.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .models import TaskSpec

MAX_OUTPUT_BYTES = 24_000


@dataclass(frozen=True, slots=True)
class VerifierResult:
    passed: bool
    checks: tuple[str, ...]
    failures: tuple[str, ...]
    command: tuple[str, ...]
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    error_type: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": list(self.checks),
            "failures": list(self.failures),
            "command": list(self.command),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 6),
            "timed_out": self.timed_out,
            "error_type": self.error_type,
        }

    def text(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"{status} deterministic verifier", *[f"check: {item}" for item in self.checks]]
        lines.extend(f"failure: {item}" for item in self.failures)
        if self.command:
            lines.append(f"command: {' '.join(self.command)}")
        if self.stdout:
            lines.append("stdout:\n" + self.stdout)
        if self.stderr:
            lines.append("stderr:\n" + self.stderr)
        return "\n".join(lines) + "\n"


def _bounded_output(value: bytes) -> str:
    if len(value) <= MAX_OUTPUT_BYTES:
        return value.decode("utf-8", "replace")
    return value[:MAX_OUTPUT_BYTES].decode("utf-8", "replace") + "\n[truncated]"


def _safe_read(workspace: Path, relative: str) -> str:
    target = (workspace / relative).resolve(strict=False)
    root = workspace.resolve(strict=False)
    if target != root and root not in target.parents:
        raise ValueError(f"verifier path escaped workspace: {relative!r}")
    return target.read_text(encoding="utf-8")


def _check_python_syntax(workspace: Path) -> tuple[str, ...]:
    failures: list[str] = []
    for path in sorted(workspace.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            failures.append(f"python syntax {path.relative_to(workspace)}: {type(error).__name__}")
    return tuple(failures)


def verify_workspace(task: TaskSpec, workspace: Path) -> VerifierResult:
    """Run deterministic file, AST, and public-test checks in ``workspace``."""

    started = monotonic()
    checks: list[str] = []
    failures: list[str] = []
    verifier = task.verifier
    for relative in (*verifier.required_files, *task.required_files):
        if (workspace / relative).is_file():
            checks.append(f"required file present: {relative}")
        else:
            failures.append(f"required file missing: {relative}")
    for relative in (*verifier.forbidden_files, *task.forbidden_files):
        if (workspace / relative).exists():
            failures.append(f"forbidden file present: {relative}")
        else:
            checks.append(f"forbidden file absent: {relative}")

    for relative, markers in verifier.required_markers:
        try:
            content = _safe_read(workspace, relative)
        except (OSError, ValueError) as error:
            failures.append(f"cannot read required marker file {relative}: {type(error).__name__}")
            continue
        for marker in markers:
            if marker in content:
                checks.append(f"marker present: {relative}:{marker}")
            else:
                failures.append(f"marker missing: {relative}:{marker}")
    for relative, markers in verifier.forbidden_markers:
        try:
            content = _safe_read(workspace, relative)
        except FileNotFoundError:
            checks.extend(f"forbidden marker file absent: {relative}" for _ in markers)
            continue
        except (OSError, ValueError) as error:
            failures.append(
                f"cannot inspect forbidden marker file {relative}: {type(error).__name__}"
            )
            continue
        for marker in markers:
            if marker in content:
                failures.append(f"forbidden marker present: {relative}:{marker}")
            else:
                checks.append(f"forbidden marker absent: {relative}:{marker}")

    syntax_failures = _check_python_syntax(workspace)
    failures.extend(syntax_failures)
    if not syntax_failures:
        checks.append("python syntax valid")

    command: tuple[str, ...] = ()
    stdout = ""
    stderr = ""
    timed_out = False
    error_type: str | None = None
    if verifier.run_pytest:
        command = (sys.executable, "-m", "pytest", "-q")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=env,
                capture_output=True,
                timeout=verifier.timeout_seconds,
                check=False,
            )
            stdout = _bounded_output(completed.stdout)
            stderr = _bounded_output(completed.stderr)
            if completed.returncode == 0:
                checks.append("pytest passed")
            else:
                failures.append(f"pytest failed with exit code {completed.returncode}")
        except subprocess.TimeoutExpired as error:
            timed_out = True
            error_type = type(error).__name__
            failures.append(f"pytest timed out after {verifier.timeout_seconds:g}s")
        except OSError as error:
            error_type = type(error).__name__
            failures.append(f"pytest could not start: {type(error).__name__}")

    return VerifierResult(
        passed=not failures,
        checks=tuple(checks),
        failures=tuple(failures),
        command=command,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=monotonic() - started,
        timed_out=timed_out,
        error_type=error_type,
    )


__all__ = ["MAX_OUTPUT_BYTES", "VerifierResult", "verify_workspace"]
