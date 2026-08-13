# ADR 0113：Windows 原生 restricted-token 沙箱架构

## 状态

作为 W1 foundation 和 W2 setup-authority 记录接受。本 ADR 建立类型化 capability、
restricted-token、installation setup 以及文件系统/防火墙 authority 原语；它不把这些
原语接入 runtime child creation，也不宣称完整的 Windows 沙箱。

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

## W2 setup authority

W2 实现 installation-time setup boundary，同时把 runtime child creation 留给 W3。该
authority 具有以下属性：

- Offline 和 Online 是 dedicated real local user：`NeuroSandboxOffline` 和
  `NeuroSandboxOnline`。两者的真实 account SID 在 setup 时解析并持久化，彼此不同，且
  与 installation-scoped synthetic restricting SID 分离。
- synthetic SID 只用于 W1 `WRITE_RESTRICTED` token membership 和仅写 ACL principal，
  不能作为 read principal 或 firewall identity；read 以及 primary-user write check
  由真实 account SID 承担。
- installation record 使用 schema version 2，并将实际 account password 放在 DPAPI
  machine-scoped 加密 payload 中。machine DPAPI 本身不是 user boundary，因此 credential
  file 另外设置只针对两个 sandbox user 的 exact NTFS deny ACE，同时保留 controller/setup
  access。
- 文件系统 setup 为两个真实 user 规划显式 read allow，在 writable roots 为两个真实 user
  规划 primary-user write allow、为 synthetic restricting SID 规划仅写 allow，在 read-only roots 规划显式 write deny，
  并为敏感路径规划两个真实 user 的 read deny。native reconciliation 使用
  `SetEntriesInAclW`，由 Windows 将 explicit deny canonicalize 到 allow 之前，同时保留
  无关 controller ACE 和 owner。重复 setup 幂等；漂移报告 `NEEDS_REPAIR`；cleanup 只删除
  exact managed tuple 和 installation 创建的 user。
- Offline 拥有一个按真实 Offline account SID 限定的 outbound block rule。Online 只删除该
  exact managed rule，不添加 global allow rule；Online user 和真实 controller user 都不会
  匹配 Offline rule。
- setup、repair 和 cleanup 是显式的管理员 boundary。普通 session 可以检查状态，并在
  后续 runtime 工作中继续运行，不需要持续管理员权限。
- 状态报告为 `READY`、`NEEDS_SETUP`、`NEEDS_REPAIR` 或 `UNSUPPORTED`。setup 成功不会
  改变 `WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES`：在 W3 接通 child boundary 之前，
  三个 runtime security axis 仍全部为 `UNSUPPORTED`。

W2 不启动 command runner、不创建 runtime child、不桥接 MCP、不改 Git/Python integration、
不重写 ConPTY 或 Job Object、不使用 AppContainer 或 WSL2，也不为 controller user 配置
firewall rule。它复用 W1 capability contract 和现有 Job/ConPTY lifecycle boundary，
也不会把 `LIMITED` read planning 重新解释为 runtime capability。

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
