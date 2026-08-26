# ADR 0139: Bounded DAG Revision / Replan

- Status: implemented as an explicit internal P0 vertical slice; validated by the final PR merge-ref CI (run 32994963168, 23/23 jobs successful); this is not a direct exact-head checkout, and live/paid provider validation remains out of scope
- Date: 2026-08-26
- Scope: one explicit revision of one failed, quiescent Task DAG into one immutable successor DAG
- Depends on: ADR 0134, ADR 0135, ADR 0136, ADR 0137, and ADR 0138

## Context

ADR 0138 publishes one immutable model-generated Task DAG.  A failed DAG can
need a new bounded proposal, but changing its published definition or replaying
its completed workers would break the existing execution and recovery
contracts.  Replan therefore needs its own durable identity, evidence, and
publication boundary.

## Decision

Keep the authority chain:

```text
explicit failed-DAG revision request
        -> immutable redacted source evidence envelope
        -> zero-tool one-step replan Planner
        -> TaskDagApplicationService validation/publication
        -> existing Leader / Writable execution
```

The source DAG is never mutated.  The replan produces a new `TaskDag` with a
new DAG identity; the canonical Task DAG service remains the only graph
validation and publication owner.  The parent identity is taken from the
actual parent `ConversationBinding`, not from request fields or model text.
This slice exposes the replan only through the explicit internal application
service; no failure transition, CLI, TUI, or ACP path invokes it implicitly.

Initial Planning and DAG Replan are separate capabilities.  The source DAG
and successor DAG are separate immutable publications.  Replan evidence is
not the predecessor-result relay, and the Replan Planner is not the Leader;
the existing Leader remains the successor's decision authority.

### Eligibility and depth

The request is explicit and names one source DAG.  The source must be
`FAILED`, quiescent, have no `RUNNING` nodes, no unresolved `INDETERMINATE`
nodes, and only terminal node states.  Successful, cancelled, active,
non-quiescent, foreign-parent, missing, or tampered snapshots fail closed.
The source publication remains immutable and must match its exact definition
fingerprint, generation, and state at claim, provider fence, and successor
publication.

`MAX_DAG_REPLAN_DEPTH` is `1`.  Exactly one successor revision is supported;
recursive replan and automatic retry are outside this ADR.

### Replan evidence envelope

The application constructs a deterministic, redacted, immutable envelope from
the source DAG.  It contains only source DAG identity/fingerprint/generation,
canonical node IDs and ordinals, dependencies, node states, bounded completed
result projections, typed bounded failure summaries, and safe bounded metadata.
Redaction happens before fingerprinting and publication.  The envelope has a
4-KiB completed-result item bound, 16-KiB completed-result aggregate bound,
8-KiB failure/state bound, and 32-KiB rendered bound.  It contains no raw
transcript, tool arguments/results, logs, environment, secrets, workspace
bytes, checkpoint data, diffs, paths, or authority instructions.

### Zero-tool replan Planner

`ApplicationComposition.create_task_dag_replan_service()` creates a fresh
persisted one-step Planner binding.  The binding has zero local and
provider-hosted tools, zero filesystem/Bash/terminal/network/MCP/LSP/Worktree/
Checkpoint/worker/background authority, and `max_steps=1`.  The model receives
the evidence as data only and returns the existing typed `ModelDagProposal`
contract.  Revision, source, successor, depth, identity, and authority fields
are application-owned and cannot be supplied by model output.

### Durable lifecycle and identity

Schema 26 adds insert-only replan attempt/proposal projections:

- `orchestration_dag_replan_attempts` binds the revision, actual parent,
  immutable source snapshot, depth, evidence fingerprint/JSON, planner
  session/turn, owner lease, intended successor ID, lifecycle, model response,
  proposal fingerprint, and published successor ID.
- `orchestration_dag_replan_proposals` stores one exact parsed proposal with
  canonical JSON and its source/evidence/successor identity.

The lifecycle is:

```text
CLAIMED -> PROVIDER_FENCED -> MODEL_COMMITTED -> PROPOSAL_PUBLISHED
         -> SUCCESSOR_DAG_PUBLISHED -> COMPLETED
```

`STALE` records invalid observable model output.  `INDETERMINATE` records an
unresolved provider or storage boundary.  The same canonical source/revision
identity allows exact idempotent recovery; a divergent evidence, proposal,
source, or successor is rejected.  No blind upsert is used.  Existing data and
foreign-key `RESTRICT` behavior are preserved by the populated 25-to-26
migration.

### Crash recovery, no replay, and races

After model output or generic provider-turn evidence is observable, recovery
never replays the provider.  A fresh composition may reuse a committed model
response, durable proposal, or already inserted exact successor.  A crash
after provider-turn evidence but before model commit becomes explicit
recovery-required `INDETERMINATE` and cannot fabricate a proposal.

Source snapshot fencing is repeated before provider invocation and before
successor publication.  A durable owner/CAS claim permits at most one provider
call, one immutable proposal, and one successor for one exact source/revision
identity.  Independent spawned controllers therefore have one winner; the
loser neither calls the provider nor changes the winner's provenance.

## Non-goals

This ADR does not add automatic retry, recursive or multi-level replan,
publication-time mutation, source DAG resurrection, merge/copy-back, rollback,
cleanup, public CLI/TUI/ACP orchestration commands, Swarm, Ultracode,
distributed scheduling, live/paid provider validation, or a new execution
runtime.  It does not change the filter-preflight, CAS, ownership, sandbox,
hooks, fsmonitor, Worktree, Leader, Writable, or LSP architecture.

## Verification

Focused and production-shaped tests use fixture providers only.  They cover
strict bounded evidence, source eligibility, exact identity, zero-tool
composition, schema migration, same-process publication recovery, real
`multiprocessing.get_context("spawn")` recovery at model-commit,
proposal-publication, successor-insert, and provider-turn-evidence boundaries,
one-winner two-process races, no provider replay, and a real composition path
from model-generated failure through replan, parallel-aware Leader, and
Writable workers.  The end-to-end path verifies that the source DAG and dirty
parent checkout remain unchanged and that completed source workers are not
rerun.
