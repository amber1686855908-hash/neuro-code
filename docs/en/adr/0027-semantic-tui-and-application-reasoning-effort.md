# ADR 0027 — Semantic TUI rendering and application reasoning effort

[简体中文](../../zh-CN/adr/0027-semantic-tui-and-application-reasoning-effort.md) · **English**

## Status

Accepted.

## Context

The stable-message TUI removed transcript jumps and repeated role prefixes, but
literal assistant text still hid Markdown structure. Local lifecycle notices
also began at different columns, important identifiers had no visual hierarchy,
and the active model was visible only in transient startup or status messages.

A user-facing “reasoning effort” control creates a separate correctness risk.
Provider APIs use incompatible names and semantics, many models expose no such
parameter, and workflow orchestration is an application capability rather than
a model setting. The interface must not imply that a private provider control or
sub-agent workflow is active when only Neuro Code's own review policy exists.

## Decision

- Assistant output is rendered with Rich's Markdown document model and a fixed,
  application-owned semantic theme. Headings, emphasis, code, lists, links, and
  tables receive restrained role-based styles. Hyperlink activation is disabled,
  and model content is never interpreted as Rich/Textual markup. User prompts,
  queries, paths, identifiers, and external values continue to use literal text.
- Local system, status, tool, and error entries use a two-column grid with a
  fixed-width label gutter and a folding body. Interpolated values are colored
  by application-known semantics such as provider/model, tool/session, path,
  outcome, effort, or error; payload text cannot select a style.
- A persistent runtime bar directly above the prompt shows the active provider
  and model, workspace, context-window usage, requested effort, and interaction
  mode. This ADR owns effort/context semantics; ADR 0028 owns mode. When requested and
  effective effort differ, it shows both. Context starts as a marked local
  estimate, then uses provider-reported input plus output tokens after each model
  step. A positive profile `context_window_tokens` value supplies the denominator;
  unknown windows remain `?`. This metadata is never sent as an API parameter.
  The bar is controller-owned, localized, refreshed on provider selection/failover
  and effort changes, and retained in narrow layouts. While the TUI is waiting
  for model output, a seven-cell collapsing pulse appears before the localized
  pending-assistant text. It is a Textual timer-driven port of the supplied
  terminal demo, stops on completion/cancellation/failure, and never writes
  ANSI cursor controls directly.
- Typing `/` opens a plain-text hint row backed by the same deterministic catalog
  as the inline suggester. Syntax includes parameters, effort choices, selectable
  profile names, and placeholders for free-form arguments. `Tab` applies the
  first candidate only in the main slash-command prompt and does not replace
  modal focus traversal.
- `ReasoningEffort` defines five provider-neutral choices: `low` (`○`), `medium`
  (`◐`), `high` (`●`), `xhigh` (`⬤`), and `ultracode` (`⚡`). `high` is the
  default. They describe increasing application review depth, not a guaranteed
  hidden-reasoning token budget.
- `Ctrl+E`, bare `/effort`, and bare `/reasoning` open the TUI picker;
  `/effort LEVEL` and `/reasoning LEVEL` select directly. `--effort LEVEL`
  works for both interactive and headless composition. Changes are rejected
  while a turn is active and take effect at the next model step.
- `UiPreferencesStore` persists the requested TUI effort alongside UI language
  in the atomic, user-only `ui-preferences.json` file. An explicit CLI value
  overrides the saved TUI value for that launch. Missing, corrupt, or unknown
  effort values fail safely to `high`; provider configuration and credentials
  remain untouched.
- `ProfileConversationController` owns the process-local selection, serializes
  changes with model turns, and reapplies it to new profile/session bindings.
  Effort is not session identity and is not written into canonical conversation
  history.
- At every model step, `AgentRuntime` adds the effective review guidance to a
  request-only system message and places the typed requested value on
  `ModelContext`. The injected guidance is excluded from persisted
  `SessionItem` values.
- Provider adapters do not blindly turn `ModelContext.reasoning_effort` into a
  proprietary request field. A native mapping may be added only behind an
  explicit, tested provider/model capability; no such mapping is implemented in
  this slice.
- `ultracode` has requested value `ultracode` and effective policy `xhigh`.
  The picker, runtime bar, and status message expose that fallback. It does not
  start sub-agents because workflow orchestration is not implemented.

## Consequences

Assistant structure and important local state are easier to scan without
allowing model or external text to inject UI styles. The active provider/model,
the current context-budget percentage, and the exact requested/effective policy
remain visible instead of depending on transient transcript lines. Provider
usage corrects the cheap startup estimate without requiring a bundled tokenizer;
the percentage remains unavailable rather than fabricated when no window is
configured. Command syntax and choices become discoverable without reintroducing
Textual's conflicting command palette.

Effort now has useful cross-provider behavior while remaining honest about its
scope: it steers Neuro Code's request-level review instructions, but does not
guarantee a provider's internal reasoning depth, token allocation, latency, or
cost. The policy instruction adds a small amount of request context and is
deliberately regenerated rather than persisted.

ADR 0029 subsequently adds bounded in-place tool cards, and ADR 0030 adds
bounded interactive detail toggling. Mermaid, inline media, provider-native
effort mapping, and workflow/sub-agent orchestration remain later vertical
slices.

## Compatibility note

This is application-owned behavior rather than a claim of wire-level parity
with the historical Rust project or any provider-specific CLI. Provider-native
controls must be documented separately if they are implemented later.
