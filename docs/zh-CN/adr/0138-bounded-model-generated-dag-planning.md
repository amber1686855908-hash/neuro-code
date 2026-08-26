# ADR 0138：有界 Model-Generated DAG Planning

- 状态：作为显式内部 P0 vertical slice 实现；最终评级等待 merge-ref CI
- 日期：2026-08-26
- 范围：一个显式 parent objective 到一个不可变有界 Task DAG
- 依赖：ADR 0134、ADR 0135、ADR 0136 与 ADR 0137

## 背景

静态 Task DAG 与 parallel-aware Leader 切片已经提供规范 graph validation、持久化
capacity、有界 wave 和隔离的 Writable worker；它们有意要求调用方提供 graph。本切片
在 graph publication 之前增加一个由 Provider 驱动的 planning step，只证明 model
generated planning，不是 replan 或 graph revision mechanism。

Planning boundary 不能把 execution authority 移入 model。Planner 存在于 worker lease、
Worktree、Checkpoint 或 child runtime 之前，因此不能把依赖 worker identity 的
Parent Context Relay 当作 planning input store。既有 Leader lifecycle 也不能直接复用
为 planning table，因为 Leader attempt 外键依赖已经发布的 `task_dags` row。

## 决策

保持以下 authority chain：

```text
显式 parent objective
        -> zero-tool Planner proposal
        -> TaskDagApplicationService validation/publication
        -> parallel-aware Leader 选择
        -> Writable worker 执行
```

Planner 只拥有不可变 proposal。TaskDagApplicationService 继续是 node、edge、dependency、
acyclic、prompt、parallelism 和 immutable graph validation/publication 的唯一 owner。
Leader 继续拥有 READY wave 选择。Writable 继续拥有 worker binding、capability intersection、
Worktree、Checkpoint、child session、tool 和 worker-scoped LSP。

### Zero-tool Planner binding

Composition 创建专用的持久化 planner session 和 one-step `ConversationBinding`。Local tool、
provider-hosted tool、filesystem、Bash、terminal、network、MCP、LSP、Worktree、Checkpoint、
worker 和 background capability 均不存在。Planner binding 不通过 public CLI、TUI 或 ACP
orchestration command 暴露。

### Planning input envelope

Request 包含调用方提供的一个 `planning_id` 和 objective。Parent identity 取自真实 parent
binding 的 runner session ID；调用方提供的 identity 不具有 authority。Planner 可以接收一个
独立的不可变 `PlanningContextEnvelope`，其中只有真实 USER 和可见 ASSISTANT 纯文本。它排除
system/tool role、synthetic item、hidden reasoning、tool call/result、media、任意 workspace
bytes 和带 authority 的结构。配置的敏感值在纳入前脱敏。

Envelope 保持 source order，并使用既有有界 context limit：最多 10 项、每项 4 KiB、投影内容
24 KiB、渲染内容 32 KiB。其 canonical JSON 与 SHA-256 fingerprint 是确定性的。Envelope 只是
evidence，不能授予 tool、root、sandbox policy、provider access、worker 或 filesystem authority。

### Strict proposal contract

Provider 必须返回一个严格 JSON object，顶层只允许以下字段：

```json
{
  "nodes": [
    {"id": "research", "prompt": "bounded task", "depends_on": []}
  ],
  "max_parallel": 1,
  "reason": "bounded decomposition"
}
```

每个 node 必须严格包含 `id`、`prompt` 和 `depends_on`。Node declaration order 是 canonical。
Dependency ID 必须唯一并按同一 declaration order 出现；parser 不会排序抹除 graph 语义。
Unknown dependency、self-dependency、cycle、duplicate node ID、edge overflow 和其他 graph-definition
规则在 strict parsing 后交给规范 Task DAG validator。冻结的 limit 保持不变：最多 8 个 node、
16 条 edge、每个 node 4 个 dependency、8 KiB node prompt，以及 1 到 4 的 `max_parallel`。
Proposal field 不能请求 capability、root、sandbox setting、provider、tool、retry、merge、shell
command、dynamic expression 或 worker behavior。Node prompt 是 data，不是 authority。

Canonical sorted-key JSON 让等价 JSON 拼写得到同一个 proposal fingerprint。Declaration order、
dependency、prompt 或 `max_parallel` 的语义差异仍得到不同 fingerprint。

### Durable identity 与 publication

Schema 25 增加两个专用 projection：

- `orchestration_planning_attempts` 保存精确 planning ID、真实 parent session、objective/context
  fingerprint、planner session/turn、owner 与 lease、lifecycle、预分配 intended DAG ID、model
  response、proposal fingerprint 和已发布 DAG ID。
