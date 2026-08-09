# ADR 0105: Unified ordinary execution budget and transient replan guidance

## Context

The public `--max-steps` option and the Runtime hard model-step limit used one
value, but the default supervisor retained separate fixed tool-round and
tool-call ceilings. Raising `--max-steps` could therefore leave a lower hidden
tool budget in place. The supervisor also produced typed `REPLAN` decisions,
but the Agent loop only traced them; the next model request received no reason
to change strategy.

## Decision

- `ExecutionBudget` remains the only domain budget value.
- `neuro_code.application.execution_policy` owns the named `normal` and `deep`
  product profiles. They resolve respectively to 48/48/192 and 96/96/384
  model-call/tool-round/tool-call limits. Read-only tools inherit the wider
  per-tool ceiling; known side-effecting/state-transition tools receive
  stricter per-tool limits.
- `ApplicationSettings`, CLI/TUI startup, ACP startup, Composition,
  `AgentRuntime`, `AgentLoopRunner`, and `AgentExecutionSupervisor` use the same
  resolved value. `--max-steps N` remains a compatibility option, but now maps
  to N model calls, N tool rounds, and 4N total tool calls rather than replacing
  only the model limit.
- Finalizer `max_attempts` remains independent and is never reserved from the
  ordinary execution budget.
- In `FINALIZE_TERMINAL` mode, a batch-ending `REPLAN` decision activates one
  request-scoped synthetic message tagged
  `SyntheticReason.RUNTIME_SUPERVISION`. It is rebuilt in memory, is never
  persisted or projected as a genuine user turn, remains active while the
  supervisor still reports replan, and is cleared after new progress or at the
  end of the turn. `OBSERVE_ONLY` continues to execute no decisions.
- `ContextBuilder` adds provider-neutral batch-first guidance to every model
  request. It encourages batching independent read-only evidence gathering but
  explicitly permits sequential operations with data dependencies.

Direct `AgentRuntime` construction without a budget retains its historical
24-step default for internal compatibility, but that value is also mapped to a
complete 24/24/96 ordinary budget. Formal product entrypoints use the `normal`
profile by default.

## Consequences

There is one owner for product budget defaults and no hidden fixed tool ceiling
under a larger `--max-steps`. Repetition, repeated-error, periodic-cycle, and
no-progress detection remain unchanged. Tool calls remain sequential; this
decision does not add batch filesystem tools, budget telemetry, automatic
compaction, segment continuation, or subagent scheduling.
