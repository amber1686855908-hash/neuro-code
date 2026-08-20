"""Benchmark orchestration over the real headless Neuro Code path."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

from neuro_code.application.permissions.policy import PermissionMode
from neuro_code.application.settings import ApplicationSettings
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.conversation.events import AgentEvent
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.shared.errors import ProviderError

from .artifacts import (
    ArtifactRun,
    initialize_workspace,
    temporary_attempt_root,
    workspace_diff,
)
from .metrics import project_metrics
from .models import (
    AttemptResult,
    BenchmarkConfig,
    BenchmarkOutcome,
    RuntimeConfig,
    TaskSpec,
    corpus_hash,
)
from .provider import ScriptedBenchmarkProvider
from .taxonomy import classify_failure
from .verifier import verify_workspace


def _secret_environment_name(name: str) -> bool:
    upper = name.upper()
    return any(
        fragment in upper for fragment in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    )


@contextmanager
def controlled_environment(
    *,
    home: Path,
    state_dir: Path,
    live: bool,
    credential_env: str | None,
) -> Iterator[None]:
    """Give the attempt a temporary HOME and remove ambient secret variables."""

    original = dict(os.environ)
    safe = {
        name: value
        for name, value in original.items()
        if not _secret_environment_name(name) and name not in {"HOME", "NEURO_CODE_HOME"}
    }
    safe.update({"HOME": str(home), "NEURO_CODE_HOME": str(state_dir)})
    if live and credential_env:
        credential = original.get(credential_env)
        if credential:
            safe[credential_env] = credential
    os.environ.clear()
    os.environ.update(safe)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _write_config(state_dir: Path, config: BenchmarkConfig) -> tuple[str, str, str]:
    profile = config.provider or "benchmark"
    model = config.model or "fixture-tool-caller"
    base_url = os.environ.get(
        "NEURO_CODE_BENCHMARK_BASE_URL",
        "https://api.invalid/v1",
    )
    credential_env = os.environ.get("NEURO_CODE_BENCHMARK_API_KEY_ENV", "NEURO_CODE_BENCHMARK_KEY")
    content = (
        "[routing]\n"
        f'default = "{profile}"\n\n'
        f"[providers.{profile}]\n"
        'protocol = "openai-chat"\n'
        'service_id = "generic-openai-compatible"\n'
        f'model = "{model}"\n'
        f'base_url = "{base_url}"\n'
        f'api_key_env = "{credential_env}"\n'
        'proxy_mode = "direct"\n'
        "context_window_tokens = 131072\n"
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "config.toml").write_text(content, encoding="utf-8")
    return profile, model, base_url


async def _execute_application(
    settings: ApplicationSettings,
    task: TaskSpec,
    events: list[AgentEvent],
    *,
    live: bool,
) -> tuple[object, ScriptedBenchmarkProvider | None]:
    fake_provider: ScriptedBenchmarkProvider | None = None
    if live:
        application = await ApplicationComposition.open(settings)
    else:
        fake_provider = ScriptedBenchmarkProvider(task.fake_commands)
        application = await ApplicationComposition.open(
            settings,
            provider_factory=lambda _config, _failover: fake_provider,
        )
    try:
        binding = await application.create_binding(enable_background_tasks=False)
        result = await binding.runner.run(task.prompt, sink=events.append)
        return result, fake_provider
    finally:
        await asyncio.shield(application.close())


def _profile(value: str) -> str:
    return SandboxProfile.parse(value).value


class BenchmarkHarness:
    """Run frozen tasks with isolated workspaces and bounded artifacts."""

    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        self.artifacts: ArtifactRun | None = None

    def run(
        self, tasks: Sequence[TaskSpec], *, rerun_failures: bool = False
    ) -> tuple[AttemptResult, ...]:
        selected = tuple(tasks)
        self.artifacts = ArtifactRun(self.config, selected)
        if self.config.live and not self.config.allow_paid:
            raise ValueError("live benchmark requires explicit --allow-paid")
        results: list[AttemptResult] = []
        for task in selected:
            result = self.run_attempt(task, 0)
            results.append(result)
        if rerun_failures:
            for task in selected:
                previous = next(result for result in results if result.task_id == task.task_id)
                if previous.outcome is not BenchmarkOutcome.FAIL:
                    continue
                for attempt in (1, 2):
                    result = self.run_attempt(task, attempt)
                    results.append(result)
                    if result.outcome is BenchmarkOutcome.PASS:
                        break
        assert self.artifacts is not None
        self.artifacts.write_summary(results)
        return tuple(results)

    def run_attempt(self, task: TaskSpec, attempt: int) -> AttemptResult:
        if self.artifacts is None:
            raise RuntimeError("run() must initialize artifacts before run_attempt")
        started = monotonic()
        events: list[AgentEvent] = []
        error: BaseException | None = None
        verifier_result = None
        diff = ""
        with temporary_attempt_root() as raw_root:
            root = Path(raw_root)
            workspace = root / "workspace"
            verifier_root = root / "hidden-verifier"
            home = root / "home"
            state_dir = home / ".neuro-code"
            workspace.mkdir(parents=True)
            verifier_root.mkdir(parents=True)
            task.materialize(workspace)
            initialize_workspace(workspace)
            if workspace.resolve() in verifier_root.resolve().parents:
                raise RuntimeError("verifier root must not be inside workspace")
            credential_env = os.environ.get(
                "NEURO_CODE_BENCHMARK_API_KEY_ENV",
                "NEURO_CODE_BENCHMARK_KEY",
            )
            profile, model, _base_url = _write_config(state_dir, self.config)
            result = None
            with controlled_environment(
                home=home,
                state_dir=state_dir,
                live=self.config.live,
                credential_env=credential_env,
            ):
                try:
                    settings = ApplicationSettings(
                        cwd=workspace,
                        provider=profile,
                        model=model,
                        sandbox=_profile(self.config.runtime.sandbox_profile),
                        failover=self.config.runtime.provider_failover,
                        permission_mode=PermissionMode.BYPASS,
                        max_steps=self.config.runtime.max_model_steps,
                        reasoning_effort=ReasoningEffort.HIGH,
                    )

                    async def run_turn() -> object:
                        return await asyncio.wait_for(
                            _execute_application(
                                settings,
                                task,
                                events,
                                live=self.config.live,
                            ),
                            timeout=self.config.effective_timeout(),
                        )

                    result, _provider = asyncio.run(run_turn())
                except BaseException as caught:
                    error = caught
            verifier_result = verify_workspace(task, workspace)
            diff = workspace_diff(workspace)

        if error is not None and not isinstance(error, ProviderError):
            outcome = BenchmarkOutcome.HARNESS_ERROR
        elif verifier_result is not None and verifier_result.passed:
            outcome = BenchmarkOutcome.PASS
        else:
            outcome = BenchmarkOutcome.FAIL
        primary, secondary, evidence = classify_failure(
            task,
            verifier_result,
            events,
            error=error,
            timed_out=isinstance(error, TimeoutError),
        )
        metrics = project_metrics(
            events,
            wall_time_seconds=monotonic() - started,
            outcome=outcome.value,
            stop_reason=(
                getattr(getattr(result, "outcome", None), "status", None).value
                if getattr(getattr(result, "outcome", None), "status", None) is not None
                else type(error).__name__
                if error is not None
                else None
            ),
        )
        if error is not None:
            evidence = (*evidence, f"error={type(error).__name__}")
        if verifier_result is not None:
            evidence = (*evidence, f"verifier_passed={verifier_result.passed}")
        verifier_payload = (
            verifier_result.to_dict()
            if verifier_result is not None
            else {
                "passed": False,
                "failures": ["verifier not reached"],
            }
        )
        attempt_result = AttemptResult(
            task_id=task.task_id,
            attempt=attempt,
            outcome=outcome,
            primary_failure=primary,
            secondary_failures=secondary,
            evidence=evidence,
            metrics=metrics,
            verifier=verifier_payload,
            error=str(error) if error is not None else None,
        )
        assert self.artifacts is not None
        self.artifacts.write_attempt(
            attempt_result,
            events,
            verifier_result.text()
            if verifier_result is not None
            else "HARNESS_ERROR: verifier not reached\n",
            diff,
        )
        return attempt_result


def estimate_run(tasks: Sequence[TaskSpec], runtime: RuntimeConfig) -> dict[str, object]:
    return {
        "task_count": len(tasks),
        "estimated_attempts": len(tasks),
        "max_model_steps": runtime.max_model_steps,
        "max_tool_calls": runtime.tool_budget,
        "worst_case_model_calls": len(tasks) * runtime.max_model_steps,
        "worst_case_wall_seconds": len(tasks) * runtime.timeout_seconds,
        "web_tasks": sum(task.web for task in tasks),
        "corpus_sha256": corpus_hash(tuple(tasks)),
    }


__all__ = ["BenchmarkHarness", "controlled_environment", "estimate_run"]
