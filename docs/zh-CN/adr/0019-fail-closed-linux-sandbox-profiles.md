# ADR 0019：失败关闭的 Linux 沙箱 profile

**简体中文** · [English](../../en/adr/0019-fail-closed-linux-sandbox-profiles.md)

## 背景

固定的历史 Rust 基线暴露 `off`、`workspace`、`read-only` 和 `strict` profile。
其可观察契约会区分模型供应商网络与本地子进程网络，并使用操作系统级文件系统限制同时
约束进程内工具和派生命令。

Neuro Code 已经具备工作区路径校验和权限提示，但两者都不是操作系统沙箱。Bash 可以
访问绝对路径，获批命令也可以继续派生后代进程。因此，如果没有内核边界却把策略命名为
“workspace”，就会错误描述安全保证。反过来，用户明确请求的 profile 不可用时静默继续，
也会弱化用户请求。

## 决策

`SandboxProfile` 是包含四个规范名称的领域值。解析优先级依次为 CLI `--sandbox`、
`NEURO_CODE_SANDBOX`、用户配置、项目配置，最后是 `off`。只有用户配置未固定 profile
时才考虑项目 profile，因此不可信工作区不能用 `off` 替换用户选择。

Linux 是第一个具备强制能力的平台适配器：

- `off` 保留原有的不启用沙箱行为并继续作为兼容性默认值；它明确不提供文件系统、网络、
  controller 私有状态或任意 detached descendant 隔离。
- `workspace` 在 Bubblewrap 下为每个本地 child 启动只读运行时视图和可写工作区。受信任
  controller 保持在宿主上，本地 child 网络保持可用。
- `read-only` 保持 child 工作区只读，同时只保留私有临时目录。编辑工具不会出现在模型
  schema 中，直接调用时也会再次拒绝。
- `strict` 每个 child 从空的白名单根目录开始，只读绑定必需的系统与 Python 运行时路径，
  可写绑定显式授权的工作区，最后把根目录重新挂载为只读。
- `read-only` 和 `strict` 下的 Bash、后台 Bash、stdio MCP 以及启用 profile 的 PTY child
  会进入嵌套 Linux 网络命名空间。父代理仍可访问模型 API；child 及其后代继承隔离后的
  网络命名空间。

平台适配器只接受不受工作区控制、调用者也不可写的 `bwrap`，以及按需使用的 `unshare`
可执行文件；使用前会预检所需 child 命名空间能力。不再使用 controller 命名空间 marker
或进程级挂载 attestation。任何不匹配、辅助程序缺失、命名空间不可用、exec 失败或平台
不支持都会终止启动。

当前所有本地子进程创建仍通过 `LocalProcessSandbox` 和自主管理的 `ProcessTree`。权限批准是
更早且独立的一道门禁；`--always-approve` 不能关闭或弱化选定的内核边界。
组合根还只把受保护的环境变量名称传入工具上下文；Bash 会在派生前移除已配置的供应商
凭据以及标准或显式代理变量，避免其值进入工具输出。

## 影响

这是有意保持范围的 M3 部分支持。Linux 已具备可强制执行的内建 profile；macOS 和
Windows 在适配器完成前会拒绝每个显式非 `off` profile。自定义 profile、`devbox`、
deny glob、Seatbelt、Landlock 专属优化和 Windows 沙箱适配器仍待实现。内建 profile
的会话保存与恢复固定由 [ADR 0020](0020-session-fixed-sandbox-profiles.md) 单独定义。

child 只接收私有临时存储；controller 状态、供应商设置、凭据和会话数据库绝不会挂载到
其中。`strict` 还会只读暴露当前 Python 运行时，使安装在系统目录外的软件包仍能启动。
以后任何需要派生进程的工具都必须使用同一本地进程端口，禁止绕过。
确实需要供应商凭据的命令以后必须使用显式密钥注入能力，不能依赖环境继承。

启用的 Linux child 还会进入 PID 命名空间，并使用 Bubblewrap 的父进程死亡边界。后代即使
调用 `setsid()` 离开 controller 的 POSIX 进程组，也不能离开该 PID 命名空间生命周期。
因此普通 POSIX `off` 适配器只承诺尽力清理原始进程组，不声称拥有任意 detached descendant。

bind mount 以路径为边界，而硬链接会别名到同一 inode。接受启用 profile 前，适配器会对
规模更小的 controller 状态目录执行有界审计；常规状态文件存在多个硬链接时失败关闭，防止
工作区中既存硬链接暴露凭据或会话状态，同时避免扫描工作区中每个文件的 inode。宿主上同一
用户仍可在启动前把其他内容放进授权工作区；这些内容属于声明的工作区信任边界，而不是
controller 私有状态。

四种 profile 含义、默认 `off`、仅限制子进程网络以及启动应用顺序的源证据，来自历史
提交 `c68e39f60462f28d9be5e683d9cbe2c57b1a5027` 中的沙箱 profile 与 Shell 启动实现。
