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

- `off` 保留原有的不启用沙箱行为。
- `workspace` 在 bubblewrap 中重新执行 Neuro Code：宿主根目录只读，工作区、状态目录
  和临时路径可写；本地子进程网络保持可用。
- `read-only` 保持宿主根目录和工作区只读，同时允许状态目录与临时路径写入。编辑工具
  不会出现在模型 schema 中，直接调用时也会再次拒绝。
- `strict` 从空的白名单根目录开始，只读绑定必需的系统与 Python 运行时路径，可写绑定
  工作区、状态目录和临时路径，最后把根目录重新挂载为只读。
- `read-only` 和 `strict` 下的 Bash 会进入嵌套 Linux 网络命名空间。父代理仍可访问模型
  API；Shell 及其全部后代继承隔离后的网络命名空间。

平台适配器只接受不受工作区控制、调用者也不可写的 `bwrap`，以及按需使用的 `unshare`
可执行文件；进程替换前会探测所需命名空间能力。子进程会收到内部 profile 标记，但不会
单独信任该标记：启动时及每次沙箱 Bash 启动时都会校验根目录、工作区与状态目录的挂载
标志，`strict` 还会验证根目录确实是白名单 `tmpfs`。标记不匹配、辅助程序缺失、命名空间
不可用、exec 失败或平台不支持都会终止启动。

当前所有本地子进程创建仍通过 `ShellSandbox` 和自主管理的 `ProcessTree`。权限批准是
更早且独立的一道门禁；`--always-approve` 不能关闭或弱化选定的内核边界。
组合根还只把受保护的环境变量名称传入工具上下文；Bash 会在派生前移除已配置的供应商
凭据以及标准或显式代理变量，避免其值进入工具输出。

## 影响

这是有意保持范围的 M3 部分支持。Linux 已具备可强制执行的内建 profile；macOS 和
Windows 在适配器完成前会拒绝每个显式非 `off` profile。自定义 profile、`devbox`、
deny glob、Seatbelt、Landlock 专属优化和 Windows 沙箱适配器仍待实现。内建 profile
的会话保存与恢复固定由 [ADR 0020](0020-session-fixed-sandbox-profiles.md) 单独定义。

状态目录和临时路径可写是 profile 契约中的有意例外。`strict` 还会只读暴露当前 Python
运行时，使安装在系统目录外的软件包仍能启动。以后任何需要派生进程的工具都必须使用
同一 Shell/平台端口，禁止绕过。
确实需要供应商凭据的命令以后必须使用显式密钥注入能力，不能依赖环境继承。

四种 profile 含义、默认 `off`、仅限制子进程网络以及启动应用顺序的源证据，来自历史
提交 `c68e39f60462f28d9be5e683d9cbe2c57b1a5027` 中的沙箱 profile 与 Shell 启动实现。
