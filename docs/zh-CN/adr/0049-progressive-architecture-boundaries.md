# ADR 0049：渐进式模块化单体架构边界

**简体中文** · [English](../../en/adr/0049-progressive-architecture-boundaries.md)

- 状态：已接受
- 日期：2026-07-22
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

Neuro Code 已经通过领域值、带类型端口、应用编排和具体适配器交付纵向能力，但当前包结构
还没有一致地表达这些职责：`application.py` 以组合根身份选择具体适配器，部分应用运行时
模块直接导入工具和平台实现，CLI 与 ACP 界面也会直接构造或访问基础设施。

一次性重排所有包会把导入变更与行为修改混在一起，使会话、权限、沙箱、凭据、ACP 和
进程所有权回归难以隔离。因此，在移动实现之前，需要先确定目标依赖模型并建立可执行的
现状基线。

## 决策

Neuro Code 继续使用单一发行包和单一导入包，采用模块化单体与 Ports and Adapters。目标
职责如下：

- `domain`：纯领域值、不变量和规则；
- `application`：代理轮次、对话、权限、会话和流程编排；
- `application/ports`：应用行为所依赖的抽象；
- `infrastructure`：模型供应商、SQLite、文件系统、进程、PTY、沙箱、工具、MCP、HTTP
  和设置实现；
- `interfaces`：CLI、TUI、ACP 和其他入站适配器；
- `bootstrap`：配置加载、工厂、生命周期所有权和依赖装配；
- `shared`：错误、有界异步辅助、脱敏以及类似的小型跨层原语。

允许的依赖方向为：

```text
interfaces ------> application ------> domain
                         |
                         +-----------> application/ports <------- infrastructure

bootstrap ------> interfaces + application + infrastructure
domain + application + infrastructure + interfaces ------> shared
```

具体规则如下：

- `domain` 只能依赖标准库、`domain` 和 `shared`；
- `application` 可以依赖 `domain`、`application/ports` 和 `shared`；
- `infrastructure` 可以依赖 `domain`、`application/ports`、`shared` 和其他基础设施内部
  模块，但不能依赖 interfaces 或 bootstrap；
- `interfaces` 可以依赖面向应用的契约、领域值和 shared 辅助，但不能构造具体基础设施；
- `bootstrap` 是唯一允许同时依赖 `interfaces`、`application` 和 `infrastructure` 的层；
- `shared` 不得成为另一套组合根或无边界的依赖杂物箱。

application 和 domain 模块不得导入具体 infrastructure 实现。所有副作用继续通过带类型
端口以及现有权限、工作区、沙箱和平台边界。

配置加载属于 bootstrap，但被多个层引用的配置值对象不得定义在 bootstrap 中，否则这些
层会被迫依赖组合根。其最终归属在专门的配置拆分阶段确定。在此之前，
`neuro_code.config` 是明确的过渡边界，不会被过早归入 bootstrap。

架构迁移采用渐进策略：

1. 增加 canonical 新模块路径；
2. 保留旧路径，并从旧路径兼容 re-export 同一对象；
3. 切换内部导入并验证行为；
4. 只有在后续独立、明确批准且版本化的变更中才能删除旧兼容路径。

文件移动不得与行为修改发生在同一迁移阶段。移动代码的阶段只改变导入和装配；行为修改
必须作为独立纵向切片并带有自己的测试。

阶段 0 使用 Python 标准库 AST 增加依赖测试。当前每一条已知禁止直接导入都记录来源模块、
目标模块和原因。活动 allowlist 必须与源码中的实际违规完全一致，并且只能是冻结初始集合
的子集。消除违规时必须同时删除活动 allowlist 项；新增违规会让测试失败。修改冻结基线
属于架构决策，不是日常 allowlist 维护。

阶段 0 不移动实现，也不改变 CLI 参数、输出、退出码、运行时事件、配置优先级、数据库或
会话格式、ACP 行为、权限、沙箱或安全语义。

### 实施状态——2026-07-28

1. 应用运行时行为已 canonical 化到明确的 `neuro_code.application.runtime` 子模块。
2. 开发阶段的 breaking cleanup 已移除 `neuro_code.runtime`；运行时应用行为仅可通过
   明确的 canonical 子模块获得，且 `neuro_code.application.runtime.__init__` 保持最小。
3. 开发阶段的 breaking cleanup 已移除 `neuro_code.ports`；端口契约仅可通过
   `neuro_code.application.ports.*` 获得。
4. 开发阶段的 breaking cleanup 已移除根级 shared compatibility 模块
   `neuro_code.errors`、`neuro_code.async_utils` 和 `neuro_code.redaction`；其原语仅可通过
   对应的 `neuro_code.shared.*` 模块获得。
5. 开发阶段的 breaking cleanup 已移除 `neuro_code.application` 的包级 composition facade；
   其 `ApplicationSettings` 包级导出仍保留，composition 仅可从
   `neuro_code.bootstrap.composition` 获得。
6. 开发阶段的 breaking cleanup 已移除 `neuro_code.cli.main`。console scripts 和
   `python -m neuro_code` 继续使用 `neuro_code.bootstrap.entrypoints:main`，注入式
   `neuro_code.cli.run` 则保持为 CLI 核心。
7. managed-provider JSON reader 已拆分到
   `neuro_code.configuration.managed_provider_settings`。
8. `neuro_code.config` 不再导入 provider-settings adapter。
9. 开发阶段的 breaking cleanup 已移除 adapter 和 config namespace 中的 managed-provider loader
   re-export，并移除 `neuro_code.config.ProviderConfig`；此边界的公开 API 为 canonical
   reader、`JsonProviderSettingsStore`、`ProviderProfile` 和 `AppConfig`。
10. active temporary dependency allowlist 现已为空。
11. Stage 0 frozen baseline 仍是历史上限记录，未被重写。
12. 通用 dynamic-import architecture guard 现已扫描生产源码。开发阶段的 breaking cleanup 已移除
   ACP composition facade：`serve_acp` 只接受 `AcpApplicationService`。唯一剩余的 Bootstrap
   窄边是 canonical `neuro_code.__main__` package-executable entrypoint；它不属于待清除的兼容债务。
13. 通用 Responses 适配器只在
   `neuro_code.providers.openai_responses.OpenAIResponsesProvider` 中实现。xAI 仍是由
   `ProviderProfile` 选择的 `openai-responses` 方言；开发阶段的 breaking cleanup 已移除
   `neuro_code.providers.xai_responses` 和 `XAIResponsesProvider`。
14. 开发阶段的 breaking cleanup 已移除 `neuro_code.permissions` 中根级的审批契约
    re-export。请求和响应契约现在只可从
    `neuro_code.application.permissions.contracts` 获得，根级模块保留同步权限策略。
15. 其他 compatibility path 的删除仍是独立、版本化的决策。

## 影响

- 在目录迁移开始前，目标依赖方向已经可以执行验证。
- 现有债务保持可见，并且可以逐条直接导入地减少。
- 兼容模块在迁移期间保持导入对象身份，代价是暂时增加模块和测试。
- bootstrap 可以包含配置加载器和工厂，但不能拥有跨层共享的配置契约。
- 本 ADR 不决定兼容 re-export 的删除时间；删除需要后续 ADR 或等价的版本化兼容决策。

## 被否决的方案

- 一次性把所有包移动到目标结构：这会掩盖行为回归，且难以安全回滚。
- 静默允许现有顶层包之间的全部导入：这会让迁移开始前的架构债务继续增长。
- 把所有配置类型放入 bootstrap：这会使 application 和 infrastructure 消费者产生反向
  依赖。