- `orchestration_plan_proposals` 保存一个 insert-only 的精确 parsed proposal，并绑定 attempt、
  parent、intended DAG、objective/context fingerprint 和 canonical proposal JSON。

`ApplicationComposition.create_model_planning_service()` 每次创建 fresh service 时都会新建并
持久化 Planner session。Service 的 `planning_session_id` 是当前 recovery controller 的 identity，
有意不同于 attempt 上保存的历史 `planner_session_id` 和 `planner_turn_id`。因此，L1 崩溃后 fresh
controller 可以在 L2 下运行，而 committed attempt 的 L1/T1 provenance 不会仅因使用新 composition
恢复而被改写。

Lifecycle 为：

```text
CLAIMED -> PROVIDER_FENCED -> MODEL_COMMITTED -> PROPOSAL_PUBLISHED
         -> DAG_PUBLISHED -> COMPLETED
```

`STALE` 用于标记可观察到的 invalid model output；`INDETERMINATE` 用于未解决的 provider 或
storage boundary。Attempt 在 provider request 之前预分配 intended DAG ID，规范 Task DAG service
publication 时使用同一个 ID。Proposal publication 是 insert-only：精确重复幂等，冲突 proposal
或被篡改的 canonical record fail closed。

### Replay 与 crash semantics

沿用既有 observable-turn invariant：Provider output 或 turn evidence 一旦可观察，Planner 绝不
自动再次调用 Provider。Fresh controller 只能从 durable exact identity 继续。

接受的 recovery boundary 如下：

1. 在 provider output 可观察之前，如果没有 turn evidence，既有 liveness/fence policy 可以允许
   安全 takeover。
2. Model output 已提交后，recovery 解析并发布同一个 durable response，不 replay Provider。
3. Proposal 已持久化后，recovery 使用同一 proposal 与 intended DAG ID，不改变任何 definition field。
4. Task DAG 已插入后，insert-only exact identity 返回已有 graph；recovery 校验 definition/fingerprint
   并完成 planning attempt，不生成第二个 graph。如果 generic session turn 已在 Planner-specific
   model commit 缺失时记录 request/output evidence，recovery 仍保持 fail-closed，不 replay Provider。

Live 或未被证明死亡的 owner 不能被抢占。并发 controller 使用 durable owner/CAS check，因此同一个
exact planning identity 不能产生重复 provider call、不同 proposal、不同 intended DAG ID 或两个
Task DAG publication。Fresh spawned composition 覆盖 committed output、proposal publication、DAG
insertion 和 provider-turn-evidence crash window；独立进程 controller race 也证明 losing controller
不会改写 winner 的 provenance。

### 明确的非目标

本 ADR 不增加 retry、DAG revision、publication 后 mutation、replan、node resurrection、recursive
planning、automatic delegation、task-complexity routing、Swarm、Ultracode、distributed scheduling、
unlimited agents、merge/copy-back、rollback orchestration、cleanup orchestration、public CLI/TUI/ACP
orchestration API 或 user-visible workflow editor。不增加 worker capability，也不增加第二个 generic runtime。

## 验证

Focused tests 覆盖 strict parsing、unknown/malformed/duplicate 与 invalid graph、冻结 bounds、canonical
fingerprint、zero-tool composition、实际 parent identity、有界 context projection 与 redaction、insert-only
和 tamper 行为、exact intended DAG identity、并发 ownership、fresh composition 在各 durable publication
boundary 的 L1→L2 crash recovery、provider-turn evidence fail-closed recovery、provider no-replay 以及
schema 24→25 保留性。

Production-shaped acceptance 使用真实 `ApplicationComposition`、scripted Provider、真实 Planner、真实
TaskDagApplicationService、真实 parallel-aware Leader 和真实 Writable worker。它验证 A -> B/C -> D graph、
`max_parallel=2`、Planner 一次调用、Planner/Leader zero-tool、B/C overlap、D 顺序、不同 managed Worktree
和 dirty parent checkout 不变。独立的 fresh OS-process acceptance 证明 L1 != L2、保留历史 L1/T1 provenance、
复用精确 response/proposal/intended DAG，并在 output、proposal 与 DAG crash window 中保持 provider
invocation 恰好一次。Provider-turn-evidence crash 按 explicit recovery required/`INDETERMINATE` 分类，
绝不自动 retry。

下一个独立切片是 Bounded DAG Revision / Replan，本实现不包含该能力。
