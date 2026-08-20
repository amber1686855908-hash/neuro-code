# Coding Agent Benchmark（P4）

## 目的

P4 是 development/evaluation boundary，用于测量 Coding Agent 的可观察结
果，不改变 Neuro Code 的 Provider、Web、AgentLoop、ToolExecutor、Context、
Sandbox 或 Session 行为。Harness 通过 `ApplicationComposition.open`、
`create_binding` 和 `ConversationRunner.run` 调用生产 headless 路径；基准
路径不使用 TUI 自动化。

本基准不声称能够测量隐藏的推理质量。它记录可观察的执行结果、工具/事件
轨迹、资源用量、验证证据以及有界的失败分类。

## Corpus

冻结 corpus 版本为 `p4.0.0`，共 40 个任务：8 个类别各 5 个任务。

| 类别 | 任务数 |
| --- | ---: |
| A Repository Navigation | 5 |
| B Localized Editing | 5 |
| C Multi-file Change | 5 |
| D Bug Diagnosis | 5 |
| E Test-driven Repair | 5 |
| F Refactor/API Migration | 5 |
| G Long-running/Tool-control | 5 |
| H External-information | 5 |

前 35 个任务属于 CORE，最后 5 个任务属于 WEB。WEB 任务使用离线冻结的
参考镜像，并记录外部依赖 URL 与参考内容 hash；corpus 校验和 smoke 不需
要网络调用。每个任务都会物化为独立 Python 仓库，包含 500 行以上的 catalog
fixture、公开测试面以及位于 Agent workspace 之外的隐藏确定性 verifier。

任何任务 seed、prompt 或公开测试的修改都必须提升 corpus 版本，并改变记
录的 corpus SHA-256。

## Runtime 与隔离

每次 attempt 都使用全新的临时 workspace、临时 HOME 和 state 目录，并使用
移除环境中现有 secret 变量的受控环境，同时建立 git seed commit。Verifier
目录与 workspace 是兄弟目录，且不会传给 Agent。默认 development runtime
profile 为 `sandbox_profile=workspace`；该配置会被明确记录，而且所有进程创建仍
通过 Neuro Code 的 local-process sandbox port。运行时可以选择更严格的 profile。

固定 manifest 记录 sandbox profile、interaction mode、reasoning effort、
model-step 与 tool budget、context budget、timeout、后台任务上限、Web
Search/Fetch 模式、provider/model/protocol、endpoint identity hash 以及
Neuro Code commit。凭据不会写入 manifest、trace、diff 或 verifier 输出。

Verifier 是确定性的：文件/AST 检查与 `pytest` 断言是主要依据，不使用 LLM
作为裁判。结果只有 `PASS`、`FAIL` 或 `HARNESS_ERROR`。

## Metrics 与 taxonomy

Harness 记录 wall time、model steps、Provider 在可用时报告的 input/output
tokens、可用时的 cache 字段、按工具统计的调用与失败、compaction、failover、
permission 事件、后台事件、Agent 是否主动调用验证命令以及 stop reason。它
不会从这些计数推断推理质量。

主 taxonomy 严格限定为：

`MODEL_REASONING`、`REPOSITORY_NAVIGATION`、`TOOL_SELECTION`、
`TOOL_EXECUTION`、`EDIT_FAILURE`、`VERIFICATION_FAILURE`、`CONTEXT_LOSS`、
`PLANNING_FAILURE`、`PERMISSION_OR_SANDBOX`、`BACKGROUND_PROCESS`、
`WEB_RESEARCH`、`PROVIDER_PROTOCOL`、`PROVIDER_TRANSIENT`、
`BUDGET_OR_TIMEOUT`、`FINALIZATION`、`HARNESS_ERROR`、`UNKNOWN`。

分类依据 event/tool trace、verifier 输出、最终 diff 与 runtime 信号，可由
人工复核，不交给另一个 LLM。`NEW_TOOL_CANDIDATE` 不会由单次失败触发，必
须在独立任务中重复出现，并有证据证明现有工具不足或成本异常高。

## CLI 与 artifacts

Evaluation CLI 有意位于生产 import 之外：

```bash
uv run python -m scripts.benchmark validate --json
uv run python -m scripts.benchmark run --smoke --output benchmark-results
uv run python -m scripts.benchmark run --all --output benchmark-results
uv run python -m scripts.benchmark estimate --all
```

`validate` 不调用模型。`run` 默认使用确定性的 fixture Provider，只有传入
`--live` 才使用路由后的真实 Provider。Live run 必须同时有 `--allow-paid` 与
显式配置的 credential；`--all --live` 会在开始前打印有界估算。Verifier 失败
后不会自动重试 live run；`--rerun-failures` 最多为首次失败任务再创建两次全新
attempt。

每次 run 写入：

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

额外的新鲜 rerun 保存在任务目录下的 `attempt-1` 与 `attempt-2`。

Benchmark results 默认被仓库忽略，必须保持脱敏且有界。Harness 不会推送结果
或源码变更。

## Acceptance 边界

只有在 architecture/isolation 测试、8-task smoke、40-task validation、完整
质量门禁以及至少一次明确授权的 live baseline 都通过后，P4 才可以进入 Agent
capability improvement。如果没有 credential 或明确的付费 opt-in，诚实状态是
`BASELINE_NOT_RUN`，不能伪造分数。Benchmark 发现的生产行为问题应标记为
`BENCHMARK_DISCOVERED_ISSUE`，在后续轮次修复，不应悄悄混入 P4。
