# ADR 0154：普通 Agent 验证获取边界

- 状态：Accepted
- 日期：2026-09-07
- 范围：VF-3c 普通 Agent generic verification requirement 与可信 coverage acquisition
- 依赖：ADR 0153、VF-1 verification freshness、VF-2 final-response truth boundary、VF-3a 与 VF-3b

## Context

VF-3a 定义了不可变、与 Provider 无关的 verification requirements，VF-3b 将精确 snapshot 传播到普通 Agent 的回合与恢复边界。
普通 Agent 仍需要一种小而确定的方式，让发生工作区修改的回合具备 verification awareness，但不能推断任务专属验收标准，
也不能发现测试 framework。

必须严格区分 verification command classification 与 requirement coverage。现有 `bash:test` 与 `bash:static_check` 只说明已经
执行了哪一类已识别命令；它们不证明该命令覆盖任意用户描述的目标。自由文本 summary、命令文本、模型 ID 和 NLP 都不是确定性的
coverage fact。

## Decision

`neuro_code.application.sessions.requirements` 中的 `NormalTurnRequirementsPolicy` 是第一版新普通用户回合 default 的唯一 producer。
在 UltraCode 路由之后、TurnInput 持久化、第一次 Provider 请求或工具执行之前，如果普通用户回合没有显式 snapshot，则准确获得一个
不可变的 Required requirement：

> After a workspace mutation, a recognized verification command must produce a current result.

它的 activation 为 `ON_WORKSPACE_MUTATION`，provenance 为有界的 workspace-mutation source；规范 domain identity 根据规范化 criterion、
空 descriptive scope 和 activation 生成。该策略不检查 prompt 或文件系统，也不会选择或执行测试运行器。

显式非空 snapshot 与显式空 snapshot 原样传递。缺少 structured field 的旧 TurnInput 行在恢复时继续保持 legacy；default 只适用于新的普通
逻辑回合。后台任务、子代理、planner/replan runtime、UltraCode parent/worker/result adoption 以及 external-result path 不会获得该 default。

`neuro_code.application.runtime.verification` 中的 `resolve_verification_coverage` 是本切片唯一可信 linkage resolver。它复用
`verification_scope_for_tool`，只有当 effective snapshot 含有 exact generic requirement ID，且已识别工具为 `bash:test` 或
`bash:static_check` 时才可以发出该 ID。现有 classifier scope 仍是有界描述性 metadata。不会通过命令文本匹配、summary 解析、模型提供的 ID、
NLP 或通用 scope algebra 建立 coverage。

Effective snapshot 沿用现有 ToolExecutor observation path。先观察 mutation，再观察 verification，因此 evidence 会被当前 workspace generation
标记。已识别命令的失败在 requirement active 后仍是 FAILED evidence，会评估为 `FAILED`，不会变成 `NO_EVIDENCE`。本切片只有无歧义的被拒绝
`MODE` permission decision 才可以产生类型化的 `POLICY_RESTRICTION` blocker。`EXPLICIT_RULE` denial 暂不分类，因为该 source 目前同时涵盖
显式 deny rule、headless ASK-to-deny 以及其他 restrictive Bash conversion。交互式拒绝、审批 UI 不可用、环境失败和其他 blocker producer 会等到
存在可靠 typed fact source 后再实现；不会从 reason string 或其他自由文本推断 blocker。

现有 `VerificationTracker` 仍是唯一可变 verification truth owner。每 requirement 最新 fact 与全局 workspace-generation freshness 仍是权威事实，
有界 diagnostic evidence ring 只负责 projection。generic finalizer 文案保持保守，只能写：

> A recognized verification check passed after the workspace changes.

不得声称所有测试、所有行为或整个任务都已验证。

## Consequences

普通回合在 default declaration 存在时仍保持现有 legacy streaming behavior；只有发生 workspace mutation 后才激活 requirement。只读与对话回合不会
仅因声明存在就调用 finalizer 或启用 gate。工作区修改后的已识别命令与 default requirement 具有精确的 typed 关系，但任意目标 coverage 有意不做声明。

本变更不增加 database schema、UI 或 Provider contract、测试发现、framework/package-manager detection、专用 TestRunner、requirement inference 或
UltraCode verification integration。VF-2 仍是 final-response boundary，`VerificationTracker` 仍是唯一 runtime truth owner。
