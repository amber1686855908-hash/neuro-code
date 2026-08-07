# ADR 0074：删除 Adapter、Tool 和 Domain 平面兼容 Facade

- 状态：Accepted
- 日期：2026-08-07
- Supersedes：ADR 0049、ADR 0072 和 ADR 0073 中保留 adapter/tool/domain facade 的决定

## 背景

Architecture Freeze v1 已在 `neuro_code.infrastructure`、
`neuro_code.application` 和嵌套 domain package 下建立 canonical owner。
仓库级消费者审计确认，剩余的 `neuro_code.adapters.*`、
`neuro_code.tools.*` 以及直接位于 `neuro_code.domain` 下的平面模块，都是不拥有状态、
组合逻辑或副作用的 identity-preserving re-export。生产代码已经迁移到 canonical owner。

继续保留这些路径会维护第二套 import surface，并允许新代码漂移回已废弃边界。
本决定是有意的 breaking cleanup；它不改变 runtime、provider、tool、权限、持久化或协议行为。

## 决策

删除完整的 `neuro_code.adapters` 和 `neuro_code.tools` facade 族，并删除直接位于
`neuro_code.domain` 下的平面 facade 模块，包括原 conversation、model-event、
provider-settings、instruction、skill、sandbox、terminal 和 background-task aggregate。

Canonical consumer 使用以下 owner：

- tool：`neuro_code.infrastructure.tools.*`；
- adapter：`neuro_code.infrastructure.*` 或对应的 `neuro_code.application.ports.*` 契约；
- domain value：`neuro_code.domain.conversation.*`、`neuro_code.domain.execution.*`、
  `neuro_code.domain.workspace.*`、`neuro_code.domain.sandbox.models`、
  `neuro_code.domain.terminal.models` 和 `neuro_code.domain.background_tasks.models` 等嵌套 canonical package。

`neuro_code.domain` 包初始化文件仍作为 canonical public value 的 aggregate 保留。
嵌套 canonical package 的初始化文件也保留；它们是实现，不是兼容 facade。

## 兼容边界

导入已删除路径必须失败并抛出 `ModuleNotFoundError`（若父 package 不存在，则允许等价的父模块缺失错误）。
architecture import-contract 测试会断言 facade 文件不存在、生产代码没有旧路径导入，并确认 canonical 嵌套 package 仍可用。
外部调用方必须迁移到 canonical path。

## 影响

- tool、infrastructure adapter 和 domain 平面值对象各自只有一个实现 owner；
- 不改变 runtime 行为、事件顺序、持久化 schema、安全边界或 provider 请求行为；
- 这是明确的 import 兼容性破坏，不是 temporary allowlist；
- 后续架构工作只有在用户能力需要新边界时才能增加模块，不得重新制造这些 facade。

## 验证

本清理以通过 architecture import contract、相关 provider/tool/domain/session 测试、Ruff、format、mypy、文档 parity 和 `git diff --check` 为验收条件。
