# ADR 0115：Windows 原生沙箱 ConPTY 纵向切片

- 状态：W4 实施中；Gate 1 已加固，Gate 2 PTY 写入/网络隔离已接受
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

启用 profile 的公开 `spawn_terminal()` 路由在证据收集期间继续 fail closed。私有
candidate 路径只由 focused native acceptance 使用。W3 非 PTY 行为及其 capability
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
启用的 privilege。私有 candidate 因而证明 READ LIMITED、WRITE STRONG、NETWORK STRONG；
STRICT 仍因要求 strong read isolation 而 fail closed。

启用 profile 的公开 `spawn_terminal()` 路由仍保持 fail closed；Gate 2 acceptance 不会
暴露该路由。W5 尚未开始，本 ADR 不认证 Python、Git、Node、NUL、curl、应用层 terminal
路由或 developer-tool compatibility。

## 后果

- `PROTOCOL_VERSION` 保持为 1；`PTY_OUTPUT` 是 event frame，`RESIZE` 是 control frame。
- runner 在发送 `EXIT` 前持续排空 PTY output channel。
- 只有 Job-owned scope 为空后才执行 `ClosePseudoConsole`；不引入第二套 lifecycle 或
  Job authority。
- 未来 production terminal route 必须先通过 Gate 1 和 Gate 2 evidence。本 ADR 尚未认证该
  公开 route。

## 参考

- [创建 pseudoconsole session](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)
- [CreatePseudoConsole 函数](https://learn.microsoft.com/en-us/windows/console/createpseudoconsole)
- [CreateProcessAsUserW 函数](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)
