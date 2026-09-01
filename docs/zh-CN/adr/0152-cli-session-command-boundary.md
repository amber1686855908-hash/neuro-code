# ADR 0152：CLI Session Command 边界

- 状态：Accepted
- 日期：2026-08-31
- 范围：叠加在 PR #80 之上的有界 CLI session-command execution 切片
- 依赖：PR #80 以及既有 CLI/bootstrap 和 session application boundary

## Context

精确冻结的 PR #80 head 是
`11a6c610fe7f9e949d5a5c2f3aab2adb2358385f`，其 base 是
`codex/acp-transport-boundary` 的
`d5c95fc5d0a621b58827c5aa9b1e9f43dff70e06`。在该 head 上，
`neuro_code.cli` 仍同时包含 parser construction、顶层 dispatch、完整的
CLI service protocol，以及 `sessions` command 的完整 execution body。下一步
consolidation 必须足够小且可审计，不能改变 CLI grammar、application session service
或 bootstrap composition。

本 ADR 只提取已经解析的 `sessions` command 的 execution boundary。不把
`neuro_code.cli` 变成完整 compatibility facade，也不改变 public command surface。

## 提取前 audit

审计针对 PR #80 exact head 完成，并在移动代码前结束。

### Boundary symbol

`neuro_code.cli` 中属于 session 的 execution symbol 是：

- `_sessions_command`，解析后的 command 的唯一 async implementation；
- 用于 list/search 的 `SessionCatalogApplicationService`、`ListSessionsRequest` 和
  `SearchSessionsRequest`；
- 用于 rename 的 `SessionLifecycleService` 与 `RenameSessionRequest`；
- 用于 inspect/abandon/retry 的 `TurnRecoveryService`；
- 用于 artifact operation 的 `SessionToolOutputArtifactApplicationService` 及其
  list/read request；以及
- 从 `neuro_code.interfaces.cli.serialization` 导入的 session execution、artifact 和
  search serializer。

`MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES` 同时被 session execution body 和 parser
construction 使用，因此不会从 `neuro_code.cli` 移走。Parser-only symbol、`_export_session`、
`_import_session` 和其他 CLI command body 都保留在原模块。

### State 与 lifecycle

Sessions command 不拥有长生命周期 mutable state。每次 invocation 加载 configuration 并打开
一个 session store。List/search/rename 与 artifact operation 通过既有 application service 使用该
store。Compact 和 recovery retry 为选中的 session 打开 application，配置 resume，创建一个
binding，调用既有 runner operation，并在既有 `finally` path 中通过 `asyncio.shield` 总是关闭
application。本切片不新增 task registry、retry state、cancellation owner、provider authority、
workspace authority 或 persistence implementation。

### Call site 与 service contract

唯一的 production call site 是 `neuro_code.cli` 中 `run()` 的 sessions dispatch。Canonical
bootstrap launcher 仍构造 `BootstrapCliServices`，tests 与 callers 仍可注入兼容 service。
审计后的 execution body 只需要：

- `load_config`；
- `create_session_store`；
- `create_tool_output_artifact_service`；以及
- `open_application`。

完整的 `CliServices` protocol 还包含与本命令无关的 agent、TUI、provider、ACP、import 和
export capability，因此不作为 canonical command boundary contract。

### Dependency direction 与冻结 behavior

目标方向是：

```text
neuro_code.cli parser/top-level dispatch
        -> neuro_code.interfaces.cli.sessions
        -> application session/artifact service 与 port
        -> 注入的 application/binding/runner seam
```

Canonical module 可以复用 `interfaces.cli.serialization`，但不得 import `neuro_code.cli`，也不得
获取 bootstrap、provider、workspace、sandbox 或 permission authority。CLI parser 仍拥有 artifact
bound，因为同一 bound 也是 parser default 的一部分。

已有 `tests/test_cli.py` 已冻结 public sessions list/search/rename 与 artifact list/read/prune
behavior，包括 JSON/plain projection 与 validation。本切片增加 direct canonical execution、public
dispatch equivalence、error mapping、JSON/plain equivalence、compact/recovery retry cleanup、
identity alias 和 import-direction coverage。

## Decision

`neuro_code.interfaces.cli.sessions` 是 `run_sessions_command(args, services)` 的 canonical owner。
它拥有已经解析的以下 operation 的 validation、application-service selection、execution 和
presentation：

- list；
- search；
- rename；
- compact；
- artifacts list/read/prune；以及
- recovery inspect/abandon/retry。

它声明窄的 `SessionCliServices`、`SessionCliApplication`、`SessionCliBinding` 和
`SessionCliRunner` protocol，只描述该 boundary 实际使用的 capability。这些 protocol 描述既有
application seam，不引入第二套 service implementation，也不改变 ownership。

`neuro_code.cli` 保留 `build_parser`、所有 sessions parser grammar、顶层 `run` dispatch 和其他
command implementation。其 private `_sessions_command` name 是指向 `run_sessions_command` 的
identity-preserving import alias，因此 private compatibility import 仍解析到同一份 implementation。

Canonical command 继续使用 `neuro_code.interfaces.cli.serialization` 提供有界 projection。本决定不
包含 parser 或 public CLI API redesign。

## Behavior 与 compatibility

本次 extraction 保持既有 command argument、default value、bound、validation message、exception type、
exit-code mapping、JSON/plain output、artifact byte limit、redaction、session visibility、storage
delegation、recovery semantics 和 application cleanup。特别是 compact 与 retry 继续保持既有的
`config_for_session_resume`、binding creation、runner invocation 和 shielded close order。Canonical
direct call 与既有 top-level dispatch 必须产生相同的 observable result。

本切片不新增 capability gate。Session、provider、workspace、sandbox、permission、background-task
或 persistence authority 不移入 interface module。

## 明确的非目标

本 ADR 不提取 parser、`build_parser`、top-level `run`、agent、provider、ACP、TUI、export/import、
subagent、provider-selection 或 bootstrap facade。不重设计 session API、serializer、session storage、
recovery/compaction service、cancellation、retry、background task、permission、workspace/sandbox
policy、orchestration 或 UI behavior。

## 状态与验证

本 ADR 对本有界 execution-boundary slice 标记为 Accepted。该 acceptance 只覆盖 canonical module、
其 identity-preserving compatibility alias、既有 session behavior 及配套 architecture/docs test；
不声称 `neuro_code.cli` 剩余职责已经完成 consolidation。

本切片的最终本地证据为：CLI/architecture focused test 通过 163 个（另有 2 个 subtest），完整测试通过
2602 个，跳过 50 个，deselected 17 个；完整 coverage 为 85.29%。`uv lock --check`、documentation
parity（163 对 English/Chinese 文件）、Ruff lint、Ruff format、mypy、`uv build` 和
`git diff --check` 全部通过。跳过项是仓库已有的平台或权限专属测试，并由仓库既有 gate 控制；这些证据
不声称 Linux 上已经完成原生 Windows/macOS acceptance。
