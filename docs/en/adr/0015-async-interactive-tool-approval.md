# ADR 0015 — Asynchronous interactive tool approval

[简体中文](../../zh-CN/adr/0015-async-interactive-tool-approval.md) · **English**

## Status

Accepted.

## Context

`PermissionManager` already provides deterministic `allow`, `ask`, and `deny`
policy decisions, with explicit deny taking precedence. The first TUI slice had
no safe way to suspend a tool call for a user response, so interactive
composition deliberately converted unresolved approval into denial.

The approval UI must not become a dependency of the agent runtime, must not let
a user response override policy denial, and must prove that no side-effecting
tool starts while approval is pending or after denial/cancellation.

## Decision

- `PermissionManager` remains the synchronous policy engine.
  `PermissionApprover` is a separate optional asynchronous port. Headless
  composition has no approver and keeps its existing fail-closed behavior.
- For an `ask` decision, the runtime emits `tool_approval_requested`, awaits the
  approver, then emits `tool_approval_resolved`. `tool_started` can occur only
  after an allowed resolution. A missing handler denies; cancellation aborts
  the turn, records a paired error result under ADR 0016, and never starts the
  tool.
- The TUI uses `SessionApprovalBroker` and a modal with the exact-action
  outcome plus only runtime-generated typed scope candidates. The initial
  focus remains deny; `Esc`, `Ctrl+C`, and `D` also deny. A user may approve
  ordinary `search_replace`/`apply_patch` targets as a primary-workspace edit
  scope, or one of the conservative `test`, `static_check`, or `git_read`
  command families. There is no “allow all” outcome, and modal `Ctrl+C` denies
  this request rather than cancelling the whole turn.
- Exact-action session approval remains scoped to the SHA-256 digest of the
  canonical tool name and complete argument mapping. Broad approvals are
  separate typed grants, bound to the trusted session identity and canonical
  workspace root. They are generated only after canonical filesystem planning
  or conservative Bash classification; a model cannot invent a candidate.
  Every call still passes through `PermissionManager` first, so explicit deny,
  explicit ask, mode decisions, and high-risk operations cannot be bypassed by
  an earlier broad grant. Arguments that cannot be canonicalized as JSON and
  Bash commands that cannot be safely classified downgrade to allow-once.
- The modal receives a bounded policy-generated summary, not the general raw
  argument mapping. Bash displays a bounded command because that is the action
  being authorized. Search/replace displays its workspace path and operation
  count while hiding old/new text; patch content is also hidden.
- Approval requests and resolutions are appended to the existing session event
  audit log. No database schema change or persistent permission rule is created.

## Consequences

Interactive edits and commands can now pause safely for a decision, and tests
can observe that the workspace is unchanged until approval. Exact-action
session caching avoids repeated prompts without granting an entire tool or
command family.

Approvals do not survive process restart and cannot yet create reviewed
persistent allow/deny rules. The broker re-checks queued equivalent requests
after the first decision, but an allow-once, denial, or cancellation never
grants a waiting request. Rich argument diffs, user-provided rejection
feedback, and ACP scoped-option presentation remain future vertical slices;
ACP still exposes only its existing exact-action options.

## Validation

The approval queue, ACP boundary, and interactive interface are validated by
Neuro Code's own contracts and behavior tests.
