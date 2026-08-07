# ADR 0073：移除过时的根级兼容门面

- 状态：已接受
- 日期：2026-08-07
- 取代：ADR 0049 中关于这五个模块继续保留根级 facade 的决定

## 背景

配置、权限策略、Bash 命令分析、工作区身份和工作区变更观察的 canonical
owner 已在架构迁移中稳定。以下根级模块只包含保持对象身份的 re-export，且
已经没有生产消费者：

- `neuro_code.bash_commands`
- `neuro_code.config`
- `neuro_code.permissions`
- `neuro_code.workspace`
- `neuro_code.workspace_changes`

在所有内部消费者完成迁移后继续保留这些模块，只会延续兼容债务，不再增加
有效的应用边界。本 ADR 是针对这五条路径的版本化 breaking cleanup 决定。

## 决定

移除这五个根级模块，调用方必须使用以下 canonical owner：

| 移除路径 | Canonical owner |
| --- | --- |
| `neuro_code.bash_commands` | `neuro_code.domain.permissions.bash_commands` |
| `neuro_code.config` | `neuro_code.configuration.app` |
| `neuro_code.permissions` | `neuro_code.application.permissions.policy` |
| `neuro_code.workspace` | `neuro_code.infrastructure.workspace.paths` |
| `neuro_code.workspace_changes` | `neuro_code.infrastructure.workspace.changes` |

所有生产代码和普通测试消费者都使用 canonical import。架构测试会断言这些
模块无法被发现，并确认导入 canonical 模块不会加载它们。历史上的
`Path.home` patch seam 改为直接 patch `neuro_code.configuration.app.Path.home`。

在本 ADR 被接受时，它不移除：

- `neuro_code.adapters.*` facade；
- `neuro_code.tools.*` facade；
- `neuro_code.domain.messages` 等会话/领域平面 facade；
- canonical domain 包或聚合包导出。

这些路径需要独立的兼容性决定。之后的消费者审计和删除记录于
[ADR 0074](0074-remove-adapter-tool-domain-facades.md)；阅读本历史 ADR 时不应据此认为这些路径今天仍然保留。

## 影响

- 五条被移除的导入路径构成有意的 breaking change。
- Runtime、Provider、SessionStore、权限、工作区和工具行为不变；变化仅限于
  import 位置。
- package smoke 和架构契约可以在任一被移除模块重新出现时 fail-closed。
- 依赖旧路径的外部调用方必须迁移到 canonical owner。

## 验证

只有在架构 import contract、配置/Provider settings、受影响的 application 和
TUI 测试，以及 Ruff、格式、mypy、文档 parity 和 `git diff --check` 全部通过后，
才接受本次移除。
