# ADR 0112：Windows 本地进程沙箱 AppContainer 可行性决策

## 状态

已接受。已研究的 classic stable unpackaged Windows AppContainer 架构延期，
不进入生产沙箱适配器。显式 Windows 文件系统/网络 profile 仍为不支持并失败关闭。
现有 `off` 路径及 Windows Job Object/ConPTY 生命周期契约保持不变。

## 背景

Neuro Code 的启用本地进程 profile 必须保持 child-scoped，并在能力不足时失败关闭，
同时支持普通开发工具工作流。本次决策采用的生产契约要求全部满足：

- 不需要管理员权限；
- 不扩展 `C:\`、`C:\Users` 或 `C:\Program Files` 等受保护 host ancestor 的 ACL；
- 不授予宽泛的 `USERPROFILE` 或 host directory 权限；
- 不使用未沙箱化的 Git broker；
- 不捆绑或永久维护 patched Git fork。

W0 evidence 阶段在不修改 production code 的前提下研究了 classic stable unpackaged
AppContainer 架构。Evidence PR #33--#39 仍是独立、未合并的 evidence branch：

| Evidence | 结果 |
|---|---|
| #33 AppContainer primitives | AppContainer、Job Object 和 ConPTY primitive 通过；controller `HANDLE_LIST` probe 仍受阻。 |
| #34 named-pipe/IPC architecture | secured named-pipe bootstrap 与 MCP byte-stream 语义通过。 |
| #35 filesystem/ACL authority | 只读/读写权限、identity、hardlink/reparse 隔离、崩溃恢复与 one-shot SID 行为通过。 |
| #36 runtime/non-admin | Python、Node、PowerShell、`cmd`、真实 MCP 与 standard-user 核心工作流通过；Git 受阻。 |
| #37 Git NUL compatibility | stock Git 受阻；最小 evidence patch 仅提供部分兼容性。 |
| #38 Git path canonicalization | 已记录的 cwd fallback 仅为部分方案；完整 Git 兼容性仍受阻。 |
| #39 Git repository discovery | 部分 ceiling/discovery 场景通过，但完整 `init`/`add`/`status`/`rev-parse` 受阻，且受保护 ancestor 不具备生产可行性。 |

因此，证据应区分“AppContainer primitive 可行”和“完整开发工具兼容契约满足”。本决策不表示
AppContainer 天生不安全，也不表示 Windows 无法提供沙箱。

## 决策

classic stable unpackaged AppContainer 架构当前不进入生产。具体 blocker 是：在非管理员、
不扩展 ACL 的契约下，current stock Git for Windows 无法兼容受保护 ancestor 的路径规范化
与 repository discovery。向 ancestor 授予宽泛权限会违反文件系统边界；对 disposable、由当前
用户拥有的 parent 授予窄权限只能作为诊断证据，不能使 `C:\`、`C:\Users` 或
`C:\Program Files` 具备生产资格。

因此，Windows 启用的 `workspace`、`read-only` 和 `strict` 文件系统/网络 profile 继续为
**不支持 / 失败关闭**。这是显式能力决策，不是静默回退。

Windows `off` 路径继续支持。其现有进程生命周期使用 Job Object 与 ConPTY 所有权，并报告
`STRONG_DESCENDANT_OWNERSHIP`；本决策不削弱或重新标记该契约。文件系统/网络沙箱权限与该
生命周期能力保持正交。

## 被拒绝的生产替代方案

以下方案不属于本 ADR 的生产决策：

- **扩展受保护 ancestor ACL：** 拒绝，因为需要管理员权限或修改非当前用户拥有的 ACL，并可能
  暴露 parent metadata 或 sibling 名称/内容。
- **捆绑或 patched Git：** 拒绝，因为会产生需要持续维护的 runtime fork，也不能为用户的
  stock developer-tool 环境建立兼容性。
- **未沙箱化 Git broker：** 拒绝，因为会把 Git 执行和权限移出 child-scoped sandbox 契约。
- **将 Windows Sandbox/Hyper-V 作为默认适配器：** 暂不采用，因为 VM/隔离桌面不是满足
  Job、ConPTY、MCP 与普通 CLI 语义的透明本地 child boundary。

## 未来重新评估候选

只有出现以下独立变化之一，才重新评估：

1. Git for Windows upstream 解决 NUL、路径规范化与 repository discovery 兼容性，同时保持
   非管理员和不扩展宽泛 ACL 的契约。
2. Microsoft 发布 documented、stable、non-admin、child-scoped primitive，支持任意 CLI
   进程，并能与 Job Object、ConPTY 和 MCP 组合。未来候选包括稳定版 Win32 App Isolation，
   或 `Experimental_CreateProcessInSandbox`/BFS 的稳定后继者。当前 preview 或 experimental
   行为不构成生产保证。

本决策不引入 Endpoint Security 类 helper、privileged broker、production adapter 或新的
evidence probe。

## 兼容性影响

兼容性矩阵记录 Windows 状态如下：

| 范围 | Windows 状态 |
|---|---|
| `off` | 支持；不承诺 OS 文件系统/网络沙箱。 |
| 生命周期 | 通过现有 Job Object/ConPTY 路径提供 `STRONG_DESCENDANT_OWNERSHIP`。 |
| `workspace` | 不支持 / 失败关闭。 |
| `read-only` | 不支持 / 失败关闭。 |
| `strict` | 不支持 / 失败关闭。 |

本 ADR 只记录 W0 evidence 决策，不修改 production code，也不合并任何 evidence PR。
