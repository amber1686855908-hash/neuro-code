# ADR 0111：macOS Seatbelt 本地进程沙箱

## 状态

已接受。macOS 上启用的内建沙箱 profile 使用生产级
`MacOSSeatbeltLocalProcessSandbox`。本决策不改变 Linux Bubblewrap 或 Windows
Job Object 的 guarantee。

## 背景

macOS evidence workload 已证明，deny-by-default Seatbelt profile 可在两种 GitHub
macOS runner 架构上强制执行 child 文件系统、网络与环境权限。同一批证据也证明，
POSIX 进程组无法拥有成功调用 `setsid()` 的后代。因此，文件系统和网络强制不能被表述为
强后代生命周期所有权。

## 决策

Composition 在 Darwin 上为 `workspace`、`read-only` 和 `strict` 选择 Seatbelt
adapter。它要求固定且由 root 拥有的 `/usr/bin/sandbox-exec`、规范 workspace 和
controller state directory，以及互不重叠的授权根目录。不支持的平台与无效权限都失败关闭。

每个 request 都使用已转义的字符串 literal 转换为 deny-by-default SBPL profile。runtime
读取仅限于 evidence 已验证的 macOS system root、controller interpreter runtime、显式
workspace root 与一组 child-private HOME/TMP。Controller state、其他 host-home 数据和外部写入
继续被拒绝。`workspace` 允许 outbound network；`read-only` 和 `strict` 不授予该权限。
`read-only` 不授予 workspace 写入，`strict` 则保留 request 的 per-root mode，主 workspace
仍可写。

授权的 runtime、workspace 与私有根目录获得其自身 subtree 的递归 metadata 权限。它们的
ancestor 仅获得用于 pathname traversal 的精确 `literal` metadata 权限，包括精确的 `/`；
任何 ancestor（包括 `/`）都不会获得递归 `subpath` metadata 权限。

在 pipe 或 PTY 创建前，`PosixWorkspaceInodeAudit` 会审计每个授权根目录，拒绝外部
hardlink alias 以及混合 read-only/read-write alias。Child environment 仅由
`LocalProcessEnvironmentPolicy` 重建，不会隐式合并 `os.environ`。HOME、TMPDIR、TMP 和
TEMP 指向独立私有目录，由返回的 process/session wrapper 持有，并在 wait、terminate、
spawn failure 或 PTY close 后删除。

Bash、background Bash、MCP stdio 和 interactive PTY request 都以
`/usr/bin/sandbox-exec` 作为外层 executable。Shell request 使用可信 `/bin/sh -c`；MCP 保持
argv-safe。POSIX pipe 创建显式使用 `close_fds=True`，`pass_fds` 仅为明确的 infrastructure
descriptor 保留。现有 PTY path 保持同一 close-FD 不变式。

## 生命周期契约

该 adapter 及其返回的每个 process 或 terminal 始终报告
`PROCESS_GROUP_BEST_EFFORT`。要求 `STRONG_DESCENDANT_OWNERSHIP` 的 request 会在创建 OS
child 前失败。即使 Seatbelt 文件系统和网络策略仍会作用于 detached descendant，
`setsid()` 也不会被描述为已拥有。

Endpoint Security、System Extension 和 privileged helper 不属于本实现。它们只是未来
hardened macOS architecture 的候选，不被声称为当前生命周期方案。

## 验证边界

专用 CI 使用 Python 3.12 和 3.14，在 `macos-15` ARM64 与 `macos-15-intel` 上无 skip
执行真实 adapter。它记录 macOS version/build、架构、固定 sandbox executable、签名详情与 SIP
状态。production implementation 已完成，GitHub ARM64/Intel integration 已验证。
GitHub-hosted evidence 当前报告 SIP disabled。由于当前没有 Mac 硬件，物理 SIP-enabled macOS
验收标记为 `DEFERRED_NO_MAC_HARDWARE`；这不是 code 或 merge blocker，也不表示物理 SIP-enabled
验收已经通过。
