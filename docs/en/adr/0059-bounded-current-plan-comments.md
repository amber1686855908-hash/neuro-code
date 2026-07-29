# ADR 0059 — Bounded comments on the current plan revision

[简体中文](../../zh-CN/adr/0059-bounded-current-plan-comments.md) · **English**

## Status

Accepted

## Context

ADR 0057 makes a structured plan durable, but a user reviewing a plan has no
way to attach feedback to a specific step without sending an ambiguous ordinary
prompt. A writable plan file, a provider-specific approval protocol, or a
background task comment stream would blur the boundary between feedback,
execution, and worker lifecycle.

The product needs a small, local, durable feedback mechanism. It must not claim
that a comment approves a plan, starts work, schedules a task, or creates a
subagent.

## Decision

- The domain owns immutable `PlanComment` values. A comment has an opaque ID, a
  one-based step index, bounded text, and an aware creation timestamp. At most
  48 comments may belong to a plan revision.
- `SessionPlan` derives a SHA-256 fingerprint from its canonical validated
  representation. `SessionStore` accepts and lists comments only for that
  exact current plan. Adding feedback after a concurrent plan replacement
  fails instead of attaching it to a different plan.
- SQLite schema v8 stores comments in a foreign-keyed `session_plan_comments`
  table, scoped by session and plan fingerprint. The table is excluded from
  FTS, JSON export, and Rust-session import. Replacing a plan deletes comments
  for earlier fingerprints; clearing a plan deletes all of its comments. A
  session fork copies only current-plan comments and gives them fresh opaque
  IDs.
- `AgentConversation` restores current comments with a session plan. On the
  next ordinary or plan-execution request, `AgentRuntime` renders them as
  transient provider-neutral plan guidance. An `update_plan` replacement clears
  the in-memory comments before a later model step. Comments are not canonical
  messages or runtime events.
- The TUI provides `/comment-plan STEP COMMENT` and `/plan-comment STEP
  COMMENT`; `/view-plan` and `/show-plan` render each comment below its
  numbered step. The command makes no provider request and is unavailable while
  a turn is running. ACP exposes no plan-comment method in this slice.
- The feature changes no permission decision, workspace rule, sandbox policy,
  execution mode, task state, or subagent lifecycle.

## Consequences

Users can leave concise, durable, step-specific feedback and inspect it after a
resume or current-plan fork. A model sees that feedback only when the user next
asks it to act, keeping review separate from execution.

Replacing a plan intentionally removes its old feedback rather than trying to
guess a mapping between changed steps. Users can re-add feedback to the new
revision. Task comments, plan approval, scheduling, shared worker assignment,
ACP plan methods, and subagent orchestration remain separate capabilities.
