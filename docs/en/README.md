# Neuro Code

[简体中文](../zh-CN/README.md) · **English**

Neuro Code is an extensible Python terminal coding agent. The project targets
stable behavior at the CLI, configuration, session, tool, MCP, and ACP
boundaries while using a Python-native internal architecture.

The implementation is pre-alpha. The headless agent runtime, an initial
minimal Textual TUI, and a partial ACP v1 stdio core are supported vertical
slices; remaining interface and protocol work is tracked in the
[compatibility matrix](compatibility-matrix.md).

## Install and launch

Install the released package once with a tool that exposes Python console
scripts globally, without activating a virtual environment for each use:

```bash
uv tool install neuro-code
# or: pipx install neuro-code
```

Then open a terminal in any directory and use any equivalent TUI launch form;
that directory becomes the workspace:

```bash
neuro
neuro code
neuro-code
```

Textual is a normal package dependency, so a standard installation includes
the TUI. During source development, use `uv run neuro` instead.

## Development

Python 3.12 or newer is required. The canonical environment manager is `uv`:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The package may also be exercised without installation during bootstrap:

```bash
PYTHONPATH=src python -m neuro_code version
PYTHONPATH=src python -m unittest discover -s tests
```

Inspect effective configuration without exposing secrets:

```bash
PYTHONPATH=src python -m neuro_code inspect --json
```

## Partial ACP v1 stdio

`neuro-code acp` serves a workspace-bound partial ACP v1 implementation over
the official Python SDK's newline-delimited stdio transport:

```bash
uv run neuro-code acp --cwd /absolute/workspace
```

The same ACP router can be served over a bounded WebSocket newline-JSON
bridge:

```bash
uv run neuro-code acp --transport websocket --host 127.0.0.1 --port 8765 --cwd /absolute/workspace
```

The slice implements `initialize`, `session/new`, `session/list`,
`session/load`, `session/delete`, `session/fork`, `session/resume`,
`session/prompt`, `session/cancel` (notification), and `session/close`, sends
`session/update` notifications, and requests interactive authority through
`session/request_permission`. It advertises `loadSession: true` and
list/delete/fork/resume/close session capabilities. Text, inline Image,
ResourceLink, and embedded text-resource prompt blocks preserve their order
and are bounded. An Image block accepts raw base64 only for a small raster MIME
allowlist, with at most eight 5 MiB images and 10 MiB in total; its associated
URI is never read or dereferenced. Audio blocks accept validated `audio/*` MIME
types and bounded base64, and embedded `BlobResourceContents` accepts validated
base64 with a bounded decoded size. Text, audio, and binary content remain
ordered in the durable model context; non-image media is projected to a safe
placeholder for providers that do not natively support it. An embedded
`TextResourceContents` block accepts only client-provided text, at most eight
64 KiB resources and 128 KiB in aggregate; its URI is an origin label and is
never resolved. ResourceLink metadata is allowlisted, `_meta` is not sent to
the model, and links are never downloaded or dereferenced during prompt
conversion.

Each connection remains bound to its launch workspace. `session/new` rejects a
different or relative `cwd`; an `off`-profile binding may additionally declare
up to four existing, absolute, non-overlapping `additionalDirectories`, while
every enabled sandbox rejects them before binding creation. It accepts bounded
stdio, Streamable HTTP, and legacy SSE `mcpServers`, fully initializes and
lists their tools before publishing the session, and rejects only ACP-transport
MCP servers. ACP session IDs are stable and separate from the internal SQLite
ID that is created lazily on the first prompt; their durable mapping allows a
later process to load the same ID. Load revalidates workspace, fixed sandbox,
and provider affinity, then replays only bounded/redacted visible user,
assistant, and tool history. Images remain in the durable model context, while
their ACP history projection is a safe textual placeholder; image bytes and
URLs are not replayed. Embedded text resources remain bounded, labeled user
text in history. System prompts, private reasoning, provider-native context,
arbitrary arguments, and raw tool data are not replayed. List returns
only safe metadata for the connection workspace, assigns durable ACP IDs to
legacy sessions, and uses bounded opaque cursor pagination. Concurrent prompts
are allowed across sessions but not within one session. Cancel, close, stdin
EOF, and connection failure cancel owned work and session-scoped background
tasks; close never deletes persisted history.

Each accepted MCP server and its tools are owned by that ACP session. The
official MCP Python SDK owns schemas, `ClientSession`, negotiation, and
JSON-RPC dispatch. Stdio uses Neuro Code's bounded `ProcessTree` bridge, while
remote transports use the SDK's Streamable HTTP or legacy SSE clients with
HTTPS/HTTP URL and header validation, no environment proxy inheritance, no
redirect following, and bounded response bodies. Tools, resource metadata,
resource templates, and prompts are projected through a bounded private MCP
session extension; that extension also supports refresh and bounded resource/
prompt reads. MCP sampling and elicitation callbacks are forwarded to the ACP
client through bounded private callback methods when the client supports them.
Every invocation is treated as side-effecting and requires ACP client approval
even under local bypass mode; explicit local deny still wins. `_meta` is
ignored and configured environment/header values are redacted. Cancelling a
remote request closes it locally and makes its connection unavailable; the
remote server is not a locally owned process, so its in-flight side effect is
never represented as successfully cancelled.

