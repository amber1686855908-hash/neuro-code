# ADR 0017 — 安全的交互式 profile 选择

**简体中文** · [English](../../en/adr/0017-safe-interactive-profile-selection.md)

## 状态

已接受。

## 背景

Neuro Code 已经支持命名供应商 profile 和一次性 CLI 选择，但 TUI 启动后无法切换
profile。只替换现有会话中的供应商并不安全：持久项可能包含加密推理、供应商托管工具
状态、方言元数据、图片回放规则或其他只对会话原 profile 有效的上下文。

本交互切片需要提供实用选择器，同时不能暴露凭据、修改配置、在活动轮次中切换，也不能
假装所有供应商上下文都可以移植。

## 决策

- `ProfileConversationController` 是包装当前 `AgentConversation` 的应用边界。它让 `run`
  与 `select_profile` 共享同一把锁，因此模型或工具轮次中不能改变 profile。
- TUI 接收从脱敏配置派生的不可变 `ProviderOption`。界面只显示 profile 名称、模型、线路
  协议、默认/当前标记和就绪状态。不可用或缺少所引用凭据的 profile 会被禁用；端点和
  凭据值永远不会进入选择器。
- `Ctrl+P`、不带参数的 `/provider` 和 `/model` 打开选择器；`/provider PROFILE` 与
  `/model PROFILE` 直接选择已配置 profile。本切片中的 `/model` 只是 profile 选择别名，
  不接受任意远程模型 ID 或推理强度。强度是独立的应用策略，通过 `Ctrl+E`、`/effort`
  或 `/reasoning` 选择，详见
  [ADR 0027](0027-semantic-tui-and-application-reasoning-effort.md)。
- 重新选择当前 profile 不执行任何操作。选择不同 profile 时，组合根针对相同工作区和
  SQLite 存储创建新的供应商、运行时与 `AgentConversation`；工厂必须返回没有会话 ID 的
  会话。旧会话保持不变并可恢复；下一条提示会延迟创建新会话。
- 选择只在当前进程生效，不修改 TOML、环境变量、CC Switch 数据或已配置默认项。所选
  profile 的已配置备用链继续生效，下一条提示开始后，普通供应商选择事件仍可能报告已
  切换到备用项。
- 只有供应商构造成功后才替换活动绑定；失败时保留现有 profile 和会话。

## 后果

用户无需重启 TUI 即可在明确配置的供应商之间切换，同时供应商亲和状态绝不会跨越切换
边界。其安全代价是新 profile 不延续当前对话；旧对话仍保存在 SQLite 中，可稍后恢复。

profile 清单是启动时快照。配置热重载、远程模型目录、兼容上下文迁移和持久默认项编辑
仍属于后续纵向切片。应用自有强度选择独立于 profile 清单，并会重新应用到新绑定；
供应商原生强度映射仍未实现。

## 验证

Neuro Code 通过自身的目录、传输和 TUI 行为测试验证 profile 选择与新会话边界。
