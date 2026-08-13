# ADR 0113：Windows 原生 restricted-token 沙箱架构

## 状态

作为 W1 foundation 接受。本 ADR 建立类型化 capability 和 restricted-token
原语；它不启用 Windows 文件系统/网络 profile，也不宣称完整的 Windows 沙箱。

## 背景

ADR 0112 记录了经典稳定 unpackaged AppContainer 路线为何不适合当前 stock
Git for Windows 工作流的 production adapter。其 evidence 仍保留为历史记录，不会
被静默 fallback 替代。

下一条 production 路线必须保留现有 child-scoped process boundary，同时明确表达每种
安全 authority。文件系统读取兼容性、写入权限、网络策略和后代所有权是相互独立的
维度，不能用一个平台标签代替。

## 决策

W1 在规范 local-process port 中增加平台无关的
`LocalProcessSecurityCapabilities` 模型，包含以下维度：

- `READ_ISOLATION`
- `WRITE_ISOLATION`
- `NETWORK_ISOLATION`
- `DESCENDANT_OWNERSHIP`

每个维度报告 `STRONG`、`LIMITED`、`BEST_EFFORT` 或 `UNSUPPORTED`。
`security_capability_satisfies()` 对每个轴显式比较。limited read provider 永远不能
满足 strong-read requirement；需要 strong read isolation 的 caller 必须在创建 OS
child 之前失败关闭。

W1 Windows 原生目标刻意保持狭窄：

| 维度 | W1 目标 | 原因 |
| --- | --- | --- |
| Read isolation | `LIMITED` | developer-tool 兼容性仍是当前读取边界。 |
| Write isolation | `STRONG` target | 后续 setup layer 只授予明确的 restricted SID。 |
| Network isolation | `UNSUPPORTED` | strong enforcement 属于 W2 firewall policy。 |
| Descendant ownership | `STRONG` | 复用现有 Job Object 生命周期。 |

W1 production token layer 提供：

- 已验证的内存内 synthetic `S-1-5-21-<u32>-<u32>-<u32>-<u32>` SID；
- 带 `WRITE_RESTRICTED`、`DISABLE_MAX_PRIVILEGE` 和 `LUA_TOKEN` flags 的类型化
  restricted-SID request；
- 通过可注入 API 延迟调用 Win32 `OpenProcessToken`、`CreateRestrictedToken`、
  `GetTokenInformation` 和 `CloseHandle`；
- 不暴露 token 内容的 restricted-token attestation；
- 失败关闭的 Win32 errors 和 source/created-handle 的确定性清理。

W1 不 provisioning user、不持久化 identity、不使用 DPAPI、不修改 ACL、不配置
firewall、不启动 command-runner binary，也不增加 Git/Python/MCP broker。不重写 Job
Object 或 ConPTY 代码。现有 Linux Bubblewrap、macOS Seatbelt 和 Windows Job
Object/ConPTY guarantee 保持不变。在后续完整 authority composition 接入前，Windows
启用 profile 仍然不支持并失败关闭。

## W2 边界

W2 可以在独立 production evidence 和 CI 基础上增加 write ACL composition 与 strong
network enforcement 所需的 setup authority。它必须复用 W1 capability contract 和
现有 Job/ConPTY process boundary，不能把 `LIMITED` read isolation 重新解释为 strong，
也不能用未沙箱化 broker 绕过 child authority 缺失。

## 后果

Capability contract 让 Windows 的部分 authority 可见，同时不弱化其他平台。Token
foundation 可以独立于未来文件系统 setup layer 使用，当前 Windows profile 行为仍然
失败关闭。ADR 0112 继续作为历史 AppContainer feasibility 记录。

## 参考

- [ADR 0112](0112-windows-appcontainer-sandbox-feasibility-decision.md)
- [跨平台 lifecycle capability contract](0110-cross-platform-lifecycle-capability-contract.md)
- [Building Codex for Windows](https://openai.com/index/building-codex-windows-sandbox/)
- [Codex Windows sandbox setup reference](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/setup.rs)