ACP session resume/delete/fork are implemented with workspace-scoped durable
identity, transactional fork/delete behavior, distinct replaying load versus
silent resume semantics, bounded additional directories, MCP HTTP/SSE tools,
and capability-gated client `fs/read_text_file` plus exact
`fs/write_text_file` replacement. A client that advertises `terminal: true`
also receives direct foreground `terminal_exec` in an `off` sandbox binding;
it takes an executable and argument vector, never a shell command, and does
not receive configured Neuro Code environment values. It also exposes the bounded standard
background lifecycle (`terminal_start`, `terminal_output`, `terminal_wait`, and `terminal_kill`)
with opaque task IDs; terminal input, resize, and PTY framing remain unavailable. This is still
explicitly not complete ACP v1
support: ACP MCP transport, binary multimedia history replay, client
interactive terminal input/resize/PTY methods, and arbitrary custom
extensions remain unsupported. The bounded private artifact, subagent,
lifecycle, MCP, and compaction extensions are intentionally outside the
standard ACP capability advertisement. See the
[compatibility matrix](compatibility-matrix.md) and
[ADR 0035](adr/0035-partial-acp-v1-stdio.md) plus
[ADR 0036](adr/0036-durable-acp-session-load.md) and
[ADR 0037](adr/0037-workspace-scoped-acp-session-list.md), plus
[ADR 0038](adr/0038-session-owned-stdio-mcp-tools.md),
[ADR 0050](adr/0050-acp-session-lifecycle.md),
[ADR 0051](adr/0051-bounded-remote-mcp-transports.md), and
[ADR 0052](adr/0052-capability-gated-acp-client-filesystem.md), and
[ADR 0053](adr/0053-capability-gated-acp-client-terminal.md), and
[ADR 0054](adr/0054-bounded-acp-inline-image-prompts.md), and
[ADR 0055](adr/0055-bounded-acp-embedded-text-resources.md), and
[ADR 0056](adr/0056-bounded-acp-client-background-terminals.md).

## Interactive TUI

During source development, launch the interactive interface without a
subcommand:

```bash
uv sync --extra dev
uv run neuro
```

On a first launch with no ready provider, the TUI opens its provider setup form
before composing an agent runtime. Normal Settings first shows language and
model-provider categories, then opens only the selected detail screen. Provider
settings can create or edit OpenAI Responses, OpenAI-compatible Chat, DeepSeek,
Anthropic, Gemini, or xAI profiles and save-and-use one immediately. Each
managed profile can inherit environment proxies, connect directly, or read an
explicit proxy from a named environment variable. Deletion requires a second
confirmation and removes the corresponding credential. If a managed default
profile cannot start because of its proxy configuration, the TUI opens that
profile with the error ready to repair instead of exiting to the terminal. The initial
TUI provides prompt input, scrollback, streamed
assistant text, provider/tool status,
and local `/help`, `/status`, `/settings` (alias `/setting`), `/provider`, `/model`,
`/effort [LEVEL]` (alias `/reasoning`), `/mode [MODE]`, `/sessions [QUERY]`, `/resume`,
`/rename TITLE` (alias `/title`), `/cancel`, `/clear`, `/quit`, and `/exit` commands.
Prompts in one launch share a durable session; `--resume SESSION_ID` opens an
existing session after workspace validation.

The full-screen interface uses a neutral dark palette with cool blue, violet,
cyan, and green semantic accents; warm colors are reserved for warnings and
errors. Textual's separate command palette is
disabled because `Ctrl+P` belongs to provider selection; session search remains
the plain-text `/sessions QUERY` flow and does not display an emoji search icon.
The app also reconciles the real TTY cell size when a terminal drops its normal
resize notification, so maximizing or resizing the window repaints the entire
viewport instead of leaving the previous canvas at the top left.

Opt-in production-CLI smoke tests now drive real terminal input rather than a
headless key hook. Linux/macOS use a standard-library PTY; Windows uses the
standard-library `ctypes` ConPTY adapter. The Windows path exercises idle
`Ctrl+C`, `Ctrl+Q`, resize, zero and non-zero exits, bounded output, parent
console-mode preservation where handles are available, and ordered teardown of
the alternate screen, cursor, and focus tracking. See
[ADR 0032](adr/0032-native-windows-conpty-lifecycle-evidence.md).

The reusable interactive-terminal substrate now sits above those native
adapters. It provides bounded cursor-based output with explicit drop counts,
raw input, resize, signals, wait and close, and refuses to spawn until
permission, workspace and any configured sandbox checks pass. POSIX owns the
complete PTY process group; Windows creates the ConPTY leader atomically inside
a kill-on-close Job. The partial ACP core does not expose this interactive
terminal substrate through client terminal APIs; interactive framing and
backpressure remain pending. See
[ADR 0034](adr/0034-bounded-owned-interactive-terminal-sessions.md).

User prompts and assistant responses use distinct restrained blocks on one
shared reading axis, with a 116-column maximum and no `You:`/`Assistant:` log
prefixes. A streamed response is mounted once in the conversation and updated
in place through its final text, so completion does not move it from a
temporary area into the scrollback. Automatic following stops when the user
scrolls upward.

