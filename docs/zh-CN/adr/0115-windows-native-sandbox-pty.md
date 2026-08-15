# ADR 0115：Windows 原生沙箱 ConPTY 纵向切片

- 状态：已接受；W4 Windows ConPTY 生产路由已通过 focused 原生验收与完整 CI 认证
- 日期：2026-08-15

## 背景

Windows W3 runtime 已经通过 W2 identity、精确 synthetic write SID、私有
HOME/TMP、私有 desktop 和 runner 所有的 Job Object 管理最终受限 child。
普通 `WindowsConPtyPlatform` 有意不承担沙箱 authority：它使用
`CreateProcessW`，因此不能用于启用的 Windows profile。

## 决策

Gate 1 在现有受信任 W3 runner 中组合 Windows ConPTY 原语。runner 创建输入/输出
channel 和 HPCON，然后在一次 `STARTUPINFOEXW` attribute list 中同时放入
`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` 与 `PROC_THREAD_ATTRIBUTE_JOB_LIST`，用于最终
`CreateProcessAsUserW` 调用。PTY 输出保持为一个原始字节流，resize 使用有方向的
control frame。child 不继承 controller、runner 或 protocol handles；其 console stream
由 ConPTY 提供。

启用 profile 的公开 `spawn_terminal()` 路由现在是规范生产边界，也是 focused native
acceptance 实际验证的同一路径。W3 非 PTY 行为及其 capability
contract 不变：READ LIMITED、WRITE STRONG、NETWORK STRONG，以及 strong descendant
ownership。STRICT 仍因要求 strong read isolation 而 fail closed。

Gate 1 probe 是一次性的原生 C 可执行文件。它会对 Online 与 Offline 两种 W2 identity
验证真实 console 尺寸、输入、合并 PTY 输出、最终输出排空、退出码、受限 token
attestation，以及 malformed resize 的有界清理。加固后的证据还记录 runner 自然退出
（runner exit code 为 0，未强制终止）以及已文档化的 ConPTY 标准句柄 contract：
`bInheritHandles=false`、没有 handle-list attribute，且 console 输入/输出句柄有效；不
声称支持未被记录的句柄枚举。

Gate 2 通过受限 PTY child 重新认证 W3 capability boundary。Workspace 的
create/append/rename/delete 成功；一个具有普通用户和 Everyone write ACE、但没有
synthetic write SID 的外部目录仍被拒绝；只读 root 和 installation/controller state 的
变更仍被拒绝。Online Winsock 可以连接，Offline Winsock 以 `WSAEACCES`（10013）拒绝；
每次运行前后都将 managed Offline firewall rule 检查为 READY，runtime 不执行 mutation。
每个 PTY SpawnReady attestation 都包含唯一精确的 synthetic restricting SID，且没有额外
启用的 privilege。已认证的生产路由因此证明 READ LIMITED、WRITE STRONG、NETWORK STRONG；
STRICT 仍因要求 strong read isolation 而 fail closed。

Gate 3 通过生产 ConPTY 路由复用 W3 原生 descendant probe。3A 中，直接
leader 以 exit code 23 退出，而不依赖 stdio 的 grandchild 仍保持 active；在
grandchild 自然写出 finished marker 且 runner 所有的 Job 变为空之前，
`poll_exit()` 持续为 pending。3B 中，`session.close()` 发送规范 TERMINATE
frame，由 runner 的 Job 终止两个仍存活的 descendant，且不需要 controller 侧
强制终止 runner；有界 termination observation 记录请求时 final child 仍为
active。3C 中，controller helper 突然退出会使 runner fail closed 并终止完整
Job scope。3D 中，强制结束受信任 runner 证明 KILL_ON_JOB_CLOSE：两个
descendant 均退出且没有自然完成 marker，controller 收到一次有界 error，而不
会伪造干净 EOF。四个场景均保持 HPCON/relay 有界 teardown，且没有 orphan
process。

Controller-loss 的判定基于状态。只有在最终 `EXIT` 已发送，或 direct child
已退出且 `Job ActiveProcesses == 0`、从而建立 `owned_scope_quiesced` 后，runner
才将 control EOF 视为无害。owned Job 仍活动时，EOF 会立即调用
`fail_closed()`；该安全判定不使用基于时间的 EXIT 宽限。

Gate 4 证明真实应用路由：terminal manager 按原有流程构造
`SandboxedProcessRequest`，并调用公开的 `LocalProcessSandbox.spawn_terminal()` 端口。
启用的 Windows profile 使用以下生产链：

`LocalInteractiveTerminalManager` → `LocalProcessSandbox.spawn_terminal()` →
`WindowsNativeLocalProcessSandbox` → W2 identity → trusted runner → restricted token →
`CreatePseudoConsole` → `PSEUDOCONSOLE` + `JOB_LIST` → `CreateProcessAsUserW` →
restricted final child。

WORKSPACE 与 READ_ONLY 只有在 W2 报告 `READY` 时才允许继续；STRICT 仍因 LIMITED read
无法满足 STRONG read requirement 而 fail closed。runtime 永远不会执行 setup、repair、UAC、
ACL 或 Firewall mutation。`SandboxProfile.OFF` 继续使用普通 Windows ConPTY 路由。

W5 尚未开始。Python、Git、Node、NUL、curl 与 developer-tool compatibility 仍属于未来
workload certification，不是本 ADR 的已认证结论。

## 后果

- `PROTOCOL_VERSION` 保持为 1；`PTY_OUTPUT` 是 event frame，`RESIZE` 是 control frame。
- runner 在发送 `EXIT` 前持续排空 PTY output channel。
- 只有 Job-owned scope 为空后才执行 `ClosePseudoConsole`；不引入第二套 lifecycle 或
  Job authority。
- Gate 1、Gate 2、Gate 3、shared-runner hardening 与 Gate 4 application routing 共同构成
  当前生产路由的验收证据。该路由已在测试的 Windows CI 矩阵中认证；W5 workload
  compatibility 明确不在本 ADR 范围内。

## 参考

- [创建 pseudoconsole session](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)
- [CreatePseudoConsole 函数](https://learn.microsoft.com/en-us/windows/console/createpseudoconsole)
- [CreateProcessAsUserW 函数](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)
