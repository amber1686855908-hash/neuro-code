# ADR 0114：Windows 原生非 PTY 沙箱运行时

- 状态：已接受；W3 已在 focused 原生验收与 full CI 通过后合并
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

controller 与 runner 通过两个随机、由 controller 创建的单向同步 named pipe
通信：controller 写入、runner 读取的 control pipe，以及 runner 写入、controller
读取的 event pipe。每个 pipe 都使用只允许 controller 与选定 sandbox 用户的精确
DACL，并使用排除 `FILE_CREATE_PIPE_INSTANCE` 的 specific client rights，以及带
版本和长度前缀的二进制 frame。stdout 与 stderr 保持分离；协议 payload 不按文本
解码，runner 诊断不会进入 MCP stdout。runner 使用 Python `-I` 和显式环境启动，
但这两项本身不能证明 provenance：在 `CreateProcessWithLogonW` 之前，resolved
interpreter、runner module、Neuro Code package root 与 dependency root 必须与所有
模型可写 root 保持不相交。

`ISOLATED` 选择持久化 Offline identity，`INHERIT` 选择 Online identity。runtime
不会修改 Firewall，也不会执行 setup、repair 或 UAC 提权。setup inspect 不是
`READY` 时，在创建 child 之前失败关闭。

完整接通的 W3 runtime 提供 read `LIMITED`、write `STRONG`、network `STRONG` 的
concrete provider contract；focused 原生验收已经认证该声明，而不是依赖 CI 环境的
runtime bypass。W1/W2 foundation actual-capability declaration 仍为
`UNSUPPORTED`，target declaration 不参与 runtime admission。`STRICT` 要求 strong
read isolation，因此失败关闭。交互式 PTY/ConPTY 留给 W4。

Gate 1 不依赖 Python 启动。实际 `CreateProcessAsUserW` 返回的 process handle
会在发送 `SpawnReady` 之前完成 attestation；controller 检查 `TokenUser`、
`IsTokenRestricted`、精确 singleton restricting SID、`SeChangeNotifyPrivilege`，
以及不存在意外的 enabled privilege。一次聚焦的 Windows Server 2025 运行显示，
当前 venv interpreter、其 `-I -S -B` 形式和 base interpreter 都在用户代码运行前失败。
根因有意保留为未确定的 W5 developer-tool compatibility blocker；不得因此放宽
token、ACL、environment、desktop、Job 或 provenance 边界。

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

## Focused 原生验收证据

当前 W3 provider 已完成完整 focused Windows runtime acceptance：执行 7 个测试，0 个 skip。
证据覆盖：

- Gate 1：Online/Offline final child 在 `SpawnReady` 前完成 identity/token attestation。
- Gate 2：workspace allow、read-only 与 sensitive-read deny、installation state deny，
  以及 real-user/Everyone WRITE 但没有 synthetic SID 的对抗 fixture；实际 restricted
  child 仍被拒绝写入。
- Gate 3：Online 重复/并发 Winsock 连接、Offline `WSAEACCES` deny、controller 前后置检查，
  以及未变化的精确持久 Firewall rule。
- Gate 4：二进制 stdout/stderr capture、merged 顺序、protocol framing、EOF、非零退出码和
  `Exit` 后无输出。
- Gate 5A：stdio-free descendant 的 normal wait 与自然完成；Gate 5B：公开
  `terminate()` 终止整个 Job；Gate 5C：controller 丢失后失败关闭整个 scope；Gate 5D：
  runner 退出证明 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。

受限 Python 启动、受限 NUL 写入和受限 curl 行为仍记录为 W5 compatibility seam，
不是 security-isolation failure。W3 不认证 Git、Python、Node 或一般 developer workload
的兼容性。Full PR CI 已通过并完成本 PR 的 merge-readiness 认证。