Assistant text is rendered as Markdown with an application-owned semantic
palette for headings, emphasis, code, lists, links, and tables. Model text is
not interpreted as Rich/Textual markup, and hyperlink activation is disabled.
User prompts and local/external values remain literal text. Activity, plan, and
error notices use inline labels and semantic hierarchy; models, paths, and
tools do not receive accent color solely by type. Each model step reports
client-observed time to its first actionable output, and a completed turn
reports total elapsed time after the final assistant message. Consecutive tool
calls appear as one default-collapsed activity group with state, bounded intent
or aggregate counts, failures, and elapsed time. Long Bash commands are
truncated in the summary. Click the group or focus it and press `Enter`/`Space`
to open the fixed-height Inline Peek; `Up`/`Down` selects a call and `Enter`
opens the independent Tool Inspector. Summary and Peek never render full
stdout or load artifacts. Inspector Output/Input/Meta views retain bounded,
control-safe, credential-redacted content and workspace diffs; sensitive,
binary, oversized, dependency, cache, and version-control-internal content
stays hidden. Added and removed lines use green/red foregrounds plus distinct
tinted backgrounds. Mermaid and inline media are not part of this slice.

Below the composer, one compact label-free status row shows model, effort, and
mode on the left and context-window use plus working directory on the right.
Long values ellipsize in narrow layouts. The permanent shortcut row is omitted;
use `/help` or F1 for the existing command reference. While waiting for model output, the
supplied seven-cell collapsing pulse animates before the pending-assistant text. The
context percentage starts as a visibly approximate local estimate and switches
to provider-reported input/output token usage after a model step. A configured
`context_window_tokens` value supplies the denominator. When no capacity is
configured, the bar shows the known token use without inventing a percentage.
Managed provider details expose this local capability field so every configured
model can supply its own real denominator. When a requested effort has a different
implemented policy, the status projection shows both values, for example
`ultracode → max`. Its text updates with the selected UI language.

Typing `/` shows command syntax and parameter hints. The suggestions include
the six effort values, four modes, and currently selectable provider profile names; free
text commands display placeholders such as `SESSION_ID`, `QUERY`, and `TITLE`.
`Tab` applies the first valid completion while ordinary prompt text and modal
focus traversal retain their normal behavior.

Use `Ctrl+,`, `/settings`, or `/setting` to open a first-level category page,
then choose interface language, model providers, or network and proxy defaults. Provider details distinguish
OpenAI Responses (`/responses`) from OpenAI-compatible Chat Completions
(`/chat/completions`); the DeepSeek preset selects the latter and fills
`https://api.deepseek.com`.
Network settings own the global proxy default. Provider details inherit it by
default and expose an explicit override only when a provider needs another
route. Both screens run the same local proxy-policy resolver used by the runtime
before writing settings; this does not send a network request. An explicit
**Test and load models** action performs a read-only catalog request with the
draft endpoint, credential, and proxy policy. It sends no conversation and
creates no model generation charge. Successful results appear as an in-memory,
bounded model picker while manual identifiers remain available.
Authentication, endpoint/protocol, timeout, rate-limit, server, proxy, network,
and malformed-catalog failures remain redacted on the current settings screen;
response bodies are never rendered or persisted. Managed deletion requires a
second confirmation.
The selected language immediately updates application-owned controls, dialogs,
and status text; prompts and model/tool content are never translated. The choice
is stored with the reasoning-effort and interaction-mode preferences, separately from provider
configuration, in
`$NEURO_CODE_HOME/ui-preferences.json` (normally
`~/.neuro-code/ui-preferences.json`) and is reused on later TUI launches.

TUI-managed provider metadata is stored atomically in
`~/.neuro-code/providers.json`; API keys are kept out of that file and ordinary
`config.toml`, in a separate private `~/.neuro-code/credentials.json` file.
Both files use owner-only permissions where POSIX modes are supported, and
configured values are redacted again at the tool-result boundary. Manual
`~/.neuro-code/config.toml` profiles remain supported. A same-name TUI-managed
profile replaces the complete TOML provider table so a workspace cannot
redirect its stored key to another endpoint. The storage port is replaceable
by a future OS-keychain adapter; the current credential file is not encrypted
at rest.

Use `Ctrl+E`, bare `/effort`, or bare `/reasoning` to open the six-level effort
picker. `/effort LEVEL` and `/reasoning LEVEL` select directly, and `--effort
LEVEL` selects a level for an interactive launch or headless run. A TUI change
is saved as a user preference and is reapplied after a later launch, profile
switch, or in-process session resume. An explicit `--effort` takes precedence
at TUI startup; without an explicit or valid saved choice the default is
`high`. A headless run also defaults to `high`. Effort cannot change while a
turn is active and applies from the next model step.

| Level | Marker | Implemented application behavior |
|---|---:|---|
| `low` | ○ | Direct response with the minimum inspection and verification required for correctness |
| `medium` | ◐ | Routine inspection, self-review, and focused verification |
| `high` | ● | Deeper investigation and proactive regression checks; the default |
| `xhigh` | ⬤ | Difficult edge cases, challenged assumptions, and multiple validation passes |
| `max` | ◆ | Maximum ordinary single-agent depth with broad but bounded verification |
| `ultracode` | ⚡ | Application-owned bounded delegation to exactly one `MAIN_MAX` or `BOUNDED_SWARM` path |

These levels are Neuro Code application policies, not claims about a
provider's private reasoning controls. `max` remains the deepest ordinary
single-agent policy. An explicit `ultracode` turn enters the application-owned
bounded delegation service, which durably chooses exactly one `MAIN_MAX` or
`BOUNDED_SWARM` branch; `MAIN_MAX` uses the existing normal Agent runtime and
`BOUNDED_SWARM` uses the existing bounded Swarm composition. Provider adapters
never receive an invented native `ultracode` value: the ordinary main branch
uses the existing provider-compatible `max` projection or omission. The
delegation decision, downstream identity, and parent-visible result are
recovery-safe and are not automatically switched or replayed. See [ADR 0027](adr/0027-semantic-tui-and-application-reasoning-effort.md)
and [ADR 0141](adr/0141-automatic-ultracode-delegation.md).

