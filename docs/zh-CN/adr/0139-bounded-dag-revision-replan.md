# ADR 0139：有界 DAG Revision / Replan

- 状态：作为显式内部 P0 vertical slice 实现；最终评级等待 exact-head CI
- 日期：2026-08-26
- 范围：将一个显式失败且静默的 Task DAG revision 一次为一个不可变 successor DAG
- 依赖：ADR 0134、ADR 0135、ADR 0136、ADR 0137 与 ADR 0138

## 背景

ADR 0138 发布一个不可变的 model-generated Task DAG。失败 DAG 可能需要一个新的有界 proposal，
但修改已发布 definition 或重放已完成 worker 会破坏既有 execution 与 recovery contract。因此
Replan 需要独立的 durable identity、evidence 与 publication boundary。

## 决策

保持以下 authority chain：

```text
显式 failed-DAG revision request
        -> 不可变脱敏 source evidence envelope
        -> zero-tool one-step replan Planner
        -> TaskDagApplicationService validation/publication
        -> 既有 Leader / Writable 执行
```

Source DAG 永不修改。Replan 产生一个新 DAG identity 的 `TaskDag`；规范 Task DAG service 仍是唯一的
graph validation 与 publication owner。Parent identity 取自真实 parent `ConversationBinding`，不取自
request 字段或 model text。
本切片只通过显式的内部 application service 暴露 replan；没有 failure transition、CLI、TUI 或 ACP
路径会隐式调用它。

Initial Planning 与 DAG Replan 是两个独立 capability。Source DAG 与 successor DAG 是两个独立的不可变
publication。Replan evidence 不是 predecessor-result relay，Replan Planner 也不是 Leader；successor 的
decision authority 仍由既有 Leader 拥有。

### Eligibility 与 depth

Request 必须显式指定一个 source DAG。Source 必须是 `FAILED`、quiescent，没有 `RUNNING` node、没有
未解决的 `INDETERMINATE` node，且全部 node 都是 terminal state。Successful、cancelled、active、
non-quiescent、foreign-parent、missing 或 tampered snapshot 都 fail closed。在 claim、provider fence
和 successor publication 时，都必须再次匹配 exact definition fingerprint、generation 与 state；source
publication 本身保持不可变。

`MAX_DAG_REPLAN_DEPTH` 为 `1`。只支持恰好一个 successor revision；recursive replan 与 automatic retry
不属于本 ADR。

### Replan evidence envelope

Application 从 source DAG 构造确定性、脱敏、不可变 envelope。它只包含 source DAG identity/fingerprint/
generation、canonical node ID 与 ordinal、dependencies、node state、有界 completed result projection、
typed 有界 failure summary 和安全的有界 metadata。脱敏发生在 fingerprint 与 publication 之前。Envelope
限制为每个 completed result 4 KiB、completed-result aggregate 16 KiB、failure/state 8 KiB、rendered
32 KiB。它不包含 raw transcript、tool argument/result、log、environment、secret、workspace bytes、
checkpoint data、diff、path 或 authority instruction。

### Zero-tool replan Planner

`ApplicationComposition.create_task_dag_replan_service()` 创建一个 fresh、持久化、one-step Planner
binding。该 binding 没有 local 或 provider-hosted tool，也没有 filesystem/Bash/terminal/network/MCP/
LSP/Worktree/Checkpoint/worker/background authority，并使用 `max_steps=1`。Model 只把 evidence 作为
data 接收，并返回既有 typed `ModelDagProposal` contract。Revision、source、successor、depth、identity
与 authority 字段全部由 application 拥有，model output 不能提供。

### Durable lifecycle 与 identity

Schema 26 增加 insert-only replan attempt/proposal projection：

- `orchestration_dag_replan_attempts` 绑定 revision、真实 parent、不可变 source snapshot、depth、evidence
  fingerprint/JSON、planner session/turn、owner lease、intended successor ID、lifecycle、model response、
  proposal fingerprint 与已发布 successor ID。
- `orchestration_dag_replan_proposals` 保存一个精确 parsed proposal、canonical JSON 及其 source/evidence/
  successor identity。

Lifecycle 为：

```text
CLAIMED -> PROVIDER_FENCED -> MODEL_COMMITTED -> PROPOSAL_PUBLISHED
         -> SUCCESSOR_DAG_PUBLISHED -> COMPLETED
```

`STALE` 记录可观察到的 invalid model output；`INDETERMINATE` 记录未解决的 provider 或 storage boundary。
同一个 canonical source/revision identity 允许 exact idempotent recovery；不同 evidence、proposal、source
或 successor 会被拒绝。不使用 blind upsert。Populated 25-to-26 migration 保留既有 data 与 foreign-key
`RESTRICT` 行为。

### Crash recovery、no replay 与 race

Model output 或 generic provider-turn evidence 一旦可观察，recovery 永不 replay Provider。Fresh composition
可以复用已提交的 model response、durable proposal 或已经插入且 identity 精确的 successor。若 provider-turn
evidence 已写入但 model commit 尚未完成，则转为 explicit recovery-required `INDETERMINATE`，不能伪造
proposal。

在 provider invocation 前和 successor publication 前重复进行 source snapshot fence。Durable owner/CAS claim
保证一个 exact source/revision identity 至多一次 provider call、一个 immutable proposal 和一个 successor。
因此独立 spawned controller 只有一个 winner；loser 不调用 Provider，也不修改 winner provenance。

## 非目标

本 ADR 不增加 automatic retry、recursive 或多层 replan、publication-time mutation、source DAG resurrection、
merge/copy-back、rollback、cleanup、public CLI/TUI/ACP orchestration command、Swarm、Ultracode、distributed
scheduling、live/paid Provider validation 或新的 execution runtime。它不改变 filter-preflight、CAS、ownership、
sandbox、hooks、fsmonitor、Worktree、Leader、Writable 或 LSP architecture。

## 验证

Focused 与 production-shaped tests 只使用 fixture Provider，覆盖 strict bounded evidence、source eligibility、
exact identity、zero-tool composition、schema migration、同进程 publication recovery、真实
`multiprocessing.get_context("spawn")` 在 model-commit、proposal-publication、successor-insert 与
provider-turn-evidence 边界的 recovery、one-winner two-process race、no provider replay，以及从
model-generated failure 经过 replan、parallel-aware Leader 到 Writable worker 的真实 composition path。端到端
路径验证 source DAG 与 dirty parent checkout 保持不变，并验证已完成的 source worker 不会重跑。
