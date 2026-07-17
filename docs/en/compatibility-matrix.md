# Compatibility matrix

[简体中文](../zh-CN/compatibility-matrix.md) · **English**

Statuses: `unassessed`, `planned`, `partial`, `compatible`, or
`intentionally-different`. “Evidence” points into the pinned Rust repository.

| Capability | Target | Status | Evidence / notes |
|---|---|---|---|
| Package and CLI composition root | M1 | partial | Python CLI skeleton implemented under the independent `neuro-code` command |
| `version` and JSON version output | M1 | partial | Independent package metadata implemented |
| Effective configuration inspection | M1 | partial | `neuro-code inspect`; secret-redacted Python view implemented |
| Named provider profiles and selection | M2/M3 | partial | Four explicit wire protocols, default/one-shot selection, legacy TOML compatibility, redacted `providers list/inspect`, and a TUI configured-profile picker with `/provider`, `/model`, and Ctrl+P implemented; remote model catalogs, reasoning-effort selection, and persistent in-app config editing remain pending |
| CC Switch compatibility | M2 | partial | Read-only `NEURO_CODE_CC_SWITCH_CONFIG` mapping, three backend formats, loopback `PROXY_MANAGED`, env references, inline-key rejection, and native project override implemented; CC Switch database/process control and its internal failover remain excluded |
| Safe provider failover | M2 | partial | Ordered lazy fallback profiles, first-event commit boundary, monotonic per-run selection, audited failures/selections, `--no-failover`, aggregate errors, and opaque-session origin protection implemented; retry, circuit breaking, and persistent health remain pending |
| Provider HTTP proxy policy | M2 | partial | Per-profile environment/direct/explicit-env modes, lazy URL validation, uniform four-adapter HTTPX options, redacted inspection/errors, and strict ambiguous-SOCKS diagnostics implemented; explicit routing was manually verified against DeepSeek while an invalid inherited `ALL_PROXY` remained present; PAC, multi-proxy mounts, and bundled SOCKS support remain pending |
| Headless single prompt | M2 | partial | Streaming text/tool loop, plain/JSON/JSONL output implemented |
| OpenAI-compatible/xAI Chat endpoint | M2 | partial | Chat Completions streaming, bounded `max_tokens`, tools, validated native user-image blocks, thinking-mode tool-roundtrip continuity, and fail-closed upstream-import visible-context replay implemented; double-gated DeepSeek live streaming/failover/read-tool tests implemented and manually passed, while recorded model-specific fixtures remain pending |
| OpenAI Responses/xAI dialect | M2 | partial | Generic portable Responses subset plus optional xAI encrypted reasoning, web/X/code hosted tools, profile-affine native replay, strict legacy official-host fallback, terminal-authoritative output, SSE lifecycle normalization, status stripping, fallback repair, and error redaction implemented; advanced hosted-tool filters, stateful response IDs, compaction items, and opt-in live fixtures remain pending |
| Anthropic provider | M2 | partial | Native Messages SSE, text/reasoning, tools, usage, errors, secret redaction, and validated user/tool-result image blocks implemented; live/model-specific fixtures pending |
| Gemini provider | M2 | partial | Native `streamGenerateContent` SSE, text/thoughts, tools, usage, errors, thought-signature round trip, and validated user inline/File API image parts implemented; live/model-specific fixtures pending |
| Core tool registry and schemas | M2 | partial | Independent Python contracts implemented |
| Read/list/grep tools | M2 | partial | Workspace-contained UTF-8 baseline implemented |
| Search/replace editing | M2 | partial | Atomic exact replacement and containment implemented |
| Bash execution and cancellation | M2 | partial | Bounded streaming output, null stdin, timeout/cancel cleanup, POSIX process-group TERM/KILL, and Windows process-group/`taskkill` fallback implemented; Windows Job Object and background-task parity pending |
| Permission rule precedence | M2/M3 | partial | Deny/ask/allow precedence, per-segment `&&`/`||`/`;`/pipe checks, wrapper peeling, nested `bash -c`, fail-closed complex scripts, asynchronous allow-once/deny, and memory-only exact-action session approval implemented; deny is re-evaluated before every approval, while persistent reviewed rules and full rule/file-access grammar remain pending |
| SQLite session event store | M2 | partial | Schema v2, transactional v1 migration, profile-affinity metadata, canonical ordered session-item persistence, message projection, append-only prefix checks, list, resume, export, and filesystem-identity workspace matching across platform path aliases implemented |
| Rust session import | M2 | partial | Read-only v0/v1 parsing, mixed legacy/current messages, structured images, ordered reasoning/backend-tool payloads, embedded `raw_output`/singular-reasoning recovery with backend-ID deduplication, bounded corrupt-line recovery, atomic SQLite import, prefix-safe resume, export v2, provider-native image replay, trusted-source xAI Chat replay, and strict-affinity native Responses encrypted/backend-tool replay implemented; compaction and stateful IDs remain pending |
| Full-screen/minimal TUI | M3 | partial | Textual prompt, scrollback, streamed text, bounded provider/tool status, durable multi-turn context, fail-closed approval modal with allow-once/exact-session/deny, safe configured-profile picker, local `/help`/`status`/`provider`/`model`/`cancel`/`clear`/`quit`, Ctrl+C in-flight cancellation, same-session retry, balanced cancellation results for active/unstarted local calls, and headless UI tests implemented; remote model catalog/effort picker, pristine pre-token rewind/interjection queues, rich tool cards/rendering, and cross-platform terminal smoke tests remain pending |
| Markdown/Mermaid/media rendering | M3/M5 | planned | Rendering crates and vendored notices apply |
| PTY and process-tree parity | M3 | planned | `ptyctl`, shell terminal modules |
| OS sandbox profiles | M3 | planned | Landlock/bwrap, Seatbelt, Windows adapter |
| ACP stdio/WebSocket | M4 | planned | `xai-acp-lib`, pager/shell ACP modules |
| MCP servers | M4 | planned | MCP lifecycle and transport implementation |
| Skills and AGENTS.md | M4 | planned | Agent prompt/discovery and user guide |
| Hooks and plugins | M4 | planned | Hooks and plugin marketplace crates |
| Subagents and plan mode | M4 | planned | Lifecycle/session/goal modules |
| Memory and compaction | M5 | planned | Memory and compaction crates |
| LSP, worktree, checkpoints | M5 | planned | Tools/workspace/worktree crates |
| Voice, image, video, web tools | M5 | planned | Provider-backed adapters required |
| Leader, relay, Computer Hub | M5 | planned | Local pieces plus external service boundary |