Ordinary Agent execution uses the `normal` budget profile by default (48 model
calls, 48 tool rounds, and 192 tool calls). `--execution-profile deep` selects
the bounded 96/96/384 profile for longer investigations. The compatibility
option `--max-steps N` overrides the selected profile and scales the complete
ordinary budget to N model calls, N tool rounds, and 4N tool calls; it no longer
leaves a separate fixed tool ceiling. Finalizer attempts remain independently
bounded. See [ADR 0105](adr/0105-unified-execution-budget-and-replan-guidance.md).

Repository inspection also exposes bounded `read_files`, `list_tree`, and
`grep_many` tools alongside the compatible single-file operations. They keep
tool execution sequential while reducing model/tool round trips: batch reads
isolate per-file errors, tree traversal skips links and common generated
directories, and multi-query search applies deterministic path ordering plus
per-query, total-result, scanned-file, and UTF-8 byte limits. All paths still
pass through the workspace resolver, model-visible output is redacted, and an
ACP client with text-read capability can serve each explicit `read_files`
request. See [ADR 0106](adr/0106-bounded-batch-repository-inspection.md).

Use `Shift+Tab` to cycle `normal`, `accept-edits`, `plan`, and `auto`, or use
`/mode MODE` to select directly. `normal` automatically permits reads and asks
for side effects; `accept-edits` additionally permits workspace edit tools;
`plan` denies side effects without prompting. Until a safety classifier is
implemented, `auto` is an explicitly labelled safe preview with the same
permission defaults as `accept-edits`, so commands and network effects still
ask. Only an explicit startup `--always-approve` retains the existing bypass
default; explicit rules and the process sandbox still win. Mode changes are
rejected during a turn, persist as a UI preference, and are reapplied after
profile/session switches.

Plan mode also gives the model a provider-neutral `update_plan` tool. It
replaces one bounded structured plan (purpose plus pending, in-progress, or
completed steps), persists it with the SQLite session, restores it on resume,
and copies it when a session is forked. Use `/plan DESCRIPTION` to enter plan
mode and immediately ask for a plan, or `/view-plan` (alias `/show-plan`) to
show the current saved plan. Once the user has reviewed it, `/execute-plan`
(alias `/run-plan`) explicitly records a plan-to-turn handoff and switches only
to `accept-edits`. Each such turn has one durable, metadata-only session task
record with an opaque ID and a `queued`, `running`, `completed`, `failed`, or
`cancelled` state; it is restored for inspection but is not copied to a fork.
Command,
network, workspace, and sandbox boundaries remain in force. `/comment-plan STEP
COMMENT` (alias `/plan-comment`) stores bounded user feedback for one current
plan step and `/view-plan` renders that feedback under its step. Comments are
shown to the model only with the next prompt; they do not approve, execute, or
schedule work. They follow the current plan when a session is forked, but a
replacement plan discards its old comments. Use `/schedule-plan` (alias
`/queue-plan`) to persist a bounded queued copy of the current plan without
contacting the model, then `/run-task TASK_ID` to start exactly that snapshot
explicitly. At most four plan tasks can be queued per session; queued tasks
never auto-start, retry, wake, or spawn subagents. `/tasks` keeps task lists
compact. To inspect the
complete immutable plan snapshot for one durable plan-execution task, explicitly
run `/view-task TASK_ID`; it resolves only that ID in the currently open session
and presents the snapshot as read-only reference. It neither changes the current
plan nor starts a model turn or any work. See
[ADR 0028](adr/0028-timed-tool-feedback-and-interaction-modes.md) and
[ADR 0057](adr/0057-durable-structured-session-plans.md), plus
[ADR 0058](adr/0058-durable-session-task-lifecycle.md) and
[ADR 0059](adr/0059-bounded-current-plan-comments.md), plus
[ADR 0060](adr/0060-plan-execution-revision-snapshots.md) and
[ADR 0061](adr/0061-read-only-plan-execution-inspection.md), plus
[ADR 0063](adr/0063-bounded-explicit-plan-task-scheduling.md).

Use `Ctrl+P`, bare `/provider` or bare `/model` to open the configured-profile
picker. `/provider PROFILE` and `/model PROFILE` select directly. The picker
shows only profile name, model, protocol, and readiness; unavailable profiles
or profiles with missing credentials are disabled. It selects configured
profiles, not arbitrary remote model IDs. Selecting a TUI-managed profile also
saves it as the default for later launches; profile creation and editing remain
in Settings.
Switching is rejected during a turn. Switching to a different profile keeps
the previous SQLite session available and gives the next prompt a fresh
conversation, preventing provider-affine or encrypted context from crossing
provider boundaries.

Use `Ctrl+R`, `/sessions`, or bare `/resume` to open the 50 most recent sessions
for the active workspace; `/resume SESSION_ID` selects directly.
`/sessions QUERY` first performs workspace-scoped full-text search over saved
titles and visible conversation content. The picker shows the deterministic
first-prompt title (or an imported title), shortened ID, update time, stored
provider/model, resume profile, and a bounded snippet for search results.
Queries, titles, and snippets render as literal text. System messages,
provider-private reasoning/native items, image URLs, tool arguments/metadata,
and raw tool-result content are not indexed. Resume prefers
a ready configured source profile. Otherwise it uses the current ready profile
while stored source/model/affinity metadata continues to filter incompatible
provider-native context fail closed. The previously active session remains
unchanged.

