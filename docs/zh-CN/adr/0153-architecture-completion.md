# ADR 0153：第一版发布前完成架构迁移

- 状态：Accepted
- 日期：2026-09-02
- 范围：第一版发布前 modular monolith 与 ports-and-adapters 的最终收敛
- 依赖：ADR 0049，以及截至 ADR 0152 的 interface/session boundary ADR

## Context

仓库已经建立 `application`、`application/ports`、`domain`、`infrastructure`、
`interfaces`、`bootstrap` 和 `shared`，但若干大型 canonical implementation 仍位于
package 根目录。这使 ownership 不清晰：interface package 仍有一部分只是 wrapper，
configuration 将 value contract 与文件加载混在一起，bootstrap 也同时承担进程启动、
具体 service 选择、factory policy 和资源图装配。

这是第一版发布前的内部架构迁移。因此目标是在源码目录中表达清晰的 ownership，
同时保持现有 CLI、TUI、ACP、Runtime、Provider、权限、沙箱、会话、持久化和安全行为。

## Decision

package 根目录不再放置 production implementation module。源码根目录只包含
`__init__.py`、`__main__.py` 以及 `application`、`bootstrap`、`domain`、
`infrastructure`、`interfaces`、`shared` 这些架构 package。

入站适配器的 canonical owner 如下：

- `interfaces.cli.app` 负责 CLI parser、dispatch、展示和 exit-code handling；
  `interfaces.cli.sessions` 负责已解析的 sessions command boundary。
- `interfaces.tui.app` 只负责 Textual lifecycle、high-level wiring 和 app-owned state。
  `interfaces.tui.contracts`、`interaction` 与 `state` 负责 TUI contract 和本地 model；
  `widgets` 与 `screens` 负责可复用的视觉 surface；`controllers` 按 reason to change 负责 turns、
  commands、preferences、provider/session selection、plans/tasks、background、transcript、runtime
  以及 tool activity orchestration。既有 `commands`、`text`、`theme` 和 `tool_activity` 模块继续是
  各自的 canonical owner。
- `interfaces.acp.agent` 负责 public ACP protocol facade 与 high-level wiring。
  `interfaces.acp.negotiation`、`session_registry`、`session_lifecycle`、`mcp`、`extensions` 和
  `prompt` 分别负责连接协商、已发布 session state、session lifecycle、实时 MCP、私有 extension
  dispatch 以及 prompt/permission execution；既有 `content`、`updates`、`client_io`、`mcp_config`、
  `transport` 和 `session` 模块继续拥有各自 boundary 的 canonical implementation。

原根级 implementation `neuro_code.cli`、`neuro_code.tui`、`neuro_code.acp`、
`neuro_code.tui_commands`、`neuro_code.tui_text` 和 `neuro_code.tui_theme` 已删除。
不存在继续拥有实现权威的根级 compatibility wrapper。

Configuration 采用明确的拆分：

- `application.ports.configuration` 负责不可变的 `AppConfig`、`ProviderProfile` 值、
  校验和显式输入的 configuration policy；它不会读取进程环境、探测可选包或解析文件系统路径。
- `bootstrap.configuration` 负责 TOML、环境变量、CC Switch、legacy format、managed
  overlay、路径解析以及 stored credential 的加载。
- `infrastructure.providers.binding` 在创建 Provider 前负责具体环境凭据和可选 HTTP 能力的解析。
- `infrastructure.providers.managed_provider_settings` 负责具体 managed JSON reader。
- `application.ports.provider_dialects` 负责 application-facing contract 使用的 dialect inference。

Bootstrap 采用明确的 composition 拆分：

- `bootstrap.entrypoints` 是惰性的、很薄的进程 launcher。
- `bootstrap.cli` 负责具体 CLI/TUI service 选择。
- `bootstrap.acp` 负责 ACP workspace 与 MCP composition adapter。
- `bootstrap.factories` 负责默认 concrete factory 选择。
- `bootstrap.composition` 仍是共享 resource graph、生命周期顺序和失败清理的唯一 owner。

Architecture tests 强制执行源码树边界，并扫描 production import，禁止
`interfaces -> infrastructure/bootstrap`、`application -> infrastructure/interfaces/bootstrap`、
`domain -> application/infrastructure/interfaces/bootstrap` 和
`infrastructure -> interfaces/bootstrap`。现有窄的 entrypoint edge 与明确的 compatibility
export 都单独测试，不通过扩大 allowlist 隐藏违规。

## Consequences

现在无需依赖 `architecture.md` 解释哪个根级 module 才是权威实现，目录结构本身即可表达
modular monolith 架构。Interface import 不会装配具体 infrastructure，application port import
也不会加载 bootstrap configuration 或 provider。

本次重构保持行为不变：command grammar、输出与 exit code、TUI 展示与快捷键、ACP wire semantics、
Runtime 与 Provider 行为、permission 与 sandbox gate，以及 session persistence semantics 均不变。

TUI decomposition 有意让 `interfaces.tui.app` 只保留 lifecycle 与 wiring；controller mixin 按
reason to change 拆分，且不导入 app 模块。`infrastructure.persistence.sqlite_session` 仍是 SQLite
session-store/schema/transaction 的唯一 owner。两个边界都没有被复制或削弱。
