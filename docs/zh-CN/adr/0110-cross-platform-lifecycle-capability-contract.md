# ADR 0110：跨平台本地进程生命周期能力契约

## 状态

已接受。Phase 1–3 为规范本地进程端口增加显式生命周期能力契约。本 ADR 不实现 macOS
Seatbelt 适配器，也不改变现有 Linux 或 Windows 安全边界。

## 背景

此前 `LocalProcessLifecycle` 使用 `TERMINATE_PROCESS_TREE` 命名取消操作，却没有声明
所选平台实际能够拥有的后代边界。因此同一种 request shape 同时覆盖了强 Linux/Windows
边界，以及可能被 `setsid()` 后代离开的 POSIX 进程组。

文件系统和网络权限与后代生命周期所有权是两个正交概念。一个平台可以强制工作区、环境、
私有目录或网络策略，同时只能提供尽力而为的进程组清理。

## 决策

新增 `LocalProcessLifecycleCapability`，包含：

- `STRONG_DESCENDANT_OWNERSHIP`
- `PROCESS_GROUP_BEST_EFFORT`

能力顺序由唯一的 `lifecycle_capability_satisfies()` 显式实现；适配器不得直接比较
`StrEnum` 值或字符串：

```text
STRONG_DESCENDANT_OWNERSHIP >= PROCESS_GROUP_BEST_EFFORT
```

`LocalProcessLifecycle.required_capability` 表示调用方的最低要求。普通 Bash、background
Bash、MCP stdio 和 interactive PTY request 显式要求 `PROCESS_GROUP_BEST_EFFORT`。强适配器
可以满足该要求；尽力而为适配器遇到强要求时，必须在创建 OS child 前以 `SandboxError`
失败关闭。

`LocalProcessSandbox` 暴露实际 capability，`OwnedLocalProcess` 暴露创建后 child 实际获得的
capability。本地终端接缝（`TerminalPlatform` 和 `TerminalPlatformSession`）也暴露同一
capability，因此 `LocalInteractiveTerminalManager` 可以观察它，而不会污染远程或由 client
委托的 terminal abstraction。

本地 `BackgroundTaskManager` 及每个会话 task scope 暴露所选 sandbox adapter 的 capability
作为运行时元数据。task snapshot 与 durable domain/session state 保持不变；capability 永不持久化。

规范取消名称为 `TERMINATE_OWNED_SCOPE`。旧的 `TERMINATE_PROCESS_TREE` enum value 仅作为
deprecated 兼容成员保留；终止算法以及 grace/force 边界保持不变。

## 能力矩阵

| 适配器 | 文件系统/网络策略 | 生命周期能力 |
| --- | --- | --- |
| Linux enabled Bubblewrap | 由现有 child-scoped 适配器强制执行 | `STRONG_DESCENDANT_OWNERSHIP` |
| Windows Job Object / ConPTY Job | 现有 Job 与句柄边界 | `STRONG_DESCENDANT_OWNERSHIP` |
| POSIX `ProcessTree`（`off`） | 不声称 OS sandbox | `PROCESS_GROUP_BEST_EFFORT` |
| 未来 macOS Seatbelt 适配器 | Seatbelt 文件系统/网络/访问控制可以 enforced | `PROCESS_GROUP_BEST_EFFORT` |

未来 macOS 适配器不属于本 ADR 的实现范围。Endpoint Security、System Extension、privileged
helper 以及其他 hardened macOS architecture 都是未来候选，本阶段不把它们当作生命周期方案。

## 调用方与兼容边界

- Bash、background、MCP stdio 和 PTY request builder 显式设置默认产品要求；调用方不根据
  `sys.platform` 分支。
- legacy background request builder 保留原 API，但构造显式 best-effort requirement，不再
  构造语义模糊的空 lifecycle。
- Linux Bubblewrap 与 Windows Job Object guarantee 保持不变。适配器报告 strong capability，
  并继续执行现有的 fail-closed preflight 与创建门禁。
- POSIX 进程组继续执行有界 TERM-to-KILL 行为；detached descendant 不被描述为已拥有。
- capability 只是运行时适配器元数据，不写入 durable session 或 domain persistence。
- `SandboxProfile` 保持不变，继续表达文件系统和网络策略，而不是生命周期强度。

## 影响

调用方现在可以请求最低生命周期保证，并检查实际保证，而不必从取消标签或平台名称推断。
新增更弱的适配器不能静默满足强 workload。强 Linux/Windows 适配器可以为普通 best-effort
request 提供更强能力，同时实际 capability 仍可被调用方观察。

该契约刻意保持很小：不增加进程枚举、PID reuse 声明、kqueue/launchd/libproc 行为，也不引入
新的 macOS 原语。
