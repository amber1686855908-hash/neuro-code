# Compatibility matrix

[简体中文](../zh-CN/compatibility-matrix.md) · **English**

Statuses: `unassessed`, `planned`, `partial`, `compatible`, or
`intentionally-different`. “Evidence” points into the pinned Rust repository.

| Capability | Target | Status | Evidence / notes |
|---|---|---|---|
| Package and CLI composition root | M1 | partial | `xai-grok-pager-bin/src/main.rs`; Python CLI skeleton implemented |
| `version` and JSON version output | M1 | partial | `xai-grok-pager/src/app/cli.rs`; branding intentionally independent |
| Effective configuration inspection | M1 | partial | `grok inspect`; secret-redacted Python view implemented |
| Headless single prompt | M2 | partial | Streaming text/tool loop, plain/JSON/JSONL output implemented |
| OpenAI-compatible/xAI model endpoint | M2 | partial | Chat Completions streaming, bounded `max_tokens`, tools, validated native user-image blocks, and thinking-mode tool-roundtrip continuity implemented; manually live-verified with DeepSeek V4 Flash, while Responses API and committed model-specific fixtures remain pending |
| Anthropic provider | M2 | partial | Native Messages SSE, text/reasoning, tools, usage, errors, secret redaction, and validated user/tool-result image blocks implemented; live/model-specific fixtures pending |
| Gemini provider | M2 | partial | Native `streamGenerateContent` SSE, text/thoughts, tools, usage, errors, thought-signature round trip, and validated user inline/File API image parts implemented; live/model-specific fixtures pending |
| Core tool registry and schemas | M2 | partial | `xai-grok-tools`; Python contracts implemented |
| Read/list/grep tools | M2 | partial | Workspace-contained UTF-8 baseline implemented |
| Search/replace editing | M2 | partial | Atomic exact replacement and containment implemented |
| Bash execution and cancellation | M2 | partial | Bounded streaming output, null stdin, timeout/cancel cleanup, POSIX process-group TERM/KILL, and Windows process-group/`taskkill` fallback implemented; Windows Job Object and background-task parity pending |
| Permission rule precedence | M2 | partial | Deny/ask/allow precedence plus per-segment `&&`/`||`/`;`/pipe checks, wrapper peeling, nested `bash -c`, and fail-closed complex scripts implemented; full rule/file-access grammar pending |
| SQLite session event store | M2 | partial | Versioned event/message store, list, resume and export implemented |
| Rust session import | M2 | partial | `xai-grok-shell/src/session/storage/jsonl/mod.rs`, `xai-grok-sampling-types/src/conversation.rs`; read-only v0/v1 parsing, mixed legacy/current messages, structured images, ordered reasoning/backend-tool payloads, embedded `raw_output`/singular-reasoning recovery with backend-ID deduplication, bounded corrupt-line recovery, atomic SQLite import, prefix-safe resume, export v2, and bounded provider-native image replay implemented; preserved provider-context replay pending |
| Full-screen/minimal TUI | M3 | planned | `xai-grok-pager`, `xai-grok-pager-minimal` |
| Markdown/Mermaid/media rendering | M3/M5 | planned | Rendering crates and vendored notices apply |
| PTY and process-tree parity | M3 | planned | `ptyctl`, shell terminal modules |
| OS sandbox profiles | M3 | planned | Landlock/bwrap, Seatbelt, Windows adapter |
| ACP stdio/WebSocket | M4 | planned | `xai-acp-lib`, pager/shell ACP modules |
| MCP servers | M4 | planned | `xai-grok-mcp` |
| Skills and AGENTS.md | M4 | planned | Agent prompt/discovery and user guide |
| Hooks and plugins | M4 | planned | Hooks and plugin marketplace crates |
| Subagents and plan mode | M4 | planned | Lifecycle/session/goal modules |
| Memory and compaction | M5 | planned | Memory and compaction crates |
| LSP, worktree, checkpoints | M5 | planned | Tools/workspace/worktree crates |
| Voice, image, video, web tools | M5 | planned | Provider-backed adapters required |
| Leader, relay, Computer Hub | M5 | planned | Local pieces plus external service boundary |