`/rename TITLE` updates the current saved session title; `/title TITLE` is an
alias. Rename is rejected before the first session is created or while a turn
is running. Titles are whitespace-normalized and bounded to 200 characters,
and SQLite updates the canonical summary and FTS title in one transaction.

The picker also shows the saved sandbox profile. Because the process sandbox is
already active, sessions created under a different profile are disabled and
must be opened by restarting Neuro Code with `--resume SESSION_ID`. Sessions
created before profile metadata existed remain selectable under the active
profile.

Startup and in-app resume replay canonical visible user/assistant messages,
image placeholders, and name-only tool lifecycle entries. Persisted reasoning,
provider-native items, tool arguments, image URLs, and raw tool-result content
never enter the transcript; each restored message also has a 20,000-character
UI bound. See
[ADR 0018](adr/0018-workspace-scoped-interactive-session-resume.md).

While a turn is running, use `Ctrl+C` or `/cancel` to request cancellation. The
runtime records cancellation, balances any active and not-yet-started local tool
calls with error results, reloads the durable conversation, and leaves the same
session ready for another prompt. TUI prompts request a pristine rewind policy:
when cancellation arrives before any non-empty model text/reasoning, completion,
or tool activity, the just-submitted user message is removed from durable model
context and restored to the input draft. The `USER_MESSAGE` and `TURN_FAILED`
events remain as an audit trail. Once output or tool activity exists, the prompt
is retained and the normal cancellation recovery path applies. Before the first
non-empty model token, up to four explicit follow-up prompts are buffered locally
and run in order after the current turn completes; they are not added to session
history until started. A cancelled or failed turn restores the first buffered
prompt to the input when a pristine rewind is not safe.

When a side-effecting tool resolves to `ask`, the TUI opens a fail-closed
approval modal. Deny is focused by default. Choose allow once, allow the
identical tool/argument action for this process session, or deny; `Esc` also
denies, as does `Ctrl+C` while the modal is open. Edit summaries show the
workspace path but hide replacement/patch content, while Bash shows the bounded
command being authorized. Session grants
store only an in-memory exact-action digest and are checked after policy, so an
explicit deny can never be overridden. Closing or cancelling approval never
starts the tool.

`Ctrl+C` inside the approval modal denies that one request; it does not invoke
whole-turn cancellation.

Use `--always-approve` only in a workspace where unrestricted tool execution is
intentional. For scripts and machine-readable output, retain the headless path;
unresolved approval remains denied there:

```bash
neuro-code -p "Explain this repository"
neuro-code agent -p "Explain this repository" --output-format jsonl
```

## Managed background commands

Normal CLI/TUI composition advertises a process-owned background-command
contract to the model. `bash` with `is_background=true` returns a task ID
immediately. `task_output` returns its current status and output without
waiting, or accepts `wait_seconds` up to 30 for event-driven bounded waiting.
`wait_tasks` accepts up to 20 IDs and waits for any one or all known tasks with
a bounded 30-second event-driven budget. Unknown or cross-conversation IDs are
reported as `not_found`; a timeout returns partial state without stopping work.
`kill_task` stops the whole owned process tree with TERM followed by KILL when
needed. Starting and killing remain side-effecting operations and pass through
the same permission/approval policy as other local actions.

An omitted background `timeout_seconds` lets the task run until it exits, is
killed, or the application closes; a positive explicit value creates a task
deadline. Output is a bounded in-memory head/tail preview with a total-byte
counter, not a persistent full log. At most 16 tasks run concurrently and at
most 64 records are retained per conversation scope.

Background records are not stored in SQLite. A task may be used across turns
in the same running TUI conversation binding, but it cannot survive a profile
switch, in-process session resume, restart, or startup resume. Switching a
binding terminates its live tasks and reports the count; a one-shot headless run
terminates any remaining tasks before returning, and TUI exit does the same.

Use `/tasks` in the TUI for a read-only list of the current binding's live
background-task metadata and its durable plan-execution task records. A
background task shows its task ID, status, exit code, bounded-output size, and
start time; a plan-execution record shows its opaque ID, kind, state, start and
terminal time, plus the first 12 characters of its immutable plan-revision
fingerprint and its completed-step count. It never prints a prompt, command,
tool output, or credential. The TUI emits one local terminal-state notice per
background task but does not print command text or raw output. `/tasks` cannot
terminate work: ask the model to use `kill_task` so the action continues through
permission/approval policy. Prefer `is_background=true` to an inner shell `&`.
For one durable plan-execution record, `/view-task TASK_ID` is the separate
explicit read-only inspection surface for its stored plan snapshot; it is limited
to the open session and cannot execute, retry, or modify anything.
On Windows, each `ProcessTree` owns a kill-on-close Job Object,
assigns the leader atomically during `CreateProcessW`, waits for descendants
after the leader exits, and fails command launch if extended creation cannot
preserve that boundary. Only the null-input and output-pipe handles in the
explicit handle list are inherited. See
[ADR 0021](adr/0021-owned-background-shell-tasks.md) and
[ADR 0031](adr/0031-fail-closed-windows-job-objects.md),
[ADR 0033](adr/0033-atomic-windows-job-process-creation.md), plus
[ADR 0022](adr/0022-session-scoped-background-task-visibility.md). Multi-task
wait semantics are defined by
[ADR 0024](adr/0024-event-driven-multi-background-task-wait.md).

