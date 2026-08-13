# ADR 0113：Windows 原生 restricted-token 沙箱架构

## 状态

作为 W1 foundation 接受。本 ADR 建立类型化 capability 和 restricted-token
原语；它不启用 Windows 文件系统/网络 profile，也不宣称完整的 Windows 沙箱。

## 背景

ADR 0112 记录了经典稳定 unpackaged AppContainer 路线为何不适合当前 stock
Git for Windows 工作流的 production adapter。其 evidence 仍保留为历史记录，不会
被静默 fallback 替代。

下一条 production 路线必须保留现有 child-scoped process boundary，同时明确表达每种
安全 authority。文件系统与网络 security authority 属于一个 contract；process lifecycle
ownership 则由独立且正交的 `LocalProcessLifecycleCapability` contract 负责。

## 决策

W1 在规范 local-process port 中增加平台无关的
`LocalProcessSecurityCapabilities` 模型，包含以下维度：

- `READ_ISOLATION`
- `WRITE_ISOLATION`
- `NETWORK_ISOLATION`

每个维度报告 `STRONG`、`LIMITED` 或 `UNSUPPORTED`。
`security_capability_satisfies()` 对三个轴显式比较：strong 可以满足 strong 或 limited，
limited 只能满足 limited，unsupported 只能满足 unsupported/无要求。当 provider 为
limited 或 unsupported 时，需要 strong read isolation 的 caller 必须在创建 OS child
之前失败关闭。

W1 建立 primitives 和 target architecture，但不宣称已完成 Windows 文件系统/网络
capability。启用的 Windows profile 仍然失败关闭，因此 W1 actual capability 是：

| 维度 | W1 actual provided |
| --- | --- |
| Read isolation | `UNSUPPORTED` |
| Write isolation | `UNSUPPORTED` |
| Network isolation | `UNSUPPORTED` |

独立 native-backend architecture target 是：

| 维度 | Target | 原因 |
| --- | --- | --- |
| Read isolation | `LIMITED` | developer-tool 兼容性仍是当前读取边界。 |
| Write isolation | `STRONG` target | 后续 setup layer 只授予明确的 restricted SID。 |
| Network isolation | `STRONG` target | strong enforcement 属于 W2 firewall policy。 |

Descendant ownership 不是这里的 security axis。现有 Job Object 和 ConPTY path
继续通过独立 lifecycle contract 提供 `STRONG_DESCENDANT_OWNERSHIP`。

W1 production token layer 提供：

- 已验证的内存内 synthetic `S-1-5-21-<u32>-<u32>-<u32>-<u32>` SID；
- 带 `WRITE_RESTRICTED`、`DISABLE_MAX_PRIVILEGE` 和 `LUA_TOKEN` flags 的类型化
  restricted-SID request；
- 通过可注入 API 延迟调用 Win32 `OpenProcessToken`、`CreateRestrictedToken`、
  `GetTokenInformation` 和 `CloseHandle`；
- 不暴露 token 内容的 restricted-token attestation；
- 失败关闭的 Win32 errors 和 source/created-handle 的确定性清理。

W1 不宣称已完成文件系统/网络 authority。它不 provisioning user、不持久化 identity、不使用 DPAPI、不修改 ACL、不配置
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

Capability contract 防止 target declaration 被当作 actual provider capability 使用，并让
security authority 与 lifecycle ownership 保持正交。Token foundation 可以独立于未来
文件系统 setup layer 使用，当前 Windows profile 行为仍然失败关闭。ADR 0112 继续作为
历史 AppContainer feasibility 记录。

## 参考

- [ADR 0112](0112-windows-appcontainer-sandbox-feasibility-decision.md)
- [跨平台 lifecycle capability contract](0110-cross-platform-lifecycle-capability-contract.md)
- [Building Codex for Windows](https://openai.com/index/building-codex-windows-sandbox/)
- [Codex Windows sandbox setup reference](https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/setup.rs)
