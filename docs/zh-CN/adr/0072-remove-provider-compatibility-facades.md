# ADR 0072：架构冻结后移除 Provider 兼容 facade

- 状态：已接受
- 日期：2026-08-07
- 取代：ADR 0049 中关于保留 Provider facade 的决定

## 背景

Architecture Freeze v1 已将 `neuro_code.infrastructure.providers` 确立为模型
Provider、故障转移、Provider 工厂和图像引用辅助函数的唯一实现所有者。旧的
`neuro_code.providers` 包及其子模块不拥有状态、组合逻辑或副作用，只从
infrastructure 所有者重新导出对象。生产代码已经使用规范路径。

冻结后继续保留重复包会延长没有必要的导入表面，也可能让新的生产导入重新漂移到
已退役的边界。

## 决策

移除 `neuro_code.providers` 包及以下子模块 facade：

- `anthropic`
- `failover`
- `gemini`
- `image_references`
- `openai_compatible`
- `openai_responses`

所有生产代码、测试、live 测试、文档和 package-smoke 引用都改用
`neuro_code.infrastructure.providers` 或其具体子模块。规范 Provider 对象、请求
payload、流式事件、故障转移顺序、取消、脱敏和错误行为保持不变。

## 兼容边界

这是架构边界上的有意 breaking cleanup，而不是 Provider 行为变更。导入已移除路径必须
以 `ModuleNotFoundError` 失败；package-smoke 和架构 import-contract 测试会明确保持该
缺失状态。公开 Provider 配置和 CLI 行为保持不变。

本 ADR 不移除 `neuro_code.tools` 和 `neuro_code.adapters` 兼容族。它们需要独立的
消费者与外部兼容性审计。之后的消费者审计和删除由
[ADR 0074](0074-remove-adapter-tool-domain-facades.md) 记录；早期边界不再表示这些路径今天仍然保留。

## 影响

- 新代码只有一个 Provider 导入所有者。
- 软件包不再携带 Provider identity-preserving wrapper。
- 仍导入退役路径的下游代码必须迁移到规范 infrastructure 路径。
- Runtime、持久化、协议和安全语义没有变化。
