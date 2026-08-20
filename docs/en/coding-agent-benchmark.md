# Coding Agent Benchmark (P4)

## Purpose

P4 is a development/evaluation boundary for measuring coding-agent outcomes
without changing Neuro Code's Provider, Web, AgentLoop, ToolExecutor, Context,
Sandbox, or Session behavior. The harness calls the production headless path
through `ApplicationComposition.open`, `create_binding`, and
`ConversationRunner.run`. TUI automation is not part of the benchmark path.

The benchmark does not claim to measure hidden reasoning quality. It records
observable outcomes, tool/event traces, resource usage, verification evidence,
and a bounded failure taxonomy.

## Corpus

The frozen corpus is versioned as `p4.0.0` and contains 40 tasks: five tasks in
each of eight categories.

| Category | Tasks |
| --- | ---: |
| A Repository Navigation | 5 |
| B Localized Editing | 5 |
| C Multi-file Change | 5 |
| D Bug Diagnosis | 5 |
| E Test-driven Repair | 5 |
| F Refactor/API Migration | 5 |
| G Long-running/Tool-control | 5 |
| H External-information | 5 |

The first 35 tasks are CORE tasks. The final five are WEB tasks. WEB tasks use
an offline, frozen reference mirror and record the external dependency URL and
reference hash; no network call is needed for corpus validation or smoke.
Each task is materialized as a separate Python repository with a 500-plus-line
catalog fixture, a public test surface, and a hidden deterministic verifier
that remains outside the agent workspace.

Any task seed, prompt, or public-test change requires a corpus version bump and
changes the recorded corpus SHA-256.

## Runtime and isolation

Every attempt receives a fresh temporary workspace, temporary HOME and state
directory, a controlled environment with ambient secret variables removed, and
a git seed commit. The verifier directory is a sibling of the workspace and
is never passed to the agent. The default development runtime profile is
`sandbox_profile=workspace`; this is recorded explicitly, and all process
creation still goes through Neuro Code's local-process sandbox port. A stricter
profile can be selected for a run.

The fixed manifest records sandbox profile, interaction mode, reasoning effort,
model-step and tool budgets, context budget, timeout, background-task limit,
Web Search/Fetch modes, provider/model/protocol, endpoint identity hash, and
the Neuro Code commit. Credentials are not written to the manifest, traces,
diffs, or verifier output.

The verifier is deterministic: file/AST checks and `pytest` assertions are
primary. LLM judging is not used. Results are `PASS`, `FAIL`, or
`HARNESS_ERROR`.

## Metrics and taxonomy

The harness records wall time, model steps, provider-reported input/output
tokens when available, cache fields when available, tool counts and failures,
compactions, failovers, permission events, background events, whether the
agent invoked a verification command, and the stop reason. It does not infer
reasoning quality from these counters.

The primary taxonomy is bounded to:

`MODEL_REASONING`, `REPOSITORY_NAVIGATION`, `TOOL_SELECTION`,
`TOOL_EXECUTION`, `EDIT_FAILURE`, `VERIFICATION_FAILURE`, `CONTEXT_LOSS`,
`PLANNING_FAILURE`, `PERMISSION_OR_SANDBOX`, `BACKGROUND_PROCESS`,
`WEB_RESEARCH`, `PROVIDER_PROTOCOL`, `PROVIDER_TRANSIENT`,
`BUDGET_OR_TIMEOUT`, `FINALIZATION`, `HARNESS_ERROR`, `UNKNOWN`.

Classification uses event/tool traces, verifier output, final diff, and runtime
signals. It can be reviewed by a human; it is not delegated to another LLM.
`NEW_TOOL_CANDIDATE` is intentionally not emitted from one failure. It requires
a repeated pattern across independent tasks and evidence that existing tools
are insufficient or unusually costly.

## CLI and artifacts

The evaluation CLI is deliberately outside production imports:

```bash
uv run python -m scripts.benchmark validate --json
uv run python -m scripts.benchmark run --smoke --output benchmark-results
uv run python -m scripts.benchmark run --all --output benchmark-results
uv run python -m scripts.benchmark estimate --all
```

`validate` makes no model calls. `run` uses the deterministic provider fixture
unless `--live` is supplied. Live runs require both `--allow-paid` and an
explicitly configured credential; `--all --live` prints a bounded estimate
before starting. A live run is never retried automatically after verifier
failure. `--rerun-failures` performs at most two additional fresh attempts for
tasks that failed their first attempt.

Each run writes:

```text
benchmark-results/<run-id>/manifest.json
benchmark-results/<run-id>/summary.json
benchmark-results/<run-id>/summary.md
benchmark-results/<run-id>/attempts/<task-id>/result.json
benchmark-results/<run-id>/attempts/<task-id>/events.jsonl
benchmark-results/<run-id>/attempts/<task-id>/tool-trace.json
benchmark-results/<run-id>/attempts/<task-id>/diff.patch
benchmark-results/<run-id>/attempts/<task-id>/verifier.txt
```

Additional fresh reruns are stored below the task directory as `attempt-1`
and `attempt-2`.

Benchmark results are ignored by default in the repository and must stay
redacted and bounded. The harness does not push benchmark results or source
changes.

## Acceptance boundary

P4 is ready for capability-improvement work only after the harness passes its
architecture/isolation tests, the eight-task smoke, 40-task validation, full
quality gates, and at least one explicitly authorized live baseline. If no
credential or explicit paid opt-in is available, the honest status is
`BASELINE_NOT_RUN`, not a fabricated score. Production behavior changes found
by the benchmark are reported as `BENCHMARK_DISCOVERED_ISSUE` and are fixed in
a later round, not silently mixed into P4.
