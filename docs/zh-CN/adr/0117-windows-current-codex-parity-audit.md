# ADR 0117：当前 OpenAI Codex Windows sandbox parity 审计

- 状态：已接受；作为当前生产 Windows sandbox 基线审计
- 日期：2026-08-20
- 范围：Neuro Code W1-W5 Windows sandbox 与当前 OpenAI Codex upstream 语义对照

## 基线

本审计固定了可复核的源码基线：

- Neuro Code 生产分支：`feat/windows-sandbox-codex-parity`，HEAD
  `dcc5cbebbaa942da89ca6e717fe5f6710b3080fc`。
- OpenAI Codex 默认分支：`59f7da58d6ae8401304554f807023610181f65f0`。
- DeepSeek Harness Windows ACL 参考：
  `141eb6fef83422698aef7a981029e843e8161534`。

当前 Neuro Code `origin/main` 为
`458afc19c478c2ecc5e9c6282f318ab1358a1586`。生产分支有意没有自动合并该提交；这
是 PR #49 的分支状态记录，不是把未审计的主干变更带入 sandbox 的理由。

## 决策

只对照可观察的 Windows 安全和生命周期语义，不复制 Codex 的 Rust 模块目录或实现。
Neuro Code 使用 Python `ctypes` 和已有的 application/infrastructure 边界实现同一类
contract；模型可控的进程创建仍只能经过 canonical sandbox adapter。

| 子系统 | Codex 当前语义 | Neuro Code 当前实现 | 结论 |
| --- | --- | --- | --- |
| identity / setup | dedicated identity、DPAPI 凭据、elevated setup、Offline/Online 选择 | `windows_sandbox_setup.py`、`windows_sandbox_identity.py`、DPAPI store、W2 READY 检查 | 语义对齐；语言实现不同 |
| restricted token | `DISABLE_MAX_PRIVILEGE`、`LUA_TOKEN`、`WRITE_RESTRICTED`；capability、extra identity、Logon SID、World SID 的有序集合 | W3 使用同样的四项 production set，并 attestation `TokenUser`、restricted SIDs、`SeChangeNotifyPrivilege` 与 unexpected privileges | 对齐 |
| default DACL / ACL | 为 child-owned IPC 设置 bounded default DACL，并由 ACL/deny-read 规则限制文件 authority | `TokenDefaultDacl`、managed capability ACE、sibling boundary deny、敏感路径保护 | 语义对齐；World SID 边界 deny 是 Neuro 的显式适配 |
| process boundary | `CreateProcessWithLogonW` → restricted token → `CreateProcessAsUserW`；attribute list 原子加入 Job/handle/PTY | trusted runner、`CreateProcessAsUserW`、Job/handle-list 或 PTY/Job attribute list | 对齐 |
| descendant lifecycle | Job ownership 在创建边界建立，kill-on-close，失败清理 | `WindowsJobObject` + `PROC_THREAD_ATTRIBUTE_JOB_LIST`，controller loss fail-closed，state-based quiescence | 对齐 |
| controller IPC | controller-owned named pipes、framed protocol、connect/timeout/cleanup | 随机双向单向 named pipe、精确 DACL、版本/长度 frame、bounded relay | 对齐 |
| ConPTY | `CreatePseudoConsole` 与 `PSEUDOCONSOLE` attribute，PTY 不继承任意 handle | 同一 attribute-list 组合、raw byte output、resize frame、PTY 无 arbitrary handle inheritance | 对齐 |
| network | native WFP provider/filter，按 sandbox identity 持久化 Offline outbound deny | W2 通过 `New-NetFirewallRule -LocalUser <SID>` 建立持久、SID-scoped Offline deny；runtime 只检查 READY，不 mutation | 行为语义对齐；API 实现是有意差异 |
| environment / provenance | trusted runner 与 child 使用显式环境、受信代码路径不落入 writable roots | `-I`、显式 allowlist、private HOME/TMP、interpreter/package/module provenance disjoint 检查 | 对齐 |
| credential failure | Codex 对选定 logon/startup 错误有一次 credential refresh retry | Neuro runtime 从 DPAPI 记录读取 authoritative credentials；失败关闭，不在 runtime 自动 repair/UAC | 有意收窄；不削弱安全边界 |
| AppContainer | 不是当前唯一生产前提；具体能力由当前 token/ACL/runtime 路径承担 | PR #48 AppContainer feasibility 保持冻结，不进入生产路径 | 符合决策 |

Codex 的 WFP native API 与 Neuro 的 Windows Firewall cmdlet 不是逐 API 等价；验收依据是
Offline 身份范围、持久性、精确规则检查、Online 不修改，以及真实 `WSAEACCES` 行为。若
未来需要绕过受管 PowerShell 策略，native WFP helper 是可独立评估的增强项，不应通过
runtime fallback 或全局 firewall rule 临时解决。

## 验收边界

- 本审计不把 HTTP 200、偶发 `partial` 或仅有的单元测试当作 Windows enforcement 证据。
- 最终生产判定必须由 Windows native token、filesystem、network、Job、pipe、ConPTY、
  child/grandchild 和 W5 workload jobs 重新产生；Linux 本地运行只能验证静态和跨平台部分。
- `docs/windows-sandbox-implementation-ledger.md` 是当前验证状态的唯一简明记录；旧的 W5
  artifact 与旧 CI run 只保留为历史证据，不能覆盖新 head 的验证。

## 参考

- [OpenAI Codex Windows sandbox token.rs](https://github.com/openai/codex/blob/59f7da58d6ae8401304554f807023610181f65f0/codex-rs/windows-sandbox-rs/src/token.rs)
- [OpenAI Codex process attributes](https://github.com/openai/codex/blob/59f7da58d6ae8401304554f807023610181f65f0/codex-rs/windows-sandbox-rs/src/proc_thread_attr.rs)
- [OpenAI Codex WFP setup](https://github.com/openai/codex/blob/59f7da58d6ae8401304554f807023610181f65f0/codex-rs/windows-sandbox-rs/src/wfp_setup.rs)
- [DeepSeek Harness Windows ACL sandbox](https://github.com/deepseek-ai/deepseek-harness/tree/141eb6fef83422698aef7a981029e843e8161534/packages/sandbox/sandbox-windows-acl)
