# ADR 0114：Windows 原生非 PTY 沙箱运行时

- 状态：W3 实现正在进行 focused 原生验收
- 日期：2026-08-14
- 范围：Windows 启用 profile 下的 BASH、后台 Bash 与 MCP stdio

## 决策

Windows runtime 将 controller 保持在沙箱边界之外。每个非 PTY child 先由
`CreateProcessWithLogonW` 使用 W2 选定的真实账户启动可信、独立于 workspace
的 runner。runner 打开自身 process token，通过 W1 的
`CreateRestrictedToken(WRITE_RESTRICTED)` 应用持久化 synthetic write SID，
再以 `CreateProcessAsUserW` 在 kill-on-close Job Object 内创建最终 child。

最终 token 的 restricting SID set 只包含 installation synthetic write SID；Everyone、
logon、sandbox-user 与 controller SID 只作为 object ACL principal。
`DISABLE_MAX_PRIVILEGE` 必须保留 `SeChangeNotifyPrivilege`；W3 会检查该事实，绝不
通过 `AdjustTokenPrivileges` 重新授予。

controller 与 runner 通过随机、由 controller 创建的 named pipe 通信。named
pipe 使用只允许 controller 与选定 sandbox 用户的精确 DACL，并使用带版本和长度
前缀的二进制 frame。stdout 与 stderr 保持分离；协议 payload 不按文本解码，runner
诊断不会进入 MCP stdout。

`ISOLATED` 选择持久化 Offline identity，`INHERIT` 选择 Online identity。runtime
不会修改 Firewall，也不会执行 setup、repair 或 UAC 提权。setup inspect 不是
`READY` 时，在创建 child 之前失败关闭。

完整接通的 W3 runtime 会声明 read `LIMITED`、write `STRONG`、network `STRONG`
的 candidate provider contract，使特权原生验收能够执行真实边界。该声明由 native
acceptance 与 required PR gate 认证，而不是依赖 CI 环境的 runtime bypass。W1/W2
foundation actual-capability declaration 仍为 `UNSUPPORTED`。`STRICT` 要求 strong
read isolation，因此失败关闭。交互式 PTY/ConPTY 留给 W4。

## 后果

- W2 仍是账户、DPAPI state、ACL 与持久化 Offline Firewall rule 的唯一 authority。
- 不重复建立 lifecycle source of truth：runner 持有的 Job 是最终 child 及其后代的
  生命周期 authority。
- 最终 child 使用显式环境，并由选定 sandbox account 推导 private profile 与临时路径；
  controller 凭据和 DPAPI 明文不会传入 child。
- Native acceptance 必须从最终 restricted child 证明 identity、exact restricted SID、
  保留的 traversal privilege、授权 write 与仅有 broad primary-user write 的对抗行为、
  ACL、network、lifecycle、二进制 stdio 与协议行为。
- 原生验收失败时阻断 W3 production admission，而不是在 runtime 改变 capability
  语义。