Natural completion is also reported once at the next explicit model boundary:
the next model step after tool execution or the next user-triggered turn while
idle. Each model-only notice is capped at 20 tasks and contains escaped status
metadata only—not command text, cwd, or output. A terminal `task_output` result
or `wait_tasks`/`kill_task` result consumes the corresponding notice to prevent
duplication.
Notices are acknowledged only after a provider returns a valid completion, are
not persisted as conversation messages, and never start an autonomous paid
model turn when the effective wake policy is disabled. The persisted user-wide
default is edited in Settings; each managed provider profile may inherit that
default or explicitly override it. An idle TUI session may still opt in or out
temporarily with `/auto-wake on|off`; that session choice takes precedence and
starts at most one model-only wake for each pending completion batch, consumes
the pending notice only after a valid completion, and keeps the wake response
and synthetic reminder out of durable conversation items. Legacy and new
profiles default to off. See
[ADR 0023](adr/0023-model-visible-background-task-completion-reminders.md).

## Operating-system sandbox profiles

Sandboxing is opt-in and defaults to `off`. Select a persistent profile in
user or project configuration, use `NEURO_CODE_SANDBOX`, or override one run:

```toml
[sandbox]
profile = "workspace"
```

```bash
neuro-code -p "Inspect and test this repository" --sandbox workspace
```

On Linux, non-`off` profiles require usable, non-workspace-controlled `bwrap`.
Neuro Code preflights the required user, mount, PID, and (for `read-only` and
`strict`) network namespaces before exposing each child. The trusted controller,
provider connections, credentials, and session store remain on the host. An
explicit enabled profile never falls back to an unsandboxed child.

| Profile | Filesystem | Local Bash network |
|---|---|---|
| `off` | Explicitly no OS filesystem, network, controller-state, or detached-descendant isolation | Available |
| `workspace` | Empty child root; required runtime read-only; authorized workspace roots writable; private HOME and temporary storage | Available |
| `read-only` | Same empty child root, but authorized workspace roots read-only; edit tool unavailable | Isolated |
| `strict` | Same allowlist root as `workspace`; authorized workspace roots remain writable | Isolated |

The parent process retains network access for model APIs. Permissions still run
before tools and remain necessary: a sandbox limits an approved action but does
not decide whether to approve it. A user-level profile cannot be weakened by a
project file. CLI and environment choices are explicit higher-priority
selections for a new session; they cannot change a saved session's profile
during resume. `neuro-code inspect` reports the canonical profile and its source.
`off` remains the compatibility default and is a deliberate request to run
children without an OS security boundary; POSIX cleanup then owns only the
original process group and cannot guarantee termination of descendants that
call `setsid()`. Enabled Linux profiles add a PID namespace and
`--die-with-parent`, so process-group escape does not escape the sandbox
lifecycle boundary. Before enabled-profile startup, Neuro Code also rejects a
controller-state regular file with multiple hardlinks: otherwise an existing
workspace hardlink could name the same private inode through an authorized bind
mount. Other pre-existing files inside an explicitly authorized workspace are
within that workspace's trust boundary.

Local Bash also strips configured provider API-key variables and standard or
explicit proxy variables instead of inheriting their secret values.

macOS and Windows currently fail closed for explicit non-`off` profiles.
Every new session, including `off`, stores its canonical profile. Resume without
an explicit sandbox restores that value; a canonically different explicit
`--sandbox` or `NEURO_CODE_SANDBOX` fails before sandbox enforcement and model
composition. Legacy sessions without the field use normal configuration.
Custom profiles remain unsupported. See
[ADR 0019](adr/0019-fail-closed-linux-sandbox-profiles.md) and
[ADR 0020](adr/0020-session-fixed-sandbox-profiles.md).

## Model providers

Neuro Code has no implicit cloud provider. Configure at least one named profile
in `~/.neuro-code/config.toml` or `.neuro-code/config.toml`; otherwise model
runs fail with setup guidance instead of silently targeting xAI.

```toml
[routing]
default = "deepseek"
fallbacks = ["anthropic"]

[providers.deepseek]
protocol = "openai-chat"
dialect = "deepseek-v4"
model = "deepseek-v4-pro"
base_url = "https://api.deepseek.com"
auth = "env"
api_key_env = "DEEPSEEK_API_KEY"
context_window_tokens = 1000000
max_output_tokens = 8192
timeout_seconds = 120
proxy_mode = "environment"

[providers.anthropic]
protocol = "anthropic-messages"
model = "replace-with-an-api-model-id"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
```

Supported wire protocols are `openai-chat`, `openai-responses`,
`anthropic-messages`, and `gemini-generate-content`. Configuration stores only
an environment-variable name. Neuro Code never writes a raw API key, never
loads project `.env` files, and redacts credentials from inspection and errors.
`context_window_tokens` is local capability metadata used for budgeting and UI
display; it is not sent as a provider request parameter.

Inspect and select profiles without editing the default:

```bash
neuro-code providers list
neuro-code providers inspect deepseek --json
neuro-code -p "Explain this repository" --provider deepseek
```

`--provider`, `--model`, and `--base-url` are one-shot overrides. The matching
`NEURO_CODE_PROVIDER`, `NEURO_CODE_MODEL`, and `NEURO_CODE_BASE_URL` environment variables
are applied before CLI overrides. Legacy `[provider.default]` and
`[model.default]` tables remain readable during migration.

### Safe provider failover

`[routing] fallbacks` defines an ordered list of alternative profiles. Provider
instances are created lazily, so an unavailable fallback does not prevent the
primary profile from running. Each candidate is attempted at most once and a
successful fallback remains selected for later model steps in the same run.

Failover is intentionally limited to failures before the candidate emits its
first model event. Once text, reasoning, a tool call, a provider-hosted tool
lifecycle event, usage, or completion has appeared, an error is propagated
without trying another provider. This commit boundary avoids replaying work
that may already have produced output, side effects, or charges. Attempts and
selections are exposed as `provider_attempt_failed` and `provider_selected`
runtime events. Use `--no-failover` to run only the selected profile:

```bash
neuro-code -p "Explain this repository" --no-failover
```

When every candidate fails, Neuro Code reports a bounded aggregate error. It
does not yet retry a candidate, maintain persistent health, or implement a
circuit breaker. See [ADR 0011](adr/0011-safe-pre-output-provider-failover.md).

### HTTP proxy policy

Every provider profile has an explicit HTTP transport policy:

- `proxy_mode = "environment"` is the backward-compatible default. HTTPX reads
  `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, and its certificate
  environment. Neuro Code validates configured proxy URL schemes lazily before
  the selected profile starts.
- `proxy_mode = "direct"` sets HTTPX `trust_env = false`. This ignores proxy and
  certificate environment variables for that profile only.
- `proxy_mode = "explicit"` requires `proxy_url_env`; the named environment
  variable supplies the proxy URL without persisting it in TOML.

For example:

```toml
[providers.deepseek]
protocol = "openai-chat"
dialect = "deepseek-v4"
model = "deepseek-v4-pro"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
proxy_mode = "explicit"
proxy_url_env = "NEURO_DEEPSEEK_PROXY_URL"
```

Inspection exposes only the mode, environment-variable name, and a configured
boolean. Proxy URLs and credentials are redacted from errors. The ambiguous
`socks://` scheme is rejected instead of guessed; use `socks5://` or
`socks5h://` with HTTPX's optional SOCKS dependency, or use an HTTP proxy. The
released package exposes that dependency as `neuro-code[socks]`. The TUI runs
the same validation before saving a managed profile; a failing managed default
opens provider settings for repair during startup preflight.
See [ADR 0012](adr/0012-provider-http-proxy-policy.md) and the official
[HTTPX environment-variable documentation](https://www.python-httpx.org/environment_variables/).

### CC Switch compatibility

Set `NEURO_CODE_CC_SWITCH_CONFIG` to a CC Switch-exported TOML file to load it
read-only as the lowest-precedence source. Its active `[models] default` and
`[model."<profile>"]` entry become an in-memory profile named
`cc-switch:<profile>`. Backend values map as follows:

| CC Switch `api_backend` | Neuro Code protocol |
|---|---|
| `responses` | `openai-responses` |
| `chat_completions` | `openai-chat` |
| `messages` | `anthropic-messages` |

An `env_key` is used as an environment-variable reference. The
`PROXY_MANAGED` placeholder is accepted only for a plain-HTTP loopback URL,
such as `http://127.0.0.1:15721/provider/v1`. Other inline keys are neither
copied nor used; the profile is reported unavailable with remediation. CC
Switch remains optional: Neuro Code does not read its private database, manage
its process, or rely on it for direct providers. CC Switch still requires a
legitimate upstream credential, OAuth grant, relay token, or local model.

Auto-detected proxy profiles default to `native_context = "disabled"` because
the proxy may change upstream providers. A project-local override may set
`native_context = "profile"` only when the endpoint/profile is stable and
trusted. Opaque reasoning is then replayed solely when the stored profile
affinity fingerprint matches exactly.

### Local safe Web Fetch

Local Web Fetch is opt-in and defaults to disabled. Add the following only
when the current session is allowed to read public web pages:

```toml
[web_fetch]
mode = "local" # disabled | local | auto | inline
```

`local` uses Neuro Code's public-only, bounded HTTP fetcher; `auto` keeps an
explicitly supported MAIN hosted fetch and otherwise uses the local tool;
`inline` requires that MAIN capability and fails closed. The local tool is
permission-gated, sends no provider proxy/cookie/auth state, rejects private or
local destinations, and labels returned text as untrusted web content. It does
not browse with JavaScript or automatically fetch search results. See
[ADR 0121](adr/0121-local-safe-web-fetch.md).

### Optional xAI dialect

xAI is an optional Responses dialect rather than the application default:

```toml
[providers.xai]
protocol = "openai-responses"
dialect = "xai"
model = "replace-with-an-xai-model-id"
base_url = "https://api.x.ai/v1"
api_key_env = "XAI_API_KEY"
native_context = "profile"
builtin_tools = ["web_search", "x_search", "code_interpreter"]
```

The generic Responses adapter uses local `store: false` history. The xAI
dialect additionally requests encrypted reasoning, supports xAI-hosted tools,
and preserves validated reasoning/backend-tool items. Hosted tools are executed
by xAI, bypass local tool execution, emit separate backend lifecycle events,
and may incur additional charges. Stateful `previous_response_id` chaining and
compaction items are not yet implemented.

DeepSeek V4 uses `openai-chat` with the explicit `dialect = "deepseek-v4"`;
other Chat Completions services use the standard dialect. During thinking-mode
tool use, assistant reasoning associated with a tool call is
persisted and replayed on the next request; completed no-tool reasoning remains
local. Load `DEEPSEEK_API_KEY` through the shell or a secret manager before
running the selected profile.

### Opt-in live regression tests

Live tests are guarded twice because they use network access and may incur
provider charges. The normal test command excludes the `live` marker. Even
when `-m live` is selected, collection adds a skip unless
`NEURO_CODE_RUN_LIVE_TESTS=1` is present. Credentials are read only from the process
environment; the test suite never loads `.env` automatically.

After exporting `DEEPSEEK_API_KEY` in the calling shell, run:

```bash
NEURO_CODE_RUN_LIVE_TESTS=1 uv run pytest -m live tests/live
```

Optional `NEURO_CODE_LIVE_DEEPSEEK_MODEL` and
`NEURO_CODE_LIVE_DEEPSEEK_BASE_URL` values override the safe defaults shown in
`.env.example`. The current DeepSeek checks cover real streaming, recovery from
an intentional pre-output primary failure, and a read-only local tool round
trip. They never enable Bash or write tools. Live tests inherit standard proxy
environment variables by default. Set `NEURO_CODE_LIVE_PROXY_MODE=direct` to ignore
them, or set `NEURO_CODE_LIVE_PROXY_MODE=explicit` together with the ephemeral
`NEURO_CODE_LIVE_PROXY_URL` environment variable to route only these checks. This
is useful when a local `ALL_PROXY` uses a URL scheme rejected by HTTPX; never
place proxy credentials in project configuration.

Resume, list, rename, export, and import sessions:

```bash
neuro-code -p "Continue the work" --resume SESSION_ID
neuro-code sessions --json
neuro-code sessions search "sqlite migration"
neuro-code sessions search "sqlite migration" --json --include-content --limit 20
neuro-code sessions rename SESSION_ID "Manual session title" --json
neuro-code export SESSION_ID --format markdown --output transcript.md
neuro-code import-session /path/to/upstream/session --json
```

The JSON forms of `sessions` and `sessions search` add a bounded
`last_execution` projection when a durable terminal result exists. It contains
only the status, reason, finalized/recoverable flags, and completion timestamp;
plain-text session listings remain unchanged.

`import-session` accepts either a supported upstream Rust session directory or
its `summary.json`. It reads the JSONL files without modifying them and
atomically creates a new SQLite session while preserving the source session
ID, workspace, model, and timestamps. A duplicate session ID is rejected
rather than overwritten. The JSON report identifies skipped corrupt or
unsupported records. Ordered reasoning/backend-tool records and image URLs are
preserved structurally. JSON export schema version 4 exposes the complete
`conversation_items` sequence alongside its ordinary `messages` projection.
It also reports the canonical saved sandbox profile and optional title, or
`null` for a legacy sandbox profile. A recognized built-in profile and
`generated_title` from an upstream summary are preserved;
an unsupported custom profile is rejected instead of silently downgraded.
Legacy assistant `raw_output`, singular reasoning, and v0
`reasoning_content` are upgraded in memory; backend-tool IDs prevent an
embedded copy from duplicating an earlier standalone record. The import report
counts recovered, deduplicated, malformed, and unsupported embedded records.
Supported image references are replayed through native provider blocks:
OpenAI-compatible and Gemini user messages, plus Anthropic user and tool-result
messages. Invalid references and unsupported roles receive an explicit image
placeholder. Resuming an imported upstream session against the official xAI
HTTPS endpoint replays visible reasoning and ordered backend-tool summaries
only when the stored source marker is trusted. Opaque encrypted Responses state
is never copied into Chat Completions, and non-affine providers receive only
the ordinary-message projection. Selecting an `openai-responses` profile with
the xAI dialect instead replays validated reasoning and supported backend-tool
items natively, strips output-only
reasoning status on input, and continues to reject non-affine opaque state.

Headless Bash permissions accept the compatible `Bash(...)` spelling. Every
command in a simple chain is evaluated independently, so allowing `git status`
does not implicitly allow a later command:

```bash
neuro-code -p "Inspect the repository" \
  --allow 'Bash(git status)' \
  --deny 'Bash(git push:*)'
```

Explicit deny rules override `--always-approve`. Under a restrictive Bash
policy, substitutions, redirections, multiline scripts, and other constructs
that cannot yet be decomposed safely are denied in headless mode. Commands run
with null stdin, bounded output, disabled pagers/prompts, and process-tree
cleanup on timeout or cancellation.

## Read-only LSP semantic navigation

Neuro Code exposes a stable `lsp` read-only tool for definition, references,
hover, document symbols, workspace symbols, diagnostics, server status, and a
bounded explicit restart. Configure a server explicitly in the existing user
or project TOML file:

```toml
[lsp.servers.python]
language = "python"
command = ["pyright-langserver", "--stdio"]
extensions = [".py"]
root_markers = ["pyproject.toml"]
```

The command is argv-only. Neuro Code does not download or install a language
server, and an unavailable executable fails closed at execution time. The
server runs through the canonical local-process boundary, receives a read-only
workspace, and cannot apply workspace edits. LSP locations are filtered to
safe local workspace files and existing permission rules; unsafe or unresolved
cross-file results are omitted. Use `grep` for text search. Rename, formatting,
code actions, and workspace edits remain unsupported. The explicit internal
writable-worker runtime may compose this same read-only LSP service into its
managed child worktree; it does not turn worktree or checkpoint lifecycle into
LSP tools. See [ADR 0132](adr/0132-worker-scoped-lsp-runtime-integration.md).

## Project status

- Minimum Python: 3.12
- Target platforms: Linux, macOS, Windows
- License: Apache-2.0; see [`NOTICE`](../../NOTICE) for provenance requirements
